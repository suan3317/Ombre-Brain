"""工单 D-4 五期（收尾的收尾）：errors.py 的 format_error 错误信封、
dream_engine.py 的 dream_book_keep 错误详情、tools/plan/core.py、
server.py 的 file_list/file_delete，同规则双语。测试各 1 条。
"""
import re

import pytest

_CJK_RE = re.compile(r"[一-鿿]")


def test_en_format_error_envelope_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    from errors import format_error

    out = format_error("OB-E004", "boom", include_logs=False)

    assert not _CJK_RE.search(out)
    assert "MCP tool execution exception" in out
    assert "Detail: boom" in out
    assert "Suggestion:" in out


def test_en_dream_book_keep_not_found_has_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    from dream_engine import dream_book_keep

    result = dream_book_keep(str(tmp_path), "2026-01-01")

    assert result["ok"] is False
    assert not _CJK_RE.search(result["error"])
    assert result["error"] == "No dream recorded for the night of 2026-01-01."


@pytest.mark.asyncio
async def test_en_plan_create_empty_content_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    import tools._runtime as rt
    from tools.plan.core import plan_create

    class NoopDecay:
        async def ensure_started(self):
            return None

    monkeypatch.setattr(rt, "decay_engine", NoopDecay(), raising=False)

    out = await plan_create(content="")

    assert not _CJK_RE.search(out)
    assert out == "Content is empty, nothing to register as a plan."


@pytest.mark.asyncio
async def test_en_file_list_empty_has_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    import server as srv

    monkeypatch.setattr(srv, "config", {"buckets_dir": str(tmp_path)}, raising=False)

    out = await srv._fz_list("")

    assert not _CJK_RE.search(out)
    assert out == "The file zone is empty. Use file_save to store the first file."
