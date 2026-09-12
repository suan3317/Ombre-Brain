"""工单 D-4 四期：server.py 里 dream_keep / file_save / file_read 三个工具
自己的返回文案英文化（_fz_safe 的校验错误一并覆盖，file_save/file_read
共用）。en 下抽 2 条断言无中文字符，zh 回归 1 条。
"""
import os
import re

import pytest

_CJK_RE = re.compile(r"[一-鿿]")


@pytest.mark.asyncio
async def test_en_dream_keep_not_found_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    import server as srv

    out = await srv._dream_keep_impl("2020-01-01")

    # dream_book_keep() 本身的 error 文本来自 dream_engine.py（不在本轮范围，
    # 仍是中文），这里只断言 server._dream_keep_impl 自己包的前缀是英文
    # （"没留成:"→"Not kept:"），不对整句做无中文断言。
    assert out.startswith("Not kept:")


@pytest.mark.asyncio
async def test_en_dream_keep_success_has_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    import server as srv
    import dream_engine
    import frontmatter as fm

    monkeypatch.setattr(srv, "config", {"buckets_dir": str(tmp_path)}, raising=False)
    date_str = "2026-01-15"
    path = dream_engine.dream_book_path(str(tmp_path), date_str)
    post = fm.Post("a plain english dream body")
    post["id"] = dream_engine.dream_book_id(date_str)
    post["date"] = date_str
    post["keep_status"] = "fresh"
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    out = await srv._dream_keep_impl(date_str)

    assert not _CJK_RE.search(out)
    assert out.startswith("Kept the dream from 2026-01-15")


@pytest.mark.asyncio
async def test_en_file_save_and_read_have_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    import server as srv

    monkeypatch.setattr(srv, "config", {"buckets_dir": str(tmp_path)}, raising=False)

    saved = await srv._fz_save("d4_four_en.md", "hello world", append=False)
    assert not _CJK_RE.search(saved)
    assert "created files/d4_four_en.md" in saved

    read_back = await srv._fz_read("d4_four_en.md", 0)
    assert not _CJK_RE.search(read_back)
    assert "has 11 chars total" in read_back


@pytest.mark.asyncio
async def test_en_fz_safe_invalid_filename_has_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    import server as srv

    monkeypatch.setattr(srv, "config", {"buckets_dir": str(tmp_path)}, raising=False)

    with pytest.raises(ValueError) as exc_info:
        srv._fz_safe("../escape.md")

    assert not _CJK_RE.search(str(exc_info.value))
    assert "Invalid filename" in str(exc_info.value)


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

@pytest.mark.asyncio
async def test_zh_default_file_save_unaffected(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    import server as srv

    monkeypatch.setattr(srv, "config", {"buckets_dir": str(tmp_path)}, raising=False)

    saved = await srv._fz_save("d4_four_zh.md", "你好", append=False)

    assert saved.startswith("已创建 files/d4_four_zh.md")
