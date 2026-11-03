"""A-1 相册（第 1 步）：存储/索引（album.py）、REST（web/album.py）、
MCP 工具（tools/album.py）、github_sync 的索引例外、server.py 三个工具的注册。"""
import io
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import album  # noqa: E402
from tools import _runtime as rt  # noqa: E402
from tools import album as tool_album  # noqa: E402
from web import _shared as sh  # noqa: E402
from web import album as web_album  # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _png(w=1600, h=900, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_exif(w=400, h=300) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (w, h), (10, 120, 200))
    exif = img.getexif()
    exif[36867] = "2025:12:24 18:30:00"  # DateTimeOriginal
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


@pytest.fixture
def buckets(tmp_path):
    return str(tmp_path / "buckets")


# ============================================================ 核心存储

def test_add_image_writes_file_and_index(buckets):
    e = album.add_entry(buckets, _png(), title="第一张", note="她发的", tags="猫, 窗台", added_by="user")
    assert re.match(r"^[0-9a-f]{12}$", e["id"])
    assert e["file"].endswith(f"/{e['id']}.png") and e["mime"] == "image/png"
    assert (e["width"], e["height"]) == (1600, 900)
    assert e["tags"] == ["猫", "窗台"] and e["added_by"] == "user"
    assert os.path.isfile(album.file_path(buckets, e))
    lines = open(album.index_path(buckets), encoding="utf-8").read().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == e["id"]
    # 带时区偏移的 ISO 时间
    assert re.search(r"[+-]\d\d:\d\d$", e["added_at"])


def test_note_only_entry_and_empty_rejected(buckets):
    e = album.add_entry(buckets, None, title="只有字", note="官端存不了图", added_by="ai")
    assert e["file"] == "" and e["added_by"] == "ai"
    with pytest.raises(album.AlbumError) as ei:
        album.add_entry(buckets, None)
    assert ei.value.code == "OB-AL01"


def test_same_image_is_not_stored_twice(buckets):
    data = _png()
    first = album.add_entry(buckets, data, title="A")
    second = album.add_entry(buckets, data, title="B")
    assert second["id"] == first["id"] and second["duplicate"] is True
    rows, total = album.list_entries(buckets)
    assert total == 1


def test_non_image_and_oversize_rejected(buckets):
    with pytest.raises(album.AlbumError) as ei:
        album.add_entry(buckets, b"not an image at all")
    assert ei.value.code == "OB-AL02"
    with pytest.raises(album.AlbumError) as ei:
        album.add_entry(buckets, b"\0" * (album.MAX_IMAGE_BYTES + 1))
    assert ei.value.code == "OB-AL03"
    assert not os.path.exists(album.index_path(buckets))


def test_exif_taken_at_is_recorded(buckets):
    e = album.add_entry(buckets, _jpeg_with_exif())
    assert e["taken_at"] == "2025-12-24T18:30:00"
    assert e["mime"] == "image/jpeg" and e["file"].endswith(".jpg")


def test_list_orders_filters_and_pages(buckets, monkeypatch):
    stamps = iter([f"2026-0{m}-01T10:00:00-08:00" for m in (1, 2, 3)])
    monkeypatch.setattr(album, "_now_iso", lambda: next(stamps))
    a = album.add_entry(buckets, None, title="一月", note="x")
    b = album.add_entry(buckets, None, title="二月", tags="猫")
    c = album.add_entry(buckets, None, title="三月", note="猫 在窗台")
    rows, total = album.list_entries(buckets, order="asc")
    assert [r["id"] for r in rows] == [a["id"], b["id"], c["id"]] and total == 3
    rows, _ = album.list_entries(buckets, order="desc", limit=1)
    assert rows[0]["id"] == c["id"]
    rows, total = album.list_entries(buckets, order="asc", limit=2, offset=2)
    assert [r["id"] for r in rows] == [c["id"]] and total == 3
    rows, total = album.list_entries(buckets, query="猫")
    assert {r["id"] for r in rows} == {b["id"], c["id"]}
    rows, total = album.list_entries(buckets, date_from="2026-02-01", date_to="2026-02-28")
    assert [r["id"] for r in rows] == [b["id"]]


def test_update_and_delete(buckets):
    e = album.add_entry(buckets, _png(), title="旧")
    path = album.file_path(buckets, e)
    thumb = album.thumb_bytes(buckets, e)
    assert thumb
    u = album.update_entry(buckets, e["id"], title="新", note="改过", tags=["a", "b"], linked_buckets="b1,b2")
    assert (u["title"], u["note"], u["tags"], u["linked_buckets"]) == ("新", "改过", ["a", "b"], ["b1", "b2"])
    assert u["file"] == e["file"] and u["updated_at"]
    assert album.get_entry(buckets, e["id"])["title"] == "新"
    removed = album.delete_entry(buckets, e["id"])
    assert removed["id"] == e["id"]
    assert not os.path.exists(path)
    assert album.get_entry(buckets, e["id"]) is None
    with pytest.raises(album.AlbumError):
        album.delete_entry(buckets, e["id"])


def test_thumb_and_view_are_bounded_jpegs(buckets):
    e = album.add_entry(buckets, _png(3000, 1000))
    for fn, side in ((album.thumb_bytes, 256), (album.view_bytes, 1024)):
        data = fn(buckets, e)
        img = Image.open(io.BytesIO(data))
        assert img.format == "JPEG"
        assert max(img.size) == side
    # 缓存落在 .thumbs 下，第二次直接命中
    cache_dir = os.path.join(album.album_root(buckets), ".thumbs")
    assert len(os.listdir(cache_dir)) == 2
    assert album.view_bytes(buckets, e) == album.view_bytes(buckets, e)
    note_only = album.add_entry(buckets, None, title="字")
    assert album.thumb_bytes(buckets, note_only) is None


def test_entry_public_hides_paths_and_gives_urls(buckets):
    e = album.add_entry(buckets, _png(), title="t")
    pub = album.entry_public(e)
    assert "file" not in pub and "sha256" not in pub
    assert pub["thumb_url"] == f"/api/album/{e['id']}/thumb"
    assert pub["file_url"] == f"/api/album/{e['id']}/file"
    assert pub["has_image"] is True


# ============================================================ REST

class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def deco(fn):
            for m in methods:
                self.routes[(m, path)] = fn
            return fn
        return deco


class FakeUpload:
    def __init__(self, data: bytes, filename="x.png"):
        self._buf = io.BytesIO(data)
        self.filename = filename

    async def read(self, n=-1):
        return self._buf.read(n)


class FakeRequest:
    def __init__(self, *, path_params=None, query=None, headers=None, form=None, json_body=None):
        self.path_params = path_params or {}
        self.query_params = query or {}
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._form = form
        self._json = json_body

    async def form(self):
        return self._form or {}

    async def json(self):
        return self._json


def _body(resp):
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture
def api(buckets, monkeypatch):
    monkeypatch.setattr(sh, "config", {"buckets_dir": buckets})
    monkeypatch.setattr(sh, "_is_authenticated", lambda request: True)
    mcp = FakeMCP()
    web_album.register(mcp)
    return mcp.routes


def test_rest_upload_list_patch_delete_roundtrip(api):
    upload = api[("POST", "/api/album")]
    list_ = api[("GET", "/api/album")]
    patch = api[("PATCH", "/api/album/{album_id}")]
    delete = api[("DELETE", "/api/album/{album_id}")]
    thumb = api[("GET", "/api/album/{album_id}/thumb")]
    file_ = api[("GET", "/api/album/{album_id}/file")]

    import asyncio
    run = asyncio.run

    r = run(upload(FakeRequest(headers={"content-type": "multipart/form-data; boundary=x"},
                               form={"file": FakeUpload(_png()), "title": "窗台", "note": "下午", "tags": "猫"})))
    assert r.status_code == 201, r.body
    entry = _body(r)["entry"]
    assert entry["added_by"] == "user" and entry["title"] == "窗台"

    # 只有字也能从 dashboard 存（JSON）
    r = run(upload(FakeRequest(headers={"content-type": "application/json"}, json_body={"title": "备忘", "note": "n"})))
    assert r.status_code == 201 and _body(r)["entry"]["has_image"] is False

    # 默认正序、每页 12
    r = run(list_(FakeRequest(query={})))
    page = _body(r)
    assert page["page_size"] == 12 and page["order"] == "asc" and page["total"] == 2 and page["pages"] == 1
    assert page["items"][0]["id"] == entry["id"]
    r = run(list_(FakeRequest(query={"page_size": "1", "page": "2"})))
    page = _body(r)
    assert page["pages"] == 2 and len(page["items"]) == 1 and page["items"][0]["title"] == "备忘"

    r = run(thumb(FakeRequest(path_params={"album_id": entry["id"]})))
    assert r.status_code == 200 and r.media_type == "image/jpeg"
    r = run(file_(FakeRequest(path_params={"album_id": entry["id"]})))
    assert r.status_code == 200 and r.media_type == "image/png"

    r = run(patch(FakeRequest(path_params={"album_id": entry["id"]}, json_body={"title": "改了", "linked_buckets": "abc"})))
    assert r.status_code == 200 and _body(r)["entry"]["title"] == "改了"
    assert _body(r)["entry"]["linked_buckets"] == ["abc"]
    r = run(patch(FakeRequest(path_params={"album_id": entry["id"]}, json_body={"nope": 1})))
    assert r.status_code == 400

    r = run(delete(FakeRequest(path_params={"album_id": entry["id"]})))
    assert r.status_code == 200 and _body(r)["deleted"] == entry["id"]
    r = run(thumb(FakeRequest(path_params={"album_id": entry["id"]})))
    assert r.status_code == 404


def test_rest_requires_dashboard_session(api, monkeypatch):
    import asyncio
    monkeypatch.setattr(sh, "_is_authenticated", lambda request: False)
    monkeypatch.setattr(sh, "_is_setup_needed", lambda: False)
    for key in (("GET", "/api/album"), ("POST", "/api/album"), ("GET", "/api/album/{album_id}/file")):
        r = asyncio.run(api[key](FakeRequest(path_params={"album_id": "0" * 12})))
        assert r.status_code == 401


def test_rest_rejects_bad_image_and_oversize(api):
    import asyncio
    upload = api[("POST", "/api/album")]
    r = asyncio.run(upload(FakeRequest(headers={"content-type": "multipart/form-data"},
                                       form={"file": FakeUpload(b"garbage"), "title": "x"})))
    assert r.status_code == 400 and _body(r)["code"] == "OB-AL02"
    r = asyncio.run(upload(FakeRequest(headers={"content-type": "multipart/form-data"},
                                       form={"file": FakeUpload(b"\0" * (album.MAX_IMAGE_BYTES + 1))})))
    assert r.status_code == 413


def test_ingest_uses_mcp_token_not_dashboard_session(api, monkeypatch):
    import asyncio
    ingest = api[("POST", "/api/album/ingest")]
    monkeypatch.setattr(sh, "_is_authenticated", lambda request: False)
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "static-secret-for-chatnest")

    r = asyncio.run(ingest(FakeRequest(headers={"content-type": "multipart/form-data"},
                                       form={"file": FakeUpload(_png())})))
    assert r.status_code == 401
    r = asyncio.run(ingest(FakeRequest(headers={"content-type": "multipart/form-data",
                                                "authorization": "Bearer wrong"},
                                       form={"file": FakeUpload(_png())})))
    assert r.status_code == 401
    r = asyncio.run(ingest(FakeRequest(headers={"content-type": "multipart/form-data",
                                                "authorization": "Bearer static-secret-for-chatnest"},
                                       form={"file": FakeUpload(_png()), "title": "她发的", "note": "值得留"})))
    assert r.status_code == 201, r.body
    entry = _body(r)["entry"]
    assert entry["added_by"] == "ai" and entry["title"] == "她发的"
    # ingest 必须带图
    r = asyncio.run(ingest(FakeRequest(headers={"content-type": "application/json",
                                                "authorization": "Bearer static-secret-for-chatnest"},
                                       json_body={"title": "no file"})))
    assert r.status_code == 400 and _body(r)["code"] == "OB-AL01"


# ============================================================ MCP 工具

@pytest.fixture
def tools(buckets):
    old = rt.config
    rt.init(config={"buckets_dir": buckets})
    yield
    rt.config = old


@pytest.mark.asyncio
async def test_tool_album_add_is_text_only(tools, buckets):
    out = await tool_album.album_add(title="官端记一笔", note="描述", tags="a,b", linked_buckets="bk1")
    assert out.startswith("📷 album→")
    rows, _ = album.list_entries(buckets)
    assert rows[0]["file"] == "" and rows[0]["added_by"] == "ai" and rows[0]["linked_buckets"] == ["bk1"]


@pytest.mark.asyncio
async def test_tool_album_list_text_and_thumbs(tools, buckets):
    assert "还没有东西" in await tool_album.album_list()
    e = album.add_entry(buckets, _png(), title="窗台", note="n" * 200, tags="猫")
    album.add_entry(buckets, None, title="字")
    text = await tool_album.album_list()
    assert isinstance(text, str) and f"[{e['id']}]" in text and "窗台" in text and "只有字" in text
    assert "…" in text  # note 摘要截断
    blocks = await tool_album.album_list(with_thumbs=True)
    from mcp.types import ImageContent, TextContent
    assert isinstance(blocks, list) and isinstance(blocks[0], TextContent)
    images = [b for b in blocks if isinstance(b, ImageContent)]
    assert len(images) == 1 and images[0].mimeType == "image/jpeg"
    import base64
    img = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
    assert max(img.size) == 256


@pytest.mark.asyncio
async def test_tool_album_view_returns_image_block(tools, buckets):
    e = album.add_entry(buckets, _png(2048, 1024), title="看我", note="细节", added_by="user")
    out = await tool_album.album_view(e["id"])
    from mcp.types import ImageContent, TextContent
    assert isinstance(out, list) and isinstance(out[0], ImageContent) and isinstance(out[1], TextContent)
    import base64
    img = Image.open(io.BytesIO(base64.b64decode(out[0].data)))
    assert img.format == "JPEG" and max(img.size) == 1024
    assert "看我" in out[1].text and "她存的" in out[1].text and "细节" in out[1].text
    note_only = album.add_entry(buckets, None, title="字")
    assert isinstance(await tool_album.album_view(note_only["id"]), str)
    assert "OB-AL04" in await tool_album.album_view("nope")


# ============================================================ 周边

def test_github_sync_includes_index_but_not_images(buckets):
    from github_sync import GitHubSync
    e = album.add_entry(buckets, _png(), title="t")
    os.makedirs(os.path.join(buckets, "dynamic"), exist_ok=True)
    with open(os.path.join(buckets, "dynamic", "b.md"), "w", encoding="utf-8") as f:
        f.write("x")
    files = GitHubSync.__new__(GitHubSync)._collect_files(buckets)
    assert "album/index.jsonl" in files
    assert "dynamic/b.md" in files
    assert not any(k.endswith(".png") for k in files), list(files)
    assert e["file"] not in files


def test_server_registers_three_album_tools_and_web_module():
    src = open(os.path.join(ROOT, "src", "server.py"), encoding="utf-8").read()
    for name in ("album_add", "album_list", "album_view"):
        assert re.search(r"@mcp\.tool\(structured_output=False\)\s*\r?\nasync def " + name + r"\(", src), name
    assert "from tools import album as _t_album" in src
    web_init = open(os.path.join(ROOT, "src", "web", "__init__.py"), encoding="utf-8").read()
    assert '("web.album", album.register)' in web_init
    req = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()
    assert re.search(r"^pillow>=", req, re.M)
    lock = open(os.path.join(ROOT, "requirements.lock.txt"), encoding="utf-8").read()
    assert re.search(r"^pillow==", lock, re.M)


@pytest.mark.asyncio
async def test_server_album_wrapper_keeps_image_blocks_and_formats_errors(buckets, monkeypatch):
    import server as srv  # noqa: F401  (import 会重置 rt.config，所以之后再指到临时目录)
    from mcp.types import ImageContent, TextContent
    monkeypatch.setattr(rt, "config", {"buckets_dir": buckets})

    e = album.add_entry(buckets, _png(), title="包起来")
    out = await srv._album_with_notice(tool_album.album_view(e["id"]), op="album_view", args={"album_id": e["id"]})
    assert isinstance(out, list)
    assert isinstance(out[0], ImageContent) and isinstance(out[1], TextContent)
    assert "包起来" in out[1].text

    text = await srv._album_with_notice(tool_album.album_list(), op="album_list", args={})
    assert isinstance(text, str) and f"[{e['id']}]" in text

    async def boom():
        raise RuntimeError("bad image")

    out = await srv._album_with_notice(boom(), op="album_view", args={})
    assert isinstance(out, str) and "OB-E004" in out and "bad image" in out
