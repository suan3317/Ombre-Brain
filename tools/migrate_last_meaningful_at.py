#!/usr/bin/env python3
"""记忆动力学二期 Commit C · 一次性 migration：last_meaningful_at 回填。

背景：Commit C 新建 last_meaningful_at 字段——强信号（hold 向既有桶追加 /
trace.meaning_append / citation_credit，见 bucket_manager.record_strong_
signal）推进它，供 activity_bonus() 读取算排序加成。施工单/回放定案：
迁移起点统一为"导入日"，即该桶自己的 created 值——不编造历史强信号，
也不让"存量桶从未真实发生过强信号"跟"这条记忆刚刚诞生"在 activity_bonus()
里长期混同为同一个 0；created 本身已经是"记录进入系统的时刻"，拿它当
起点是最保守、最不武断的选择，K 家已知导入窗口内的桶也一样处理（不额外
特殊对待——"K 家窗口按上方参数"只用来在报告里标注落在窗口内的桶数，供
人工核对，不改变回填逻辑本身；那批桶的 retention 年龄侧的"中性化"是
decay_engine.py 的 retention()/_encoded_age_days() 在读时动态处理的，
跟这里 last_meaningful_at 的回填是两回事）。

只回填缺失的：已经有 last_meaningful_at 的桶（比如 Commit C 上线后已经
真实发生过强信号）原样跳过，不覆盖——维护操作与 surfaced 都不得触碰这个
字段，这条规矩迁移脚本本身也要遵守。

写盘纪律：只写实际改动的文件（缺失 last_meaningful_at 的），已有值的桶
连 mtime 都不会碰。

范围：permanent / dynamic / archive 三个子目录都扫（archive 桶虽然不在
浮现候选池，但字段一致性成本很低，不留个例外）。

用法：
    python tools/migrate_last_meaningful_at.py
        仅扫描出报告，不写盘。
    python tools/migrate_last_meaningful_at.py --apply
        实际执行回填；会先自动全量备份（buckets/.backups/ 下的 zip），
        备份失败就整个中止，不会碰任何桶文件。
    python tools/migrate_last_meaningful_at.py --import-window "2026-07-06T02:08:37" "2026-07-06T02:33:32"
        可选，仅用于报告里额外标出落在该窗口内的桶数（供 K 家核对），
        不影响回填逻辑本身——窗口内窗口外都按同一条规则回填。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import frontmatter as fm  # noqa: E402

from utils import load_config, now_iso, get_version, parse_iso_datetime  # noqa: E402
from backup_archive import build_export_archive, BackupArchiveError  # noqa: E402


def _iter_bucket_files(buckets_dir: str):
    for sub in ("permanent", "dynamic", "archive"):
        root = os.path.join(buckets_dir, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in sorted(filenames):
                if fn.endswith(".md"):
                    yield os.path.join(dirpath, fn)


def _make_backup(config: dict) -> str:
    buckets_dir = config["buckets_dir"]
    embed_cfg = config.get("embedding", {}) or {}
    embedding_db_path = str(
        embed_cfg.get("db_path") or os.path.join(buckets_dir, "embeddings.db")
    )
    export_meta = {
        "exported_at": now_iso(),
        "version": get_version(),
        "reason": "migrate_last_meaningful_at pre-apply backup",
    }
    payload, _manifest = build_export_archive(buckets_dir, embedding_db_path, export_meta)

    backup_dir = os.path.join(buckets_dir, ".backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"pre_migrate_last_meaningful_at_{ts}.zip")
    with open(backup_path, "wb") as f:
        f.write(payload)
    return backup_path


def _in_window(created_raw: str, window) -> bool:
    if not window or not created_raw:
        return False
    try:
        created = parse_iso_datetime(created_raw)
        start = parse_iso_datetime(window[0])
        end = parse_iso_datetime(window[1])
    except (ValueError, TypeError):
        return False
    return start <= created <= end


def run(buckets_dir: str, apply: bool, import_window=None) -> dict:
    """扫描 + （apply 时）回填，返回统计字典供 main()/测试复用。"""
    filled: list[str] = []
    filled_in_window: list[str] = []
    skipped_already_set = 0
    skipped_no_created: list[str] = []
    errors: list[tuple[str, str]] = []

    for path in _iter_bucket_files(buckets_dir):
        try:
            post = fm.load(path)
        except Exception as e:
            errors.append((path, f"读取失败: {e}"))
            continue

        if post.get("last_meaningful_at"):
            skipped_already_set += 1
            continue

        created = post.get("created")
        bucket_id = str(post.get("id") or os.path.splitext(os.path.basename(path))[0])
        if not created:
            skipped_no_created.append(bucket_id)
            continue

        filled.append(bucket_id)
        if _in_window(str(created), import_window):
            filled_in_window.append(bucket_id)

        if apply:
            post["last_meaningful_at"] = created
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(fm.dumps(post))
            except OSError as e:
                errors.append((bucket_id, f"写入失败: {e}"))

    return {
        "filled": filled,
        "filled_in_window": filled_in_window,
        "skipped_already_set": skipped_already_set,
        "skipped_no_created": skipped_no_created,
        "errors": errors,
    }


def _print_report(stats: dict, apply: bool, import_window) -> None:
    verb = "已回填" if apply else "将回填(预演)"
    print("=" * 60)
    print(f"last_meaningful_at 回填{'（已写盘）' if apply else '（预演，未写盘）'}")
    print("=" * 60)
    print(
        f"{verb} {len(stats['filled'])} 条，已有值跳过 {stats['skipped_already_set']} 条，"
        f"无 created 跳过 {len(stats['skipped_no_created'])} 条，失败 {len(stats['errors'])} 条。\n"
    )
    if import_window:
        print(
            f"其中落在导入窗口 [{import_window[0]}, {import_window[1]}] 内："
            f"{len(stats['filled_in_window'])} 条\n"
        )
    if stats["skipped_no_created"]:
        print("无 created 字段（异常桶，需要人工核对）：")
        for bucket_id in stats["skipped_no_created"]:
            print(f"  - {bucket_id}")
        print()
    if stats["errors"]:
        print("失败：")
        for bucket_id, msg in stats["errors"]:
            print(f"  - {bucket_id}: {msg}")
        print()
    if not apply:
        print("（只读预演，未写盘；确认报告无误后加 --apply 执行，会先自动全量备份）")


def main(apply: bool, import_window=None) -> int:
    config = load_config()
    buckets_dir = config["buckets_dir"]
    if not buckets_dir or not os.path.isdir(buckets_dir):
        print(f"找不到 buckets 目录: {buckets_dir!r}", file=sys.stderr)
        return 1

    if apply:
        print(f"全量备份 buckets（{buckets_dir}）……")
        try:
            backup_path = _make_backup(config)
        except BackupArchiveError as e:
            print(f"[中止] 备份失败，未改动任何桶文件：{e}")
            return 1
        print(f"备份完成：{backup_path}\n")

    stats = run(buckets_dir, apply, import_window)
    _print_report(stats, apply, import_window)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="v3 Commit C 迁移：last_meaningful_at 回填为各桶自己的 created 值"
    )
    ap.add_argument("--apply", action="store_true", help="实际执行回填（先自动全量备份）；默认仅扫描出报告")
    ap.add_argument(
        "--import-window", nargs=2, metavar=("START", "END"), default=None,
        help="可选，仅用于报告标出落在窗口内的桶数，不影响回填逻辑本身",
    )
    args = ap.parse_args()
    raise SystemExit(main(apply=args.apply, import_window=args.import_window))
