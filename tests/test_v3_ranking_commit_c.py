"""
记忆动力学二期 · Commit C：时间语义 + 排序结构（retention + activity_bonus
双轴，band 配额浮现）。

架构裁定（F，2026-08-09）范围：双轴只接管"排序面"——band 配额浮现(4/4/2)、
breath 默认浮现排序、检索排序的 band 不可越界约束。归档阈值判定与
Dashboard 活跃度分显示继续用老 calculate_score()，本期不切。

覆盖：
- decay_engine：band_of/band_floor/apply_band_quota 纯函数；
  retention()（created 年龄基准、负反馈两段式、seed floor、cohort 中性化、
  特殊类型锁分）；activity_bonus()（last_meaningful_at 新鲜度曲线、
  importance 递减、从未设置=0）；band_ranked()（band 内归一化、band 间
  不重叠、tie-breaker）。
- bucket_manager：record_strong_signal() 推进 last_meaningful_at。
- trace_core：event_at 读写/校验/清空，且不影响 age_decay。
- tools/breath/surface.py：真实 DecayEngine 下 band 配额（4/4/2）端到端生效；
  极简 decay_engine 替身下优雅退化为旧排序（回归——不强迫所有测试替身
  实现新接口）。
- tools/breath/search.py：检索排序相邻 band 不可越界。
- tools/migrate_last_meaningful_at.py：回填 = created、跳过已有值、
  mtime 写盘纪律、导入窗口报告。
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import frontmatter as fm
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

import tools._runtime as rt
from decay_engine import DecayEngine, apply_band_quota, band_floor, band_of
from tools.trace.core import trace_core


def install_runtime(bucket_mgr, decay_eng=None):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = decay_eng or DecayEngine({}, bucket_mgr)
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


# ============================================================
# band_of / band_floor / apply_band_quota（纯函数）
# ============================================================
class TestBandHelpers:
    @pytest.mark.parametrize(
        ("importance", "expected"),
        [(10, "high"), (8, "high"), (7, "mid"), (6, "mid"), (5, "low"), (1, "low")],
    )
    def test_band_of_boundaries(self, importance, expected):
        assert band_of(importance) == expected

    def test_band_of_handles_bad_input(self):
        assert band_of(None) in ("high", "mid", "low")
        assert band_of("not-a-number") in ("high", "mid", "low")

    def test_band_floor_strictly_ordered_and_non_overlapping(self):
        low, mid, high = band_floor("low"), band_floor("mid"), band_floor("high")
        assert low < mid < high

    def test_apply_band_quota_respects_per_band_limits(self):
        buckets = (
            [{"id": f"h{i}", "_band": "high"} for i in range(6)]
            + [{"id": f"m{i}", "_band": "mid"} for i in range(6)]
            + [{"id": f"l{i}", "_band": "low"} for i in range(6)]
        )
        result = apply_band_quota(buckets, {"high": 4, "mid": 4, "low": 2})
        bands = [b["_band"] for b in result]
        assert bands.count("high") == 4
        assert bands.count("mid") == 4
        assert bands.count("low") == 2
        assert bands == ["high"] * 4 + ["mid"] * 4 + ["low"] * 2

    def test_apply_band_quota_empty_band_yields_no_representation(self):
        buckets = [{"id": "h1", "_band": "high"}]
        result = apply_band_quota(buckets, {"high": 4, "mid": 4, "low": 2})
        assert [b["id"] for b in result] == ["h1"]


# ============================================================
# DecayEngine.retention()
# ============================================================
class TestRetention:
    def test_age_decay_from_created_not_last_active(self, decay_eng):
        now = datetime.now()
        old_created_recent_active = {
            "type": "dynamic", "importance": 7,
            "created": (now - timedelta(days=200)).isoformat(),
            "last_active": now.isoformat(),
        }
        recent_created_old_active = {
            "type": "dynamic", "importance": 7,
            "created": now.isoformat(),
            "last_active": (now - timedelta(days=200)).isoformat(),
        }
        # retention 只看 created；上面两个桶 created 差 200 天，retention 应该
        # 明显不同，尽管 last_active 的新旧关系正好相反。
        r1 = decay_eng.retention(old_created_recent_active, now)
        r2 = decay_eng.retention(recent_created_old_active, now)
        assert r1 < r2

    def test_seed_returns_floor_not_age_decayed(self, decay_eng):
        now = datetime.now()
        meta = {
            "type": "dynamic", "seed": True, "importance": 9,
            "created": (now - timedelta(days=1000)).isoformat(),
        }
        assert decay_eng.retention(meta, now) == decay_eng.seed_floor

    def test_pinned_locks_to_pinned_constant(self, decay_eng):
        meta = {"type": "dynamic", "pinned": True, "importance": 3}
        assert decay_eng.retention(meta) == 999.0

    def test_negative_feedback_grace_before_streak_5(self, decay_eng):
        now = datetime.now()
        base = {"type": "dynamic", "importance": 3, "created": (now - timedelta(days=100)).isoformat()}
        no_streak = decay_eng.retention({**base, "semantic_unused_streak": 0}, now)
        under_grace = decay_eng.retention({**base, "semantic_unused_streak": 4}, now)
        # 都在宽限期内（<5），衰减率打折未取消，两者应该相等。
        assert no_streak == under_grace

    def test_negative_feedback_grace_cancelled_at_streak_5(self, decay_eng):
        now = datetime.now()
        base = {"type": "dynamic", "importance": 3, "created": (now - timedelta(days=100)).isoformat()}
        under_grace = decay_eng.retention({**base, "semantic_unused_streak": 4}, now)
        grace_cancelled = decay_eng.retention({**base, "semantic_unused_streak": 5}, now)
        # 取消宽限 → 衰减率变高（打折取消）→ 同样的年龄，分数应该更低。
        assert grace_cancelled < under_grace

    def test_negative_feedback_extra_decay_at_streak_10(self, decay_eng):
        now = datetime.now()
        base = {"type": "dynamic", "importance": 3, "created": (now - timedelta(days=100)).isoformat()}
        at_grace_cancelled = decay_eng.retention({**base, "semantic_unused_streak": 9}, now)
        extra_decay = decay_eng.retention({**base, "semantic_unused_streak": 10}, now)
        assert extra_decay < at_grace_cancelled
        # retention() 内部 round(...,4)，跟未取整的期望值比要放宽容差。
        assert extra_decay == pytest.approx(at_grace_cancelled * 0.875, abs=1e-3)

    def test_negative_feedback_does_not_apply_to_importance_6_or_above(self, decay_eng):
        now = datetime.now()
        base = {"type": "dynamic", "importance": 6, "created": (now - timedelta(days=100)).isoformat()}
        no_streak = decay_eng.retention({**base, "semantic_unused_streak": 0}, now)
        high_streak = decay_eng.retention({**base, "semantic_unused_streak": 10}, now)
        assert no_streak == high_streak

    def test_negative_feedback_does_not_apply_to_pinned_anchor_seed(self, decay_eng):
        # 这三种桶各自走各自的早返回分支，streak 高低不该有任何影响
        # （pinned/anchor 走锁分常量，seed 走 floor，都在 streak 判断之前返回）。
        now = datetime.now()
        for extra in ({"pinned": True}, {"seed": True}, {"anchor": True}):
            meta = {
                "type": "dynamic", "importance": 3,
                "created": (now - timedelta(days=100)).isoformat(),
                "semantic_unused_streak": 10,
                **extra,
            }
            meta_no_streak = {**meta, "semantic_unused_streak": 0}
            assert decay_eng.retention(meta, now) == decay_eng.retention(meta_no_streak, now)

    def test_import_cohort_neutralization_gives_uniform_age(self, test_config, fake_embedding_engine):
        window_start = "2026-07-06T02:08:37"
        window_end = "2026-07-06T02:33:32"
        cfg = dict(test_config, retention={
            "import_cohort_windows": [{"start": window_start, "end": window_end, "exempt": []}]
        })
        eng = DecayEngine(cfg, None)
        now = datetime.now()

        # 两个桶 created 时间戳在窗口内相差 20 分钟（典型批量导入的聚集特征），
        # 中性化后年龄应该按窗口结束时刻算，两者 retention 应该完全相等。
        early_in_window = {"type": "dynamic", "importance": 5, "created": "2026-07-06T02:09:00"}
        late_in_window = {"type": "dynamic", "importance": 5, "created": "2026-07-06T02:33:00"}
        assert eng.retention(early_in_window, now) == eng.retention(late_in_window, now)

    def test_import_cohort_exempt_timestamp_not_neutralized(self, test_config):
        window_start = "2026-07-06T02:08:37"
        window_end = "2026-07-06T02:33:32"
        exempt_ts = "2026-07-06T02:20:00"
        cfg = dict(test_config, retention={
            "import_cohort_windows": [{"start": window_start, "end": window_end, "exempt": [exempt_ts]}]
        })
        eng = DecayEngine(cfg, None)
        now = datetime.now()

        exempt_bucket = {"type": "dynamic", "importance": 5, "created": exempt_ts}
        neutralized_bucket = {"type": "dynamic", "importance": 5, "created": "2026-07-06T02:09:00"}
        # exempt 的桶走自己真实的 created 年龄，不该跟中性化后的另一条相等
        # （除非巧合，这里两者相差超过10分钟不会巧合相等）。
        assert eng.retention(exempt_bucket, now) != eng.retention(neutralized_bucket, now)

    def test_outside_cohort_window_uses_real_created(self, test_config):
        cfg = dict(test_config, retention={
            "import_cohort_windows": [{
                "start": "2026-07-06T02:08:37", "end": "2026-07-06T02:33:32", "exempt": [],
            }]
        })
        eng = DecayEngine(cfg, None)
        now = datetime.now()
        outside = {"type": "dynamic", "importance": 5, "created": (now - timedelta(days=5)).isoformat()}
        far_outside = {"type": "dynamic", "importance": 5, "created": (now - timedelta(days=50)).isoformat()}
        assert eng.retention(outside, now) > eng.retention(far_outside, now)


# ============================================================
# DecayEngine.activity_bonus()
# ============================================================
class TestActivityBonus:
    def test_never_set_returns_zero(self, decay_eng):
        assert decay_eng.activity_bonus({"type": "dynamic", "importance": 5}) == 0.0

    def test_recent_strong_signal_gives_positive_bonus(self, decay_eng):
        now = datetime.now()
        meta = {"type": "dynamic", "importance": 3, "last_meaningful_at": now.isoformat()}
        assert decay_eng.activity_bonus(meta, now) > 0.0

    def test_bonus_decays_toward_zero_over_time(self, decay_eng):
        now = datetime.now()
        fresh = decay_eng.activity_bonus(
            {"type": "dynamic", "importance": 3, "last_meaningful_at": now.isoformat()}, now
        )
        stale = decay_eng.activity_bonus(
            {"type": "dynamic", "importance": 3,
             "last_meaningful_at": (now - timedelta(days=30)).isoformat()}, now
        )
        assert fresh > stale >= 0.0

    def test_diminishes_at_high_importance(self, decay_eng):
        now = datetime.now()
        base = {"type": "dynamic", "last_meaningful_at": now.isoformat()}
        low_imp = decay_eng.activity_bonus({**base, "importance": 1}, now)
        high_imp = decay_eng.activity_bonus({**base, "importance": 10}, now)
        assert high_imp == 0.0, "宪法推论一：importance=10 时递减到 0"
        assert low_imp > high_imp

    def test_is_not_a_monotonic_counter(self, decay_eng):
        """强信号不构成永久累积优势——同一个 last_meaningful_at，不管"发生过
        多少次"强信号（这个信息本来就没被存下来），值应该完全由新鲜度决定，
        重复调用不应该递增。"""
        now = datetime.now()
        meta = {"type": "dynamic", "importance": 3, "last_meaningful_at": now.isoformat()}
        first = decay_eng.activity_bonus(meta, now)
        second = decay_eng.activity_bonus(meta, now)
        third = decay_eng.activity_bonus(meta, now)
        assert first == second == third


# ============================================================
# DecayEngine.band_ranked()
# ============================================================
class TestBandRanked:
    def test_bands_never_overlap_in_final_score(self, decay_eng):
        now = datetime.now()
        buckets = [
            {"id": "high1", "metadata": {"type": "dynamic", "importance": 9, "created": now.isoformat()}},
            {"id": "mid1", "metadata": {"type": "dynamic", "importance": 7, "created": now.isoformat()}},
            {"id": "low1", "metadata": {"type": "dynamic", "importance": 2, "created": now.isoformat()}},
        ]
        ranked = decay_eng.band_ranked(buckets, now)
        by_id = {b["id"]: b for b in ranked}
        assert by_id["high1"]["_rank_score"] > by_id["mid1"]["_rank_score"] > by_id["low1"]["_rank_score"]

    def test_low_retention_high_band_still_outranks_high_retention_low_band(self, decay_eng):
        """band 不可越界的核心断言：哪怕 low band 桶的原始分数远高于 high band
        （比如极新鲜、极高 activity_bonus），band 归一化后 high band 依然
        必须排在前面——这是"检索排序最终分不得越 band"这条不变量的排序面版本。"""
        now = datetime.now()
        buckets = [
            {"id": "high_but_old", "metadata": {
                "type": "dynamic", "importance": 9,
                "created": (now - timedelta(days=900)).isoformat(),
            }},
            {"id": "low_but_fresh", "metadata": {
                "type": "dynamic", "importance": 2,
                "created": now.isoformat(),
                "last_meaningful_at": now.isoformat(),
            }},
        ]
        ranked = decay_eng.band_ranked(buckets, now)
        assert [b["id"] for b in ranked] == ["high_but_old", "low_but_fresh"]

    def test_within_band_normalization_uses_full_zero_to_range(self, decay_eng):
        now = datetime.now()
        buckets = [
            {"id": "a", "metadata": {"type": "dynamic", "importance": 5, "created": now.isoformat()}},
            {"id": "b", "metadata": {"type": "dynamic", "importance": 5,
                                      "created": (now - timedelta(days=900)).isoformat()}},
        ]
        ranked = decay_eng.band_ranked(buckets, now)
        low_floor = band_floor("low")
        scores = sorted(b["_rank_score"] for b in ranked)
        assert scores[0] == pytest.approx(low_floor, abs=1e-6)  # 池内最低值归一化到 band floor

    def test_tie_breaker_is_bucket_id_not_age(self, decay_eng):
        now = datetime.now()
        # 两个桶 importance/created 完全一致 → retention/activity_bonus 相等
        # → 归一化后 _rank_score 相等 → tie-breaker 必须是 bucket_id。
        buckets = [
            {"id": "zzz", "metadata": {"type": "dynamic", "importance": 5, "created": now.isoformat()}},
            {"id": "aaa", "metadata": {"type": "dynamic", "importance": 5, "created": now.isoformat()}},
        ]
        ranked = decay_eng.band_ranked(buckets, now)
        assert [b["id"] for b in ranked] == ["zzz", "aaa"]  # 降序排列，字典序更大的在前

    def test_mutates_input_dicts_in_place(self, decay_eng):
        buckets = [{"id": "a", "metadata": {"type": "dynamic", "importance": 5}}]
        decay_eng.band_ranked(buckets)
        assert "_rank_score" in buckets[0]
        assert "_band" in buckets[0]


# ============================================================
# bucket_manager.record_strong_signal() 推进 last_meaningful_at
# ============================================================
class TestRecordStrongSignalAdvancesLastMeaningfulAt:
    @pytest.mark.asyncio
    async def test_sets_last_meaningful_at(self, bucket_mgr):
        bid = await bucket_mgr.create(content="强信号测试", importance=5, domain=["测试"])
        bucket_before = await bucket_mgr.get(bid)
        assert "last_meaningful_at" not in bucket_before["metadata"]

        await bucket_mgr.record_strong_signal(bid, kind="hold_append")

        bucket_after = await bucket_mgr.get(bid)
        assert bucket_after["metadata"].get("last_meaningful_at")

    @pytest.mark.asyncio
    async def test_advances_on_repeated_signals(self, bucket_mgr):
        bid = await bucket_mgr.create(content="重复强信号", importance=5, domain=["测试"])
        await bucket_mgr.record_strong_signal(bid, kind="hold_append")

        # 人为把值往回拨 5 天，模拟"上次强信号是很久以前"，避免同一秒内两次
        # record_strong_signal() 因为时钟精度看起来"没变化"这种假阳性。
        backdated = (datetime.now() - timedelta(days=5)).isoformat()
        fpath = bucket_mgr._find_bucket_file(bid)
        post = fm.load(fpath)
        post["last_meaningful_at"] = backdated
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(fm.dumps(post))

        await bucket_mgr.record_strong_signal(bid, kind="citation_credit")
        second = (await bucket_mgr.get(bid))["metadata"]["last_meaningful_at"]
        assert second != backdated, "强信号应该把 last_meaningful_at 推进到当下，不是停在5天前的旧值"


# ============================================================
# trace_core: event_at
# ============================================================
class TestTraceEventAt:
    @pytest.mark.asyncio
    async def test_sets_valid_event_at(self, bucket_mgr):
        bid = await bucket_mgr.create(content="有事件时间的记忆", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        result = await trace_core(bid, event_at="2026-05-01T12:00:00")

        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["event_at"] == "2026-05-01T12:00:00"
        assert "event_at" in result

    @pytest.mark.asyncio
    async def test_rejects_invalid_event_at(self, bucket_mgr):
        bid = await bucket_mgr.create(content="待测试非法输入", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        result = await trace_core(bid, event_at="不是一个日期")

        assert "不是合法" in result
        bucket = await bucket_mgr.get(bid)
        assert "event_at" not in bucket["metadata"]

    @pytest.mark.asyncio
    async def test_clear_event_at(self, bucket_mgr):
        bid = await bucket_mgr.create(content="待清空", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)
        await trace_core(bid, event_at="2026-05-01T12:00:00")

        await trace_core(bid, event_at="\\clear")

        bucket = await bucket_mgr.get(bid)
        assert "event_at" not in bucket["metadata"]

    @pytest.mark.asyncio
    async def test_event_at_does_not_affect_age_decay(self, bucket_mgr, decay_eng):
        """event_at 不进通用 age_decay——retention()/calculate_score() 都不该
        因为 event_at 是过去还是未来而改变结果。"""
        bid = await bucket_mgr.create(content="event_at 不影响衰减", importance=5, domain=["测试"])
        bucket_before = await bucket_mgr.get(bid)
        score_before = decay_eng.calculate_score(bucket_before["metadata"])
        retention_before = decay_eng.retention(bucket_before["metadata"])

        install_runtime(bucket_mgr, decay_eng)
        await trace_core(bid, event_at="2020-01-01T00:00:00")  # 很久以前

        bucket_after = await bucket_mgr.get(bid)
        assert decay_eng.calculate_score(bucket_after["metadata"]) == score_before
        assert decay_eng.retention(bucket_after["metadata"]) == retention_before


# ============================================================
# tools/breath/surface.py：band 配额端到端 + 优雅退化
# ============================================================
class TestSurfaceDefaultBandQuota:
    @pytest.mark.asyncio
    async def test_real_decay_engine_applies_4_4_2_quota(self, bucket_mgr, decay_eng):
        for i in range(6):
            await bucket_mgr.create(content=f"高档桶{i}", importance=9, domain=["测试"])
        for i in range(6):
            await bucket_mgr.create(content=f"中档桶{i}", importance=7, domain=["测试"])
        for i in range(6):
            await bucket_mgr.create(content=f"低档桶{i}", importance=3, domain=["测试"])
        install_runtime(bucket_mgr, decay_eng)

        from tools.breath.surface import surface_default
        result = await surface_default(max_results=100, max_tokens=100000, tag_filter=[])

        # 6 高 + 6 中 + 6 低，配额 4/4/2 应该总共最多 10 条动态桶浮现
        # （冷启动插队机制可能额外带 0~2 条 importance>=8 的桶，允许一定余量）。
        # 直接数 [权重: 行数作为浮现条数的近似
        weight_lines = [ln for ln in result.splitlines() if ln.startswith("[权重:")]
        assert len(weight_lines) <= 10 + 2  # 4+4+2=10，留 2 条冷启动余量

    @pytest.mark.asyncio
    async def test_mock_decay_engine_without_band_ranked_falls_back_gracefully(self, bucket_mgr):
        """极简 decay_engine 替身（只有 calculate_score，没有 band_ranked）
        应该继续用旧排序，不报错——回归 test_surface_weight_order_regression.py
        等既有测试文件的假设。"""
        class MinimalDecay:
            is_running = True

            async def ensure_started(self):
                return None

            def calculate_score(self, meta):
                return float(meta.get("importance") or 5)

        await bucket_mgr.create(content="退化路径测试", importance=7, domain=["测试"])
        install_runtime(bucket_mgr, MinimalDecay())

        from tools.breath.surface import surface_default
        result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

        assert "退化路径测试" in result or "[权重:" in result  # 没有抛异常，正常渲染


# ============================================================
# tools/breath/search.py：检索排序相邻 band 不可越界
# ============================================================
class TestSearchBandBoundary:
    def test_enforce_band_order_keeps_high_band_first(self):
        from tools.breath.search import _enforce_band_order

        matches = [
            {"id": "low_relevant", "metadata": {"importance": 2}},
            {"id": "high_less_relevant", "metadata": {"importance": 9}},
            {"id": "mid_mid", "metadata": {"importance": 6}},
        ]
        ordered = _enforce_band_order(matches)
        assert [b["id"] for b in ordered] == ["high_less_relevant", "mid_mid", "low_relevant"]

    def test_enforce_band_order_is_stable_within_band(self):
        from tools.breath.search import _enforce_band_order

        matches = [
            {"id": "high_a", "metadata": {"importance": 8}},
            {"id": "high_b", "metadata": {"importance": 10}},
            {"id": "low_a", "metadata": {"importance": 1}},
        ]
        ordered = _enforce_band_order(matches)
        # 稳定排序：high 段内部 high_a/high_b 的相对顺序（search() 给出的
        # 相关度顺序）必须原样保留，不因为 importance 10>8 就交换。
        assert [b["id"] for b in ordered if b["id"].startswith("high")] == ["high_a", "high_b"]


# ============================================================
# tools/migrate_last_meaningful_at.py
# ============================================================
class TestMigrateLastMeaningfulAt:
    def _write_bucket(self, root, subdir, filename, frontmatter_lines, body="正文"):
        d = os.path.join(root, subdir)
        os.makedirs(d, exist_ok=True)
        text = "---\n" + "\n".join(frontmatter_lines) + "\n---\n" + body
        path = os.path.join(d, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_dry_run_does_not_write(self, tmp_path):
        from migrate_last_meaningful_at import run

        path = self._write_bucket(
            str(tmp_path), "dynamic", "b1.md",
            ["id: b1", "created: 2026-01-01T00:00:00", "importance: 5"],
        )
        before_mtime = os.path.getmtime(path)

        stats = run(str(tmp_path), apply=False)

        assert os.path.getmtime(path) == before_mtime
        assert stats["filled"] == ["b1"]

    def test_apply_backfills_missing_field_to_created(self, tmp_path):
        from migrate_last_meaningful_at import run
        import frontmatter as fm2

        path = self._write_bucket(
            str(tmp_path), "dynamic", "b1.md",
            ["id: b1", "created: 2026-01-01T00:00:00", "importance: 5"],
        )

        stats = run(str(tmp_path), apply=True)

        assert stats["filled"] == ["b1"]
        post = fm2.load(path)
        # frontmatter 把 ISO 格式值自动解析成 datetime（跟 created 本身的
        # 往返行为一致），不是字符串——两边都转字符串比较。
        assert str(post["last_meaningful_at"]) == str(post["created"])

    def test_apply_skips_bucket_that_already_has_last_meaningful_at(self, tmp_path):
        from migrate_last_meaningful_at import run

        path = self._write_bucket(
            str(tmp_path), "dynamic", "b1.md",
            ["id: b1", "created: 2026-01-01T00:00:00",
             "last_meaningful_at: 2026-06-01T00:00:00", "importance: 5"],
        )
        before_mtime = os.path.getmtime(path)

        stats = run(str(tmp_path), apply=True)

        assert stats["filled"] == []
        assert stats["skipped_already_set"] == 1
        assert os.path.getmtime(path) == before_mtime, "已有值的桶不该被重写，mtime 纪律"

    def test_apply_is_idempotent_second_run_touches_nothing(self, tmp_path):
        from migrate_last_meaningful_at import run

        path = self._write_bucket(
            str(tmp_path), "dynamic", "b1.md",
            ["id: b1", "created: 2026-01-01T00:00:00", "importance: 5"],
        )
        run(str(tmp_path), apply=True)
        after_first_mtime = os.path.getmtime(path)

        stats_second = run(str(tmp_path), apply=True)

        assert stats_second["filled"] == []
        assert stats_second["skipped_already_set"] == 1
        assert os.path.getmtime(path) == after_first_mtime

    def test_bucket_without_created_is_reported_not_crashed(self, tmp_path):
        from migrate_last_meaningful_at import run

        self._write_bucket(str(tmp_path), "dynamic", "b1.md", ["id: b1", "importance: 5"])

        stats = run(str(tmp_path), apply=False)

        assert stats["filled"] == []
        assert stats["skipped_no_created"] == ["b1"]

    def test_import_window_reporting_does_not_change_fill_logic(self, tmp_path):
        from migrate_last_meaningful_at import run

        self._write_bucket(
            str(tmp_path), "dynamic", "in_window.md",
            ["id: in_window", "created: 2026-07-06T02:15:00", "importance: 5"],
        )
        self._write_bucket(
            str(tmp_path), "dynamic", "outside.md",
            ["id: outside", "created: 2026-01-01T00:00:00", "importance: 5"],
        )

        stats = run(
            str(tmp_path), apply=False,
            import_window=("2026-07-06T02:08:37", "2026-07-06T02:33:32"),
        )

        assert set(stats["filled"]) == {"in_window", "outside"}  # 两条都照常回填
        assert stats["filled_in_window"] == ["in_window"]  # 只是报告里额外标注

    def test_scans_permanent_dynamic_and_archive(self, tmp_path):
        from migrate_last_meaningful_at import run

        for sub, bid in (("permanent", "p1"), ("dynamic", "d1"), ("archive", "a1")):
            self._write_bucket(
                str(tmp_path), sub, f"{bid}.md",
                [f"id: {bid}", "created: 2026-01-01T00:00:00", "importance: 5"],
            )

        stats = run(str(tmp_path), apply=False)

        assert set(stats["filled"]) == {"p1", "d1", "a1"}
