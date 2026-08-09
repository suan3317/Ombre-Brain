"""
GPT-5.x Chat Completions 用 max_completion_tokens 取代 max_tokens，旧模型仍只认
max_tokens——移植自上游 03a0a1c，覆盖 dehydrator._chat_once 与 config_api 的
API Key 连通性探测两处调用点。
"""

import json
from types import SimpleNamespace

import httpx
import pytest

import web.config_api as config_api_web
from dehydrator import Dehydrator, chat_completion_token_limit


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn

        return decorator


@pytest.mark.parametrize(
    ("model", "expected_limit_key"),
    [
        ("gpt-5", "max_completion_tokens"),
        ("openai/gpt-5-mini", "max_completion_tokens"),
        ("models/gpt-5.1", "max_completion_tokens"),
        ("gpt-50x", "max_tokens"),
        ("deepseek-v4-flash", "max_tokens"),
    ],
)
def test_chat_completion_token_limit_selects_key_by_model_prefix(model, expected_limit_key):
    other_key = "max_tokens" if expected_limit_key == "max_completion_tokens" else "max_completion_tokens"
    result = chat_completion_token_limit(model, 7)

    assert result == {expected_limit_key: 7}
    assert other_key not in result


@pytest.mark.parametrize(
    ("model", "expected_limit_key"),
    [
        ("gpt-5", "max_completion_tokens"),
        ("openai/gpt-5-mini", "max_completion_tokens"),
        ("gpt-50x", "max_tokens"),
        ("deepseek-v4-flash", "max_tokens"),
    ],
)
@pytest.mark.asyncio
async def test_openai_compat_chat_once_uses_model_specific_completion_limit(
    tmp_path, model, expected_limit_key
):
    dehy = Dehydrator(
        {
            "buckets_dir": str(tmp_path),
            "dehydration": {"api_key": "test-key", "model": model},
        }
    )
    captured = {}

    class Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
            )

    dehy.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    result = await dehy._chat_once("system", "user", max_tokens=7)

    other_limit_key = "max_tokens" if expected_limit_key == "max_completion_tokens" else "max_completion_tokens"
    assert result == "OK"
    assert captured[expected_limit_key] == 7
    assert other_limit_key not in captured


@pytest.mark.parametrize(
    ("model", "expected_limit_key"),
    [
        ("azure/gpt-5.1", "max_completion_tokens"),
        ("gpt-4o-mini", "max_tokens"),
    ],
)
@pytest.mark.asyncio
async def test_dehydration_probe_uses_model_specific_completion_limit(
    monkeypatch, model, expected_limit_key
):
    calls = []

    class Response:
        status_code = 200

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
    monkeypatch.setattr(config_api_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        config_api_web.sh,
        "config",
        {
            "dehydration": {
                "api_key": "probe-key",
                "base_url": "https://provider.example/v1",
                "model": model,
            }
        },
    )
    mcp = FakeMCP()
    config_api_web.register(mcp)

    response = await mcp.routes[("POST", "/api/test/dehydration")](object())

    other_limit_key = "max_tokens" if expected_limit_key == "max_completion_tokens" else "max_completion_tokens"
    assert response.status_code == 200
    assert json.loads(response.body)["ok"] is True
    [(url, kwargs)] = calls
    assert url == "https://provider.example/v1/chat/completions"
    assert kwargs["json"][expected_limit_key] == 5
    assert other_limit_key not in kwargs["json"]
