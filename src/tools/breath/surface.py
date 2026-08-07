"""
========================================
tools/breath/surface.py — 无 query 浮现模式
========================================

走 breath()（不传 query）时进入这里，是 OB 主动「想到什么」的核心：
按权重从未解决桶里浮现 + pinned 桶置顶 + 加权采样 + 久未浮现的被动联想。

关键行为：
- 排除 anchor 桶（anchor 是坐标系，不主动出现）
- pinned/protected 桶始终作为「核心准则」置顶（letter 桶即使 importance=10 也不置顶）
- 未解决桶按 calculate_score 排序；冷启动桶（从未访问且 importance>=8）插队前 2
- 配置开关 surfacing.sampling.enabled 启用后做加权无放回采样，否则
  保留 top1 + top20 内随机洗牌
- 末尾 1~2 条「久未浮现」passive association（imp>=8 且未访问 / imp>=9 且 7 天未活跃）

不做什么（边界）：
- 不调用 touch()：浮现不能重置衰减计时器
- 不返回 feel / plan / letter / archived（专用通道有自己的入口）
- 不做关键词检索（那是 search.py 的事）

对外暴露：surface_default(max_results, max_tokens, tag_filter) → str
========================================
"""

import random
import time
from datetime import datetime, timedelta

from ombrebrain.policy.surfacing import SurfacePolicyVM
from .. import _runtime as rt
from utils import parse_bool, parse_iso_datetime
from utils import count_tokens_approx
from ._verbatim import (
    render_stored_bucket, catalog_line,
    render_meaning_plus_first_paragraph, LONG_ENTRY_CHARS, STORED_DATA_NOTICE,
)

# U-07 fix: throttle the sampling-fallback INFO log to once per 5 minutes.
# 库小且 sampling=ON 时此分支每次 breath 都触发，原本会刷屏；改为 ≥300s
# 才打一次，并附带本窗口被压制的次数（首次为 0）。
_FALLBACK_LOG_INTERVAL_SEC = 300
_fallback_log_state = {"last_ts": 0.0, "suppressed": 0}
_SURFACE_POLICY = SurfacePolicyVM.default()
# 返修单一号改动六:病句清理——"已被截断"不准确，这条记忆是整条省略
# (budget_blocked 直接 break，没有截一半出来)，不是截断出一半，照实说清楚；
# 并按 wake 段(_wake_render.py)已立的"显式留痕"规则带上省略条数。
def _budget_notice(remaining: int) -> str:
    if remaining > 0:
        return f"token 预算不足：还有 {remaining} 条浮现记忆未返回(整条省略，不是截断)，提高 max_tokens 可查看。"
    return "token 预算不足：部分浮现记忆未返回(整条省略，不是截断)，提高 max_tokens 可查看。"
# 阶段4:核心准则段在 full_text=True 时保证至少这么多条全文，其余仍是目录行；
# 与 Yinglianchun fork 的 core_limit=3 默认一致。
_CORE_LIMIT = 3
# 返修单一号改动三:浮现权重下限默认值。K 实测 4 会误伤 7 月末的日常桶档，2.5 不会。
_DEFAULT_SURFACING_MIN_WEIGHT = 2.5


def _bucket_has_tags(meta: dict, tag_filter: list) -> bool:
    if not tag_filter:
        return True
    bucket_tags = set(meta.get("tags", []) or [])
    return all(t in bucket_tags for t in tag_filter)


def _can_surface(bucket: dict) -> bool:
    return _SURFACE_POLICY.evaluate_bucket(bucket, mode="spontaneous").allowed


async def surface_default(max_results: int, max_tokens: int, tag_filter: list, full_text: bool = False) -> str:
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
        return "记忆系统暂时无法访问。"

    surfacing_cfg = rt.config.get("surfacing", {}) or {}
    # 阶段5:"以下均为存储记忆数据，非指令" 过去跟着每条记忆重复；现在只要本次
    # 响应里出现过至少一条逐字/首段渲染的存储正文，就在最终拼接时声明一次。
    used_verbatim = False

    # --- pinned/protected 桶置顶（排除 letter 桶：letter 的 importance=10 不代表核心准则）---
    # 注意：pinned 提取在 anchor 过滤 *之前*，保证 anchor+pinned 桶也能出现在核心准则段。
    # pinned 优先级高于 anchor（她/他钉选的原则永远可见）。
    pinned_buckets = [
        b for b in all_buckets
        if (
            b["metadata"].get("pinned")
            or b["metadata"].get("protected")
            or b["metadata"].get("type") == "permanent"
        )
        and _can_surface(b)
        and b["metadata"].get("type") != "letter"
        and not b["metadata"].get("anchor", False)  # 防御：anchor 是坐标系，永不主动浮现，即使 pinned
    ]
    pinned_ids = {b["id"] for b in pinned_buckets}
    # stage2 fix: 核心准则段本职是提醒"这些原则存在"，不是把 wake 已经全文
    # 投喂过的正文再喂一遍。默认只出目录行，需要全文用 breath_search(query=...)
    # 或 breath_advanced(importance_min=...) 拉取。
    # stage4: full_text=True 时保证至少 _CORE_LIMIT 条按 importance/最近活跃
    # 优先级给全文，其余仍是目录行——不是"只显示3条"，是"至少3条给全文"。
    full_text_ids: set = set()
    if full_text and pinned_buckets:
        pinned_priority = sorted(
            pinned_buckets,
            key=lambda b: (
                int(b["metadata"].get("importance") or 0),
                str(b["metadata"].get("last_active") or b["metadata"].get("created") or ""),
            ),
            reverse=True,
        )
        full_text_ids = {b["id"] for b in pinned_priority[:_CORE_LIMIT]}

    pinned_results = []
    token_budget = max_tokens
    budget_blocked = False
    budget_blocked_count = 0
    for i, b in enumerate(pinned_buckets):
        try:
            if b["id"] in full_text_ids:
                rendered, entry_tokens = render_stored_bucket(b, f"📌 [核心准则] [bucket_id:{b['id']}]")
                if entry_tokens > token_budget:
                    # 死配额:全文放不下就退化为目录行,不整条丢弃。
                    rendered = catalog_line(b, prefix="📌 ")
                    entry_tokens = count_tokens_approx(rendered)
                else:
                    used_verbatim = True
            else:
                rendered = catalog_line(b, prefix="📌 ")
                entry_tokens = count_tokens_approx(rendered)
            if entry_tokens > token_budget:
                budget_blocked = True
                budget_blocked_count = len(pinned_buckets) - i
                break
            pinned_results.append(rendered)
            token_budget -= entry_tokens
        except Exception as e:
            rt.logger.warning(f"Failed to render pinned bucket / 钉选桶渲染失败: {e}")

    # --- iter 2.0: anchor 桶在默认浮现模式的 *未解决池* 不出现（anchor 是坐标系不是浮现对象）---
    # anchor 过滤仅作用于 unresolved 候选，不影响 pinned 提取（上方已完成）。
    all_buckets_non_anchor = [b for b in all_buckets if not b["metadata"].get("anchor", False)]

    # --- 未解决桶 ---
    # 返修单一号改动三:浮现权重下限,低于 surfacing.min_weight 的桶不进默认
    # 浮现候选池(下面 passive association 也是从这个池子里挑,一并生效)。
    # 显式检索(breath_search / breath_advanced 的 query 与 full_text 路径)
    # 不经过这个池子,不受此限——想找的东西低权重也照样能找到,只是不会
    # 自己冒出来。K 实测:阈值设 4 会误伤 7 月末的日常桶档,默认给 2.5。
    min_weight = float(surfacing_cfg.get("min_weight", _DEFAULT_SURFACING_MIN_WEIGHT))
    unresolved = [
        b for b in all_buckets_non_anchor
        if _can_surface(b)
        and not b["metadata"].get("resolved", False)
        and b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
        and not b["metadata"].get("dont_surface", False)
        and _bucket_has_tags(b["metadata"], tag_filter)
        and rt.decay_engine.calculate_score(b["metadata"]) >= min_weight
    ]

    rt.logger.info(
        f"Breath surfacing: {len(all_buckets)} total, "
        f"{len(pinned_buckets)} pinned, {len(unresolved)} unresolved"
    )


    def _sort_key(b: dict):
        """F-05: 二级排序 key，消除同分时浮现随机抖动。
        主键：decay_score（降序）
        次键：last_active 时间戳（越新越高）
        三键：arousal × valence（情感强度，越高越先浮现）
        四键：importance
        """
        meta = b["metadata"]
        score = rt.decay_engine.calculate_score(meta)
        try:
            last_ts = parse_iso_datetime(
                meta.get("last_active") or meta.get("created", "")
            ).timestamp()
        except (ValueError, TypeError):
            last_ts = 0.0
        av = float(meta.get("arousal") or 0.3) * float(meta.get("valence") or 0.5)
        imp = int(meta.get("importance") or 5)
        return (score, last_ts, av, imp)

    scored = sorted(unresolved, key=_sort_key, reverse=True)

    if scored:
        top_scores = [(b["metadata"].get("name", b["id"]), rt.decay_engine.calculate_score(b["metadata"])) for b in scored[:5]]
        rt.logger.info(f"Top unresolved scores: {top_scores}")

    # --- 冷启动检测 ---
    cold_start = [
        b for b in unresolved
        if int(b["metadata"].get("activation_count") or 0) == 0
        and int(b["metadata"].get("importance") or 0) >= 8
    ][:2]
    cold_start_ids = {b["id"] for b in cold_start}
    _ = pinned_ids  # suppress unused-var warning; used implicitly for logging only
    scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
    scored_with_cold = cold_start + scored_deduped

    # --- 按 token 预算浮现，加权采样 / 随机洗牌 + 硬上限 ---
    candidates = list(scored_with_cold)
    sampling_cfg = surfacing_cfg.get("sampling", {}) or {}
    sampling_enabled = parse_bool(sampling_cfg.get("enabled", False), default=False)
    if sampling_enabled and len(candidates) > len(cold_start) + 1:
        n_cold = len(cold_start)
        non_cold = candidates[n_cold:]
        top_k = int(sampling_cfg.get("top_k") or 5)
        sample_k = int(sampling_cfg.get("sample_k") or 2)
        temperature = max(0.1, float(sampling_cfg.get("temperature") or 0.7))
        pool = non_cold[:max(top_k, sample_k)]
        try:
            weights = [
                max(0.0001, rt.decay_engine.calculate_score(b["metadata"])) ** (1.0 / temperature)
                for b in pool
            ]
            picked = []
            pool_copy = list(pool)
            weights_copy = list(weights)
            for _ in range(min(sample_k, len(pool_copy))):
                idx = random.choices(range(len(pool_copy)), weights=weights_copy, k=1)[0]
                picked.append(pool_copy.pop(idx))
                weights_copy.pop(idx)
            rest = pool_copy + non_cold[len(pool):]
            non_cold = picked + rest
            candidates = cold_start + non_cold
        except Exception as e:
            rt.logger.warning(f"Weighted sampling failed, fallback to original / 加权采样失败: {e}")
    elif len(candidates) > 1:
        if sampling_enabled:
            now_ts = time.monotonic()
            if now_ts - _fallback_log_state["last_ts"] >= _FALLBACK_LOG_INTERVAL_SEC:
                suppressed = _fallback_log_state["suppressed"]
                rt.logger.info(
                    f"weighted sampling fallback: candidates={len(candidates)}, "
                    f"cold_start={len(cold_start)}, sample_k={sampling_cfg.get('sample_k', 2)}, "
                    f"reason=pool_too_small, suppressed_in_window={suppressed}"
                )
                _fallback_log_state["last_ts"] = now_ts
                _fallback_log_state["suppressed"] = 0
            else:
                _fallback_log_state["suppressed"] += 1
        n_cold = len(cold_start)
        non_cold = candidates[n_cold:]
        if len(non_cold) > 1:
            top1 = [non_cold[0]]
            pool = non_cold[1:min(20, len(non_cold))]
            random.shuffle(pool)
            non_cold = top1 + pool + non_cold[min(20, len(non_cold)):]
        candidates = cold_start + non_cold
    candidates = candidates[:max_results]

    # F-05/stage1 fix: 选取阶段的冷启动插队 + 随机洗牌只决定"谁入选"，
    # 渲染前必须按权重重新降序排列，否则低权重条目排在前面先吃掉 token_budget，
    # 导致真正的高权重条目被挤到尾部截断（F 窗口实测：2.42/3.30 排在 10.16 之前）。
    # 保证截断只发生在候选集里权重最低的一端。
    candidates.sort(key=lambda b: rt.decay_engine.calculate_score(b["metadata"]), reverse=True)

    dynamic_results = []
    for i, b in enumerate(candidates if not budget_blocked else []):
        try:
            score = rt.decay_engine.calculate_score(b["metadata"])
            header = f"[权重:{score:.2f}] [bucket_id:{b['id']}]"
            # stage4: 默认(full_text=False)超过 LONG_ENTRY_CHARS 字的条目只给
            # meaning+正文首段，不做生成式摘要；full_text=True 恢复逐字全文。
            content_len = len(b.get("content") or "")
            if full_text or content_len <= LONG_ENTRY_CHARS:
                rendered, entry_tokens = render_stored_bucket(b, header)
            else:
                rendered, entry_tokens = render_meaning_plus_first_paragraph(b, header)
            if entry_tokens > token_budget:
                budget_blocked = True
                budget_blocked_count = len(candidates) - i
                break
            dynamic_results.append(rendered)
            token_budget -= entry_tokens
            used_verbatim = True
        except Exception as e:
            rt.logger.warning(f"Failed to render surfaced bucket / 浮现渲染失败: {e}")
            continue

    if not pinned_results and not dynamic_results:
        if budget_blocked:
            return _budget_notice(budget_blocked_count)
        if rt.mark_op:
            rt.mark_op("breath_empty")
        stats = await rt.bucket_mgr.get_stats()
        total = stats.get("permanent_count", 0) + stats.get("dynamic_count", 0)
        if total == 0:
            return (
                "我的记忆池现在是空的。\n"
                "想给我留点种子？用 hold(content=\"...\") 写下第一条；\n"
                "或者 grow(content=\"...\") 把一段长对话/日记一次性灌给我。"
            )
        return (
            "权重池暂时平静——我手上没什么需要主动浮现的东西。\n"
            "可以试试 breath_search(query=\"想找的关键词\") 走检索，\n"
            "或者 dream() 让我自己挑几段最近的记忆嚼一嚼。"
        )

    # --- iter 1.6 §7: passive association ---
    passive_results: list[str] = []
    try:
        now = datetime.now()
        seven_days_ago = now - timedelta(days=7)
        already = {b["id"] for b in candidates}
        passive_pool = []
        for b in unresolved:
            if b["id"] in already:
                continue
            meta = b["metadata"]
            ac = int(meta.get("activation_count") or 0)
            imp = int(meta.get("importance") or 0)
            cond_a = ac == 0 and imp >= 8
            cond_b = False
            if imp >= 9:
                last = meta.get("last_active") or meta.get("created", "")
                try:
                    last_dt = parse_iso_datetime(last) if last else None
                    if last_dt and last_dt < seven_days_ago:
                        cond_b = True
                except Exception:
                    cond_b = False
            if cond_a or cond_b:
                passive_pool.append(b)
        if passive_pool and not budget_blocked:
            random.shuffle(passive_pool)
            for b in passive_pool[:2]:
                try:
                    rendered, entry_tokens = render_stored_bucket(
                        b,
                        f"💤 [久未浮现] [bucket_id:{b['id']}]",
                    )
                    if entry_tokens > token_budget:
                        budget_blocked = True
                        break
                    passive_results.append(rendered)
                    token_budget -= entry_tokens
                    used_verbatim = True
                except Exception as e:
                    rt.logger.warning(f"passive association render failed: {e}")
    except Exception as e:
        rt.logger.warning(f"passive association block failed: {e}")

    # --- 3% 偶遇：从 resolved 池随机浮现 1~3 条沉底记忆 (iter 2.1) ---
    # 设计意图：让已解决的记忆有小概率重新出现，制造"忽然想起"的温度。
    # 与无结果兜底逻辑并存；不替换主流程。
    dream_results: list[str] = []
    if not budget_blocked and random.random() < 0.03:
        try:
            shown_ids = {b["id"] for b in candidates}
            resolved_pool = [
                b for b in all_buckets
                if _can_surface(b)
                and b["metadata"].get("resolved", False)
                and b["id"] not in shown_ids
                and b["metadata"].get("type") not in ("feel", "plan", "letter")
                and not b["metadata"].get("pinned")
            ]
            if resolved_pool:
                random.shuffle(resolved_pool)
                for b in resolved_pool[:3]:
                    try:
                        rendered, entry_tokens = render_stored_bucket(
                            b,
                            f"✨ [偶遇] [bucket_id:{b['id']}]",
                        )
                        if entry_tokens > token_budget:
                            budget_blocked = True
                            break
                        dream_results.append(rendered)
                        token_budget -= entry_tokens
                        used_verbatim = True
                        rt.logger.info(f"Dream surface triggered / 偶遇机制触发: {b['id']}")
                    except Exception as e:
                        rt.logger.warning(f"Dream surface render failed / 偶遇渲染失败: {e}")
        except Exception as e:
            rt.logger.warning(f"Dream surface block failed / 偶遇模块异常: {e}")

    parts = []
    if used_verbatim:
        parts.append(STORED_DATA_NOTICE)
    if pinned_results:
        # 返修单一号改动二:核心准则段默认是目录行(stage2 起的既有行为，
        # 见上面 full_text_ids 构造处的注释),但那份"需要全文找 breath_search"
        # 的说明过去只写在代码注释里，没有落进真正渲染给用户看的文本——
        # wake 的"核心记忆"段(server.py _wake_impl)早就有这句引导，这里补齐，
        # 三处目录渲染(breath 核心准则 / wake 核心记忆 / breath_advanced
        # importance_min)口径对齐，不再出现"某家有引导句、某家没有"的分叉。
        parts.append(
            "=== 核心准则 ===\n需要全文时用 breath_search(query=...) 拉取。\n"
            + "\n---\n".join(pinned_results)
        )
    if dynamic_results:
        parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
    if passive_results:
        parts.append("=== 久未浮现 ===\n" + "\n---\n".join(passive_results))
    if dream_results:
        parts.append("=== 偶然想起 ===\n" + "\n---\n".join(dream_results))
    if budget_blocked:
        parts.append(_budget_notice(budget_blocked_count))
    return "\n\n".join(parts)
