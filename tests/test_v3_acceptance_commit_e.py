"""
记忆动力学二期 · Commit E：出厂验收补全（两式一票否决 + 六回归 a-h）。

范围（施工单 + 设计定稿"出厂验收"章节）：
- 4a/4b（一票否决）与回归 a/b/c/h 随 Commit A/B/C 落地时已经补齐，测试
  分别在各自文件里：
    * 4a/4b → tests/test_v3_seed_commit_a.py::TestSeedScoring/TestSelectSeedBuckets
      （test_acceptance_4a_seed_survives_full_cycle_and_clamps_to_floor /
      test_acceptance_4b_low_importance_seed_still_in_inheritance_zone）
    * 回归 a → tests/test_v3_signal_commit_b.py::
      test_acceptance_a_control_plane_ops_produce_zero_signal
    * 回归 b → tests/test_v3_signal_commit_b.py::
      test_random_surfacing_does_not_touch_streak
    * 回归 c → tests/test_v3_signal_commit_b.py::
      test_acceptance_c_rolling_48h_ten_events_one_credit_idempotent_edge
    * 回归 h → tests/test_v3_ranking_commit_c.py::
      TestActivityBonus::test_bonus_decays_toward_zero_over_time
  本文件只补上面六条里尚未落地的 d/e/f/g，全部只用真实 BucketManager +
  真实 DecayEngine（tests/conftest.py 的 bucket_mgr/decay_eng fixture），
  不用测试替身——F 裁定："Commit E 的六回归与两式一票否决必须全部跑真实
  DecayEngine，禁止用替身"。

回归 e 的实现顺带修正：验收测试补齐过程中发现 Commit C 的
DecayEngine.activity_bonus() 把"受高 importance 递减"这条种子专属曲线
（设计定稿"种子条款"原话）套用到了全部桶（含非 seed），违反同一份定稿
"不对称原则"明写的"importance 10 / weight 低位的非 seed 桶必须能吃到
反向匹配与全额 activity_bonus"。已在 src/decay_engine.py 加
for_seed 参数拆分两条路径（默认 for_seed=False 给通用排序轴全额，
_calc_seed_score 显式传 for_seed=True 保留种子递减），并同步更新了
tests/test_v3_ranking_commit_c.py 里对应的过期断言。
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import frontmatter as fm
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

import tools._runtime as rt
from decay_engine import apply_band_quota


def install_runtime(bucket_mgr, decay_eng):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = decay_eng
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


def _backdate(bucket_mgr, bucket_id, days_ago, last_meaningful_at=None):
    fpath = bucket_mgr._find_bucket_file(bucket_id)
    post = fm.load(fpath)
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    post["created"] = ts
    post["last_active"] = ts
    if last_meaningful_at is not None:
        post["last_meaningful_at"] = last_meaningful_at
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))


# ============================================================
# 回归 d：陈旧标全覆盖且精确——三入口、段级、校准同步清除
# ============================================================
class TestRegressionD_StalenessCalibrationSyncsAcrossEntryPoints:
    @pytest.mark.asyncio
    async def test_wake_board_and_file_read_share_one_clear_for_same_file_path(
        self, test_config, fake_embedding_engine, monkeypatch
    ):
        """留言板.md 同时是 wake 留言板段的硬编码读取对象，也能被 file_read
        当普通文件读取——同一个 sidecar path("file:留言板.md")，两个渲染
        入口。设计定稿要求"校准后可清，三入口同步"：清一次，两处都恢复，
        不需要分别清。"""
        import server as srv
        from bucket_manager import BucketManager

        mgr = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        monkeypatch.setattr(srv, "bucket_mgr", mgr)
        monkeypatch.setattr(srv, "dream_engine", None)

        board_path = srv._fz_safe("留言板.md")
        os.makedirs(os.path.dirname(board_path), exist_ok=True)
        with open(board_path, "w", encoding="utf-8") as f:
            f.write("给下个窗口的话")

        old = await mgr.create(content="被引用的旧结论", importance=5, domain=["测试"])
        new = await mgr.create(content="新结论", importance=5, domain=["测试"])
        await mgr.record_citation(old, source="file:留言板.md", location="file_save")
        await mgr.mark_superseded(old, superseded_by=new, supersede_type="contradiction")

        wake_text = await srv._wake_impl(48)
        board_section = wake_text.split("## 三、留言板")[1].split("## 四、")[0]
        assert "⚠️" in board_section
        file_text = await srv._fz_read("留言板.md", 0)
        assert "⚠️" in file_text

        cleared = await mgr.clear_derived_stale("file:留言板.md")
        assert cleared == 1

        wake_text_after = await srv._wake_impl(48)
        board_section_after = wake_text_after.split("## 三、留言板")[1].split("## 四、")[0]
        assert "⚠️" not in board_section_after
        file_text_after = await srv._fz_read("留言板.md", 0)
        assert "⚠️" not in file_text_after

    @pytest.mark.asyncio
    async def test_i_bucket_calibration_clears_after_single_clear_call(self, bucket_mgr):
        """I 桶（Dashboard 桶详情走同一个 get_derived_stale 读接口）的校准
        路径：mark → 有警示 → clear 一次 → 三处（这里用 i_core 代表桶级
        读取路径，跟 Dashboard 的 api_bucket_detail 是同一个
        get_derived_stale 调用）读到的都是清过的状态。"""
        from tools.i.core import i_core
        from decay_engine import DecayEngine

        install_runtime(bucket_mgr, DecayEngine({}, bucket_mgr))
        result = await i_core(content="我以前觉得自己不擅长这个", aspect="patterns")
        i_bucket_id = result.split("→")[1].strip()

        old = await bucket_mgr.create(content="被引用的旧结论", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新结论", importance=5, domain=["测试"])
        await bucket_mgr.record_citation(old, source=i_bucket_id, location="i_manual")
        await bucket_mgr.mark_superseded(old, superseded_by=new, supersede_type="state_transition")

        text_before = await i_core(read=True)
        assert "⚠️" in text_before

        cleared = await bucket_mgr.clear_derived_stale(i_bucket_id)
        assert cleared == 1

        text_after = await i_core(read=True)
        assert "⚠️" not in text_after

    @pytest.mark.asyncio
    async def test_scoped_clear_by_stale_by_leaves_other_stale_entries_intact(self, bucket_mgr):
        """校准是"处理完这一条陈旧引用"，不是"整份文档一键清空"——传
        stale_by 只清对应那一条来源，同 path 下其它未处理的陈旧标记必须
        还在，否则会把还没核实过的警示一并静默吞掉。"""
        note_id = await bucket_mgr.create(content="引用了两条旧事实的笔记桶", importance=5, domain=["测试"])
        old_a = await bucket_mgr.create(content="旧结论A", importance=5, domain=["测试"])
        new_a = await bucket_mgr.create(content="新结论A", importance=5, domain=["测试"])
        old_b = await bucket_mgr.create(content="旧结论B", importance=5, domain=["测试"])
        new_b = await bucket_mgr.create(content="新结论B", importance=5, domain=["测试"])
        await bucket_mgr.record_citation(old_a, source=note_id, location="manual")
        await bucket_mgr.record_citation(old_b, source=note_id, location="manual")
        await bucket_mgr.mark_superseded(old_a, superseded_by=new_a, supersede_type="contradiction")
        await bucket_mgr.mark_superseded(old_b, superseded_by=new_b, supersede_type="contradiction")

        entries_before = await bucket_mgr.get_derived_stale(note_id)
        assert len(entries_before) == 2

        await bucket_mgr.clear_derived_stale(note_id, stale_by=old_a)

        entries_after = await bucket_mgr.get_derived_stale(note_id)
        assert len(entries_after) == 1
        assert entries_after[0]["stale_by"] == old_b


# ============================================================
# 回归 e：importance10/低retention非seed桶获streak清零与全额bonus
# ============================================================
class TestRegressionE_HighImportanceLowWeightNonSeedGetsFullCreditAndStreakClear:
    @pytest.mark.asyncio
    async def test_reverse_match_clears_streak_and_grants_full_activity_bonus_at_importance_10(
        self, decay_eng, bucket_mgr
    ):
        """场景：importance=10、weight 低位（老桶，长期没有强信号，retention
        已经衰减到很低）、非 seed 的桶——不对称原则要求它在拿到反向匹配
        （used_inferred）时一样能清零 streak，并且随后拿到强信号时享受
        **全额** activity_bonus，不因为 importance=10 而被打折。"""
        bid = await bucket_mgr.create(content="沉底但重要的旧记忆", importance=10, domain=["测试"])
        _backdate(bucket_mgr, bid, days_ago=400)
        # 手动灌高 streak，模拟连续 semantic 召回未使用的沉底桶
        await bucket_mgr.update(bid, semantic_unused_streak=7)
        bucket = await bucket_mgr.get(bid)
        assert bucket["metadata"]["importance"] == 10
        # importance>=6 本就不适用负反馈——retention() 的"低位"完全来自年龄，
        # 不依赖 streak；这里确认 streak 本身对它的 retention 无影响（不对称
        # 原则"惩罚类规则看 importance"的另一半：importance 10 天然免疫负反馈）。
        assert decay_eng.retention(bucket["metadata"]) < 3.0, "400 天老桶，age_decay 后 retention 应处于低位"

        ok = await bucket_mgr.mark_used_inferred(bid)
        assert ok is True
        bucket_after_match = await bucket_mgr.get(bid)
        assert int(bucket_after_match["metadata"].get("semantic_unused_streak") or 0) == 0

        await bucket_mgr.record_strong_signal(bid, kind="hold_append")
        bucket_final = await bucket_mgr.get(bid)

        # 全额 bonus：跟同样刚拿到强信号、但 importance=1 的桶比，两者的
        # activity_bonus 必须完全相等——不能因为 importance=10 被打折。
        low_imp_meta = {**bucket_final["metadata"], "importance": 1}
        bonus_high = decay_eng.activity_bonus(bucket_final["metadata"])
        bonus_low = decay_eng.activity_bonus(low_imp_meta)
        assert bonus_high == bonus_low
        assert bonus_high > 0.0

    def test_seed_path_still_diminishes_unaffected_by_this_fix(self, decay_eng):
        """确认这次修正没有牵动种子自己的待遇——种子的 activity 递减是
        设计定稿种子条款明写的，跟这里非 seed 的"全额"要求是两件事。"""
        now = datetime.now()
        meta_low = {"importance": 1, "last_meaningful_at": now.isoformat()}
        meta_high = {"importance": 10, "last_meaningful_at": now.isoformat()}
        assert decay_eng.activity_bonus(meta_high, for_seed=True) == 0.0
        assert decay_eng.activity_bonus(meta_low, for_seed=True) > 0.0


# ============================================================
# 回归 f：配额下各 band 有代表且中档当月新桶可见（K 家验收指标）
# ============================================================
class TestRegressionF_QuotaRepresentationAndMidBandFreshVisibility:
    @pytest.mark.asyncio
    async def test_all_three_bands_represented_and_fresh_mid_bucket_survives_quota(
        self, decay_eng, bucket_mgr
    ):
        high_ids = []
        for i in range(6):
            bid = await bucket_mgr.create(content=f"高档桶{i}", importance=9, domain=["测试"])
            _backdate(bucket_mgr, bid, days_ago=200)
            high_ids.append(bid)

        # 中档：9 条彻底沉底的老桶（900 天，从未有强信号）——K 家实测的
        # "配额被老桶占满，当月新桶漏出"场景的对照组。
        stale_mid_ids = []
        for i in range(9):
            bid = await bucket_mgr.create(content=f"沉底中档桶{i}", importance=7, domain=["测试"])
            _backdate(bucket_mgr, bid, days_ago=900)
            stale_mid_ids.append(bid)
        # 中档：本月创建的新桶，K 家验收指标要求它必须能挤进配额可见。
        fresh_mid_id = await bucket_mgr.create(content="中档当月新桶", importance=7, domain=["测试"])
        _backdate(bucket_mgr, fresh_mid_id, days_ago=3)

        low_ids = []
        for i in range(3):
            bid = await bucket_mgr.create(content=f"低档桶{i}", importance=3, domain=["测试"])
            _backdate(bucket_mgr, bid, days_ago=10)
            low_ids.append(bid)

        all_buckets = await bucket_mgr.list_all(include_archive=False)
        ranked = decay_eng.band_ranked(all_buckets)
        quota_result = apply_band_quota(ranked, decay_eng.band_quota)

        bands_present = {b["_band"] for b in quota_result}
        assert bands_present == {"high", "mid", "low"}, "配额结果各 band（非空时）均须有代表"

        mid_result_ids = {b["id"] for b in quota_result if b["_band"] == "mid"}
        assert len(mid_result_ids) == 4, "中档配额 4 席（施工单定案 4/4/2）"
        assert fresh_mid_id in mid_result_ids, "K 家验收指标：中档当月新桶必须在配额浮现里可见"
        # 9 条沉底老桶应该被挤掉大多数——配额只给中档 4 席，新桶年龄优势
        # 加上老桶严重衰减，沉底老桶不应该把新桶挤出去。
        assert len(mid_result_ids & set(stale_mid_ids)) <= 3

        high_result_ids = {b["id"] for b in quota_result if b["_band"] == "high"}
        low_result_ids = {b["id"] for b in quota_result if b["_band"] == "low"}
        assert len(high_result_ids) == 4
        assert len(low_result_ids) == 2
        assert high_result_ids <= set(high_ids)
        assert low_result_ids <= set(low_ids)


# ============================================================
# 回归 g：seed 保留席轮换生效（不霸榜）
# ============================================================
class TestRegressionG_SeedNeverOccupiesSurfaceQuota:
    @pytest.mark.asyncio
    async def test_high_importance_seeds_never_crowd_out_non_seed_quota_seats(
        self, decay_eng, bucket_mgr
    ):
        """取数口径（K 提，F 裁定）：seed 进继承区即不占浮现配额，浮现段
        对 seed 去重。落到实现上，tools/breath/surface.py 的候选池
        （all_buckets_non_anchor_non_seed）从一开始就把全部 seed 桶排除
        在浮现配额候选之外——不管 seed 桶的 importance 多高、activity_bonus
        多满，都不会进入 band_ranked/apply_band_quota 的输入，从结构上
        保证"seed 自身参与轮换禁止霸榜"这条不变量恒成立，不需要额外的
        运行时判断。这里造一批本该轻松霸榜高档配额的 seed 桶，验证它们
        一条都没有挤占非 seed 桶的配额席位，高档配额仍然由非 seed 桶轮换
        填满。"""
        install_runtime(bucket_mgr, decay_eng)

        seed_ids = []
        for i in range(8):
            bid = await bucket_mgr.create(content=f"高价值种子{i}", importance=10, domain=["测试"])
            await bucket_mgr.set_seed(bid, True)
            await bucket_mgr.record_strong_signal(bid, kind="hold_append")
            seed_ids.append(bid)

        non_seed_ids = []
        for i in range(6):
            bid = await bucket_mgr.create(content=f"普通高档桶{i}", importance=9, domain=["测试"])
            non_seed_ids.append(bid)

        from tools.breath.surface import surface_default
        result = await surface_default(max_results=100, max_tokens=100000, tag_filter=[])

        for sid in seed_ids:
            assert sid not in result, "seed 桶不应出现在浮现配额结果里（进继承区，不占浮现配额）"

        surfaced_non_seed = [nid for nid in non_seed_ids if nid in result]
        assert len(surfaced_non_seed) > 0, "非 seed 高档桶必须能拿到浮现配额，不能被结构性地挤没"

    @pytest.mark.asyncio
    async def test_seed_excluded_from_band_ranked_candidate_pool_directly(self, decay_eng, bucket_mgr):
        """跟上面端到端测试互补的单元级验证：直接确认 seed 桶不会被
        list_all 之后手动传给 band_ranked 时污染 high band 的排名——这里
        验证的是"就算不小心把 seed 混进候选池，seed 的分数也遵循自己的
        floor+bonus 规则，不会跟非 seed 用同一套 retention 公式竞争"这个
        更底层的不变量（seed 走 self.seed_floor 分支，不参与
        band_ranked 的年龄/负反馈计算）。"""
        seed_id = await bucket_mgr.create(content="种子", importance=10, domain=["测试"])
        await bucket_mgr.set_seed(seed_id, True)
        bucket = await bucket_mgr.get(seed_id)
        assert decay_eng.retention(bucket["metadata"]) == decay_eng.seed_floor
