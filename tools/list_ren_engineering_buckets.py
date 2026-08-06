#!/usr/bin/env python3
"""
tools/list_ren_engineering_buckets.py — 任务书阶段6:存量清理(Ren相关)候选清单生成器
========================================

只读脚本，不写入/修改任何桶。扫描 buckets_dir 下的 markdown 文件，找出内容
提到 Ren 部署工程细节（renshuo/Ren 部署/金丝雀等关键词）的桶，列成候选
清单交给 Silvia 人工决定逐条 resolved 或归档——脚本本身不做任何自动化
处理决定，也不判断"这条到底该不该清理"。

涉及她的家人史/情感记忆的桶不能靠关键词自动排除得干净（Ren 到底是谁、
跟家庭/关系记忆有没有交叉，脚本判断不了），所以这里分两栏输出：
- 【工程候选】domain/type 没有命中情感/家庭类特征的匹配桶
- 【疑似家人/情感，已从工程候选排除，请人工复核】domain 命中
  家庭/恋爱/情绪/心理/回忆/友谊，或 type=feel 的匹配桶——不会默认进入
  "清单"给 Silvia 处理，但打印出来防止被脚本静默漏审；如果这里有误判
  （比如某条其实是纯工程记录，只是恰好被标了"回忆"域），人工确认后自己
  挪回工程候选即可，脚本不代替判断。

同时按任务书要求做一次代码层诊断（纯静态分析，基于当前 decay_engine.py/
tools/breath 的逻辑，不依赖实际桶数据、不需要连接任何实例）：resolved
标记为什么可能没有把这些桶从浮现里压下去、衰减引擎对"工程"域桶是否有
豁免。

用法（在能访问真实 buckets_dir 的环境里跑，例如生产实例本机/挂载了同一块
持久卷的地方）：
  python tools/list_ren_engineering_buckets.py [--json] [--vault-dir PATH]

默认从 config.yaml 的 buckets_dir 读取；--vault-dir 可显式指定。
========================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import frontmatter as fm  # noqa: E402

from utils import load_config  # noqa: E402


KEYWORDS = ["renshuo", "ren 部署", "ren部署", "金丝雀", "canary", "renshuo.zeabur.app"]

# 与 tools/reclassify_domains.py 的域分类口径一致——命中这些域名的桶不进
# 默认"工程候选"清单，单独列到复核区。
FAMILY_EMOTIONAL_DOMAINS = {"家庭", "恋爱", "情绪", "心理", "回忆", "友谊"}


def _iter_bucket_files(vault_dir: str):
    for sub in ("permanent", "dynamic", "archive"):
        root = os.path.join(vault_dir, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.endswith(".md"):
                    yield os.path.join(dirpath, fn)


def _matches_keywords(haystack: str) -> bool:
    low = haystack.lower()
    return any(kw.lower() in low for kw in KEYWORDS)


def _looks_family_or_emotional(meta: dict) -> bool:
    domains = meta.get("domain") or []
    if isinstance(domains, str):
        domains = [domains]
    if any(d in FAMILY_EMOTIONAL_DOMAINS for d in domains):
        return True
    if meta.get("type") == "feel":
        return True
    return False


def scan(vault_dir: str) -> dict:
    """只读扫描；不修改、不删除、不写回任何文件。"""
    engineering = []
    review = []
    errors = []
    for path in _iter_bucket_files(vault_dir):
        try:
            post = fm.load(path)
        except Exception as e:
            errors.append({"path": path, "error": f"{type(e).__name__}: {e}"})
            continue
        body = str(post.content or "")
        haystack = body + " " + str(post.get("name", ""))
        if not _matches_keywords(haystack):
            continue
        meta = dict(post.metadata)
        row = {
            "id": meta.get("id", os.path.splitext(os.path.basename(path))[0]),
            "name": meta.get("name", ""),
            "domain": meta.get("domain", []),
            "type": meta.get("type", "dynamic"),
            "importance": meta.get("importance"),
            "resolved": meta.get("resolved", False),
            "digested": meta.get("digested", False),
            "valence": meta.get("valence"),
            "arousal": meta.get("arousal"),
            "created": meta.get("created", ""),
            "last_active": meta.get("last_active", ""),
            "path": os.path.relpath(path, vault_dir),
            "preview": body.strip().replace("\n", " ")[:120],
        }
        if _looks_family_or_emotional(meta):
            review.append(row)
        else:
            engineering.append(row)
    return {
        "engineering_candidates": engineering,
        "needs_human_review_family_emotional": review,
        "parse_errors": errors,
    }


DIAGNOSTIC_REPORT = """\
=== 阶段6诊断（静态代码分析，不依赖实际桶数据） ===
检查项：resolved 标记为什么没有压低这些桶的浮现权重；衰减引擎是否对
"工程"域桶有豁免。

结论：decay_engine.py 的 calculate_score() 完全不检查 domain 字段——
pinned/protected/permanent/feel/plan/letter 六种 type 会提前返回固定分，
除此之外（包括所有"工程"/"编程"/"AI"域的普通 dynamic 桶）都走同一套
importance × 时间衰减 × resolved_factor 公式。没有针对任何 domain 的
豁免逻辑，这条本身不成立。

resolved_factor 本身很激进：仅 resolved → ×0.05（降 95%），resolved+
digested → ×0.02（降 98%）——如果 resolved=True 被正确写入，衰减分数
会被压得很低。

但更关键的一点：tools/breath/surface.py 的 surface_default()（breath()
无 query 时的自发浮现模式）在候选池筛选阶段就把 resolved=True 的桶整条
排除出"未解决桶"池——不是"权重降低后仍可能挤进结果"，是根本不出现在
自发浮现候选集里。所以如果这些桶仍然全文浮现，两种可能（脚本本身分不清，
需要看下面清单里每条的 resolved 字段实际值）：

  1. resolved 从未被真正设置为 True——只是讨论过"这些该整理了"但没有
     实际调用 trace(bucket_id, resolved=1)。这是数据/操作问题，不是
     代码 bug。
  2. resolved=True 已经设置，但桶是经由 breath_search(query=...) 关键词
     检索命中的——tools/breath/search.py 的 surface_search() 不检查
     resolved 字段，这是刻意设计（README:"resolved=1=标记已放下，沉底
     仅在关键词触发时返回"）。这种情况下"仍在浮现"其实是搜索命中，不是
     自发浮现，属于按设计工作，不是 bug。

下面清单里每条的 resolved 字段就是区分这两种情况的关键——建议先看
resolved 是否为 True，再决定要不要用 trace(resolved=1) 补一刀，还是这就是
预期内的可搜索归档状态、不需要动。
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读扫描：列出 Ren 相关工程部署记忆桶候选清单（任务书阶段6），不修改任何数据。"
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--vault-dir", default=None, help="覆盖 config.yaml 里的 buckets_dir")
    args = parser.parse_args()

    vault_dir = args.vault_dir or load_config().get("buckets_dir")
    if not vault_dir or not os.path.isdir(vault_dir):
        print(f"找不到 buckets 目录: {vault_dir!r}", file=sys.stderr)
        return 1

    result = scan(vault_dir)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(DIAGNOSTIC_REPORT)

    eng = result["engineering_candidates"]
    rev = result["needs_human_review_family_emotional"]

    print(f"=== 工程候选（{len(eng)} 条）——关键词命中，domain/type 未命中家庭/情感特征 ===")
    if not eng:
        print("(无)")
    for row in eng:
        print(
            f"- [{row['id']}] {row['name']} | domain={row['domain']} | "
            f"importance={row['importance']} | resolved={row['resolved']} digested={row['digested']} | "
            f"created={row['created']} last_active={row['last_active']}\n"
            f"  {row['preview']}"
        )

    print(f"\n=== 疑似家人/情感，已从工程候选排除，请人工复核（{len(rev)} 条） ===")
    if not rev:
        print("(无)")
    for row in rev:
        print(
            f"- [{row['id']}] {row['name']} | domain={row['domain']} | type={row['type']}\n"
            f"  {row['preview']}"
        )

    if result["parse_errors"]:
        print(f"\n=== 解析失败，未纳入统计（{len(result['parse_errors'])} 个文件） ===")
        for err in result["parse_errors"]:
            print(f"- {err['path']}: {err['error']}")

    print(
        "\n本脚本只读，未修改任何桶。以上清单请 Silvia 逐条确认后手动用 "
        "trace(bucket_id, resolved=1) 或 trace(bucket_id, delete=True) 处理。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
