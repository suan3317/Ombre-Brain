"""施工单·工程二：梦境书 Dashboard API（web/dream_book_api.py）。

同一套 dream_engine 模块函数(list_dream_book_entries/dream_book_keep/
dream_book_delete)背后既服务 MCP dream_keep 工具也服务这里的三个路由，
这里只验证路由层的请求/响应粘合是否正确——业务逻辑本身在
tests/test_dream_engine.py 里已经覆盖过。
"""
import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from web import dream_book_api  # noqa: E402
import frontmatter as fm  # noqa: E402
from dream_engine import dream_book_id, dream_book_path  # noqa: E402


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn
        return decorator


class FakeRequest:
    def __init__(self, path_params=None):
        self.path_params = path_params or {}


def payload(response):
    return json.loads(response.body.decode("utf-8"))


def _write_entry(buckets_dir, day, keep_status="fresh", content="正文"):
    date_str = day.isoformat()
    post = fm.Post(content)
    post["id"] = dream_book_id(date_str)
    post["date"] = date_str
    post["tone"] = "daily"
    post["level"] = "full"
    post["sources"] = []
    post["noise"] = 0
    post["read_status"] = "read"
    post["keep_status"] = keep_status
    post["created_at"] = dt.datetime.now().isoformat(timespec="seconds")
    path = dream_book_path(str(buckets_dir), date_str)
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))
    return path


@pytest.fixture
def mcp_and_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(dream_book_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(dream_book_api.sh, "config", {"buckets_dir": str(tmp_path)})
    mcp = FakeMCP()
    dream_book_api.register(mcp)
    return mcp


@pytest.mark.asyncio
async def test_list_returns_entries_sorted_desc(tmp_path, mcp_and_routes):
    _write_entry(tmp_path, dt.date(2026, 7, 1))
    _write_entry(tmp_path, dt.date(2026, 8, 3))

    handler = mcp_and_routes.routes[("GET", "/api/dream-book")]
    response = await handler(FakeRequest())
    body = payload(response)

    assert response.status_code == 200
    assert body["ok"] is True
    assert [e["date"] for e in body["entries"]] == ["2026-08-03", "2026-07-01"]


@pytest.mark.asyncio
async def test_keep_route_marks_kept(tmp_path, mcp_and_routes):
    _write_entry(tmp_path, dt.date(2026, 7, 6))

    handler = mcp_and_routes.routes[("POST", "/api/dream-book/{date}/keep")]
    response = await handler(FakeRequest(path_params={"date": "2026-07-06"}))
    body = payload(response)

    assert response.status_code == 200
    assert body["ok"] is True
    post = fm.load(dream_book_path(str(tmp_path), "2026-07-06"))
    assert post["keep_status"] == "kept"


@pytest.mark.asyncio
async def test_keep_route_404s_cleanly_on_missing_date(tmp_path, mcp_and_routes):
    handler = mcp_and_routes.routes[("POST", "/api/dream-book/{date}/keep")]
    response = await handler(FakeRequest(path_params={"date": "2099-01-01"}))
    body = payload(response)

    assert response.status_code == 400
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_delete_route_removes_entry(tmp_path, mcp_and_routes):
    path = _write_entry(tmp_path, dt.date(2026, 7, 6))

    handler = mcp_and_routes.routes[("DELETE", "/api/dream-book/{date}")]
    response = await handler(FakeRequest(path_params={"date": "2026-07-06"}))
    body = payload(response)

    assert response.status_code == 200
    assert body["ok"] is True
    assert not os.path.isfile(path)


@pytest.mark.asyncio
async def test_delete_route_rejects_burned(tmp_path, mcp_and_routes):
    path = _write_entry(tmp_path, dt.date(2026, 7, 6), keep_status="burned", content="2026-07-06 那晚做了梦，没留下来。")

    handler = mcp_and_routes.routes[("DELETE", "/api/dream-book/{date}")]
    response = await handler(FakeRequest(path_params={"date": "2026-07-06"}))
    body = payload(response)

    assert response.status_code == 400
    assert body["ok"] is False
    assert os.path.isfile(path)


class _FakeEngineWithHeartbeat:
    def __init__(self, last_run_at):
        self.last_run_at = last_run_at


@pytest.mark.asyncio
async def test_list_includes_last_run_at_from_dream_engine(tmp_path, mcp_and_routes, monkeypatch):
    monkeypatch.setattr(dream_book_api.sh, "dream_engine", _FakeEngineWithHeartbeat("2026-08-13T06:00:00-07:00"))

    handler = mcp_and_routes.routes[("GET", "/api/dream-book")]
    response = await handler(FakeRequest())
    body = payload(response)

    assert body["last_run_at"] == "2026-08-13T06:00:00-07:00"


@pytest.mark.asyncio
async def test_list_last_run_at_is_none_when_engine_not_wired(tmp_path, mcp_and_routes):
    handler = mcp_and_routes.routes[("GET", "/api/dream-book")]
    response = await handler(FakeRequest())
    body = payload(response)

    assert body["last_run_at"] is None
