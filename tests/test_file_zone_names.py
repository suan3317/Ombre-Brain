"""工单 FZ-1(2026-11-19):文件区文件名放宽 + 撞名另存 + 草稿兜底。

- 中文标点、全角符号、全角空格存得进、原名读得回;
- : ? 等 Windows 不认的字符换成 _,回执写明;
- 超长名按 UTF-8 200 字节截主名、保后缀;
- ../x.md、/x.md、a/b/c.md 修名字后都落在 files/ 里;
- 写入失败 → 内容进 files/草稿/,报错里有路径;
- 追加 1:改名后撞上不同的文件 → 另存 _2,回执喊一声;同一原名再存 → 覆盖;
- Dashboard 上传走同一套规则。
"""
import os

import pytest

_NAMES = [
    "雨（三）.md",
    "雨(三).md",
    "《雨》.md",
    "「雨」，又下了！.md",
    "a·b、c.md",
    "雨　天 晴.md",
]


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    import server as srv_mod

    monkeypatch.setattr(srv_mod, "config", {"buckets_dir": str(tmp_path)}, raising=False)
    return srv_mod


def _files(tmp_path):
    return os.path.join(str(tmp_path), "files")


def _all_paths(base):
    out = []
    for r, _, fns in os.walk(base):
        for fn in fns:
            out.append(os.path.relpath(os.path.join(r, fn), base).replace("\\", "/"))
    return sorted(out)


# ------------------------------------------------------------------ 放宽

@pytest.mark.asyncio
@pytest.mark.parametrize("name", _NAMES)
async def test_chinese_punct_and_wide_space_saved_as_is_and_read_back(srv, tmp_path, name):
    saved = await srv._fz_save(name, f"正文 {name}", append=False)
    assert saved.startswith(f"已创建 files/{name} ("), saved
    assert "原名" not in saved
    assert os.path.isfile(os.path.join(_files(tmp_path), name))
    read = await srv._fz_read(name, 0)
    assert f"正文 {name}" in read
    assert not os.path.exists(os.path.join(_files(tmp_path), ".renamed.json"))


@pytest.mark.asyncio
async def test_windows_illegal_chars_replaced_and_reported(srv, tmp_path):
    saved = await srv._fz_save("a:b?.md", "内容", append=False)
    assert "已创建 files/a_b_.md" in saved
    assert "原名 a:b?.md" in saved
    assert ": ?" in saved and "换成了 _" in saved
    assert os.path.isfile(os.path.join(_files(tmp_path), "a_b_.md"))
    # 原名、实际名都读得回
    assert "内容" in await srv._fz_read("a:b?.md", 0)
    assert "内容" in await srv._fz_read("a_b_.md", 0)


@pytest.mark.asyncio
async def test_long_chinese_name_truncated_keeps_suffix(srv, tmp_path):
    name = "长" * 100 + ".md"
    saved = await srv._fz_save(name, "长文", append=False)
    actual = [p for p in _all_paths(_files(tmp_path)) if not p.startswith(".")]
    assert len(actual) == 1
    rel = actual[0]
    assert rel.endswith(".md")
    assert len(rel.encode("utf-8")) <= 200
    assert rel == "长" * 65 + ".md"
    assert f"files/{rel}" in saved and "截掉了" in saved
    assert "长文" in await srv._fz_read(name, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,expected", [
    ("../x.md", "_/x.md"),
    ("/x.md", "x.md"),
    ("a/b/c.md", "a/b_c.md"),
])
async def test_path_cases_repaired_inside_files(srv, tmp_path, name, expected):
    saved = await srv._fz_save(name, "里面", append=False)
    assert f"已创建 files/{expected}" in saved, saved
    assert f"原名 {name}" in saved
    assert os.path.isfile(os.path.join(_files(tmp_path), *expected.split("/")))
    # files/ 外面什么都没多:tmp_path 下只有 files/ 这一项
    assert sorted(os.listdir(str(tmp_path))) == ["files"]
    assert "里面" in await srv._fz_read(name, 0)


@pytest.mark.asyncio
async def test_en_escape_name_now_saved_inside_files(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    import server as srv_mod

    monkeypatch.setattr(srv_mod, "config", {"buckets_dir": str(tmp_path)}, raising=False)
    saved = await srv_mod._fz_save("a:b.md", "x", append=False)
    assert "created files/a_b.md (original name a:b.md;" in saved


@pytest.mark.asyncio
async def test_delete_by_original_name_and_index_cleared(srv, tmp_path):
    await srv._fz_save("a:b.md", "x", append=False)
    out = await srv._fz_delete("a:b.md")
    assert out.startswith("已删除 files/a_b.md")
    assert not os.path.exists(os.path.join(_files(tmp_path), "a_b.md"))
    import file_zone

    assert file_zone.load_index(_files(tmp_path)) == {}


def test_normalize_is_idempotent():
    import file_zone

    for name in _NAMES + ["a:b?.md", "../x.md", "/x.md", "a/b/c.md", ".hidden", "x. .",
                          "长" * 100 + ".md", "a\x07b.md", "a\\b.md", "../../..", "🌧️" * 80]:
        rel = file_zone.normalize_rel(name)
        assert file_zone.normalize_rel(rel) == rel
        for seg in rel.split("/"):
            assert len(seg.encode("utf-8")) <= 200
            assert not seg.startswith(".")
            assert not seg.endswith((" ", "."))


# ------------------------------------------------------------------ 草稿兜底

@pytest.mark.asyncio
async def test_write_failure_goes_to_draft(srv, tmp_path):
    os.makedirs(os.path.join(_files(tmp_path), "已有目录"))
    content = "整篇长稿\n" * 50
    out = await srv._fz_save("已有目录", content, append=False)
    assert out.startswith("OB-FZ04 写入 files/已有目录 失败")
    drafts = os.listdir(os.path.join(_files(tmp_path), "草稿"))
    assert len(drafts) == 1
    rel = f"草稿/{drafts[0]}"
    assert f"files/{rel}" in out
    with open(os.path.join(_files(tmp_path), "草稿", drafts[0]), encoding="utf-8") as f:
        assert f.read() == content
    import re

    assert re.fullmatch(r"\d{8}-\d{6}\.md", drafts[0])


@pytest.mark.asyncio
async def test_empty_name_goes_to_draft(srv, tmp_path):
    out = await srv._fz_save(" .. / ", "没名字的稿", append=False)
    # ".." 段换成 "_" → 名字是 "_",能正常存;真正什么都不剩的才进草稿
    assert out.startswith("已创建 files/_")
    out2 = await srv._fz_save("   ", "没名字的稿", append=False)
    assert out2.startswith("OB-FZ04")
    assert "files/草稿/" in out2


@pytest.mark.asyncio
async def test_over_2mb_still_refused_without_draft(srv, tmp_path):
    out = await srv._fz_save("大.md", "x" * (2 * 1024 * 1024 + 1), append=False)
    assert out.startswith("OB-FZ01 内容超过 2MB 上限")
    assert not os.path.exists(os.path.join(_files(tmp_path), "草稿"))


# ------------------------------------------------------------------ 追加 1:撞名

@pytest.mark.asyncio
async def test_two_different_names_mapping_to_same_actual_both_kept(srv, tmp_path):
    first = await srv._fz_save("a:b.md", "第一份", append=False)
    assert "已创建 files/a_b.md" in first
    second = await srv._fz_save("a?b.md", "第二份", append=False)
    assert "files/a_b.md 已经有一个不同的文件,这篇另存为 files/a_b_2.md" in second
    assert "第一份" in await srv._fz_read("a:b.md", 0)
    assert "第二份" in await srv._fz_read("a?b.md", 0)
    assert "第二份" in await srv._fz_read("a_b_2.md", 0)


@pytest.mark.asyncio
async def test_renamed_collides_with_file_that_really_has_that_name(srv, tmp_path):
    await srv._fz_save("a_b.md", "本名", append=False)
    out = await srv._fz_save("a:b.md", "改名来的", append=False)
    assert "files/a_b.md 已经有一个不同的文件,这篇另存为 files/a_b_2.md" in out
    assert "本名" in await srv._fz_read("a_b.md", 0)
    assert "改名来的" in await srv._fz_read("a:b.md", 0)


@pytest.mark.asyncio
async def test_same_renamed_original_saved_twice_overwrites(srv, tmp_path):
    await srv._fz_save("a:b.md", "旧", append=False)
    out = await srv._fz_save("a:b.md", "新", append=False)
    assert out.startswith("已覆盖 files/a_b.md")
    assert "已经有一个不同的文件" not in out
    assert not os.path.exists(os.path.join(_files(tmp_path), "a_b_2.md"))
    read = await srv._fz_read("a:b.md", 0)
    assert "新" in read and "旧" not in read
    # append 照常追加
    out3 = await srv._fz_save("a:b.md", "再加", append=True)
    assert out3.startswith("已追加到 files/a_b.md")


@pytest.mark.asyncio
async def test_two_long_names_same_first_200_bytes(srv, tmp_path):
    base = "长" * 80
    n1, n2 = base + "上.md", base + "下.md"
    await srv._fz_save(n1, "上篇", append=False)
    out = await srv._fz_save(n2, "下篇", append=False)
    t1 = "长" * 65 + ".md"
    assert f"files/{t1} 已经有一个不同的文件,这篇另存为 files/" in out
    rels = [p for p in _all_paths(_files(tmp_path)) if not p.startswith(".")]
    assert len(rels) == 2
    assert all(len(r.encode("utf-8")) <= 200 and r.endswith(".md") for r in rels)
    assert "上篇" in await srv._fz_read(n1, 0)
    assert "下篇" in await srv._fz_read(n2, 0)
    # 同一个长名再存一次是覆盖
    again = await srv._fz_save(n1, "上篇改", append=False)
    assert again.startswith(f"已覆盖 files/{t1}")


@pytest.mark.asyncio
async def test_unrenamed_name_still_plain_overwrite(srv, tmp_path):
    await srv._fz_save("日记.md", "一", append=False)
    out = await srv._fz_save("日记.md", "二", append=False)
    assert out.startswith("已覆盖 files/日记.md (")


def test_index_hidden_from_listing(srv, tmp_path):
    import asyncio

    asyncio.run(srv._fz_save("a:b.md", "x", append=False))
    assert os.path.isfile(os.path.join(_files(tmp_path), ".renamed.json"))
    listing = asyncio.run(srv._fz_list(""))
    assert ".renamed" not in listing
    assert "a_b.md" in listing


# ------------------------------------------------------------------ Dashboard 同一套规则

class _FakeMCP:
    def __init__(self):
        self.routes = []

    def custom_route(self, path, methods):
        def deco(fn):
            from starlette.routing import Route

            self.routes.append(Route(path, fn, methods=methods))
            return fn
        return deco


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    import web._shared as sh
    from web import files_api

    monkeypatch.setattr(sh, "config", {"buckets_dir": str(tmp_path)}, raising=False)
    monkeypatch.setattr(sh, "_require_auth", lambda request: None)
    fake = _FakeMCP()
    files_api.register(fake)
    with TestClient(Starlette(routes=fake.routes)) as client:
        yield client


@pytest.mark.parametrize("name,expected", [
    ("雨（三）.md", "雨（三）.md"),
    ("「雨」，又下了！.md", "「雨」，又下了！.md"),
    ("a:b?.md", "a_b_.md"),
    ("../x.md", "_/x.md"),
    ("a/b/c.md", "a/b_c.md"),
    ("长" * 100 + ".md", "长" * 65 + ".md"),
])
def test_dashboard_upload_same_rules_as_mcp(web_client, tmp_path, name, expected):
    import file_zone

    assert file_zone.normalize_rel(name) == expected
    r = web_client.put("/api/files/raw", params={"name": name}, content="网页传的".encode("utf-8"))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["name"] == expected
    assert j["renamed"] == (name != expected)
    d = web_client.get("/api/files/download", params={"name": name})
    assert d.status_code == 200
    assert d.content.decode("utf-8") == "网页传的"


def test_dashboard_and_mcp_share_index(web_client, srv, tmp_path):
    import asyncio

    r = web_client.put("/api/files/raw", params={"name": "a:b.md"}, content=b"web")
    assert r.json()["name"] == "a_b.md"
    out = asyncio.run(srv._fz_save("a?b.md", "mcp", append=False))
    assert "另存为 files/a_b_2.md" in out
    r2 = web_client.put("/api/files/raw", params={"name": "a:b.md"}, content=b"web2")
    assert r2.json()["name"] == "a_b.md" and r2.json()["collided_with"] is None
    dl = web_client.post("/api/files/delete", params={"name": "a?b.md"})
    assert dl.json()["deleted"] == "a_b_2.md"
