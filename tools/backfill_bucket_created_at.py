#!/usr/bin/env python3
"""返修单一号 §4：created_at 回填脚本。

病根（K 挖出）：7 月 6 日批量导入的一百余桶，created（衰减引擎用来算
「多老」的字段）全部是导入那一刻的时间戳，不是记忆真实发生的时间。
衰减引擎按"同龄"计算，这批桶权重全部卡死在同一个值（K 家实测十几条
齐刷刷 2.20）——基线错，不是衰减参数错，调参数救不了。

v2（K 家 dry-run 打回后重写）：v1 把桶名的标准自动前缀
"YYYY-MM-DD HH-MM-SS"（bucket_manager.py create() 里 `_ts =
datetime.now().strftime(...)`，跟 created 同一次调用产生）当成"标题证据"
优先采信——但这个前缀对**所有**桶（不管是不是批量导入的病桶）都严格等于
created 本身，不是独立信息。对病桶来说，标题前缀记录的正是导入那一刻，
跟 created 里的坏值必然逐秒相同：v1 拿它去跟 created 比对"一致就跳过"，
等于拿病灶自己证明病灶没病，123 条本该修的桶就是这样被"已正确跳过"放过的
（`current_dt == target_dt`，两边算出来的其实是同一个数）。v2 彻底不信
这个自动前缀，只信"标准前缀之外"的日期痕迹：
  - id / name 里标准前缀**之后**残留的日期（人工/LLM 起的标题里带的日期，
    或本身就不是标准前缀命名的桶，如梦境书 id="dream_2026-07-31"）
  - 正文里最早出现的日期

这些才是跟 created 生成机制无关的独立证据，冲突时按 id → name → 正文
的顺序取先命中的（标题类证据优先于正文猜测）。

解析优先级（按序，命中就停；标准自动前缀本身不算数）：
    1. id 里标准前缀之后的日期（YYYY-MM-DD 或裸 YYYYMMDD）
    2. name 里标准前缀之后的日期（同上，格式覆盖 dream_YYYY-MM-DD /
       YYYYMMDD / xxx_YYYYMMDD 等——只要是连字符或裸 8 位数字都认）
    3. 正文里所有 YYYY-MM-DD 模式取最早一个（未来日期视为噪声丢弃，
       不可能是桶创建时刻）
    4. 都没有 → 不动，列进"未解析清单"交人工定

幂等：目标日期（这次是真正独立算出来的）与当前 created 的日期部分已经
相同的桶跳过，不重写文件；重复跑不会有变化。跳过清单在报告里逐条列出
命中来源和值，方便审计"这条真的没病"还是"又被放过一次"。

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


# 标准自动前缀：bucket_manager.py create() 生成，跟 created 同一次
# datetime.now() 调用——只用来定位"从这里往后剩余部分开始找"，前缀本身
# 从不采信为独立证据（见上方 v2 说明）。
_AUTO_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2})-(\d{2})-(\d{2})\s*")
_HYPHENATED_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# 裸 8 位数字日期（YYYYMMDD），前后不能再挨着数字，避免截断更长的数字串
# （比如 feel 桶 id 里的 "202506011423" 这类 12 位分钟级时间戳不会被误吃
# 成 8 位日期）。
_BARE_DATE_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def _valid_hyphenated_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _valid_bare_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return None


def _find_structured_date(text: str) -> datetime | None:
    """在 id/name 里找"标准自动前缀之外"的日期证据。跳过前缀本身
    （它跟 created 是同一个值，不是独立信息），只看剩余部分；剩余部分
    可能是 LLM/人工起的标题（如"谈到 2026-01-15 的计划"），也可能整个
    字符串压根不是标准前缀命名的（如梦境书 id="dream_2026-07-31"，这时
    "剩余部分"就是全部原文）。连字符 YYYY-MM-DD 优先于裸 YYYYMMDD——
    两种都命中时前者通常是更明确写出来的日期。"""
    if not text:
        return None
    m = _AUTO_PREFIX_RE.match(text)
    remainder = text[m.end():] if m else text

    hm = _HYPHENATED_DATE_RE.search(remainder)
    if hm:
        dt = _valid_hyphenated_date(hm.group(1))
        if dt:
            return dt

    bm = _BARE_DATE_RE.search(remainder)
    if bm:
        dt = _valid_bare_date(bm.group(1))
        if dt:
            return dt

    return None


def _earliest_content_date(content: str) -> datetime | None:
    now = datetime.now()
    earliest = None
    for m in _HYPHENATED_DATE_RE.finditer(content or ""):
        candidate = _valid_hyphenated_date(m.group(1))
        if candidate is None:
            continue
        if candidate > now:
            continue  # 未来日期不可能是桶的真实创建时刻，是噪声不是证据
        if earliest is None or candidate < earliest:
            earliest = candidate
    return earliest


def _parse_real_date(bucket_id: str, name: str, content: str) -> tuple[datetime | None, str]:
    """按 ①id ②name ③正文最早日期 的优先级找真实日期（标准自动前缀本身
    不算数，见模块 docstring）。返回 (解析结果或 None, 命中来源标签)。"""
    dt = _find_structured_date(bucket_id or "")
    if dt:
        return dt, "id_date"

    dt = _find_structured_date(name or "")
    if dt:
        return dt, "title_date"

    dt = _earliest_content_date(content or "")
    if dt:
        return dt, "content_earliest"

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

    fixed: list[tuple[str, str, str, str]] = []            # (id, old_created, new_created, source)
    skipped_already_correct: list[tuple[str, str, str]] = []  # (id, created, source)
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

        target_dt, source = _parse_real_date(bucket_id, name, content)
        if target_dt is None:
            unresolved.append(bucket_id)
            continue

        current_dt = _load_current_created(post)
        target_iso = target_dt.isoformat(timespec="seconds")
        if current_dt is not None and current_dt.date() == target_dt.date():
            skipped_already_correct.append((bucket_id, str(post.get("created") or ""), source))
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
    print(f"改动 {len(fixed)} 条，已正确跳过 {len(skipped_already_correct)} 条，"
          f"无法解析 {len(unresolved)} 条。\n")

    if fixed:
        print("改动清单（id | 原 created → 新 created | 来源）：")
        for bucket_id, old, new, source in fixed:
            print(f"  [{source}] {bucket_id}: {old} -> {new}")
        print()

    if skipped_already_correct:
        print("已正确跳过清单（id | 当前 created | 独立算出的来源——供审计，"
              "确认真的没病而不是又被放过）：")
        for bucket_id, created, source in skipped_already_correct:
            print(f"  [{source}] {bucket_id}: {created}")
        print()

    if unresolved:
        print("未解析清单（id/标题/正文都找不到独立日期证据，交人工定）：")
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
