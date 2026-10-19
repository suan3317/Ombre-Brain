"""小单 S-6：缩小「登上 dashboard 即可取走明文 key」的面。

- /api/models 走 sentinel（__use_current__ / __use_current_embed__）时，base_url / api_format
  一律取服务器配置，忽略请求体；请求体带了不同的 base_url → 400，且不发任何外部请求。
- 显式 key + 外来 base_url 行为不变（key 是调用方自己的）。
- /api/env-config POST 改 *_BASE_URL → WARNING 日志 + 响应 base_url_changed。
- /auth/status 已登录时带 active_sessions；登录成功打 INFO 日志含 client key。
"""
import json
import logging
import time

import pytest

import web.config_api as config_api
import web.auth as auth_mod
import web._shared as shared


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn

        return decorator


class JsonRequest:
    def __init__(self, body):
        self._body = body
        self.cookies = {}
        self.client = None

    async def json(self):
        return self._body


class _FakeResp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.text = json.dumps(data)

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeAsyncClient:
    """替身 httpx.AsyncClient：记录出站 URL / headers / params，不真的联网。"""

    calls: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        _FakeAsyncClient.calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        return _FakeResp({"data": [{"id": "m-b"}, {"id": "m-a"}]})


@pytest.fixture
def models_route(monkeypatch):
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(
        config_api.sh,
        "config",
        {
            "dehydration": {
                "api_key": "sk-server-secret",
                "base_url": "https://openrouter.ai/api/v1",
                "api_format": "openai_compat",
            },
            "embedding": {
                "api_key": "sk-embed-secret",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_format": "openai_compat",
            },
        },
    )
    monkeypatch.setattr(config_api.httpx, "AsyncClient", _FakeAsyncClient)
    mcp = FakeMCP()
    config_api.register(mcp)
    return mcp.routes[("POST", "/api/models")]


async def _call(route, body):
    resp = await route(JsonRequest(body))
    return resp.status_code, json.loads(resp.body)


@pytest.mark.asyncio
async def test_sentinel_with_foreign_base_url_is_rejected_without_outbound_call(models_route):
    status, payload = await _call(
        models_route,
        {"api_key": "__use_current__", "base_url": "https://evil.example/v1", "api_format": "openai_compat"},
    )
    assert status == 400
    assert payload["ok"] is False
    assert "不接受自定义 base_url" in payload["error"]
    assert _FakeAsyncClient.calls == [], "被拒绝的请求不得发出任何携带服务器 key 的出站调用"


@pytest.mark.asyncio
async def test_embed_sentinel_with_foreign_base_url_is_rejected(models_route):
    status, payload = await _call(
        models_route,
        {"api_key": "__use_current_embed__", "base_url": "https://evil.example/v1", "api_format": "openai_compat"},
    )
    assert status == 400
    assert "不接受自定义 base_url" in payload["error"]
    assert _FakeAsyncClient.calls == []


@pytest.mark.asyncio
async def test_sentinel_with_empty_base_url_uses_config_values(models_route):
    status, payload = await _call(models_route, {"api_key": "__use_current__", "base_url": ""})
    assert status == 200
    assert payload["ok"] is True
    assert payload["models"] == ["m-a", "m-b"]
    assert len(_FakeAsyncClient.calls) == 1
    call = _FakeAsyncClient.calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/models"
    assert call["headers"]["Authorization"] == "Bearer sk-server-secret"


@pytest.mark.asyncio
async def test_sentinel_with_same_base_url_trailing_slash_is_accepted(models_route):
    status, payload = await _call(
        models_route,
        {"api_key": "__use_current__", "base_url": "https://openrouter.ai/api/v1/"},
    )
    assert status == 200 and payload["ok"] is True
    assert _FakeAsyncClient.calls[0]["url"] == "https://openrouter.ai/api/v1/models"


@pytest.mark.asyncio
async def test_sentinel_ignores_request_api_format(models_route):
    """请求体说 anthropic，配置是 openai_compat → 按配置走 /models，而不是 /v1/models。"""
    status, payload = await _call(
        models_route,
        {"api_key": "__use_current__", "base_url": "", "api_format": "anthropic"},
    )
    assert status == 200 and payload["ok"] is True
    call = _FakeAsyncClient.calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/models"
    assert "x-api-key" not in call["headers"]


@pytest.mark.asyncio
async def test_explicit_key_with_foreign_base_url_unchanged(models_route):
    status, payload = await _call(
        models_route,
        {"api_key": "sk-my-own", "base_url": "https://other.example/v1", "api_format": "openai_compat"},
    )
    assert status == 200 and payload["ok"] is True
    call = _FakeAsyncClient.calls[0]
    assert call["url"] == "https://other.example/v1/models"
    assert call["headers"]["Authorization"] == "Bearer sk-my-own"
    assert "sk-server-secret" not in json.dumps(call)


@pytest.mark.asyncio
async def test_env_config_base_url_change_logs_warning_and_reports(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(config_api.sh, "_project_env_path", lambda: str(tmp_path / ".env"))
    monkeypatch.setattr(
        config_api.sh,
        "config",
        {"dehydration": {"api_key": "sk-x", "base_url": "https://openrouter.ai/api/v1"}},
    )
    import utils as _utils

    monkeypatch.setattr(_utils, "config_file_path", lambda: str(tmp_path / "config.yaml"))
    monkeypatch.setattr(config_api.sh, "dehydrator", type("D", (), {})())

    mcp = FakeMCP()
    config_api.register(mcp)
    route = mcp.routes[("POST", "/api/env-config")]

    with caplog.at_level(logging.WARNING, logger="ombre_brain"):
        resp = await route(
            JsonRequest({"updates": {"OMBRE_COMPRESS_BASE_URL": "https://new-relay.example/v1"}})
        )
    payload = json.loads(resp.body)
    assert payload["ok"] is True
    assert payload["base_url_changed"] == ["OMBRE_COMPRESS_BASE_URL"]
    warn = [r for r in caplog.records if r.levelno == logging.WARNING and "base_url changed" in r.getMessage()]
    assert warn, caplog.text
    msg = warn[0].getMessage()
    assert "https://openrouter.ai/api/v1" in msg and "https://new-relay.example/v1" in msg

    # 同值再存一次：不算变更，不打 WARNING
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ombre_brain"):
        resp = await route(
            JsonRequest({"updates": {"OMBRE_COMPRESS_BASE_URL": "https://new-relay.example/v1"}})
        )
    payload = json.loads(resp.body)
    assert "base_url_changed" not in payload
    assert not [r for r in caplog.records if "base_url changed" in r.getMessage()]


@pytest.mark.asyncio
async def test_auth_status_reports_active_sessions_only_when_authenticated(monkeypatch):
    monkeypatch.setattr(auth_mod, "_ensure_bootstrap_secret", lambda: None)
    monkeypatch.setattr(shared, "_is_setup_needed", lambda: False)
    now = time.time()
    monkeypatch.setattr(shared, "_sessions", {"live-1": now + 100, "live-2": now + 200, "dead": now - 1})
    mcp = FakeMCP()
    auth_mod.register(mcp)
    status_route = mcp.routes[("GET", "/auth/status")]

    monkeypatch.setattr(shared, "_is_authenticated", lambda request: True)
    payload = json.loads((await status_route(JsonRequest({}))).body)
    assert payload["authenticated"] is True
    assert payload["active_sessions"] == 2  # 过期的不算

    monkeypatch.setattr(shared, "_is_authenticated", lambda request: False)
    payload = json.loads((await status_route(JsonRequest({}))).body)
    assert payload["authenticated"] is False
    assert "active_sessions" not in payload


@pytest.mark.asyncio
async def test_login_success_logs_client_key(monkeypatch, caplog):
    monkeypatch.setattr(auth_mod, "_ensure_bootstrap_secret", lambda: None)
    monkeypatch.setattr(shared, "_login_retry_after", lambda request: 0)
    monkeypatch.setattr(shared, "_verify_any_password", lambda pwd: pwd == "right")
    monkeypatch.setattr(shared, "_record_login_success", lambda request: None)
    monkeypatch.setattr(shared, "_create_session", lambda: "tok")
    monkeypatch.setattr(shared, "_set_session_cookie", lambda resp, token, request: None)
    monkeypatch.setattr(shared, "_client_key", lambda request: "203.0.113.9")
    monkeypatch.setattr(shared, "_sessions", {"tok": time.time() + 100})
    mcp = FakeMCP()
    auth_mod.register(mcp)
    login = mcp.routes[("POST", "/auth/login")]

    with caplog.at_level(logging.INFO, logger="ombre_brain"):
        resp = await login(JsonRequest({"password": "right"}))
    assert resp.status_code == 200
    hits = [r for r in caplog.records if r.levelno == logging.INFO and "login success" in r.getMessage()]
    assert hits, caplog.text
    assert "203.0.113.9" in hits[0].getMessage()
