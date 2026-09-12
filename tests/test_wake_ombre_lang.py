"""工单 D-4 三期：_wake_seed.py + _wake_render.py + server._wake_impl（wake
六部分小标题/引导句）面向模型的返回文案英文化。en 下抽 3 条断言无中文
字符，zh 回归各 1 条。

_wake_seed.py 本身没有面向模型的文案（纯排序函数），不单独测；
_wake_render.py 的目录段渲染在 tests/test_wake_render.py 已有结构性覆盖，
这里只加 OMBRE_LANG 开关本身的断言。server._wake_impl 的六部分标题实际
定义在 server.py（不在 tools/ 下），工单第 4 条明确要求覆盖，一并测试。
"""
import re

import pytest

from tools._wake_render import render_catalog_segment, render_file_zone_summary

_CJK_RE = re.compile(r"[一-鿿]")


def _bucket(bid, name):
    return {"id": bid, "content": "text", "metadata": {"name": name, "importance": 8, "meaning": []}}


# ============================================================
# en 断言（无中文字符）
# ============================================================

def test_en_render_catalog_segment_overflow_line_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    buckets = [_bucket("a", "A"), _bucket("b", "B"), _bucket("c", "C")]

    lines = render_catalog_segment(buckets, budget_tokens=0, overflow_hint="see dream()")

    assert len(lines) == 1
    assert not _CJK_RE.search(lines[0])
    assert "3 more not shown" in lines[0]
    assert "see dream()" in lines[0]


def test_en_render_file_zone_summary_empty_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")

    out = render_file_zone_summary([])

    assert not _CJK_RE.search(out)
    assert out == "The file zone is empty. Use file_save to store the first file."


@pytest.mark.asyncio
async def test_en_wake_impl_section_titles_have_no_chinese_characters(
    test_config, fake_embedding_engine, monkeypatch
):
    monkeypatch.setenv("OMBRE_LANG", "en")
    import server as srv
    from bucket_manager import BucketManager

    mgr = BucketManager(test_config, embedding_engine=fake_embedding_engine)
    monkeypatch.setattr(srv, "bucket_mgr", mgr)
    monkeypatch.setattr(srv, "dream_engine", None)

    text = await srv._wake_impl(48)

    assert "## 1. Self (I, last 5 entries)" in text
    assert "## 2. Core Memory" in text
    assert "## 6. Inheritance Zone" in text
    header = text.split("\n\n", 1)[0]
    assert "# Waking Briefing" in header
    # 只断言 header 无中文——"自我"段正文来自 tools/i/core.py（没有自我记录
    # 时返回"还没有任何自我认知记录。"），那个工具不在本工单范围内，仍是
    # 中文，见任务6清单，不应算进这条断言。
    assert not _CJK_RE.search(header)


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

def test_zh_default_render_catalog_segment_overflow_line_unaffected(monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    buckets = [_bucket("a", "桶A"), _bucket("b", "桶B")]

    lines = render_catalog_segment(buckets, budget_tokens=0, overflow_hint="用 X 查看")

    assert "还有 2 条未展示" in lines[0]


@pytest.mark.asyncio
async def test_zh_default_wake_impl_section_titles_unaffected(
    test_config, fake_embedding_engine, monkeypatch
):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    import server as srv
    from bucket_manager import BucketManager

    mgr = BucketManager(test_config, embedding_engine=fake_embedding_engine)
    monkeypatch.setattr(srv, "bucket_mgr", mgr)
    monkeypatch.setattr(srv, "dream_engine", None)

    text = await srv._wake_impl(48)

    assert "## 一、自我(I 最近 5 条)" in text
    assert "# 醒来简报" in text
