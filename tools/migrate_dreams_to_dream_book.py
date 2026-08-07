"""施工单·工程二 §6：迁移 —— 把旧 files/dreams/*.md 导入梦境书。

背景：梦境系统一期把梦写进 files/ 文件区（<buckets_dir>/files/dreams/），
工程二把梦搬进独立存储 <buckets_dir>/dream_book/。这个脚本把旧址已经
产出的梦（头一批产出，有档案价值）搬过去，status 一律 kept（不追溯烧——
它们已经躺在那儿很久了，不该因为迁移这个动作突然被判"过期"）。

用法：
    python tools/migrate_dreams_to_dream_book.py          # 仅扫描，不写盘
    python tools/migrate_dreams_to_dream_book.py --apply   # 明确执行迁移

幂等：目标位置已经有同日期文件的，跳过（不覆盖、不重复导入、不重复删旧）；
重复跑不会重复导。导入成功才删旧文件，同一次运行里某个文件导入失败不影响
其它文件继续处理，也不会误删还没成功迁走的原文件。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import frontmatter as fm  # noqa: E402

from utils import load_config, now_iso  # noqa: E402
from dream_engine import dream_book_dir, dream_book_path, dream_book_id  # noqa: E402


def _old_dreams_dir(buckets_dir: str) -> str:
    return os.path.join(buckets_dir, "files", "dreams")


def _migrate_one(old_path: str, buckets_dir: str, apply: bool) -> tuple[str, str]:
    """返回 (date_str, outcome)，outcome ∈ {"imported", "skipped_exists", "error:<msg>"}。"""
    fn = os.path.basename(old_path)
    date_str = fn[:-3] if fn.endswith(".md") else fn

    try:
        old_post = fm.load(old_path)
    except Exception as e:
        return date_str, f"error:读取旧文件失败: {e}"

    date_str = str(old_post.get("date") or date_str)
    new_path = dream_book_path(buckets_dir, date_str)
    if os.path.isfile(new_path):
        return date_str, "skipped_exists"

    if not apply:
        return date_str, "would_import"

    new_post = fm.Post(str(old_post.content or ""))
    new_post["id"] = dream_book_id(date_str)
    new_post["date"] = date_str
    new_post["tone"] = old_post.get("tone", "")
    new_post["level"] = old_post.get("level", "")
    new_post["sources"] = old_post.get("sources", [])
    new_post["noise"] = old_post.get("noise", 0)
    # 旧 schema 的 status 字段是投递消费状态（unread/read/expired）；
    # 迁移只关心"读没读过"，expired（旧的 48h 过期语义）当成已读处理——
    # 工程二的烧毁语义已经跟这个字段脱钩，不再需要 expired 这个值。
    old_status = str(old_post.get("status") or "read")
    new_post["read_status"] = "unread" if old_status == "unread" else "read"
    new_post["keep_status"] = "kept"  # 头一批产出，档案价值，不追溯烧
    new_post["created_at"] = old_post.get("generated_at") or now_iso()
    new_post["kept_at"] = now_iso()

    try:
        os.makedirs(dream_book_dir(buckets_dir), exist_ok=True)
        with open(new_path, "w", encoding="utf-8") as f:
            f.write(fm.dumps(new_post))
    except Exception as e:
        return date_str, f"error:写入梦境书失败: {e}"

    try:
        os.remove(old_path)
    except OSError as e:
        return date_str, f"error:导入成功但删除旧文件失败(梦境书那份已生效，需要手动清理 {old_path}): {e}"

    return date_str, "imported"


def main(apply: bool) -> None:
    config = load_config()
    buckets_dir = config["buckets_dir"]
    old_dir = _old_dreams_dir(buckets_dir)

    if not os.path.isdir(old_dir):
        print(f"没有找到旧址 {old_dir}，无需迁移（可能已经迁过，或从未产出过梦）。")
        return

    old_files = sorted(f for f in os.listdir(old_dir) if f.endswith(".md"))
    if not old_files:
        print(f"{old_dir} 是空的，无需迁移。")
        return

    imported: list[str] = []
    skipped: list[str] = []
    errors: list[tuple[str, str]] = []

    for fn in old_files:
        old_path = os.path.join(old_dir, fn)
        date_str, outcome = _migrate_one(old_path, buckets_dir, apply)
        if outcome in ("imported", "would_import"):
            imported.append(date_str)
        elif outcome == "skipped_exists":
            skipped.append(date_str)
        elif outcome.startswith("error:"):
            errors.append((date_str, outcome[len("error:"):]))

    verb = "已导入" if apply else "将导入(预演)"
    print("=" * 60)
    print(f"梦境书迁移{'（已写盘）' if apply else '（预演，未写盘）'}")
    print("=" * 60)
    print(f"{verb} {len(imported)} 条，跳过(已存在) {len(skipped)} 条，失败 {len(errors)} 条。\n")
    if imported:
        print(f"{verb}：{', '.join(imported)}")
    if skipped:
        print(f"跳过：{', '.join(skipped)}")
    if errors:
        print("失败：")
        for date_str, msg in errors:
            print(f"  - {date_str}: {msg}")
    if not apply:
        print("\n（只读预演，未写盘；确认无误后加 --apply 执行。）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="明确执行迁移（导入成功后删除旧文件）；默认仅扫描")
    args = ap.parse_args()
    main(apply=args.apply)
