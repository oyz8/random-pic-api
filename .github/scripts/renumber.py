#!/usr/bin/env python3
"""
合并 hd/hl → h, vd/vl → v，重新编号 1..N，更新 count.json 和 hash_registry.json
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ========== 配置 ==========
RI_DIR = Path("ri")
FOLDERS = ["h", "v"]
SOURCE_FOLDERS = {
    "h": ["hd", "hl"],
    "v": ["vd", "vl"],
}
COUNT_FILE = Path("count.json")
REGISTRY_FILE = Path("hash_registry.json")
OFFSET = 200   # 第一阶段偏移，避开 1..N
HOLD = 400     # 冲突缓冲区间
# ==========================

def git(*args):
    """执行 git 命令"""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"git {' '.join(args)} 失败:")
        print(result.stderr)
        sys.exit(1)
    return result.stdout.strip()

def get_max_index(folder_path):
    """获取目录下最大的数字编号（从 1 开始）"""
    if not folder_path.exists():
        return 0
    max_idx = 0
    for f in folder_path.iterdir():
        if f.is_file():
            stem = f.stem
            try:
                idx = int(stem)
                if idx > max_idx:
                    max_idx = idx
            except ValueError:
                pass
    return max_idx

def collect_files(folder_path):
    """收集目录下所有文件，返回 [(编号, Path)] 列表"""
    files = []
    if not folder_path.exists():
        return files
    for f in folder_path.iterdir():
        if f.is_file():
            try:
                idx = int(f.stem)
                files.append((idx, f))
            except ValueError:
                pass
    return sorted(files)

def main():
    print("=" * 50)
    print("开始合并目录: hd/hl → h, vd/vl → v")
    print("=" * 50)

    # ---- 阶段 0: 检查源目录是否存在 ----
    all_sources = []
    for sources in SOURCE_FOLDERS.values():
        all_sources.extend(sources)

    missing = [s for s in all_sources if not RI_DIR.joinpath(s).exists()]
    if missing:
        print(f"❌ 源目录不存在: {missing}")
        print("请确认 ri/ 下有这些目录")
        sys.exit(1)

    # ---- 阶段 1: 创建新目录，删除旧目录（git 跟踪）----
    print("\n📁 阶段 1: 准备目录结构...")

    # 确保新目录存在
    for new_folder in FOLDERS:
        new_path = RI_DIR.joinpath(new_folder)
        new_path.mkdir(exist_ok=True)

    # 用 git rm 删除旧目录（如果 git 跟踪了的话）
    for src in all_sources:
        src_path = RI_DIR.joinpath(src)
        if src_path.exists():
            # 先尝试 git rm（如果文件被 git 跟踪）
            try:
                subprocess.run(
                    ["git", "rm", "-rf", str(src_path)],
                    capture_output=True, text=True, check=False
                )
            except Exception:
                pass
            # 再物理删除（以防 git rm 没生效）
            if src_path.exists():
                shutil.rmtree(src_path)

    print("✅ 目录结构已调整")

    # ---- 阶段 2: 从源目录复制文件到新目录（偏移编号）----
    print("\n📦 阶段 2: 合并文件...")

    new_counts = {}

    for new_folder, sources in SOURCE_FOLDERS.items():
        new_path = RI_DIR.joinpath(new_folder)
        current_max = 0

        for src in sources:
            src_path = RI_DIR.joinpath(src)
            if not src_path.exists():
                continue

            files = collect_files(src_path)
            print(f"  {src}: {len(files)} 个文件")

            for old_idx, file_path in files:
                current_max += 1
                # 复制文件，用新编号
                new_file = new_path.joinpath(f"{current_max}.webp")
                shutil.copy2(file_path, new_file)

            print(f"    → 已合并到 {new_folder}/ (当前 {current_max} 个)")

        new_counts[new_folder] = current_max
        print(f"  ✅ {new_folder}/ 共 {current_max} 个文件")

    # ---- 阶段 3: 更新 count.json ----
    print("\n📝 阶段 3: 更新 count.json...")
    count_data = {}
    for folder, count in new_counts.items():
        count_data[folder] = {
            "max": count,
            "exclude": []
        }

    with open(COUNT_FILE, 'w', encoding='utf-8') as f:
        json.dump(count_data, f, indent=2, ensure_ascii=False)

    print(f"✅ count.json 已更新: {count_data}")

    # ---- 阶段 4: 更新 hash_registry.json ----
    print("\n🔐 阶段 4: 更新 hash_registry.json...")

    if REGISTRY_FILE.exists():
        with open(REGISTRY_FILE, 'r', encoding='utf-8') as f:
            old_registry = json.load(f)
    else:
        old_registry = {}

    new_registry = {}

    # 建立旧路径到 hash 的映射
    # 旧格式: "hd/1.webp" → hash
    # 新格式: "h/1.webp" → hash

    # 源到新目录的映射
    source_to_new = {}
    for new_folder, sources in SOURCE_FOLDERS.items():
        for src in sources:
            source_to_new[src] = new_folder

    # 重新编号计数
    new_counters = {f: 0 for f in FOLDERS}

    for new_folder, sources in SOURCE_FOLDERS.items():
        for src in sources:
            # 找出旧 registry 中属于这个源目录的条目
            prefix = f"{src}/"
            src_entries = {
                k: v for k, v in old_registry.items()
                if k.startswith(prefix)
            }

            # 按编号排序
            sorted_entries = []
            for key, value in src_entries.items():
                try:
                    idx = int(key.replace(prefix, "").replace(".webp", ""))
                    sorted_entries.append((idx, key, value))
                except ValueError:
                    pass

            sorted_entries.sort(key=lambda x: x[0])

            for idx, old_key, hash_value in sorted_entries:
                new_counters[new_folder] += 1
                new_key = f"{new_folder}/{new_counters[new_folder]}.webp"
                new_registry[new_key] = hash_value

    with open(REGISTRY_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_registry, f, indent=2, ensure_ascii=False)

    print(f"✅ hash_registry.json 已更新，共 {len(new_registry)} 条记录")

    # ---- 阶段 5: Git 提交 ----
    print("\n📤 阶段 5: 提交到 git...")

    # 添加所有变更
    git("add", ".")
    git("add", "ri/")

    # 检查是否有变更需要提交
    status = git("status", "--porcelain")
    if not status:
        print("ℹ️ 没有需要提交的变更")
        return

    # 提交
    git("commit", "-m", "合并目录: hd/hl → h, vd/vl → v")

    # 推送
    branch = git("branch", "--show-current")
    git("push", "origin", branch)

    print(f"✅ 已推送到 {branch}")

    print("\n" + "=" * 50)
    print("🎉 完成！目录合并结果:")
    for folder, count in new_counts.items():
        print(f"  {folder}/: {count} 个文件")
    print("=" * 50)

if __name__ == "__main__":
    main()
