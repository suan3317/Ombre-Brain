"""
记忆动力学二期 · Commit B：信号体系。

覆盖范围（施工单 Commit B + 设计定稿"信号体系"）：
- bucket_manager：citation_event/citation_credit/provenance edge 记账、
  强信号清零 streak、反向匹配自动层（used_inferred）+ 模糊语义人工队列、
  负反馈 streak 记账（作用域排除）。
- tools/_common.py：resolve_citations 解析/去重、反向匹配挂在
  merge_or_create 里的分档记账。
- tools/hold、tools/trace：cited 参数、强信号触发点（hold 追加 /
  trace.meaning_append）、控制面操作零信号。
- tools/breath/search.py：语义命中记 streak，random/rotation 不计。

出厂验收六回归里本 commit 自包含、可独立验证的三条：
  a. 控制面零 credit
  b. random/rotation 不积 streak
  c. 滚动 48h 内十次 event → 一次 credit + 一条幂等 relation(event_count=10)
其余（d/e/f/g/h）依赖 Commit C/D 的字段（retention/activity_bonus/派生
校准），留给 Commit E 的验收测试补全。
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from bucket_manager import derive_provenance_edges
from tools._common import resolve_citations
from tools.hold.core import store_core
from tools.trace.core import trace_core


def install_runtime(bucket_mgr):
    from decay_engine import DecayEngine

    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.decay_engine = DecayEngine({}, bucket_mgr)

    class _EchoDehydrator:
        async def analyze(self, content):
            return {"domain": ["测试"], "valence": 0.5, "arousal": 0.3, "tags": [], "suggested_name": ""}

        async def merge(self, old, new):
            return f"{old}\n{new}"

        def invalidate_cache(self, content):
            return None

    rt.dehydrator = _EchoDehydrator()


# ============================================================
# derive_provenance_edges: 纯函数聚合
# ============================================================
class TestDeriveProvenanceEdges:
    def test_empty_input(self):
        assert derive_provenance_edges([]) == []

    def test_single_event_creates_one_edge(self):
        events = [{"payload": {"source": "s1", "target": "t1", "location": "hold", "at": "2026-08-09T00:00:00"}}]
        edges = derive_provenance_edges(events)
        assert len(edges) == 1
        assert edges[0] == {
            "source": "s1", "target": "t1", "location": "hold",
            "first_seen": "2026-08-09T00:00:00", "last_seen": "2026-08-09T00:00:00",
            "event_count": 1,
        }

    def test_repeated_same_key_aggregates_idempotently(self):
        events = [
            {"payload": {"source": "s1", "target": "t1", "location": "hold", "at": f"2026-08-{d:02d}T00:00:00"}}
            for d in range(1, 11)
        ]
        edges = derive_provenance_edges(events)
        assert len(edges) == 1
        assert edges[0]["event_count"] == 10
        assert edges[0]["first_seen"] == "2026-08-01T00:00:00"
        assert edges[0]["last_seen"] == "2026-08-10T00:00:00"

    def test_different_keys_produce_separate_edges(self):
        events = [
            {"payload": {"source": "s1", "target": "t1", "location": "hold", "at": "2026-08-01T00:00:00"}},
            {"payload": {"source": "s2", "target": "t1", "location": "hold", "at": "2026-08-01T00:00:00"}},
            {"payload": {"source": "s1", "target": "t2", "location": "hold", "at": "2026-08-01T00:00:00"}},
            {"payload": {"source": "s1", "target": "t1", "location": "trace", "at": "2026-08-01T00:00:00"}},
        ]
        edges = derive_provenance_edges(events)
        assert len(edges) == 4

    def test_out_of_order_events_still_compute_correct_bounds(self):
        events = [
            {"payload": {"source": "s1", "target": "t1", "location": "", "at": "2026-08-05T00:00:00"}},
            {"payload": {"source": "s1", "target": "t1", "location": "", "at": "2026-08-01T00:00:00"}},
            {"payload": {"source": "s1", "target": "t1", "location": "", "at": "2026-08-09T00:00:00"}},
        ]
        edges = derive_provenance_edges(events)
        assert edges[0]["first_seen"] == "2026-08-01T00:00:00"
        assert edges[0]["last_seen"] == "2026-08-09T00:00:00"
        assert edges[0]["event_count"] == 3


# ============================================================
# bucket_manager: record_citation / 48h 滚动窗口 / provenance
# ============================================================
class TestBucketManagerCitation:
    @pytest.mark.asyncio
    async def test_record_citation_first_time_credits(self, bucket_mgr):
        target = await bucket_mgr.create(content="被引用的记忆", importance=5, domain=["测试"])
        result = await bucket_mgr.record_citation(target, source="hold:abc", location="hold")
        assert result == {"ok": True, "target": target, "credited": True}

    @pytest.mark.asyncio
    async def test_record_citation_missing_bucket_fails_gracefully(self, bucket_mgr):
        result = await bucket_mgr.record_citation("does_not_exist", source="hold:abc")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_citation_event_always_appended_even_when_not_credited(self, bucket_mgr):
        target = await bucket_mgr.create(content="被反复引用", importance=5, domain=["测试"])
        await bucket_mgr.record_citation(target, source="hold:1", location="hold")
        await bucket_mgr.record_citation(target, source="hold:2", location="hold")
        events = await bucket_mgr.list_citation_events(target)
        assert len(events) == 2  # citation_event 永不封顶，即便第二次没触发 credit

    @pytest.mark.asyncio
    async def test_acceptance_c_rolling_48h_ten_events_one_credit_idempotent_edge(self, bucket_mgr):
        target = await bucket_mgr.create(content="十次引用的记忆", importance=5, domain=["测试"])
        now = datetime.now()
        for i in range(10):
            at = now - timedelta(hours=i * 4)  # 全部落在 48h 窗口内（0h~36h 前）
            payload = {"source": "trace:x", "target": target, "location": "trace"}
            bucket_mgr.citation_ledger.append_event(
                event_type="CitationEvent", trace_id=target, trace_kind="citation",
                payload={**payload, "at": at.isoformat()},
            )
        credited = not bucket_mgr._citation_credited_within_window(target, now)
        assert credited is True  # 此刻还没真正记过 credit（上面手工插的是 CitationEvent，不是 CitationCredit）

        # 走真实入口再触发一次，此时窗口内已有 10 条 CitationEvent，
        # 应该只产生 1 条 CitationCredit + 1 条幂等 provenance edge(event_count=10)。
        result = await bucket_mgr.record_citation(target, source="trace:x", location="trace")
        assert result["credited"] is True

        credit_events = [
            e for e in bucket_mgr.citation_ledger.iter_events()
            if e.get("event_type") == "CitationCredit" and e.get("trace_id") == target
        ]
        assert len(credit_events) == 1

        edges = await bucket_mgr.provenance_edges(target)
        matching = [e for e in edges if e["source"] == "trace:x" and e["location"] == "trace"]
        assert len(matching) == 1
        assert matching[0]["event_count"] == 11  # 10 条手工插入 + 1 条 record_citation 自己追加的

    @pytest.mark.asyncio
    async def test_citation_credit_not_repeated_within_48h(self, bucket_mgr):
        target = await bucket_mgr.create(content="48小时内多次引用", importance=5, domain=["测试"])
        first = await bucket_mgr.record_citation(target, source="hold:1", location="hold")
        second = await bucket_mgr.record_citation(target, source="hold:1", location="hold")
        assert first["credited"] is True
        assert second["credited"] is False

    @pytest.mark.asyncio
    async def test_citation_credit_fires_again_after_48h(self, bucket_mgr):
        target = await bucket_mgr.create(content="48小时后再次引用", importance=5, domain=["测试"])
        old_at = (datetime.now() - timedelta(hours=49)).isoformat()
        bucket_mgr.citation_ledger.append_event(
            event_type="CitationCredit", trace_id=target, trace_kind="citation",
            payload={"source": "hold:1", "target": target, "location": "hold", "at": old_at},
        )
        result = await bucket_mgr.record_citation(target, source="hold:1", location="hold")
        assert result["credited"] is True

    @pytest.mark.asyncio
    async def test_record_citation_credit_resets_semantic_unused_streak(self, bucket_mgr):
        target = await bucket_mgr.create(content="有 streak 的记忆", importance=3, domain=["测试"])
        await bucket_mgr.record_semantic_recall_without_use(target)
        await bucket_mgr.record_semantic_recall_without_use(target)
        bucket = await bucket_mgr.get(target)
        assert bucket["metadata"]["semantic_unused_streak"] == 2

        await bucket_mgr.record_citation(target, source="hold:1", location="hold")

        bucket = await bucket_mgr.get(target)
        assert bucket["metadata"]["semantic_unused_streak"] == 0


# ============================================================
# bucket_manager: record_strong_signal / mark_used_inferred /
# 模糊语义人工队列
# ============================================================
class TestStrongSignalAndReverseMatch:
    @pytest.mark.asyncio
    async def test_record_strong_signal_resets_streak_and_logs_event(self, bucket_mgr):
        bid = await bucket_mgr.create(content="强信号目标", importance=3, domain=["测试"])
        await bucket_mgr.record_semantic_recall_without_use(bid)
        assert (await bucket_mgr.get(bid))["metadata"]["semantic_unused_streak"] == 1

        await bucket_mgr.record_strong_signal(bid, kind="hold_append")

        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["semantic_unused_streak"] == 0
        events = [e for e in bucket_mgr.citation_ledger.iter_events() if e.get("event_type") == "StrongSignal"]
        assert len(events) == 1
        assert events[0]["payload"]["kind"] == "hold_append"

    @pytest.mark.asyncio
    async def test_mark_used_inferred_sets_flag_and_resets_streak_without_strong_signal_event(self, bucket_mgr):
        bid = await bucket_mgr.create(content="近逐字候选", importance=3, domain=["测试"])
        await bucket_mgr.record_semantic_recall_without_use(bid)

        ok = await bucket_mgr.mark_used_inferred(bid)

        assert ok is True
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["used_inferred"] is True
        assert bucket["metadata"]["semantic_unused_streak"] == 0
        # used_inferred 与强信号是两条独立清零路径（设计原话并列陈述），不应该
        # 借用 record_strong_signal，所以不该产生 StrongSignal 事件。
        strong_events = [e for e in bucket_mgr.citation_ledger.iter_events() if e.get("event_type") == "StrongSignal"]
        assert strong_events == []

    @pytest.mark.asyncio
    async def test_queue_and_list_fuzzy_review_candidate(self, bucket_mgr):
        bid = await bucket_mgr.create(content="模糊候选", importance=3, domain=["测试"])
        await bucket_mgr.queue_fuzzy_review_candidate(bid, score=45.0, preview="部分相关的新内容")

        queue = await bucket_mgr.list_fuzzy_review_queue()

        assert len(queue) == 1
        assert queue[0]["trace_id"] == bid
        assert queue[0]["payload"]["score"] == 45.0
        # 模糊候选只进队列，不碰桶本身任何字段。
        bucket = await bucket_mgr.get(bid)
        assert "used_inferred" not in bucket["metadata"]


# ============================================================
# 负反馈 streak 记账：作用域排除
# ============================================================
class TestNegativeFeedbackScope:
    @pytest.mark.asyncio
    async def test_low_importance_normal_bucket_gets_streak(self, bucket_mgr):
        bid = await bucket_mgr.create(content="低重要度普通桶", importance=3, domain=["测试"])
        await bucket_mgr.record_semantic_recall_without_use(bid)
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["semantic_unused_streak"] == 1

    @pytest.mark.asyncio
    async def test_high_importance_excluded(self, bucket_mgr):
        bid = await bucket_mgr.create(content="高重要度", importance=6, domain=["测试"])
        await bucket_mgr.record_semantic_recall_without_use(bid)
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"].get("semantic_unused_streak") is None

    @pytest.mark.asyncio
    async def test_pinned_excluded(self, bucket_mgr):
        bid = await bucket_mgr.create(content="pinned", importance=10, pinned=True, domain=["测试"])
        await bucket_mgr.record_semantic_recall_without_use(bid)
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"].get("semantic_unused_streak") is None

    @pytest.mark.asyncio
    async def test_anchor_excluded(self, bucket_mgr):
        bid = await bucket_mgr.create(content="anchor 桶", importance=3, domain=["测试"])
        await bucket_mgr.set_anchor(bid, True)
        await bucket_mgr.record_semantic_recall_without_use(bid)
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"].get("semantic_unused_streak") is None

    @pytest.mark.asyncio
    async def test_seed_excluded(self, bucket_mgr):
        bid = await bucket_mgr.create(content="seed 桶", importance=3, domain=["测试"])
        await bucket_mgr.set_seed(bid, True)
        await bucket_mgr.record_semantic_recall_without_use(bid)
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"].get("semantic_unused_streak") is None

    @pytest.mark.asyncio
    async def test_streak_accumulates_across_multiple_recalls(self, bucket_mgr):
        bid = await bucket_mgr.create(content="连续未使用", importance=3, domain=["测试"])
        for _ in range(3):
            await bucket_mgr.record_semantic_recall_without_use(bid)
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["semantic_unused_streak"] == 3


# ============================================================
# resolve_citations（_common.py）
# ============================================================
class TestResolveCitations:
    @pytest.mark.asyncio
    async def test_empty_cited_returns_empty(self, bucket_mgr):
        install_runtime(bucket_mgr)
        assert await resolve_citations("", source="x") == []
        assert await resolve_citations(None, source="x") == []

    @pytest.mark.asyncio
    async def test_dedupes_within_same_call(self, bucket_mgr):
        target = await bucket_mgr.create(content="重复引用同一目标", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        recorded = await resolve_citations(f"{target},{target},{target}", source="hold:x")

        assert recorded == [target]
        events = await bucket_mgr.list_citation_events(target)
        assert len(events) == 1  # 同轮去重：只记一条 citation_event，不是三条

    @pytest.mark.asyncio
    async def test_nonexistent_bucket_id_skipped_silently(self, bucket_mgr):
        install_runtime(bucket_mgr)
        recorded = await resolve_citations("does_not_exist_123", source="hold:x")
        assert recorded == []

    @pytest.mark.asyncio
    async def test_accepts_list_input(self, bucket_mgr):
        target = await bucket_mgr.create(content="list 形式输入", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)
        recorded = await resolve_citations([target, target], source="grow_batch:x")
        assert recorded == [target]


# ============================================================
# hold 强信号：hold 追加算数，grow 的 LLM 压缩合并不算
# ============================================================
class TestHoldStrongSignal:
    @pytest.mark.asyncio
    async def test_hold_append_merge_triggers_strong_signal(self, bucket_mgr):
        import asyncio
        install_runtime(bucket_mgr)
        first = await store_core(
            content="今天写了一段测试相关的话，这是原始正文用来触发合并判定。",
            extra_tags=[], importance=5, valence=0.5, arousal=0.3, why_remembered="",
        )
        bucket_id = first.split("→")[1].split(" ")[0]
        await bucket_mgr.record_semantic_recall_without_use(bucket_id)
        assert (await bucket_mgr.get(bucket_id))["metadata"]["semantic_unused_streak"] == 1

        # 同样内容原文再 hold 一次，大概率判定为合并（exact content match）。
        result = await store_core(
            content="今天写了一段测试相关的话，这是原始正文用来触发合并判定。",
            extra_tags=[], importance=5, valence=0.5, arousal=0.3, why_remembered="",
        )
        assert result.startswith("合并→")
        await asyncio.sleep(0)  # 强信号记录走 asyncio.create_task 后台执行，让它落盘

        bucket = await bucket_mgr.get(bucket_id)
        assert bucket["metadata"]["semantic_unused_streak"] == 0
        strong_events = [
            e for e in bucket_mgr.citation_ledger.iter_events()
            if e.get("event_type") == "StrongSignal" and e.get("trace_id") == bucket_id
        ]
        assert len(strong_events) == 1
        assert strong_events[0]["payload"]["kind"] == "hold_append"

    @pytest.mark.asyncio
    async def test_grow_shortpath_raw_merge_does_not_trigger_strong_signal(self, bucket_mgr):
        """grow_shortpath 也用 raw_merge=True（逐字追加），但 source_tool="grow"
        不是 "hold"——设计定稿"字段白名单"按工具（hold）而不是按合并机制（是否
        逐字）区分，grow 的任何合并都不该算强信号，即使内容保真度跟 hold 一样。
        """
        import asyncio
        from tools.grow.shortpath import grow_shortpath

        install_runtime(bucket_mgr)
        content = "今天写了一段用于验证 grow 快速路径的测试正文，足够长以避免误判。"
        first = await grow_shortpath(content)
        bucket_id = first.split("→ ")[1].split(" ")[0].strip()
        await bucket_mgr.record_semantic_recall_without_use(bucket_id)

        result = await grow_shortpath(content)
        await asyncio.sleep(0)

        assert "合并" in result
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket["metadata"]["semantic_unused_streak"] == 1  # 没被清零
        strong_events = [
            e for e in bucket_mgr.citation_ledger.iter_events()
            if e.get("event_type") == "StrongSignal" and e.get("trace_id") == bucket_id
        ]
        assert strong_events == []

    @pytest.mark.asyncio
    async def test_hold_cited_records_citation(self, bucket_mgr):
        target = await bucket_mgr.create(content="被 hold 引用的旧记忆", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        result = await store_core(
            content="全新内容，参考了之前那条记忆",
            extra_tags=[], importance=5, valence=0.5, arousal=0.3, why_remembered="",
            cited=target,
        )
        assert result.startswith("新建→")

        # cited 走 asyncio.create_task 后台记账，给事件循环一个 tick 落盘。
        import asyncio
        await asyncio.sleep(0)
        events = await bucket_mgr.list_citation_events(target)
        assert len(events) == 1
        assert events[0]["payload"]["location"] == "hold"


# ============================================================
# trace 强信号 / 控制面零信号 / cited
# ============================================================
class TestTraceStrongSignalAndControlPlane:
    @pytest.mark.asyncio
    async def test_meaning_append_triggers_strong_signal(self, bucket_mgr):
        bid = await bucket_mgr.create(content="待追加 meaning", importance=3, domain=["测试"])
        await bucket_mgr.record_semantic_recall_without_use(bid)
        install_runtime(bucket_mgr)

        await trace_core(bid, meaning_append="这条记忆后来真的派上用场了")

        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["semantic_unused_streak"] == 0
        strong_events = [
            e for e in bucket_mgr.citation_ledger.iter_events()
            if e.get("event_type") == "StrongSignal" and e.get("trace_id") == bid
        ]
        assert len(strong_events) == 1
        assert strong_events[0]["payload"]["kind"] == "meaning_append"

    # ---- 出厂验收回归 a：控制面零 credit ----
    @pytest.mark.asyncio
    async def test_acceptance_a_control_plane_ops_produce_zero_signal(self, bucket_mgr):
        bid = await bucket_mgr.create(content="只做控制面操作", importance=3, domain=["测试"])
        await bucket_mgr.record_semantic_recall_without_use(bid)
        install_runtime(bucket_mgr)

        for kwargs in (
            {"resolved": 1}, {"pinned": 0}, {"digested": 1}, {"dont_surface": 1},
            {"status": "active"}, {"weight": 0.5}, {"name": "改个名字"},
            {"domain": "工作"}, {"tags": "a,b"}, {"importance": 7},
        ):
            await trace_core(bid, **kwargs)

        bucket = await bucket_mgr.get(bid)
        # 一系列纯控制面操作后，streak 应该原封不动——一次都没被清零过，
        # 因为它们本来就不该触发 record_strong_signal。
        assert bucket["metadata"]["semantic_unused_streak"] == 1
        strong_events = [
            e for e in bucket_mgr.citation_ledger.iter_events()
            if e.get("event_type") in ("StrongSignal", "CitationCredit") and e.get("trace_id") == bid
        ]
        assert strong_events == []

    @pytest.mark.asyncio
    async def test_trace_cited_alone_records_citation_without_no_op_message(self, bucket_mgr):
        target = await bucket_mgr.create(content="被 trace 单独引用", importance=5, domain=["测试"])
        bid = await bucket_mgr.create(content="正在编辑的桶", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        result = await trace_core(bid, cited=target)

        assert "没有任何字段需要修改" not in result
        assert "已记引用" in result
        events = await bucket_mgr.list_citation_events(target)
        assert len(events) == 1
        assert events[0]["payload"]["source"] == bid
        assert events[0]["payload"]["location"] == "trace"


# ============================================================
# 反向匹配：merge_or_create 的近逐字 / 模糊语义分档
# ============================================================
class TestReverseMatchBands:
    @pytest.mark.asyncio
    async def test_near_verbatim_marks_used_inferred(self, bucket_mgr, monkeypatch):
        existing_id = await bucket_mgr.create(content="老内容", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        async def fake_search(*args, **kwargs):
            return [{"id": existing_id, "score": 65.0, "metadata": {}, "content": "老内容"}]

        monkeypatch.setattr(bucket_mgr, "search", fake_search)
        monkeypatch.setattr(bucket_mgr, "find_exact_content", lambda *a, **k: None, raising=False)

        from tools._common import merge_or_create
        # merge_threshold 默认 75，65 < 75 不会合并；65 >= near_verbatim_min(60)
        # 应该走"近逐字"分支。
        await merge_or_create(
            content="新内容，跟老内容不完全一样", tags=[], importance=5, domain=["测试"],
            valence=0.5, arousal=0.3, source_tool="hold",
        )

        bucket = await bucket_mgr.get(existing_id)
        assert bucket["metadata"]["used_inferred"] is True

    @pytest.mark.asyncio
    async def test_fuzzy_range_queues_for_review_without_touching_bucket(self, bucket_mgr, monkeypatch):
        existing_id = await bucket_mgr.create(content="老内容2", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        async def fake_search(*args, **kwargs):
            return [{"id": existing_id, "score": 45.0, "metadata": {}, "content": "老内容2"}]

        monkeypatch.setattr(bucket_mgr, "search", fake_search)
        monkeypatch.setattr(bucket_mgr, "find_exact_content", lambda *a, **k: None, raising=False)

        from tools._common import merge_or_create
        await merge_or_create(
            content="有点相关但不算近逐字的新内容", tags=[], importance=5, domain=["测试"],
            valence=0.5, arousal=0.3, source_tool="hold",
        )

        bucket = await bucket_mgr.get(existing_id)
        assert "used_inferred" not in bucket["metadata"]
        queue = await bucket_mgr.list_fuzzy_review_queue()
        assert any(q["trace_id"] == existing_id for q in queue)

    @pytest.mark.asyncio
    async def test_below_fuzzy_review_min_does_nothing(self, bucket_mgr, monkeypatch):
        existing_id = await bucket_mgr.create(content="老内容3", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        async def fake_search(*args, **kwargs):
            return [{"id": existing_id, "score": 10.0, "metadata": {}, "content": "老内容3"}]

        monkeypatch.setattr(bucket_mgr, "search", fake_search)
        monkeypatch.setattr(bucket_mgr, "find_exact_content", lambda *a, **k: None, raising=False)

        from tools._common import merge_or_create
        await merge_or_create(
            content="完全不相关的新内容", tags=[], importance=5, domain=["测试"],
            valence=0.5, arousal=0.3, source_tool="hold",
        )

        bucket = await bucket_mgr.get(existing_id)
        assert "used_inferred" not in bucket["metadata"]
        queue = await bucket_mgr.list_fuzzy_review_queue()
        assert not any(q["trace_id"] == existing_id for q in queue)


# ============================================================
# breath/search.py：语义命中记 streak，出厂验收回归 b
# ============================================================
class TestBreathSearchStreak:
    @pytest.mark.asyncio
    async def test_semantic_match_increments_streak(self, bucket_mgr):
        bid = await bucket_mgr.create(content="纯语义命中的记忆", importance=3, domain=["测试"])
        install_runtime(bucket_mgr)
        rt.embedding_engine = None  # 无 embedding，走关键词/BM25——用手工构造场景更可控

        # 直接调内部记账函数，绕开真实向量检索基础设施（集成测试职责在
        # search() 本身的测试文件里，这里只验证"vector_match=True 才计数"
        # 这条 Commit B 新增的规则本身）。
        from tools.breath.search import _record_semantic_recall_streaks
        await _record_semantic_recall_streaks([bid])

        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["semantic_unused_streak"] == 1

    # ---- 出厂验收回归 b：random/rotation 不积 streak ----
    @pytest.mark.asyncio
    async def test_random_surfacing_does_not_touch_streak(self, bucket_mgr):
        from tools.breath.surface import surface_default

        bid = await bucket_mgr.create(content="纯随机浮现候选", importance=3, domain=["测试"])
        install_runtime(bucket_mgr)

        await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

        bucket = await bucket_mgr.get(bid)
        # surface_default()（含其 top1+shuffle、加权采样、冷启动插队、passive
        # association、3% 偶遇等全部 random/rotation 子路径）完全不接触
        # semantic_unused_streak——本 commit 只在 breath/search.py 的语义
        # 命中路径记账。
        assert bucket["metadata"].get("semantic_unused_streak") is None
