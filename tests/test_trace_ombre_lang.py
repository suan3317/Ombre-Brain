"""工单 D-4 四期：tools/trace/core.py 面向模型的返回文案英文化。
en 下抽 2 条断言无中文字符，zh 回归 1 条。resolved_hint()（来自
memory_messages.py）不在本轮范围，固定返回中文，不参与断言。
"""
import re
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.trace.core import trace_core

_CJK_RE = re.compile(r"[一-鿿]")


def install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_a, **_k: None


@pytest.mark.asyncio
async def test_en_bucket_not_found_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(bucket_mgr)

    out = await trace_core("does-not-exist", importance=8)

    assert not _CJK_RE.search(out)
    assert out == "Bucket not found: does-not-exist"


@pytest.mark.asyncio
async def test_en_update_success_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(bucket_mgr)
    bucket_id = await bucket_mgr.create(content="A plain english memory.", importance=5, domain=["work"])

    out = await trace_core(bucket_id, importance=8)

    assert not _CJK_RE.search(out)
    assert out == f"Updated bucket {bucket_id}: importance=8"


@pytest.mark.asyncio
async def test_en_no_fields_to_change_has_no_chinese_characters(bucket_mgr, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(bucket_mgr)
    bucket_id = await bucket_mgr.create(content="A plain english memory.", importance=5, domain=["work"])

    out = await trace_core(bucket_id)

    assert not _CJK_RE.search(out)
    assert out == "No fields to change."


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

@pytest.mark.asyncio
async def test_zh_default_bucket_not_found_unaffected(bucket_mgr, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    install_runtime(bucket_mgr)

    out = await trace_core("does-not-exist", importance=8)

    assert out == "未找到记忆桶: does-not-exist"
