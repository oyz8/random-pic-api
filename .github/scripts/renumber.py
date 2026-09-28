import os
import json
import base64
import time
import requests

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["REPO"]
API = f"https://api.github.com/repos/{REPO}"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

FOLDERS = ["hd", "hl", "vd", "vl"]
IMAGE_EXT = ".webp"
BATCH_SIZE = 100


def _req(method, url, **kw):
    for attempt in range(3):
        try:
            r = requests.request(method, url, headers=HEADERS, timeout=60, **kw)
            r.raise_for_status()
            return r.json() if r.content else None
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 422, 502, 503) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise


def api_get(url): return _req("GET", url)
def api_post(url, data): return _req("POST", url, json=data)
def api_patch(url, data): return _req("PATCH", url, json=data)


def get_branch():
    return api_get(API)["default_branch"]


def get_ref(branch):
    return api_get(f"{API}/git/ref/heads/{branch}")["object"]["sha"]


def get_tree(sha):
    return api_get(f"{API}/git/trees/{sha}")


def create_blob(content):
    encoded = base64.b64encode(content.encode()).decode()
    return api_post(f"{API}/git/blobs",
                    {"content": encoded, "encoding": "base64"})["sha"]


def create_tree(entries, base_tree_sha=None):
    payload = {"tree": entries}
    if base_tree_sha:
        payload["base_tree"] = base_tree_sha
    return api_post(f"{API}/git/trees", payload)["sha"]


def create_commit(message, tree_sha, parent_sha):
    return api_post(f"{API}/git/commits",
                    {"message": message, "tree": tree_sha, "parents": [parent_sha]})["sha"]


def update_ref(branch, commit_sha):
    api_patch(f"{API}/git/refs/heads/{branch}", {"sha": commit_sha, "force": True})


def set_temp_ref(name, sha):
    try:
        api_get(f"{API}/git/ref/{name}")
        api_patch(f"{API}/git/refs/{name}", {"sha": sha, "force": True})
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            api_post(f"{API}/git/refs", {"ref": f"refs/{name}", "sha": sha})
        else:
            raise


def delete_temp_ref(name):
    try:
        requests.delete(f"{API}/git/refs/{name}", headers=HEADERS, timeout=60)
    except Exception:
        pass


def rename_batch(folder_tree_sha, renames, base_commit, temp_ref, prefix=""):
    """
    分批重命名。old/new 编号不相交（old > N，new <= N），
    每批同时删除旧名、添加新名，互不冲突。
    """
    current_sha = folder_tree_sha
    current_commit = base_commit
    total = len(renames)
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, total, BATCH_SIZE):
        batch = renames[i:i + BATCH_SIZE]
        entries = []
        for old, new, mode, blob_sha in batch:
            entries.append({"path": old, "mode": mode, "type": "blob", "sha": None})
            entries.append({"path": new, "mode": mode, "type": "blob", "sha": blob_sha})

        new_sha = create_tree(entries, base_tree_sha=current_sha)
        current_commit = create_commit(
            f"temp {prefix}batch {i//BATCH_SIZE+1}", new_sha, current_commit
        )
        set_temp_ref(temp_ref, current_commit)
        current_sha = new_sha
        print(f"{prefix}批次 {i//BATCH_SIZE+1}/{batches} 完成")

    return current_sha


def main():
    branch = get_branch()
    head_sha = get_ref(branch)
    root_tree_sha = api_get(f"{API}/git/commits/{head_sha}")["tree"]["sha"]
    root_tree = get_tree(root_tree_sha)

    ri_entry = next((i for i in root_tree["tree"]
                     if i["path"] == "ri" and i["type"] == "tree"), None)
    if not ri_entry:
        raise Exception("未找到 ri 目录")
    ri_sha = ri_entry["sha"]

    other_root = [
        {"path": i["path"], "mode": i["mode"], "type": i["type"], "sha": i["sha"]}
        for i in root_tree["tree"] if i["path"] != "ri"
    ]

    ri_tree = get_tree(ri_sha)
    folder_shas = {}
    ri_other = []
    for i in ri_tree["tree"]:
        if i["path"] in FOLDERS and i["type"] == "tree":
            folder_shas[i["path"]] = i["sha"]
        elif i["path"] == "count.json":
            continue
        else:
            ri_other.append({"path": i["path"], "mode": i["mode"],
                             "type": i["type"], "sha": i["sha"]})

    temp_ref = f"heads/_renumber_tmp_{branch}"
    delete_temp_ref(temp_ref)

    # 累积的 ri 条目（用于逐步锚定）
    current_ri_entries = [
        {"path": f, "mode": "040000", "type": "tree", "sha": folder_shas[f]}
        for f in FOLDERS if f in folder_shas
    ]
    current_ri_entries.extend(ri_other)

    new_folder_shas = {}
    new_count = {}

    for folder in FOLDERS:
        if folder not in folder_shas:
            continue

        # 图片目录是扁平的，非递归获取即可（避免 recursive=1 的 500 错误）
        sub_tree = get_tree(folder_shas[folder])

        existing = {}
        for item in sub_tree["tree"]:
            if item["type"] != "blob":
                continue
            name = item["path"]  # 非递归时就是文件名
            if name.endswith(IMAGE_EXT):
                num_str = name[:-len(IMAGE_EXT)]
                if num_str.isdigit():
                    existing[int(num_str)] = (name, item["mode"], item["sha"])

        nums = sorted(existing.keys())
        N = len(nums)
        if N == 0:
            new_count[folder] = {"max": 0, "exclude": []}
            continue

        missing = sorted(set(range(1, N + 1)) - set(nums))
        extra = sorted(set(nums) - set(range(1, N + 1)))

        print(f"处理 {folder}: {N} 个文件，需重命名 {len(missing)} 个")

        renames = []
        for old_num, new_num in zip(extra, missing):
            old_name, mode, blob_sha = existing[old_num]
            renames.append((old_name, f"{new_num}{IMAGE_EXT}", mode, blob_sha))

        if renames:
            new_folder_sha = rename_batch(
                folder_shas[folder], renames, head_sha, temp_ref,
                prefix=f"[{folder}] "
            )
        else:
            new_folder_sha = folder_shas[folder]

        new_folder_shas[folder] = new_folder_sha
        new_count[folder] = {"max": N, "exclude": []}

        # 更新累积条目并锚定
        for entry in current_ri_entries:
            if entry["path"] == folder:
                entry["sha"] = new_folder_sha
                break

        temp_ri_sha = create_tree(current_ri_entries, base_tree_sha=ri_sha)
        temp_root_sha = create_tree(
            other_root + [{"path": "ri", "mode": "040000",
                           "type": "tree", "sha": temp_ri_sha}],
            base_tree_sha=root_tree_sha
        )
        anchor = create_commit(f"temp anchor after {folder}", temp_root_sha, head_sha)
        set_temp_ref(temp_ref, anchor)

    # 最终 count.json
    count_blob_sha = create_blob(json.dumps(new_count, indent=2, ensure_ascii=False))

    final_ri_entries = [
        {"path": f, "mode": "040000", "type": "tree", "sha": new_folder_shas[f]}
        for f in FOLDERS if f in new_folder_shas
    ]
    final_ri_entries.extend(ri_other)
    final_ri_entries.append({"path": "count.json", "mode": "100644",
                             "type": "blob", "sha": count_blob_sha})

    final_ri_sha = create_tree(final_ri_entries, base_tree_sha=ri_sha)
    final_root_sha = create_tree(
        other_root + [{"path": "ri", "mode": "040000",
                       "type": "tree", "sha": final_ri_sha}],
        base_tree_sha=root_tree_sha
    )
    final_commit = create_commit(
        "chore: fill gaps by renaming files", final_root_sha, head_sha
    )
    update_ref(branch, final_commit)
    delete_temp_ref(temp_ref)
    print("完成。")


if __name__ == "__main__":
    main()
