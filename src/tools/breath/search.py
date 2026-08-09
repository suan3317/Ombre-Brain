"""
========================================
tools/breath/search.py — 有 query 的检索模式
========================================

走 breath(query=...) 时进入这里。一次向量查询与 bucket_manager 的
关键词/BM25 检索融合，命中后逐字返回桶正文并套 token 预算。

关键行为：
- domain/valence/arousal 作为过滤参数传给 bucket_mgr.search
- embedding 未配置/未启用/调用失败时明确提示并继续关键词/BM25 检索
- 向量通道阈值 sim>=0.65；domain/tags/type 过滤与关键词通道完全一致
- 命中正文不经过 LLM 摘要、改写或压缩，直接返回当前存储的 content
- 命中后调 touch()，但不修改本次返回的正文或元数据
- 检索结果 < 3 时 40% 概率从低权重旧桶里随机漂出 1-3 条「忽然想起来」
- 命中 0 条时回 webhook 报空，并给出可操作的引导文案

不做什么（边界）：
- 不返回 feel/plan/letter（专用通道有自己的入口）
- pinned/protected/permanent 仍可被检索（也是记忆，只是同时在浮现模式置顶）
- dont_surface=True 在检索中保留——主动遗忘只限制无参浮现

对外暴露：surface_search(query, max_results, max_tokens, domain, valence,
                          arousal, tag_filter) → str
========================================
"""

import asyncio
import random

from ombrebrain.policy.surfacing import SurfacePolicyVM
from .. import _runtime as rt
from ._verbatim import render_stored_bucket, STORED_DATA_NOTICE
from decay_engine import band_of

_SURFACE_POLICY = SurfacePolicyVM.default()

_VECTOR_QUERY_TOPK = 50

# v3 Commit C：band 优先级，仅用于"检索排序相邻 band 不可越界"这条定稿
# 不变量——跟 decay_engine.py 的 band_floor() 是同一个高低顺序，但检索这里
# 不需要 band_floor() 的绝对分数区间（不重新计算 within-band 分数，只用
# 稳定排序保序），所以就地定义一个轻量的整数优先级，不依赖 DecayEngine 实例。
_BAND_PRIORITY = {"high": 2, "mid": 1, "low": 0}


def _enforce_band_order(matches: list) -> list:
    """检索排序相邻 band 不可越界：高 band 结果永远排在中/低 band 之前，
    band 内部保留 bucket_mgr.search() 自身给出的相关度顺序（稳定排序）。
    """
    return sorted(
        matches,
        key=lambda b: _BAND_PRIORITY.get(band_of((b.get("metadata") or {}).get("importance")), 0),
        reverse=True,
    )

_SEMANTIC_DISABLED_NOTE = "[检索降级：语义索引暂不可用，本次仅使用关键词/BM25。]"
# 返修单一号改动六:病句清理——"未被截断或摘要"是双重否定式的费解表述，
# 且这条记忆本来就是整条省略(不是截断出一半)，照实说清楚；同时按 wake
# 段(_wake_render.py)已立的"显式留痕"规则补上省略条数，不再是模糊的
# "下一条"，全段自查后跟 feel.py/importance.py/surface.py 口径统一。
def _budget_notice(remaining: int) -> str:
    # remaining<=0：命中列表本身已经全部渲染完，是随机"忽然想起来"分支
    # 另外撞了预算(边界情况，说不清切确条数)，不硬凑数字，给通用措辞。
    if remaining > 0:
        return (
            f"[token 预算不足：还有 {remaining} 条命中的记忆未返回"
            f"(整条省略，不是截断)，请提高 max_tokens 后重试。]"
        )
    return "[token 预算不足：部分内容未返回(整条省略，不是截断)，请提高 max_tokens 后重试。]"


def _bucket_has_tags(meta: dict, tag_filter: list) -> bool:
    if not tag_filter:
        return True
    bucket_tags = set(meta.get("tags", []) or [])
    return all(t in bucket_tags for t in tag_filter)


def _can_surface_search(bucket: dict) -> bool:
    return _SURFACE_POLICY.evaluate_bucket(bucket, mode="search").allowed


async def _record_semantic_recall_streaks(bucket_ids: list) -> None:
    """v3 Commit B：后台记负反馈 streak，逐条失败不影响其他/不影响响应。"""
    for bucket_id in bucket_ids:
        try:
            await rt.bucket_mgr.record_semantic_recall_without_use(bucket_id)
        except Exception as e:
            rt.logger.warning(f"负反馈 streak 记录失败(不影响浮现) bucket={bucket_id}: {e}")


async def _semantic_scores(query: str, top_k: int) -> tuple[dict[str, float], str]:
    """Run the vector query once and return scores plus an optional notice."""
    engine = rt.embedding_engine
    if not engine or not getattr(engine, "enabled", False):
        rt.logger.warning("breath semantic search unavailable; using keyword/BM25 only")
        return {}, _SEMANTIC_DISABLED_NOTE

    try:
        strict_search = getattr(engine, "search_similar_strict", None)
        if callable(strict_search):
            pairs = await strict_search(query, top_k=top_k)
        else:
            pairs = await engine.search_similar(query, top_k=top_k)
        return {bucket_id: float(score) for bucket_id, score in pairs}, ""
    except Exception as exc:
        rt.logger.warning(
            f"breath semantic search failed; using keyword/BM25 only: "
            f"{type(exc).__name__}: {exc}"
        )
        return {}, _SEMANTIC_DISABLED_NOTE


async def surface_search(
    query: str,
    max_results: int,
    max_tokens: int,
    domain: str,
    valence: float,
    arousal: float,
    tag_filter: list,
) -> str:
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    # A full bucket id is an address, not a semantic query.  Resolve it before
    # embedding/BM25 work so callers can reliably read the on-disk source text
    # immediately before trace(content=...) without an LLM or derived index in
    # the path.  Archived/deleted and dedicated bucket types keep the same
    # visibility boundary as ordinary search.
    exact_id = query.strip()
    try:
        exact_bucket = await rt.bucket_mgr.get(exact_id)
    except Exception as exc:
        rt.logger.warning(
            f"breath exact bucket lookup failed; continuing with search: "
            f"{type(exc).__name__}: {exc}"
        )
        exact_bucket = None
    if exact_bucket:
        meta = exact_bucket.get("metadata", {}) or {}
        is_archived = meta.get("type") == "archived" or bool(meta.get("deleted_at"))
        if (
            not is_archived
            and meta.get("type") not in ("feel", "plan", "letter")
            and _can_surface_search(exact_bucket)
            and _bucket_has_tags(meta, tag_filter)
        ):
            rendered, entry_tokens = render_stored_bucket(
                exact_bucket,
                f"[exact_bucket_id:true] [bucket_id:{exact_bucket['id']}]",
            )
            if entry_tokens > max_tokens:
                return _budget_notice(0)
            asyncio.create_task(
                rt.bucket_mgr.touch_many([exact_bucket["id"]], ripple=False)
            )
            if rt.fire_webhook:
                await rt.fire_webhook(
                    "breath",
                    {"mode": "exact_id", "matches": 1, "chars": len(rendered)},
                )
            return STORED_DATA_NOTICE + "\n\n" + rendered

    vector_scores, semantic_notice = await _semantic_scores(
        query, top_k=max(max_results, _VECTOR_QUERY_TOPK)
    )

    try:
        matches = await rt.bucket_mgr.search(
            query,
            limit=max(max_results, 20),
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
            vector_scores=vector_scores,
        )
    except Exception as e:
        rt.logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    matches = [
        b for b in matches
        if _can_surface_search(b)
        and b["metadata"].get("type") not in ("feel", "plan", "letter")
    ]
    matches = [b for b in matches if _bucket_has_tags(b["metadata"], tag_filter)]
    # v3 Commit C 定稿不变量："检索排序相邻 band 不可越界"——高 band 的结果
    # 永远排在中/低 band 之前，band 内部保留 bucket_mgr.search() 自身的
    # 相关度排序（稳定排序，不重新计算 within-band 分数；band 之间不比较
    # 相关度强弱，只比较 band 高低）。
    matches = _enforce_band_order(matches)
    matches = matches[:max_results]

    results = []
    token_used = 0
    budget_blocked = False
    budget_blocked_count = 0
    touched_ids: list = []   # 性能 P2：浮现后统一在后台 touch，不在响应路径逐条 await
    semantic_recall_ids: list = []  # v3 Commit B：纯语义命中(vector_match)才计负反馈 streak
    for i, bucket in enumerate(matches):
        meta = bucket["metadata"]
        bucket_id = bucket["id"]
        is_core = meta.get("pinned") or meta.get("protected") or meta.get("type") == "permanent"
        if is_core:
            header = f"📌 [核心准则] [bucket_id:{bucket_id}]"
        elif bucket.get("vector_match"):
            header = f"[语义关联] [bucket_id:{bucket_id}]"
        else:
            header = f"[bucket_id:{bucket_id}]"
        rendered, entry_tokens = render_stored_bucket(bucket, header)
        if token_used + entry_tokens > max_tokens:
            budget_blocked = True
            budget_blocked_count = len(matches) - i
            break
        results.append(rendered)
        token_used += entry_tokens
        touched_ids.append(bucket_id)
        if bucket.get("vector_match"):
            semantic_recall_ids.append(bucket_id)

    # 性能 P2：把 touch 移出响应路径 —— 浮现完的桶在后台一次性更新激活，
    # ripple=False 跳过读全库的时间涟漪。响应不再等这些写盘/涟漪。
    if touched_ids:
        asyncio.create_task(rt.bucket_mgr.touch_many(touched_ids, ripple=False))
    # v3 Commit B：负反馈只计 semantic 召回(vector_match)，random/rotation/
    # 关键词命中不算——这里只对纯语义命中的子集记 streak，不影响上面的
    # touch_many（那个是"最近活跃"时间戳，跟负反馈计数是两件事）。只加计数，
    # 不改排序/不影响 breath_search 本身的可达性。
    if semantic_recall_ids:
        asyncio.create_task(_record_semantic_recall_streaks(semantic_recall_ids))

    # --- 检索结果 < 3 时 40% 概率随机浮现 ---
    if not budget_blocked and len(matches) < min(3, max_results) and random.random() < 0.4:
        try:
            all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and b["metadata"].get("type") not in ("feel", "plan", "letter")
                and rt.decay_engine.calculate_score(b["metadata"]) < 2.0
            ]
            if low_weight:
                remaining_slots = max(0, max_results - len(matches))
                drifted = random.sample(
                    low_weight,
                    min(random.randint(1, 3), len(low_weight), remaining_slots),
                )
                drift_results = []
                for b in drifted:
                    rendered, entry_tokens = render_stored_bucket(
                        b,
                        f"[surface_type: random] [bucket_id:{b['id']}]",
                    )
                    if token_used + entry_tokens > max_tokens:
                        budget_blocked = True
                        break
                    drift_results.append(rendered)
                    token_used += entry_tokens
                if drift_results:
                    results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            rt.logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    if not results:
        if budget_blocked:
            notice = _budget_notice(budget_blocked_count)
            return f"{semantic_notice}\n{notice}" if semantic_notice else notice
        if rt.fire_webhook:
            await rt.fire_webhook("breath", {"mode": "empty", "matches": 0})
        empty_text = (
            f"没有匹配到「{query}」相关的记忆。\n"
            "可以换个关键词试试，或用 breath() 看当下权重池；feel 用 breath_advanced(domain=\"feel\")，信件用 letter_read。"
        )
        return f"{semantic_notice}\n{empty_text}" if semantic_notice else empty_text

    final_text = "\n---\n".join(results)
    notices = [STORED_DATA_NOTICE]
    if semantic_notice:
        notices.append(semantic_notice)
    if budget_blocked:
        notices.append(_budget_notice(budget_blocked_count))
    final_text = "\n".join(notices + [final_text])
    if rt.fire_webhook:
        await rt.fire_webhook("breath", {"mode": "ok", "matches": len(matches), "chars": len(final_text)})
    return final_text
