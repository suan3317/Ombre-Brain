"""工单 D-4 四期：tools/_common.py 的 _EMBED_WARN（现为 _embed_warn()
函数）英文化。en 下抽 2 条断言无中文字符，zh 回归 1 条。
"""
import re
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools._common import _embed_warn
from tools.hold.core import store_core

_CJK_RE = re.compile(r"[一-鿿]")


class StubDehydrator:
    async def analyze(self, content):
        return {"domain": ["work"], "valence": 0.6, "arousal": 0.4, "tags": [], "suggested_name": ""}


class BrokenEmbeddingEngine:
    """enabled=True 但 get_embedding 永远返回 None——触发 embedding_state=missing 分支。"""

    enabled = True

    async def get_embedding(self, bucket_id):
        return None


class NoopDecay:
    async def ensure_started(self):
        return None


def _install_runtime(monkeypatch, bucket_mgr):
    monkeypatch.setattr(rt, "config", {"limits": {}, "merge_threshold": 75}, raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "dehydrator", StubDehydrator(), raising=False)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay(), raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "embedding_engine", BrokenEmbeddingEngine(), raising=False)


# ============================================================
# en 断言（无中文字符）
# ============================================================

def test_en_embed_warn_function_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")

    warning = _embed_warn()

    assert not _CJK_RE.search(warning)
    assert "Check OMBRE_EMBED_API_KEY" in warning


@pytest.mark.asyncio
async def test_en_hold_store_core_appends_english_embed_warning(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_runtime(monkeypatch, bucket_mgr)

    out = await store_core(
        content="a plain english memory that gets a broken embedding",
        extra_tags=[], importance=5, valence=-1, arousal=-1, why_remembered="",
    )

    assert not _CJK_RE.search(out)
    assert "Check OMBRE_EMBED_API_KEY" in out


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

def test_zh_default_embed_warn_function_unaffected(monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)

    warning = _embed_warn()

    assert warning == "向量化失败，该桶不参与语义检索，仅支持关键词匹配。请检查 OMBRE_EMBED_API_KEY。"
