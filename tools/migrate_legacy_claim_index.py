#!/usr/bin/env python3
"""记忆动力学二期 Commit D · 一次性 migration：legacy claim index。

背景：derived_freshness sidecar 只在 trace(superseded_by=...) 触发时，
通过 provenance_edges（Commit B 的 cited 参数记账）传播到已知引用者。但
早于 Commit B 上线之前、或者从没走过 cited 参数的旧派生文档——比如户主
手工把 breath_search/catalog_line 渲染出的 "[bucket_id:xxx]" 复制进
files/*.md——系统并不知道这份文档引用过那个桶，superseded_by 的自动
传播覆盖不到它。

这个脚本做 best-effort 补救：扫描现存 files/*.md，找字面
"[bucket_id:xxx]" 标记（catalog_line()/render_stored_bucket() 渲染桶
正文时的固定格式，跟 bucket_manager.find_marker_line() 用的锚点是同一个），
对每个命中、且目标桶已经被 superseded_by 标记过的情形，生成一条候选
记录——**不自动标 stale**（设计定稿"不确定匹配只进候选队列，禁止自动标
stale"），写入 citation_ledger 的 LegacyClaimCandidate 事件，供人工用
bucket_mgr.list_legacy_claim_candidates() 复核。

跟 Commit B 的 FuzzyReviewQueued（模糊语义候选）不同：这里命中的是字面
标记，确定性很高（marker 摆在那儿），但"该不该标 stale"仍需要人工判断——
可能这份文档早就更新过内容只是 marker 没删、或者压根不需要处理。

写盘纪律：这个脚本完全不碰 files/*.md 或任何桶文件本体，只追加事件到
citation_ledger（append-only，不是"重写"）。--apply 前不做全量备份——
append-only 日志本身就是安全的增量写，不存在"改坏原文件"的风险。

用法：
    python tools/migrate_legacy_claim_index.py           # 仅扫描出报告，不写 ledger
    python tools/migrate_legacy_claim_index.py --apply     # 把候选写入 citation_ledger
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from utils import load_config  # noqa: E402
from bucket_manager import BucketManager, find_marker_line  # noqa: E402

_MARKER_RE = re.compile(r"\[bucket_id:([0-9a-fA-F]+)\]")


def _iter_markdown_files(files_root: str):
    if not os.path.isdir(files_root):
        return
    for dirpath, _dirnames, filenames in os.walk(files_root):
        for fn in sorted(filenames):
            if fn.endswith(".md"):
                yield os.path.join(dirpath, fn)


async def scan(bucket_mgr: BucketManager, files_root: str) -> dict:
    """扫描 files/*.md，返回候选清单（不写盘）。"""
    candidates: list[dict] = []
    scanned_files = 0
    marker_hits = 0

    for path in _iter_markdown_files(files_root):
        scanned_files += 1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        rel = os.path.relpath(path, files_root).replace("\\", "/")
        seen_in_file: set[str] = set()
        for m in _MARKER_RE.finditer(text):
            bucket_id = m.group(1)
            if bucket_id in seen_in_file:
                continue  # 同一文件同一 bucket_id 只报一次，不逐行重复
            seen_in_file.add(bucket_id)
            marker_hits += 1

            bucket = await bucket_mgr.get(bucket_id)
            if not bucket:
                continue  # 桶不存在（已硬删/id 打错），不是候选，跳过
            meta = bucket.get("metadata", {})
            if not meta.get("superseded_by"):
                continue  # 目标桶还没被 supersede，不需要人工核实

            line_no = find_marker_line(text, bucket_id)
            location = f"line:{line_no}" if line_no is not None else "unknown"
            candidates.append({
                "path": f"file:{rel}",
                "bucket_id": bucket_id,
                "location": location,
                "superseded_by": meta.get("superseded_by"),
            })

    return {
        "scanned_files": scanned_files,
        "marker_hits": marker_hits,
        "candidates": candidates,
    }


async def main(apply: bool) -> int:
    config = load_config()
    buckets_dir = config.get("buckets_dir")
    if not buckets_dir or not os.path.isdir(buckets_dir):
        print(f"找不到 buckets 目录: {buckets_dir!r}", file=sys.stderr)
        return 1
    files_root = os.path.join(buckets_dir, "files")

    bucket_mgr = BucketManager(config, embedding_engine=None)
    stats = await scan(bucket_mgr, files_root)

    print("=" * 60)
    print(f"legacy claim index 扫描{'（已写入候选队列）' if apply else '（预演，未写盘）'}")
    print("=" * 60)
    print(
        f"扫描 {stats['scanned_files']} 个文件，命中字面标记 {stats['marker_hits']} 处，"
        f"其中目标桶已被 supersede 的候选 {len(stats['candidates'])} 条。\n"
    )
    if stats["candidates"]:
        print("候选清单（path | bucket_id | location | superseded_by）：")
        for c in stats["candidates"]:
            print(f"  - {c['path']} | {c['bucket_id']} | {c['location']} | {c['superseded_by']}")
        print()

    if apply:
        for c in stats["candidates"]:
            await bucket_mgr.queue_legacy_claim_candidate(
                path=c["path"], bucket_id=c["bucket_id"], location=c["location"],
            )
        print(f"已写入 {len(stats['candidates'])} 条候选到 citation_ledger（LegacyClaimCandidate）。")
        print("人工复核用 bucket_mgr.list_legacy_claim_candidates()，不会自动标 stale。")
    elif stats["candidates"]:
        print("（只读预演，未写入候选队列；确认报告无误后加 --apply 执行）")

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="v3 Commit D 迁移：扫描 files/*.md 里字面 [bucket_id:xxx] 标记，"
                    "对已被 supersede 的目标桶生成人工复核候选（不自动标 stale）"
    )
    ap.add_argument("--apply", action="store_true", help="把候选写入 citation_ledger；默认仅扫描出报告")
    args = ap.parse_args()

    raise SystemExit(asyncio.run(main(apply=args.apply)))
