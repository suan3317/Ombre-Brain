"""工单 D-4 四期：tools/i/core.py 面向模型的返回文案英文化。
en 下抽 2 条断言无中文字符，zh 回归 1 条。
"""
import re
from unittest.mock import MagicMock

import pytest

from tools import _runtime as rt
from tools.i.core import i_core

_CJK_RE = re.compile(r"[一-鿿]")


class DummyDecay:
    async def ensure_started(self):
        return None


class FakeBucketMgr:
    def __init__(self, buckets=None):
        self._buckets = buckets or []

    async def list_all(self, include_archive=False):
        return self._buckets


def _install_runtime(monkeypatch, buckets=None):
    monkeypatch.setattr(rt, "decay_engine", DummyDecay(), raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", FakeBucketMgr(buckets), raising=False)


@pytest.mark.asyncio
async def test_en_read_empty_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_runtime(monkeypatch)

    out = await i_core(read=True)

    assert not _CJK_RE.search(out)
    assert out == "No self-knowledge entries yet."


@pytest.mark.asyncio
async def test_en_invalid_aspect_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    _install_runtime(monkeypatch)

    out = await i_core(content="I notice something about myself.", aspect="not_a_real_aspect")

    assert not _CJK_RE.search(out)
    assert out.startswith("Invalid aspect: not_a_real_aspect.")


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

@pytest.mark.asyncio
async def test_zh_default_read_empty_unaffected(monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    _install_runtime(monkeypatch)

    out = await i_core(read=True)

    assert out == "还没有任何自我认知记录。"
