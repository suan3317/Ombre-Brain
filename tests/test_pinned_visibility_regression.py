from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.surface import surface_default
from tools.dream import dispatch as dream_dispatch


class EmptyDehydrator:
    async def dehydrate(self, content, meta=None):
        return ""


class EchoDehydrator:
    async def dehydrate(self, content, meta=None):
        return content


class DummyDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        if meta.get("pinned") or meta.get("protected") or meta.get("type") == "permanent":
            return 999.0
        return float(meta.get("importance") or 5)


class EmptyEmbedding:
    enabled = False

    async def search_similar(self, query, top_k=20):
        return []


def install_runtime(bucket_mgr, dehydrator):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = DummyDecay()
    rt.dehydrator = dehydrator
    rt.embedding_engine = EmptyEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_args, **_kwargs: None


@pytest.mark.asyncio
async def test_default_breath_pinned_segment_is_catalog_only(bucket_mgr):
    """阶段2：breath 默认浮现的核心准则段本职是"提醒存在"，不是二次投喂正文。

    只出 [bucket_id] 标题 目录行；桶 ID 必须可见（可定位），但任意长度的
    原文不再整段塞进默认输出——要看全文用 breath_search(query=...) 或
    breath_advanced(importance_min=...)。"""
    long_body = (
        "Pinned bucket body must remain readable but this sentence is now "
        "deliberately much longer than the 50-character catalog-line cutoff "
        "so a coincidental short-content pass can't hide a regression back "
        "to full-text dumping."
    )
    bucket_id = await bucket_mgr.create(
        content=long_body,
        pinned=True,
        domain=["rules"],
    )
    install_runtime(bucket_mgr, EmptyDehydrator())

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    assert bucket_id in result
    assert "=== 核心准则 ===" in result
    # 目录行，不是全文：完整长句不应该整句出现在默认输出里。
    assert long_body not in result
    # 返修单一号改动二：核心准则段头必须补引导句，跟 wake 的"核心记忆"段
    # 口径对齐（过去这句只写在代码注释里，没进真正渲染给用户看的文本）。
    assert "需要全文时用 breath_search(query=...) 拉取。" in result


@pytest.mark.asyncio
async def test_dream_includes_core_bucket_content_as_reference(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="Pinned dream context must remain visible.",
        pinned=True,
        domain=["rules"],
    )
    install_runtime(bucket_mgr, EchoDehydrator())

    result = await dream_dispatch(window_hours=48)

    assert bucket_id in result
    assert "Pinned dream context must remain visible" in result


@pytest.mark.asyncio
async def test_default_surfacing_budget_notice_reports_explicit_remaining_count(bucket_mgr):
    """返修单一号改动六:省略提示要带具体条数(显式留痕)，不能是费解的
    "未被截断或摘要"，也不能是模糊的"下一条"——跟 wake 段
    (_wake_render.py)已立的"还有 N 条未展示"规则口径统一。"""
    long_body = "浮现记忆正文标记" * 200
    await bucket_mgr.create(content=long_body, importance=8, domain=["daily"])
    await bucket_mgr.create(content=long_body, importance=7, domain=["daily"])
    install_runtime(bucket_mgr, EmptyDehydrator())

    result = await surface_default(max_results=10, max_tokens=5, tag_filter=[])

    assert "还有 2 条浮现记忆未返回" in result
    assert "整条省略，不是截断" in result
    assert "未被截断或摘要" not in result
    assert "已被截断" not in result
