"""S-4 故障二(2026-08-25):PINBOARD_URL/PINBOARD_TOKEN 加进 Dashboard 的
env-config 白名单——验收点:POST 能写(TOKEN 走 sensitive 分支,GET 回来
脱敏;URL 不脱敏),改完立刻反映到 os.environ(tools/pinboard.py 每次调用
现读 os.environ,不用重启)。
"""
import json
import os

import pytest

import web.config_api as config_api


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

    async def json(self):
        return self._body


class NoBodyRequest:
    pass


@pytest.mark.asyncio
async def test_env_config_can_set_pinboard_url_and_token(monkeypatch, tmp_path):
    monkeypatch.delenv("PINBOARD_URL", raising=False)
    monkeypatch.delenv("PINBOARD_TOKEN", raising=False)
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(config_api.sh, "_project_env_path", lambda: str(tmp_path / ".env"))
    monkeypatch.setattr(config_api.sh, "config", {})

    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/env-config")](
        JsonRequest(
            {
                "updates": {
                    "PINBOARD_URL": "https://chatnest.example/broadcast-mcp",
                    "PINBOARD_TOKEN": "service-token-abc123",
                }
            }
        )
    )
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert set(payload["updated"]) == {"PINBOARD_URL", "PINBOARD_TOKEN"}
    assert os.environ["PINBOARD_URL"] == "https://chatnest.example/broadcast-mcp"
    assert os.environ["PINBOARD_TOKEN"] == "service-token-abc123"


@pytest.mark.asyncio
async def test_env_config_get_masks_token_not_url(monkeypatch, tmp_path):
    monkeypatch.setenv("PINBOARD_URL", "https://chatnest.example/broadcast-mcp")
    monkeypatch.setenv("PINBOARD_TOKEN", "service-token-abcdefghijklmnop")
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(config_api.sh, "_project_env_path", lambda: str(tmp_path / ".env"))
    monkeypatch.setattr(config_api.sh, "config", {})

    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("GET", "/api/env-config")](NoBodyRequest())
    payload = json.loads(response.body)

    assert payload["fields"]["PINBOARD_URL"]["value"] == "https://chatnest.example/broadcast-mcp"
    assert payload["fields"]["PINBOARD_TOKEN"]["value"] != "service-token-abcdefghijklmnop"
    assert payload["fields"]["PINBOARD_TOKEN"]["is_set"] is True
