"""工单 D-4 三期：tools/breath/* 面向模型的返回文案英文化。

范围以 D-4 一期扫描为准（catalog/feel/importance/search/surface/_verbatim）。
按工单要求：en 下抽 3 条断言无中文字符，zh 回归各 1 条（zh 路径字符串必须
逐字不变——所有既有 breath 测试在全量跑里已经验证过这点，这里只加
OMBRE_LANG 开关本身的最小回归）。
"""
import re
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.catalog import surface_catalog
from tools.breath.feel import surface_feels
from tools.breath.importance import surface_by_importance
from tools.breath.surface import surface_default
from tools.breath.search import surface_search

_CJK_RE = re.compile(r"[一-鿿]")


class DummyDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        if meta.get("pinned") or meta.get("protected") or meta.get("type") == "permanent":
            return 999.0
        return float(meta.get("importance") or 5)


class NoopEmbedding:
    enabled = False

    async def search_similar(self, query, top_k=20):
        return []


def install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = DummyDecay()
    rt.dehydrator = MagicMock()
    rt.embedding_engine = NoopEmbedding()
    rt.dream_engine = None
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_a, **_k: None


@pytest.mark.asyncio
async def test_en_surface_catalog_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    # domain 显式传值，绕开 bucket_manager._DEFAULT_DOMAIN_NAME("未分类")这个
    # 独立于本工单范围之外的兜底——它是 dynamic/<domain>/ 的真实目录命名
    # 约定（bucket_manager.py 不在 D-4 三期范围内），不是本轮要翻的返回文案。
    await bucket_mgr.create(content="a plain memory", importance=6, domain=["testing"])
    install_runtime(bucket_mgr)

    out = await surface_catalog()

    assert not _CJK_RE.search(out)
    assert "=== Memory Catalog" in out


@pytest.mark.asyncio
async def test_en_surface_feels_empty_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(bucket_mgr)

    out = await surface_feels(max_tokens=1000)

    assert not _CJK_RE.search(out)
    assert out == "No feels written yet."


@pytest.mark.asyncio
async def test_en_surface_default_empty_pool_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(bucket_mgr)

    out = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    assert not _CJK_RE.search(out)
    assert "empty right now" in out


@pytest.mark.asyncio
async def test_en_surface_by_importance_no_match_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(bucket_mgr)

    out = await surface_by_importance(importance_min=9, max_tokens=1000, tag_filter=[])

    assert not _CJK_RE.search(out)
    assert "No memories with importance >= 9." == out


@pytest.mark.asyncio
async def test_en_surface_search_empty_result_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(bucket_mgr)

    out = await surface_search(
        query="nothing will match this",
        max_results=10, max_tokens=1000, domain="", valence=-1, arousal=-1, tag_filter=[],
    )

    assert not _CJK_RE.search(out)
    assert "No memories matched" in out


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

@pytest.mark.asyncio
async def test_zh_default_surface_catalog_unaffected(bucket_mgr, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    install_runtime(bucket_mgr)

    out = await surface_catalog()

    assert out == "记忆库为空。"


@pytest.mark.asyncio
async def test_zh_default_surface_feels_unaffected(bucket_mgr, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    install_runtime(bucket_mgr)

    out = await surface_feels(max_tokens=1000)

    assert out == "没有留下过 feel。"


@pytest.mark.asyncio
async def test_zh_default_surface_by_importance_unaffected(bucket_mgr, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    install_runtime(bucket_mgr)

    out = await surface_by_importance(importance_min=9, max_tokens=1000, tag_filter=[])

    assert out == "没有重要度 >= 9 的记忆。"
