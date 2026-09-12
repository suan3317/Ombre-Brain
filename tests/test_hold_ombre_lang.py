"""工单 D-4 三期：tools/hold/* 面向模型的返回文案英文化。

范围以 D-4 一期扫描为准（__init__/core/feel/pinned）。en 下抽 3 条断言无
中文字符，zh 回归各 1 条。
"""
import re

import pytest

from tools import _runtime as rt
from tools.hold import dispatch as hold_dispatch
from tools.hold import core as hold_core
from tools.hold import pinned as hold_pinned

_CJK_RE = re.compile(r"[一-鿿]")


class DummyDecay:
    async def ensure_started(self):
        return None


class _Logger:
    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass

    def error(self, *_a, **_k):
        pass


def _install_minimal_runtime(monkeypatch):
    monkeypatch.setattr(rt, "decay_engine", DummyDecay(), raising=False)
    monkeypatch.setattr(rt, "logger", _Logger(), raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)


# ============================================================
# en 断言（无中文字符）
# ============================================================

@pytest.mark.asyncio
async def test_en_empty_content_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_minimal_runtime(monkeypatch)

    out = await hold_dispatch(content="")

    assert not _CJK_RE.search(out)
    assert out == "Content is empty, nothing to store."


@pytest.mark.asyncio
async def test_en_feel_without_source_bucket_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_minimal_runtime(monkeypatch)

    out = await hold_dispatch(content="something I felt", feel=True, source_bucket="")

    assert not _CJK_RE.search(out)
    assert out.startswith("feel must point to an original memory")


@pytest.mark.asyncio
async def test_en_store_core_action_prefix_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")

    class FailingDehydrator:
        async def analyze(self, _content):
            raise TimeoutError("tagger unavailable")

    async def fake_merge_or_create(**kwargs):
        return "bucket-1", False, ""

    async def background(*_a, **_k):
        return None

    def close_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(hold_core.rt, "dehydrator", FailingDehydrator(), raising=False)
    monkeypatch.setattr(hold_core.rt, "logger", _Logger(), raising=False)
    monkeypatch.setattr(hold_core, "merge_or_create", fake_merge_or_create)
    monkeypatch.setattr(hold_core, "check_plan_resolution", background)
    monkeypatch.setattr(hold_core, "check_duplicate_for", background)
    monkeypatch.setattr(hold_core.asyncio, "create_task", close_task)

    out = await hold_core.store_core(
        content="a plain english memory", extra_tags=[], importance=5,
        valence=-1, arousal=-1, why_remembered="",
    )

    assert not _CJK_RE.search(out)
    assert out.startswith("created→bucket-1")
    assert "unclassified" in out
    assert "Tagging API temporarily unavailable" in out


@pytest.mark.asyncio
async def test_en_store_pinned_prefix_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")

    class FailingDehydrator:
        async def analyze(self, _content):
            raise TimeoutError("tagger unavailable")

    class FakeBucketMgr:
        async def create(self, **kwargs):
            return "bucket-2"

    async def no_quota_error():
        return None

    def close_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(hold_pinned.rt, "dehydrator", FailingDehydrator(), raising=False)
    monkeypatch.setattr(hold_pinned.rt, "logger", _Logger(), raising=False)
    monkeypatch.setattr(hold_pinned.rt, "bucket_mgr", FakeBucketMgr(), raising=False)
    monkeypatch.setattr(hold_pinned, "check_pinned_quota", no_quota_error)
    monkeypatch.setattr(hold_pinned.asyncio, "create_task", close_task)

    out = await hold_pinned.store_pinned(
        content="a plain english memory", extra_tags=[], valence=-1, arousal=-1, why_remembered="",
    )

    assert not _CJK_RE.search(out)
    assert out.startswith("📌pinned→bucket-2")
    assert "unclassified" in out


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

@pytest.mark.asyncio
async def test_zh_default_empty_content_unaffected(monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    _install_minimal_runtime(monkeypatch)

    out = await hold_dispatch(content="")

    assert out == "内容为空，无法存储。"


@pytest.mark.asyncio
async def test_zh_default_feel_without_source_bucket_unaffected(monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    _install_minimal_runtime(monkeypatch)

    out = await hold_dispatch(content="一些感受", feel=True, source_bucket="")

    assert out.startswith("feel 必须指向一条原始记忆")
