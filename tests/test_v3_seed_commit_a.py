"""
记忆动力学二期 · Commit A：seed 字段与继承入口。

覆盖范围（施工单 Commit A + 设计定稿推论四）：
- bucket_manager：set_seed/count_seeds/list_seeds/上限校验/update() 透传。
- decay_engine：skip-list 排除 seed（不归档、不自动结案）+ calculate_score()
  的 floor+递减 activity bonus 打分；出厂验收 4a/4b 两式一票否决。
- trace_core：seed=0/1 读写、上限拒绝给人话错误。
- tools/breath/surface.py：seed 从"浮现配额"候选池排除，但不影响 pinned
  核心准则段。
- tools/_wake_seed.py：select_seed_buckets 纯函数，不受 importance 阈值限制。
- server._wake_impl：继承区段落 + 与核心记忆/最近连续性段去重。
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import frontmatter as fm
import pytest

import tools._runtime as rt
from tools._wake_seed import select_seed_buckets
from tools.trace.core import trace_core


def install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


# ============================================================
# bucket_manager: set_seed / count_seeds / list_seeds / 上限
# ============================================================
class TestBucketManagerSeed:
    @pytest.mark.asyncio
    async def test_seed_count_starts_zero(self, bucket_mgr):
        assert await bucket_mgr.count_seeds() == 0

    @pytest.mark.asyncio
    async def test_set_seed_increments_count(self, bucket_mgr):
        bid = await bucket_mgr.create(content="种子候选", importance=6, domain=["测试"])
        result = await bucket_mgr.set_seed(bid, True)
        assert result["ok"] is True
        assert result["seed"] is True
        assert await bucket_mgr.count_seeds() == 1
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["seed"] is True

    @pytest.mark.asyncio
    async def test_seed_limit_defaults_to_30(self, bucket_mgr):
        assert bucket_mgr.SEED_LIMIT == 30

    @pytest.mark.asyncio
    async def test_seed_limit_configurable_via_config(self, test_config, fake_embedding_engine):
        from bucket_manager import BucketManager
        mgr = BucketManager(dict(test_config, seed={"max_count": 2}), embedding_engine=fake_embedding_engine)
        assert mgr.SEED_LIMIT == 2

    @pytest.mark.asyncio
    async def test_set_seed_rejects_at_cap(self, test_config, fake_embedding_engine):
        from bucket_manager import BucketManager
        mgr = BucketManager(dict(test_config, seed={"max_count": 1}), embedding_engine=fake_embedding_engine)
        b1 = await mgr.create(content="第一个种子", importance=6, domain=["测试"])
        b2 = await mgr.create(content="第二个种子", importance=6, domain=["测试"])
        ok1 = await mgr.set_seed(b1, True)
        assert ok1["ok"] is True
        ok2 = await mgr.set_seed(b2, True)
        assert ok2["ok"] is False
        assert "上限" in ok2["error"]
        assert await mgr.count_seeds() == 1

    @pytest.mark.asyncio
    async def test_set_seed_idempotent_noop(self, bucket_mgr):
        bid = await bucket_mgr.create(content="种子", importance=6, domain=["测试"])
        await bucket_mgr.set_seed(bid, True)
        result = await bucket_mgr.set_seed(bid, True)
        assert result["ok"] is True
        assert result.get("noop") is True
        assert await bucket_mgr.count_seeds() == 1

    @pytest.mark.asyncio
    async def test_set_seed_release_then_reacquire_frees_cap_slot(self, test_config, fake_embedding_engine):
        from bucket_manager import BucketManager
        mgr = BucketManager(dict(test_config, seed={"max_count": 1}), embedding_engine=fake_embedding_engine)
        b1 = await mgr.create(content="第一个种子", importance=6, domain=["测试"])
        b2 = await mgr.create(content="第二个种子", importance=6, domain=["测试"])
        await mgr.set_seed(b1, True)
        await mgr.set_seed(b1, False)
        result = await mgr.set_seed(b2, True)
        assert result["ok"] is True
        assert await mgr.count_seeds() == 1

    @pytest.mark.asyncio
    async def test_seed_not_mutually_exclusive_with_pinned_or_anchor(self, bucket_mgr):
        pinned_id = await bucket_mgr.create(content="核心准则", importance=10, pinned=True, domain=["测试"])
        anchor_id = await bucket_mgr.create(content="坐标桶", importance=5, domain=["测试"])
        await bucket_mgr.set_anchor(anchor_id, True)

        pinned_result = await bucket_mgr.set_seed(pinned_id, True)
        anchor_result = await bucket_mgr.set_seed(anchor_id, True)

        assert pinned_result["ok"] is True
        assert anchor_result["ok"] is True
        pinned_bucket = await bucket_mgr.get(pinned_id)
        anchor_bucket = await bucket_mgr.get(anchor_id)
        assert pinned_bucket["metadata"]["pinned"] is True
        assert pinned_bucket["metadata"]["seed"] is True
        assert anchor_bucket["metadata"]["anchor"] is True
        assert anchor_bucket["metadata"]["seed"] is True

    @pytest.mark.asyncio
    async def test_update_seed_string_false_is_not_truthy(self, bucket_mgr):
        bid = await bucket_mgr.create(content="种子", importance=6, domain=["测试"])
        ok = await bucket_mgr.update(bid, seed="false")
        assert ok is True
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"].get("seed") is not True

    @pytest.mark.asyncio
    async def test_update_seed_rejects_at_cap_returns_false(self, test_config, fake_embedding_engine):
        from bucket_manager import BucketManager
        mgr = BucketManager(dict(test_config, seed={"max_count": 1}), embedding_engine=fake_embedding_engine)
        b1 = await mgr.create(content="第一个种子", importance=6, domain=["测试"])
        b2 = await mgr.create(content="第二个种子", importance=6, domain=["测试"])
        assert await mgr.update(b1, seed=True) is True
        assert await mgr.update(b2, seed=True) is False

    @pytest.mark.asyncio
    async def test_list_seeds_sorted_by_created_ascending(self, bucket_mgr):
        b1 = await bucket_mgr.create(content="第一个", importance=6, domain=["测试"])
        b2 = await bucket_mgr.create(content="第二个", importance=6, domain=["测试"])
        # 两次 create() 之间真实时间差可能小于时间戳精度，显式回填 created
        # 避免 sort 落到"创建先后"以外的次序上（fs 迭代顺序等），保证断言稳定。
        _backdate(bucket_mgr, b1, days_ago=2)
        _backdate(bucket_mgr, b2, days_ago=1)
        await bucket_mgr.set_seed(b2, True)
        await bucket_mgr.set_seed(b1, True)
        seeds = await bucket_mgr.list_seeds()
        assert [s["id"] for s in seeds] == [b1, b2]


# ============================================================
# decay_engine: skip-list + calculate_score floor/递减
# ============================================================
def _backdate(bucket_mgr, bucket_id, days_ago, activation_count=1):
    fpath = bucket_mgr._find_bucket_file(bucket_id)
    post = fm.load(fpath)
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    post["created"] = ts
    post["last_active"] = ts
    post["activation_count"] = activation_count
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))


class TestDecayEngineSeed:
    @pytest.mark.asyncio
    async def test_run_cycle_skips_seed_but_archives_equivalent_dynamic(self, decay_eng, bucket_mgr):
        decay_eng.threshold = 9999.0
        seed_id = await bucket_mgr.create(content="老种子", importance=5, domain=["测试"])
        plain_id = await bucket_mgr.create(content="老普通桶", importance=5, domain=["测试"])
        await bucket_mgr.set_seed(seed_id, True)
        _backdate(bucket_mgr, seed_id, 365)
        _backdate(bucket_mgr, plain_id, 365)

        await decay_eng.run_decay_cycle()

        active_ids = {b["id"] for b in await bucket_mgr.list_all(include_archive=False)}
        assert seed_id in active_ids, "seed 不应被普通衰减周期自动归档"
        assert plain_id not in active_ids, "普通低分动态桶仍应按既有规则归档"

    @pytest.mark.asyncio
    async def test_run_cycle_skips_seed_from_auto_resolve(self, decay_eng, bucket_mgr):
        seed_id = await bucket_mgr.create(content="低重要度老种子", importance=2, domain=["测试"])
        await bucket_mgr.set_seed(seed_id, True)
        _backdate(bucket_mgr, seed_id, 100)

        await decay_eng.run_decay_cycle()

        bucket = await bucket_mgr.get(seed_id)
        assert bucket["metadata"].get("resolved", False) is False, "seed 不受自动结案(负反馈的一种)影响"

    def test_calculate_score_seed_floor_when_never_touched(self, decay_eng):
        meta = {
            "type": "dynamic", "seed": True, "importance": 10,
            "created": (datetime.now() - timedelta(days=500)).isoformat(),
            "last_active": (datetime.now() - timedelta(days=500)).isoformat(),
            "activation_count": 1,
        }
        assert decay_eng.calculate_score(meta) == decay_eng.seed_floor

    def test_calculate_score_seed_bonus_diminishes_at_high_importance(self, decay_eng):
        # v3 Commit C：activity_bonus 部分改用 last_meaningful_at 新鲜度，
        # 不再是 activation_count 代理（原断言随 Commit A 过渡实现一起退役，
        # 见 tests/test_v3_ranking_commit_c.py 的等价新断言）。
        base = {
            "type": "dynamic", "seed": True,
            "created": (datetime.now() - timedelta(days=10)).isoformat(),
            "last_meaningful_at": datetime.now().isoformat(),
        }
        low_importance_score = decay_eng.calculate_score({**base, "importance": 1})
        high_importance_score = decay_eng.calculate_score({**base, "importance": 10})

        assert low_importance_score > decay_eng.seed_floor
        assert high_importance_score == decay_eng.seed_floor, "宪法推论一：importance=10 时递减系数为 0"
        assert low_importance_score > high_importance_score

    def test_calculate_score_seed_is_not_a_frozen_constant(self, decay_eng):
        # 对比 pinned：pinned 恒定 999，seed 应随 last_meaningful_at 新鲜度
        # 变化（floor 不是冻结）。
        never_touched = decay_eng.calculate_score(
            {"type": "dynamic", "seed": True, "importance": 3}
        )
        recently_touched = decay_eng.calculate_score(
            {"type": "dynamic", "seed": True, "importance": 3,
             "last_meaningful_at": datetime.now().isoformat()}
        )
        assert never_touched != recently_touched

    def test_calculate_score_seed_branch_no_longer_references_activation_count(self):
        """F 附加要求：Commit C 落地双轴时必须整体移除 seed 分支的
        activation_count 代理路径，这里断言源码层面确实没有残留——不是
        靠行为断言侧面推测，直接读 _calc_seed_score 的函数体。"""
        import inspect
        import decay_engine as decay_engine_module

        source = inspect.getsource(decay_engine_module.DecayEngine._calc_seed_score)
        assert "activation_count" not in source

    def test_calculate_score_pinned_seed_combo_stays_pinned_constant(self, decay_eng):
        meta = {"type": "dynamic", "pinned": True, "seed": True, "importance": 5}
        assert decay_eng.calculate_score(meta) == 999.0

    def test_calculate_score_string_seed_false_is_not_treated_as_seed(self, decay_eng):
        meta = {
            "type": "dynamic", "seed": "false", "importance": 9,
            "created": (datetime.now() - timedelta(days=500)).isoformat(),
            "last_active": (datetime.now() - timedelta(days=500)).isoformat(),
            "activation_count": 1,
        }
        assert decay_eng.calculate_score(meta) != decay_eng.seed_floor

    # ---- 出厂验收 4a：种子存活（一票否决）----
    @pytest.mark.asyncio
    async def test_acceptance_4a_seed_survives_full_cycle_and_clamps_to_floor(self, decay_eng, bucket_mgr):
        decay_eng.threshold = 9999.0  # 逼迫一切非豁免桶归档，制造"迁移前已严重衰减"的对照场景
        seed_id = await bucket_mgr.create(
            content="从未检索、importance=10 的种子", importance=10, domain=["测试"],
        )
        await bucket_mgr.set_seed(seed_id, True)
        _backdate(bucket_mgr, seed_id, 1000, activation_count=1)  # 从未检索：activation_count 停在创建基线
        # 对照组：同批一个普通老桶，证明"seed 不归档"不是因为归档流程本身被
        # 关掉了，而是 seed 的专属豁免。
        plain_id = await bucket_mgr.create(content="同批的普通老桶", importance=5, domain=["测试"])
        _backdate(bucket_mgr, plain_id, 1000)

        bucket_before = await bucket_mgr.get(seed_id)
        score_before = decay_eng.calculate_score(bucket_before["metadata"])
        assert score_before == decay_eng.seed_floor  # clamp 回 floor，不是普通公式算出的接近 0 的衰减值

        stats = await decay_eng.run_decay_cycle()

        active_ids = {b["id"] for b in await bucket_mgr.list_all(include_archive=False)}
        assert seed_id in active_ids, "整周期后仍应在活跃池（对应 wake/breath 继承区可达）"
        assert stats["archived"] >= 1, "同批非 seed 的桶该走的归档流程不受影响（对照组）"

    # ---- 出厂验收 4b：种子资格独立（一票否决）----
    def test_acceptance_4b_low_importance_seed_still_in_inheritance_zone(self):
        buckets = [
            {"id": "low_seed", "metadata": {
                "seed": True, "importance": 4, "type": "dynamic",
                "created": "2026-01-01T00:00:00",
            }},
            {"id": "plain_low", "metadata": {
                "importance": 4, "type": "dynamic", "created": "2026-01-01T00:00:00",
            }},
        ]
        selected = select_seed_buckets(buckets)
        assert [b["id"] for b in selected] == ["low_seed"]


# ============================================================
# tools/_wake_seed.py: select_seed_buckets 纯函数
# ============================================================
class TestSelectSeedBuckets:
    def test_filters_only_seed_true(self):
        buckets = [
            {"id": "a", "metadata": {"seed": True, "importance": 5, "created": "2026-01-01"}},
            {"id": "b", "metadata": {"seed": False, "importance": 9, "created": "2026-01-02"}},
            {"id": "c", "metadata": {"importance": 9, "created": "2026-01-03"}},
        ]
        assert [b["id"] for b in select_seed_buckets(buckets)] == ["a"]

    def test_ignores_importance_threshold(self):
        buckets = [{"id": "a", "metadata": {"seed": True, "importance": 1, "created": "2026-01-01"}}]
        assert [b["id"] for b in select_seed_buckets(buckets)] == ["a"]

    def test_sorted_importance_desc_then_created_asc(self):
        buckets = [
            {"id": "low", "metadata": {"seed": True, "importance": 3, "created": "2026-01-01"}},
            {"id": "high_later", "metadata": {"seed": True, "importance": 9, "created": "2026-02-01"}},
            {"id": "high_earlier", "metadata": {"seed": True, "importance": 9, "created": "2026-01-15"}},
        ]
        result = [b["id"] for b in select_seed_buckets(buckets)]
        assert result == ["high_earlier", "high_later", "low"]

    def test_accepts_string_seed_values(self):
        buckets = [
            {"id": "a", "metadata": {"seed": "true", "importance": 5, "created": "2026-01-01"}},
            {"id": "b", "metadata": {"seed": "false", "importance": 5, "created": "2026-01-01"}},
        ]
        assert [b["id"] for b in select_seed_buckets(buckets)] == ["a"]

    def test_handles_empty_input(self):
        assert select_seed_buckets([]) == []


# ============================================================
# trace_core: seed=0/1 读写
# ============================================================
class TestTraceCoreSeed:
    @pytest.mark.asyncio
    async def test_trace_sets_seed_true(self, bucket_mgr):
        bid = await bucket_mgr.create(content="待圈定的种子", importance=6, domain=["测试"])
        install_runtime(bucket_mgr)

        result = await trace_core(bid, seed=1)

        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["seed"] is True
        assert "seed=True" in result

    @pytest.mark.asyncio
    async def test_trace_clears_seed_false(self, bucket_mgr):
        bid = await bucket_mgr.create(content="已是种子", importance=6, domain=["测试"])
        await bucket_mgr.set_seed(bid, True)
        install_runtime(bucket_mgr)

        await trace_core(bid, seed=0)

        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"].get("seed") is not True

    @pytest.mark.asyncio
    async def test_trace_seed_default_does_not_touch_field(self, bucket_mgr):
        bid = await bucket_mgr.create(content="不该被动到", importance=6, domain=["测试"])
        install_runtime(bucket_mgr)

        result = await trace_core(bid, name="改个名字")

        bucket = await bucket_mgr.get(bid)
        assert "seed" not in bucket["metadata"]
        assert "没有任何字段" not in result

    @pytest.mark.asyncio
    async def test_trace_seed_rejects_at_cap_with_readable_message(self, test_config, fake_embedding_engine):
        from bucket_manager import BucketManager
        mgr = BucketManager(dict(test_config, seed={"max_count": 1}), embedding_engine=fake_embedding_engine)
        b1 = await mgr.create(content="第一个种子", importance=6, domain=["测试"])
        b2 = await mgr.create(content="第二个种子", importance=6, domain=["测试"])
        install_runtime(mgr)
        await trace_core(b1, seed=1)

        result = await trace_core(b2, seed=1)

        assert "上限" in result
        bucket = await mgr.get(b2)
        assert bucket["metadata"].get("seed") is not True


# ============================================================
# tools/breath/surface.py: seed 排除出浮现候选池,不影响 pinned 核心准则段
# ============================================================
class TestSurfaceSeedExclusion:
    @pytest.mark.asyncio
    async def test_seed_excluded_from_unresolved_pool(self, bucket_mgr):
        from tools.breath.surface import surface_default

        seed_id = await bucket_mgr.create(
            content="种子桶不该出现在默认浮现", importance=9, domain=["测试"],
        )
        await bucket_mgr.set_seed(seed_id, True)
        install_runtime(bucket_mgr)

        result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

        assert seed_id not in result

    @pytest.mark.asyncio
    async def test_pinned_seed_combo_still_shows_in_core_principles(self, bucket_mgr):
        from tools.breath.surface import surface_default

        bid = await bucket_mgr.create(
            content="既是核心准则又是种子", importance=10, pinned=True, domain=["测试"],
        )
        await bucket_mgr.set_seed(bid, True)
        install_runtime(bucket_mgr)

        result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

        assert bid in result, "pinned 的可见性不该被 seed 影响——两者是正交的"


# ============================================================
# server._wake_impl: 继承区段落 + 去重
# ============================================================
class TestWakeInheritanceZone:
    @pytest.mark.asyncio
    async def test_wake_shows_low_importance_seed_and_dedupes_core_section(
        self, test_config, fake_embedding_engine, monkeypatch
    ):
        import server as srv
        from bucket_manager import BucketManager

        mgr = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        monkeypatch.setattr(srv, "bucket_mgr", mgr)
        monkeypatch.setattr(srv, "dream_engine", None)

        low_seed_id = await mgr.create(
            content="importance 只有 4 的种子，验收 4b 场景", importance=4, domain=["测试"],
        )
        await mgr.set_seed(low_seed_id, True)
        high_seed_id = await mgr.create(
            content="importance 9 且是种子——不该在核心记忆段重复出现", importance=9, domain=["测试"],
        )
        await mgr.set_seed(high_seed_id, True)
        plain_high_id = await mgr.create(
            content="普通高重要度桶，不是种子", importance=9, domain=["测试"],
        )

        text = await srv._wake_impl(48)

        assert "六、继承区" in text
        inherit_section = text.split("## 六、继承区")[1]
        core_section = text.split("## 二、核心记忆")[1].split("## 三、")[0]

        assert low_seed_id in inherit_section, "importance=4 的种子必须出现在继承区（验收 4b）"
        assert high_seed_id in inherit_section
        assert plain_high_id in core_section, "普通高重要度桶照常出现在核心记忆段"
        assert high_seed_id not in core_section, "已进继承区的种子不在核心记忆段重复列"
        assert low_seed_id not in core_section, "importance 不够也不该出现在核心记忆段，且不重复"
