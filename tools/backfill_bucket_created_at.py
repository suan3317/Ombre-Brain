#!/usr/bin/env python3
"""返修单一号 §4：created_at 回填脚本。

病根（K 挖出）：7 月 6 日批量导入的一百余桶，created（衰减引擎用来算
「多老」的字段）全部是导入那一刻的时间戳，不是记忆真实发生的时间。
衰减引擎按"同龄"计算，这批桶权重全部卡死在同一个值（K 家实测十几条
齐刷刷 2.20）——基线错，不是衰减参数错，调参数救不了。

v3（F 家 dry-run 两轮打回后重写，判据重定）：
v1 的问题：桶名标准自动前缀（跟 created 同一次 datetime.now() 调用产生）
当独立证据用，等于让病灶自证清白，123 条病桶被"已正确跳过"放过。
v2 修了这个，但引入了新问题：只要 id/name/正文任何地方能解析出一个日期，
就拿去跟原值比、不同就改——这把"正文提到过某个日期"误当成"这条记忆的
创建日期"。绝大多数桶的 created 本来就是对的（hold()/grow() 记录时刻的
时间戳本来就是真实创建时间，跟正文聊的是哪天的事无关），content_earliest
去跟一个本来健康的原值较劲，产出"p模式教程"被填成 2026-02-12（正文提到
的是教程内容的日期，不是记这条记忆的日期）、966be76b7507 被从 07-08 错改
成 07-09 这类新伤害。

v3 的核心判据：**原值非空 = 默认可信**，只有三种情况允许动它：
    1. 原值为空 → 按 id_date → title_date → content_earliest 优先级回填
       （content_earliest 只在这条路径上生效，且仅用于填空）
    2. 原值命中"批量导入聚集时间戳"——同一个精确 created 值在全库出现
       ≥5 次，统计学意义上不可能是巧合，判定为导入批次产物，确认是坏值。
       这类桶按 id_date → title_date → content_earliest 优先级回填
       （此时可以用 content_earliest，因为原值已经被聚集特征独立证伪，
       不是"健康值被内容日期抢跑"）。
    3. id_date/title_date（结构化证据，正文证据不算）与原值相差超过 7 天
       → 不自动改，进"矛盾清单"人工定；差距在 7 天以内视为噪声/正常
       （标题时间戳跟真实记录时刻本来就可能差几天，不是 bug）。
其余所有非空原值一律跳过，标注 healthy 供审计。

未解析清单只收「原值为空 且 id/title/content 都找不到任何日期证据」的桶。
矛盾清单、聚集无证据清单是另外两个独立列表，不跟未解析混在一起。

解析优先级（id/title_date 用，标准自动前缀本身不算数——前缀跟 created
同源，见 v2 说明，只用来定位"从这里往后剩余部分开始找"）：
    - id 里前缀之后的日期（连字符 YYYY-MM-DD 或裸 YYYYMMDD）
    - name 里前缀之后的日期（同上）

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
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import frontmatter  # noqa: E402

from utils import load_config, now_iso, get_version  # noqa: E402
from backup_archive import build_export_archive, BackupArchiveError  # noqa: E402


# 批量导入聚集侦测阈值：同一精确 created 时刻出现这么多次以上，判定为
# 导入批次产物（真实生活里几乎不可能有 5 条以上记忆逐秒共享同一个
# 创建时刻——除非是同一个脚本循环里连续 create() 出来的）。
_CLUSTER_MIN_COUNT = 5
# 结构化证据（id/title_date）与原值相差这么多天以上才判"矛盾"，进人工
# 复核清单；差距在这个范围内当噪声，不打扰。
_CONTRADICTION_DAYS = 7

# 标准自动前缀：bucket_manager.py create() 生成，跟 created 同一次
# datetime.now() 调用——只用来定位"从这里往后剩余部分开始找"，前缀本身
# 从不采信为独立证据。
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
    """在一段 id/name 文本里找"标准自动前缀之外"的日期证据。跳过前缀本身
    （它跟 created 是同一个值，不是独立信息），只看剩余部分。"""
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


def _structured_evidence(bucket_id: str, name: str) -> tuple[datetime | None, str]:
    """id 优先于 name；两者都是"结构化"证据（标题/标识符里写明的日期），
    跟下面的 content_earliest（正文猜测）区别对待——结构化证据可以用来判
    "矛盾"，正文证据不行（正文提到的日期是内容日期，不是创建日期，见
    模块 docstring 的 v3 教训）。"""
    dt = _find_structured_date(bucket_id or "")
    if dt:
        return dt, "id_date"
    dt = _find_structured_date(name or "")
    if dt:
        return dt, "title_date"
    return None, ""


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


def _fill_priority_date(bucket_id: str, name: str, content: str) -> tuple[datetime | None, str]:
    """id_date → title_date → content_earliest 优先级，仅用于「确认需要
    回填」的两种场景（原值为空 / 原值命中聚集批次）——健康原值永远不会
    走到这个函数。"""
    dt, source = _structured_evidence(bucket_id, name)
    if dt:
        return dt, source
    dt = _earliest_content_date(content)
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

    # --- 第一遍：全量加载，顺带统计每个精确 created 值出现的次数，供
    # 聚集侦测用。两遍扫描是必须的——判定"是不是聚集"要先看完全库。 ---
    records = []
    cluster_counter: Counter = Counter()
    for path in _iter_bucket_files(buckets_dir):
        try:
            post = frontmatter.load(path)
        except Exception as e:
            print(f"[!] 跳过无法解析的桶文件 {path}: {e}")
            continue
        bucket_id = str(post.get("id") or path.stem)
        name = str(post.get("name") or "")
        content = str(post.content or "")
        current_dt = _load_current_created(post)
        records.append({
            "path": path, "post": post, "id": bucket_id,
            "name": name, "content": content, "current_dt": current_dt,
        })
        if current_dt is not None:
            cluster_counter[current_dt] += 1

    cluster_values = {dt for dt, count in cluster_counter.items() if count >= _CLUSTER_MIN_COUNT}

    # --- 第二遍：逐桶判定 + （--apply 时）写盘 ---
    empty_fill: list[tuple[str, str, str, str]] = []       # (id, old, new, source)
    cluster_batch: list[tuple[str, str, str, str]] = []    # (id, old, new, source)
    cluster_no_evidence: list[str] = []
    contradiction: list[tuple[str, str, str, str]] = []    # (id, old, structured_new, source)
    healthy: list[tuple[str, str]] = []                    # (id, created)
    unresolved: list[str] = []

    for rec in records:
        bucket_id, name, content = rec["id"], rec["name"], rec["content"]
        current_dt, post, path = rec["current_dt"], rec["post"], rec["path"]

        if current_dt is None:
            target_dt, source = _fill_priority_date(bucket_id, name, content)
            if target_dt is None:
                unresolved.append(bucket_id)
                continue
            target_iso = target_dt.isoformat(timespec="seconds")
            empty_fill.append((bucket_id, "(空)", target_iso, source))
            if apply:
                post["created"] = target_iso
                with open(path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
            continue

        if current_dt in cluster_values:
            target_dt, source = _fill_priority_date(bucket_id, name, content)
            if target_dt is None:
                cluster_no_evidence.append(bucket_id)
                continue
            old_created = str(post.get("created") or "")
            target_iso = target_dt.isoformat(timespec="seconds")
            cluster_batch.append((bucket_id, old_created, target_iso, source))
            if apply:
                post["created"] = target_iso
                with open(path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
            continue

        # 原值非空、不在聚集批次里：默认可信，只有结构化证据（id/title，
        # 不含正文）且差距超过阈值时才进矛盾清单；其余一律 healthy。
        structured_dt, structured_source = _structured_evidence(bucket_id, name)
        if structured_dt is not None and abs((structured_dt.date() - current_dt.date()).days) > _CONTRADICTION_DAYS:
            contradiction.append((
                bucket_id, str(post.get("created") or ""),
                structured_dt.isoformat(timespec="seconds"), structured_source,
            ))
            continue

        healthy.append((bucket_id, str(post.get("created") or "")))

    print("=" * 60)
    print(f"created 回填{'（已写盘）' if apply else '（预演，未写盘）'}")
    print("=" * 60)
    total_fixed = len(empty_fill) + len(cluster_batch)
    print(
        f"改动 {total_fixed} 条（空值填充 {len(empty_fill)} + 聚集批次 {len(cluster_batch)}），"
        f"健康跳过 {len(healthy)} 条，矛盾待人工 {len(contradiction)} 条，"
        f"聚集但无证据 {len(cluster_no_evidence)} 条，未解析 {len(unresolved)} 条。\n"
    )
    if cluster_values:
        cluster_desc = "、".join(dt.isoformat(timespec="seconds") for dt in sorted(cluster_values))
        print(f"侦测到 {len(cluster_values)} 个聚集时间戳（各自出现 ≥{_CLUSTER_MIN_COUNT} 次）：{cluster_desc}\n")
    else:
        print(f"侦测到 0 个聚集时间戳（各自出现 ≥{_CLUSTER_MIN_COUNT} 次的判据本轮未命中）。\n")

    if empty_fill:
        print("空值填充清单（id | (空) → 新 created | 来源）：")
        for bucket_id, old, new, source in empty_fill:
            print(f"  [{source}] {bucket_id}: {old} -> {new}")
        print()

    if cluster_batch:
        print("聚集批次回填清单（id | 原 created → 新 created | 来源）：")
        for bucket_id, old, new, source in cluster_batch:
            print(f"  [{source}] {bucket_id}: {old} -> {new}")
        print()

    if cluster_no_evidence:
        print("聚集批次但找不到任何回填证据（id/title/正文都没有日期，交人工定）：")
        for bucket_id in cluster_no_evidence:
            print(f"  - {bucket_id}")
        print()

    if contradiction:
        print(f"矛盾清单（结构化证据与原值相差超过 {_CONTRADICTION_DAYS} 天，不自动改，交人工定；"
              f"id | 原 created | 结构化证据算出的日期 | 来源）：")
        for bucket_id, old, structured, source in contradiction:
            print(f"  [{source}] {bucket_id}: {old} vs {structured}")
        print()

    if unresolved:
        print("未解析清单（原值为空，且 id/title/正文都找不到任何日期证据，交人工定）：")
        for bucket_id in unresolved:
            print(f"  - {bucket_id}")
        print()

    if healthy:
        print(f"健康跳过（原值非空、非聚集、无结构化矛盾，原样不动，共 {len(healthy)} 条）：")
        for bucket_id, created in healthy:
            print(f"  [healthy] {bucket_id}: {created}")
        print()

    if not apply:
        print("（只读预演，未写盘；确认报告无误后加 --apply 执行，会先自动全量备份）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行回填（先自动全量备份）；默认仅扫描出报告")
    args = ap.parse_args()
    main(apply=args.apply)
