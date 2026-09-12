"""工单 D-4 三期：tools/dream/*（反思工具 dream()，不是 dream_engine.py 的
夜间做梦系统）面向模型的返回文案英文化。范围以 D-4 一期扫描为准
（__init__/candidates/hints/output）。en 下抽 3 条断言无中文字符，zh 回归
各 1 条。
"""
import re
from unittest.mock import MagicMock

import pytest

from tools import _runtime as rt
from tools.dream import dispatch as dream_dispatch
from tools.dream.hints import build_connection_hint
from tools.dream.output import format_dream_output

_CJK_RE = re.compile(r"[一-鿿]")


class DummyDecay:
    async def ensure_started(self):
        return None


class FakeBucketMgr:
    def __init__(self, buckets):
        self._buckets = buckets

    async def list_all(self, include_archive=False):
        return self._buckets


def _install_runtime(monkeypatch, buckets):
    monkeypatch.setattr(rt, "decay_engine", DummyDecay(), raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", FakeBucketMgr(buckets), raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)


class FakeEmbeddingEngine:
    enabled = True

    def __init__(self, embeddings, similarity):
        self._embeddings = embeddings
        self._similarity = similarity

    async def get_embedding(self, bucket_id):
        return self._embeddings.get(bucket_id)

    def _cosine_similarity(self, a, b):
        return self._similarity


# ============================================================
# en 断言（无中文字符）
# ============================================================

@pytest.mark.asyncio
async def test_en_dispatch_empty_window_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_runtime(monkeypatch, buckets=[])

    out = await dream_dispatch(window_hours=48)

    assert not _CJK_RE.search(out)
    assert out == "No new memories to digest from the past 48 hours."


def test_en_format_dream_output_header_and_labels_have_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    bucket = {
        "id": "b1",
        "content": "I sat with the feeling for a while.",
        "metadata": {
            "name": "Sitting with it",
            "resolved": False,
            "domain": ["inner life"],
            "valence": 0.6,
            "arousal": 0.4,
            "created": "2026-09-01T00:00:00",
            "last_active": "2026-09-01T00:00:00",
        },
    }
    out = format_dream_output(
        recent=[bucket], all_buckets=[bucket], window_hours=48,
        connection_hint="", crystal_hint="", core_context=None,
    )
    assert not _CJK_RE.search(out)
    assert "=== Dreaming" in out
    assert "[unresolved]" in out
    assert "topic:inner life" in out


@pytest.mark.asyncio
async def test_en_build_connection_hint_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    recent = [
        {"id": "a", "metadata": {"name": "First memory"}},
        {"id": "b", "metadata": {"name": "Second memory"}},
    ]
    embeddings = {"a": [1.0, 0.0], "b": [1.0, 0.0]}
    monkeypatch.setattr(rt, "embedding_engine", FakeEmbeddingEngine(embeddings, similarity=0.9), raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)

    out = await build_connection_hint(recent)

    assert not _CJK_RE.search(out)
    assert "seem connected" in out


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

@pytest.mark.asyncio
async def test_zh_default_dispatch_empty_window_unaffected(monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    _install_runtime(monkeypatch, buckets=[])

    out = await dream_dispatch(window_hours=48)

    assert out == "过去 48 小时内没有需要消化的新记忆。"
