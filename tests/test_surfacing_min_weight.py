"""返修单一号改动三：浮现权重下限(surfacing.min_weight)。

低于 config surfacing.min_weight(默认 2.5)的桶不进 breath() 默认浮现候选
池；breath_search / breath_advanced 的显式检索不受此限——想找的东西低权重
也照样能找到，只是不会自己冒出来。
"""
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.surface import surface_default
from tools.breath.search import surface_search


class EmptyEmbedding:
    enabled = False

    async def search_similar(self, query, top_k=20):
        return []


class ScoreIsImportanceDecay:
    """给测试用的假衰减引擎：直接把 importance 当浮现权重用，不跑真实衰减
    数学，只用来在测试里精确控制"这个桶的浮现权重是多少"这一个变量
    （importance 是 create() 就能直接传的白名单字段，不像自定义 metadata
    键那样会被 update() 的字段白名单挡在外面）。"""
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        if meta.get("pinned") or meta.get("protected") or meta.get("type") == "permanent":
            return 999.0
        return float(meta.get("importance", 5))


def install_runtime(bucket_mgr, surfacing_cfg=None):
    rt.config = {"surfacing": surfacing_cfg or {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = ScoreIsImportanceDecay()
    rt.dehydrator = None
    rt.embedding_engine = EmptyEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_args, **_kwargs: None


@pytest.mark.asyncio
async def test_default_surfacing_excludes_below_min_weight_uses_default_2_5(bucket_mgr):
    low_id = await bucket_mgr.create(content="低权重的日常桶正文标记ABCLOW", domain=["daily"], importance=2)
    high_id = await bucket_mgr.create(content="高权重的日常桶正文标记ABCHIGH", domain=["daily"], importance=3)

    install_runtime(bucket_mgr)  # 不配 min_weight，走默认 2.5
    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    assert high_id in result, "权重 3 高于默认阈值 2.5，应该出现在默认浮现里"
    assert low_id not in result, "权重 2 低于默认阈值 2.5，不该出现在默认浮现里"


@pytest.mark.asyncio
async def test_min_weight_is_configurable(bucket_mgr):
    bucket_id = await bucket_mgr.create(content="被配置阈值挡住的桶正文标记XYZ", domain=["daily"], importance=3)

    install_runtime(bucket_mgr, surfacing_cfg={"min_weight": 4.0})
    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    assert bucket_id not in result, "阈值调到 4.0 时，权重 3 的桶不该出现"


@pytest.mark.asyncio
async def test_breath_search_still_finds_bucket_below_min_weight(bucket_mgr):
    low_id = await bucket_mgr.create(
        content="低权重但可以被关键词找到的桶正文标记findme关键词", domain=["daily"], importance=1,
    )

    install_runtime(bucket_mgr)  # 默认阈值 2.5，远高于这个桶的 importance=1
    result = await surface_search(
        query="findme关键词", max_results=10, max_tokens=10000,
        domain="", valence=-1, arousal=-1, tag_filter=[],
    )

    assert low_id in result, "显式检索(breath_search)不该被 min_weight 挡住"
