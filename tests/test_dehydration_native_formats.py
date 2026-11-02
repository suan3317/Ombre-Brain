"""
2026-11-02 Rio 首配事故两条回归:
1. /api/test/dehydration 必须按 dehydration.api_format 分流——anthropic 走
   <base>/v1/messages + x-api-key,gemini 走 generateContent,openai_compat 维持
   Bearer + /chat/completions。此前写死 openai 形状,选 anthropic 时测试按钮假阴性。
2. Dehydrator._chat_anthropic 不得下发 temperature(Haiku 5.5 实测 400
   `temperature is deprecated for this model`)。
"""

import json

import httpx
import pytest

import web.config_api as config_api_web
from dehydrator import Dehydrator


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn

        return decorator


def _fake_httpx(monkeypatch, calls, body=None):
    class Response:
        status_code = 200

        def json(self):
            return body or {}

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)


async def _probe(monkeypatch, dehydration_cfg):
    calls = []
    _fake_httpx(monkeypatch, calls)
    monkeypatch.setattr(config_api_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api_web.sh, "config", {"dehydration": dehydration_cfg})
    mcp = FakeMCP()
    config_api_web.register(mcp)
    response = await mcp.routes[("POST", "/api/test/dehydration")](object())
    assert response.status_code == 200
    assert json.loads(response.body)["ok"] is True
    [(url, kwargs)] = calls
    return url, kwargs


@pytest.mark.asyncio
async def test_probe_anthropic_uses_messages_endpoint_and_x_api_key(monkeypatch):
    url, kwargs = await _probe(monkeypatch, {
        "api_key": "sk-ant-probe", "api_format": "anthropic",
        "base_url": "https://api.anthropic.com", "model": "claude-haiku-5-5",
        "temperature": 0.1,
    })
    assert url == "https://api.anthropic.com/v1/messages"
    assert kwargs["headers"]["x-api-key"] == "sk-ant-probe"
    assert kwargs["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["json"]["model"] == "claude-haiku-5-5"
    assert kwargs["json"]["max_tokens"] == 5
    assert "temperature" not in kwargs["json"]


@pytest.mark.asyncio
async def test_probe_anthropic_defaults_base_url_when_blank(monkeypatch):
    url, _ = await _probe(monkeypatch, {
        "api_key": "sk-ant-probe", "api_format": "anthropic", "base_url": "", "model": "claude-haiku-5-5",
    })
    assert url == "https://api.anthropic.com/v1/messages"


@pytest.mark.asyncio
async def test_probe_gemini_uses_generate_content_with_key_param(monkeypatch):
    url, kwargs = await _probe(monkeypatch, {
        "api_key": "AIza-probe", "api_format": "gemini", "base_url": "", "model": "models/gemini-2.5-flash",
    })
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert kwargs["params"] == {"key": "AIza-probe"}
    assert "headers" not in kwargs


@pytest.mark.asyncio
async def test_probe_openai_compat_keeps_bearer_chat_completions(monkeypatch):
    url, kwargs = await _probe(monkeypatch, {
        "api_key": "sk-probe", "api_format": "openai_compat",
        "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
    })
    assert url == "https://api.deepseek.com/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-probe"


@pytest.mark.asyncio
async def test_chat_anthropic_omits_temperature(tmp_path, monkeypatch):
    calls = []
    _fake_httpx(monkeypatch, calls, body={"content": [{"type": "text", "text": "ok"}]})
    d = Dehydrator({
        "dehydration": {
            "api_key": "sk-ant-probe", "api_format": "anthropic",
            "base_url": "https://api.anthropic.com", "model": "claude-haiku-5-5",
            "temperature": 0.1,
        },
        "buckets_dir": str(tmp_path),
    })
    out = await d._chat_anthropic("sys", "user", temperature=0.0, max_tokens=7)
    assert out == "ok"
    [(url, kwargs)] = calls
    assert url == "https://api.anthropic.com/v1/messages"
    assert "temperature" not in kwargs["json"]
    assert kwargs["json"]["max_tokens"] == 7
    assert kwargs["headers"]["x-api-key"] == "sk-ant-probe"
