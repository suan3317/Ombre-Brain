#!/usr/bin/env python3
"""返修单一号 §4：created_at 回填脚本。

病根（K 挖出）：7 月 6 日批量导入的一百余桶，created（衰减引擎用来算
「多老」的字段）全部是导入那一刻的时间戳，不是记忆真实发生的时间。
衰减引擎按"同龄"计算，这批桶权重全部卡死在同一个值（K 家实测十几条
齐刷刷 2.20）——基线错，不是衰减参数错，调参数救不了。

修法：桶名（name 字段）在正常创建路径下是
"YYYY-MM-DD HH-MM-SS [标题]"（见 bucket_manager.py create()，用连字符
不用冒号），跟 created 出自同一个 datetime.now() 调用，正常情况下两者
应该一致。批量导入把这个约定破坏了：桶名/正文里留着真实日期的痕迹，
created 却被写成了导入时刻。这个脚本反过来，从桶名/正文里找回那个
真实日期，回填到 created。

解析优先级（按序，命中就停）：
    1. 桶名匹配标准前缀 "YYYY-MM-DD HH-MM-SS" → 用完整日期时间
    2. 桶名里任意位置的 "YYYY-MM-DD" → 用该日期（当天 00:00:00）
    3. 正文里所有 "YYYY-MM-DD" 模式，取最早一个 → 用该日期（当天 00:00:00）
    4. 都没有 → 不动，列进"未解析清单"交人工定

幂等：目标日期时间与当前 created 已经相同的桶直接跳过（不重写文件、
不计入"改了几条"），所以重复跑不会有任何变化，也不会在报告里刷屏。

用法：
    python tools/backfill_bucket_created_at.py          # 只扫描出报告，不写盘
    python tools/backfill_bucket_created_at.py --apply   # 先备份，再实际回填

这是一次性维护脚本，不进 server 运行时（不在 tools/ 之外的任何地方被
import）。真实数据跑前务必先跑一次不带 --apply 的预演，报告过一遍再跑
--apply；--apply 会先做一份全量备份（buckets/.backups/ 下的 zip），备份
失败就整个中止，不会碰任何桶文件。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import frontmatter  # noqa: E402

from utils import load_config, now_iso, get_version  # noqa: E402
from backup_archive import build_export_archive, BackupArchiveError  # noqa: E402


_TITLE_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2})-(\d{2})-(\d{2})")
_DATE_ONLY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _parse_real_date(name: str, content: str) -> tuple[datetime | None, str]:
    """按 ①标题完整时间戳 ②标题内日期 ③正文最早日期 的优先级找真实日期。

    返回 (解析结果或 None, 命中的来源标签，供报告用)。
    """
    name = name or ""
    m = _TITLE_DATETIME_RE.match(name)
    if m:
        date_str, hh, mm, ss = m.groups()
        try:
            return datetime.strptime(f"{date_str} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S"), "title_datetime"
        except ValueError:
            pass  # 出现在标题最前面但不是合法日期时间（如月份>12），继续往下试

    m = _DATE_ONLY_RE.search(name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d"), "title_date"
        except ValueError:
            pass

    earliest = None
    for m in _DATE_ONLY_RE.finditer(content or ""):
        try:
            candidate = datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            continue
        if earliest is None or candidate < earliest:
            earliest = candidate
    if earliest is not None:
        return earliest, "content_earliest"

    return None, ""


def _iter_bucket_files(buckets_dir: str):
    base = Path(buckets_dir).resolve()
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*.md")):
        yield path


def _load_current_created(post: "frontmatter.Post") -> datetime | None:
    raw = post.get("created")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _make_backup(config: dict) -> str:
    buckets_dir = config["buckets_dir"]
    embed_cfg = config.get("embedding", {}) or {}
    embedding_db_path = str(
        embed_cfg.get("db_path") or os.path.join(buckets_dir, "embeddings.db")
    )
    export_meta = {
        "exported_at": now_iso(),
        "version": get_version(),
        "reason": "backfill_bucket_created_at pre-apply backup",
    }
    payload, _manifest = build_export_archive(buckets_dir, embedding_db_path, export_meta)

    backup_dir = os.path.join(buckets_dir, ".backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"pre_backfill_created_at_{ts}.zip")
    with open(backup_path, "wb") as f:
        f.write(payload)
    return backup_path


def main(apply: bool) -> None:
    config = load_config()
    buckets_dir = config["buckets_dir"]

    if apply:
        print(f"全量备份 buckets（{buckets_dir}）……")
        try:
            backup_path = _make_backup(config)
        except BackupArchiveError as e:
            print(f"[中止] 备份失败，未改动任何桶文件：{e}")
            return
        print(f"备份完成：{backup_path}\n")

    fixed: list[tuple[str, str, str, str]] = []   # (id, old_created, new_created, source)
    skipped_already_correct = 0
    unresolved: list[str] = []

    # archive/ 下是已归档桶，仍然纳入回填范围（衰减引擎不给它们打分，但
    # created 错了同样会误导以后万一被恢复/被 anchor 排序时的判断）——
    # _iter_bucket_files 用 rglob 本来就会扫到 archive/，不用额外处理。
    for path in _iter_bucket_files(buckets_dir):
        try:
            post = frontmatter.load(path)
        except Exception as e:
            print(f"[!] 跳过无法解析的桶文件 {path}: {e}")
            continue

        bucket_id = str(post.get("id") or path.stem)
        name = str(post.get("name") or "")
        content = str(post.content or "")

        target_dt, source = _parse_real_date(name, content)
        if target_dt is None:
            unresolved.append(bucket_id)
            continue

        current_dt = _load_current_created(post)
        target_iso = target_dt.isoformat(timespec="seconds")
        if current_dt is not None and current_dt == target_dt:
            skipped_already_correct += 1
            continue

        old_created = str(post.get("created") or "(空)")
        fixed.append((bucket_id, old_created, target_iso, source))
        if apply:
            post["created"] = target_iso
            with open(path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

    print("=" * 60)
    print(f"created 回填{'（已写盘）' if apply else '（预演，未写盘）'}")
    print("=" * 60)
    print(f"改动 {len(fixed)} 条，已正确跳过 {skipped_already_correct} 条，"
          f"无法解析 {len(unresolved)} 条。\n")

    if fixed:
        print("改动清单（id | 原 created → 新 created | 来源）：")
        for bucket_id, old, new, source in fixed:
            print(f"  [{source}] {bucket_id}: {old} -> {new}")
        print()

    if unresolved:
        print("未解析清单（标题/正文都找不到日期，交人工定）：")
        for bucket_id in unresolved:
            print(f"  - {bucket_id}")
        print()

    if not apply:
        print("（只读预演，未写盘；确认报告无误后加 --apply 执行，会先自动全量备份）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行回填（先自动全量备份）；默认仅扫描出报告")
    args = ap.parse_args()
    main(apply=args.apply)
