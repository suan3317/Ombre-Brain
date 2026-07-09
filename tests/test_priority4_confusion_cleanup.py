import json
from pathlib import Path

import pytest

import web.buckets as buckets_web
import web.import_api as import_api


ROOT = Path(__file__).resolve().parents[1]


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class JsonRequest:
    def __init__(self, body=None, *, path_params=None):
        self._body = body or {}
        self.path_params = path_params or {}
        self.headers = {}
        self.query_params = {}

    async def json(self):
        return self._body


def _json(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_pulse_shows_special_bucket_counts_separately(monkeypatch):
    from tools.anchor import core as anchor_core

    class FakeDecay:
        is_running = True

        async def ensure_started(self):
            return None

    class FakeBucketManager:
        async def get_stats(self):
            return {
                "permanent_count": 1,
                "dynamic_count": 2,
                "archive_count": 3,
                "feel_count": 4,
                "plan_count": 5,
                "letter_count": 6,
                "total_size_kb": 7.0,
            }

        async def list_all(self, include_archive=False):
            return []

    monkeypatch.setattr(anchor_core.rt, "decay_engine", FakeDecay(), raising=False)
    monkeypatch.setattr(anchor_core.rt, "bucket_mgr", FakeBucketManager(), raising=False)
    monkeypatch.setattr(anchor_core.rt, "embedding_engine", None, raising=False)

    result = await anchor_core.pulse()

    assert "feel 桶: 4 条" in result
    assert "plan 桶: 5 条" in result
    assert "letter 桶: 6 封" in result


@pytest.mark.asyncio
async def test_grow_shortpath_explains_hold_style_single_memory(monkeypatch):
    from tools.grow import shortpath

    class FakeLogger:
        def info(self, *_args, **_kwargs):
            pass

    class FakeDehydrator:
        async def analyze(self, _content):
            return {
                "importance": 5,
                "tags": ["短句"],
                "domain": ["测试"],
                "valence": 0.5,
                "arousal": 0.3,
                "suggested_name": "短内容",
            }

    async def fake_merge_or_create(**_kwargs):
        return "bucket-1", False, ""

    async def fake_background(*_args, **_kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(shortpath.rt, "logger", FakeLogger(), raising=False)
    monkeypatch.setattr(shortpath.rt, "dehydrator", FakeDehydrator(), raising=False)
    monkeypatch.setattr(shortpath, "merge_or_create", fake_merge_or_create)
    monkeypatch.setattr(shortpath, "check_plan_resolution", fake_background)
    monkeypatch.setattr(shortpath, "check_duplicate_for", fake_background)
    monkeypatch.setattr(shortpath.asyncio, "create_task", fake_create_task)

    result = await shortpath.grow_shortpath("短句")

    assert "短内容已按 hold 路径保存为单条记忆" in result


@pytest.mark.asyncio
async def test_host_vault_set_returns_restart_required_message(monkeypatch, tmp_path):
    monkeypatch.setattr(import_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_api.sh, "_project_env_path", lambda: str(tmp_path / ".env"))
    monkeypatch.setattr(import_api.sh, "_write_env_var", lambda _key, _value: None)

    mcp = FakeMCP()
    import_api.register(mcp)

    response = await mcp.routes[("POST", "/api/host-vault")](
        JsonRequest({"value": "D:/Vault/Ombre"})
    )
    payload = _json(response)

    assert payload["ok"] is True
    assert payload["restart_required"] is True
    assert "重启" in payload["message"]


@pytest.mark.asyncio
async def test_bucket_resolve_route_returns_trace_aligned_message(monkeypatch):
    class FakeBucketManager:
        def __init__(self):
            self.row = {"id": "b1", "metadata": {"id": "b1", "resolved": False}}
            self.updates = []

        async def get(self, bucket_id):
            return self.row if bucket_id == "b1" else None

        async def update(self, bucket_id, **updates):
            self.updates.append((bucket_id, updates))
            self.row["metadata"].update(updates)
            return True

    bucket_mgr = FakeBucketManager()
    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(buckets_web.sh, "bucket_mgr", bucket_mgr, raising=False)

    mcp = FakeMCP()
    buckets_web.register(mcp)

    response = await mcp.routes[("POST", "/api/bucket/{bucket_id}/resolve")](
        JsonRequest(path_params={"bucket_id": "b1"})
    )
    payload = _json(response)

    assert payload["resolved"] is True
    assert payload["message"] == "已沉底，只在关键词触发时重新浮现"


def test_config_example_marks_wikilink_deprecated_without_active_stanza():
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")

    assert "\nwikilink:" not in text
    assert "wikilink" in text
    assert "deprecated" in text.lower() or "已废弃" in text


def test_dashboard_single_bucket_delete_is_not_labeled_as_hard_delete():
    for rel in ("dashboard.html", "frontend/dashboard.html"):
        text = (ROOT / rel).read_text(encoding="utf-8")

        assert "删除到档案" in text
        assert "这将彻底删除此记忆桶" not in text
        assert "你真的要永久删除吗" not in text
        assert "彻底删除这封信" not in text
        assert "你真的要永久删除这封信吗" not in text
        assert 'title="彻底删除"' not in text
        assert "物理删除，不可恢复" in text
