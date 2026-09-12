"""工单 D-4 四期：tools/grow/*（__init__/core/shortpath）面向模型的返回
文案英文化。en 下每个改动文件抽 2 条断言无中文字符，zh 回归各 1 条。
"""
import re
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.grow import dispatch as grow_dispatch
from tools.grow.core import grow_items
from tools.grow.shortpath import grow_shortpath

_CJK_RE = re.compile(r"[一-鿿]")


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return 1.0


def _install_runtime(monkeypatch, bucket_mgr, dehydrator):
    monkeypatch.setattr(rt, "config", {"limits": {}, "merge_threshold": 75}, raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "dehydrator", dehydrator, raising=False)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay(), raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)


class StubDehydrator:
    async def analyze(self, content):
        return {"domain": ["work"], "valence": 0.6, "arousal": 0.4, "tags": ["tag"], "suggested_name": "event"}


class FailingDehydrator:
    async def analyze(self, content):
        raise TimeoutError("api unavailable")

    async def digest(self, content):
        raise TimeoutError("api unavailable")


# ============================================================
# tools/grow/__init__.py
# ============================================================

@pytest.mark.asyncio
async def test_en_grow_dispatch_empty_content_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    monkeypatch.setattr(rt, "decay_engine", NoopDecay(), raising=False)

    out = await grow_dispatch(content="")

    assert not _CJK_RE.search(out)
    assert out == "Content is empty, nothing to organize."


@pytest.mark.asyncio
async def test_en_grow_items_empty_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    monkeypatch.setattr(rt, "decay_engine", NoopDecay(), raising=False)

    out = await grow_dispatch(items=["   "])

    assert not _CJK_RE.search(out)
    assert out == "items is empty or all invalid, no bucket created."


def test_zh_default_grow_dispatch_empty_content_unaffected(monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    import asyncio
    monkeypatch.setattr(rt, "decay_engine", NoopDecay(), raising=False)

    out = asyncio.run(grow_dispatch(content=""))

    assert out == "内容为空，无法整理。"


# ============================================================
# tools/grow/shortpath.py
# ============================================================

@pytest.mark.asyncio
async def test_en_grow_shortpath_action_prefix_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_runtime(monkeypatch, bucket_mgr, StubDehydrator())

    out = await grow_shortpath("a short english note")

    assert not _CJK_RE.search(out)
    assert out.startswith("Short content saved as a single memory via the hold path, not split.\n")
    assert "created →" in out


@pytest.mark.asyncio
async def test_en_grow_shortpath_api_failure_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_runtime(monkeypatch, bucket_mgr, FailingDehydrator())

    with pytest.raises(RuntimeError) as exc_info:
        await grow_shortpath("a short english note")

    assert not _CJK_RE.search(str(exc_info.value))
    assert "API key not configured or call failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_zh_default_grow_shortpath_action_prefix_unaffected(bucket_mgr, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    _install_runtime(monkeypatch, bucket_mgr, StubDehydrator())

    out = await grow_shortpath("一段中文短笔记")

    assert out.startswith("短内容已按 hold 路径保存为单条记忆，没有拆分。\n")
    assert "新建 →" in out


# ============================================================
# tools/grow/core.py
# ============================================================

@pytest.mark.asyncio
async def test_en_grow_items_summary_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_runtime(monkeypatch, bucket_mgr, StubDehydrator())

    out = await grow_items(["first english note", "second english note"])

    assert not _CJK_RE.search(out)
    assert "2 items (pre-split, verbatim)|new:2 merged:0" in out


@pytest.mark.asyncio
async def test_en_grow_core_digest_failure_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_runtime(monkeypatch, MagicMock(), FailingDehydrator())

    with pytest.raises(RuntimeError) as exc_info:
        await grow_dispatch(content="a long enough english diary entry to skip shortpath, over thirty chars")

    assert not _CJK_RE.search(str(exc_info.value))
    assert "API key not configured or call failed, diary digest couldn't complete" in str(exc_info.value)


@pytest.mark.asyncio
async def test_zh_default_grow_items_summary_unaffected(bucket_mgr, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    _install_runtime(monkeypatch, bucket_mgr, StubDehydrator())

    out = await grow_items(["第一条中文笔记", "第二条中文笔记"])

    assert "2条(预拆分·逐字)|新2合0" in out
