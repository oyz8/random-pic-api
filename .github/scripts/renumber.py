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

# h: 横屏（原 hd + hl 合并，hd 在前），v: 竖屏（原 vd + vl 合并，vd 在前）
FOLDERS = ["h", "v"]
SOURCE_FOLDERS = {"h": ["hd", "hl"], "v": ["vd", "vl"]}
IMAGE_EXT = ".webp"
BATCH_SIZE = 100

# 关键：源目录里 1..N 的编号在目标目录里会冲突，所以重命名分两阶段进行。
# 第一阶段把每个源目录的整体偏移 MAX_H+MAX_V 之外（例如 hd 偏移 200），
# 这样绝不会踩到任何现存文件名；第二阶段再紧凑归位到 1..N。
OFFSET = 200


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


def list_files(sha):
    """返回 {name: (mode, blob_sha)}，仅 blob，非递归"""
    out = {}
    for item in get_tree(sha)["tree"]:
        if item["type"] != "blob":
            continue
        if item["path"].endswith(IMAGE_EXT):
            out[item["path"]] = (item["mode"], item["sha"])
    return out


def apply_batch(base_sha, parent_commit, deletions, additions, temp_ref, label):
    """
    deletions: [old_path, ...]
    additions: [(new_path, mode, blob_sha), ...]
    """
    entries = [{"path": p, "mode": "100644", "type": "blob", "sha": None}
               for p in deletions]
    for path, mode, blob_sha in additions:
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob_sha})
    new_tree = create_tree(entries, base_tree_sha=base_sha)
    new_commit = create_commit(label, new_tree, parent_commit)
    set_temp_ref(temp_ref, new_commit)
    return new_tree, new_commit


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
    source_shas = {}
    ri_other = []
    for i in ri_tree["tree"]:
        if i["path"] in FOLDERS + ["hd", "hl", "vd", "vl"] and i["type"] == "tree":
            source_shas[i["path"]] = i["sha"]
        elif i["path"] == "count.json":
            continue
        else:
            ri_other.append({"path": i["path"], "mode": i["mode"],
                             "type": i["type"], "sha": i["sha"]})

    temp_ref = f"heads/_renumber_tmp_{branch}"
    delete_temp_ref(temp_ref)

    # 新建空目录树，用于承载合并结果
    new_folder_shas = {}
    for f in FOLDERS:
        new_folder_shas[f] = create_tree([])
    new_count = {}

    current_commit = head_sha
    # 锚点：先让 ri 树包含四个新空目录（若存在同名旧目录会冲突，见下）
    # 策略：先删除旧目录，再添加新目录。
    # 第一次先构造"无旧目录、无新目录"的 ri 树（删旧）
    deletions_ri = [{"path": f, "mode": "040000", "type": "tree", "sha": None}
                    for f in SOURCE_FOLDERS.keys()  # 不会命中，占位
                    if False]
    # 用 base_tree=ri_sha 做增量：删除 hd/hl/vd/vl，添加 h/v
    del_entries = [{"path": f, "mode": "040000", "type": "tree", "sha": None}
                   for f in ["hd", "hl", "vd", "vl"] if f in source_shas]
    add_entries = [{"path": f, "mode": "040000", "type": "tree", "sha": new_folder_shas[f]}
                   for f in FOLDERS]
    ri_step1 = create_tree(del_entries + add_entries, base_tree_sha=ri_sha)
    ri_step1 = ri_step1  # 上面已经包含删除与新增
    root_step1 = create_tree(
        other_root + [{"path": "ri", "mode": "040000",
                       "type": "tree", "sha": ri_step1}],
        base_tree_sha=root_tree_sha,
    )
    current_commit = create_commit(
        "chore(ri): replace hd/hl/vd/vl with empty h/v", root_step1, current_commit
    )
    set_temp_ref(temp_ref, current_commit)
    ri_sha_now = ri_step1

    for folder in FOLDERS:
        nxt = 1
        for src in SOURCE_FOLDERS[folder]:
            if src not in source_shas:
                continue
            files = list_files(source_shas[src])
            if not files:
                continue
            # 按原编号排序
            items = sorted(files.items(),
                           key=lambda kv: int(kv[0][:-len(IMAGE_EXT)]))
            print(f"[{folder}] 合并 {src}: {len(items)} 个文件")

            # 第一阶段：整体偏移到 OFFSET 之外，保证不冲突
            stage1 = [
                (old, f"{OFFSET + nxt0}{IMAGE_EXT}", mode, blob)
                for nxt0, (old, (mode, blob)) in enumerate(items, start=nxt)
            ]
            additions1 = [(p, m, b) for (_, p, m, b) in stage1]
            dels1 = [old for (old, _, _, _) in stage1]

            per = BATCH_SIZE
            # 第一阶段
            tree_cur = new_folder_shas[folder]
            for i in range(0, len(stage1), per):
                chunk = stage1[i:i + per]
                tree_cur, current_commit = apply_batch(
                    tree_cur, current_commit,
                    [o for (o, _, _, _) in chunk],
                    [(p, md, bs) for (_, p, md, bs) in chunk],
                    temp_ref,
                    f"stage1 {folder} {src} batch {i//per+1}",
                )
            new_folder_shas[folder] = tree_cur

            nxt += len(items)

        total = nxt - 1
        new_count[folder] = {"max": total, "exclude": []}

        # 第二阶段：把 OFFSET 之外的文件归位到 1..total
        if total == 0:
            continue
        files2 = list_files(new_folder_shas[folder])
        # 只处理 OFFSET 区间内的文件
        stage2 = []
        for name in sorted(files2,
                           key=lambda p: int(p[:-len(IMAGE_EXT)]) - OFFSET):
            if not name.endswith(IMAGE_EXT):
                continue
            num = int(name[:-len(IMAGE_EXT)])
            if num <= OFFSET:
                continue
            orig = num - OFFSET
            if orig < 1 or orig > total:
                continue
            mode, blob = files2[name]
            stage2.append((name, f"{orig}{IMAGE_EXT}", mode, blob))

        # 归位也要防冲突：先把 1..total 中"即将被占"的槽位移开。
        # 简化：逐批处理，每批只把当前槽位腾到更高区间再落位。
        # 由于 stage2 里目标槽位在 1..total 且当前文件都在 OFFSET 之外，
        # 直接做"删旧 + 加新"会冲突（目标路径已存在同名旧文件）。
        # 因此归位分两步：先全部再偏移到 OFFSET*2 之外，再从那里落位。
        HOLD = OFFSET * 2
        tree_cur = new_folder_shas[folder]
        # 先把 stage2 文件抬到 HOLD 区间
        hold_map = {}
        for old, new_name, mode, blob in stage2:
            hold_map[old] = (f"{HOLD + int(old[:-len(IMAGE_EXT)]) - OFFSET}{IMAGE_EXT}",
                            mode, blob)
        for i in range(0, len(hold_map), per):
            keys = list(hold_map)[i:i + per]
            tree_cur, current_commit = apply_batch(
                tree_cur, current_commit,
                keys,
                [hold_map[k] for k in keys],
                temp_ref,
                f"hold {folder} batch {i//per+1}",
            )
        # 再从 HOLD 区间落位到 1..total
        files3 = list_files(tree_cur)
        final_map = {}
        for name in sorted(files3):
            if not name.endswith(IMAGE_EXT):
                continue
            num = int(name[:-len(IMAGE_EXT)])
            if num <= HOLD:
                continue
            orig = num - HOLD
            if orig < 1 or orig > total:
                continue
            mode, blob = files3[name]
            final_map[name] = (f"{orig}{IMAGE_EXT}", mode, blob)
        for i in range(0, len(final_map), per):
            keys = list(final_map)[i:i + per]
            tree_cur, current_commit = apply_batch(
                tree_cur, current_commit,
                keys,
                [final_map[k] for k in keys],
                temp_ref,
                f"finalize {folder} batch {i//per+1}",
            )
        new_folder_shas[folder] = tree_cur

        print(f"[{folder}] 合并完成: {total} 个文件")

    # 最终 count.json
    count_blob_sha = create_blob(json.dumps(new_count, indent=2, ensure_ascii=False))

    final_ri_entries = [
        {"path": f, "mode": "040000", "type": "tree", "sha": new_folder_shas[f]}
        for f in FOLDERS
    ]
    final_ri_entries.extend(ri_other)
    final_ri_entries.append({"path": "count.json", "mode": "100644",
                             "type": "blob", "sha": count_blob_sha})

    final_ri_sha = create_tree(final_ri_entries, base_tree_sha=ri_sha_now)
    final_root_sha = create_tree(
        other_root + [{"path": "ri", "mode": "040000",
                       "type": "tree", "sha": final_ri_sha}],
        base_tree_sha=root_tree_sha
    )
    final_commit = create_commit(
        "chore(ri): merge hd+hl into h, vd+vl into v and update count.json",
        final_root_sha, head_sha
    )
    update_ref(branch, final_commit)
    delete_temp_ref(temp_ref)
    print("完成:", new_count)


if __name__ == "__main__":
    main()
