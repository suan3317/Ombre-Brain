"""U-1 第 3 条：信件时间锁（上游 79227b3 / 4033f15 / 499e839 手工移植，按我们的 API 改写）。"""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.plan.core import (
    PERMANENT_UNLOCK_DATE,
    letter_lock_state,
    letter_lock_update,
    letter_read,
    letter_write,
    normalize_unlock_date,
)
from web import letters


class DisabledEmbedding:
    enabled = False


class RecordingEmbedding:
    enabled = True

    def __init__(self, results):
        self.results = results
        self.allowed = None

    async def search_similar(self, query, top_k=10, allowed_bucket_ids=None):
        self.allowed = set(allowed_bucket_ids or ())
        return [pair for pair in self.results if pair[0] in self.allowed]


def install_runtime(bucket_mgr, embedding=None):
    rt.bucket_mgr = bucket_mgr
    rt.embedding_engine = embedding or DisabledEmbedding()
    rt.logger = MagicMock()


def created_id(result):
    assert result.startswith("💌letter→"), result
    return result.split("→", 1)[1].split(" ", 1)[0]


def future_iso(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@pytest.mark.asyncio
async def test_old_and_none_letters_remain_readable_and_proxy_write_compatible(bucket_mgr):
    install_runtime(bucket_mgr)
    old = await bucket_mgr.create(content="historical body", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(old, author="user", title="historical title")
    result = await letter_write(author="user", content="proxy body", lock_type="none")
    output = await letter_read(limit=10)

    assert "historical title" in output
    assert "historical body" in output
    assert "proxy body" in output
    assert created_id(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_type", ["timed", "permanent"])
async def test_mcp_creates_locked_letter_owned_by_ai_without_echoing_content(
    bucket_mgr, monkeypatch, lock_type
):
    monkeypatch.setenv("AI_NAME", "Flint")
    install_runtime(bucket_mgr)
    unlock = future_iso()
    result = await letter_write(
        author="ai",
        content="secret locked body",
        title="secret locked title",
        lock_type=lock_type,
        unlock_date=unlock if lock_type == "timed" else "",
    )
    bucket = await bucket_mgr.get(created_id(result))

    assert "secret" not in result
    assert "🔒" in result
    assert bucket["metadata"]["locked_by"] == "ai"
    assert bucket["metadata"]["writer_name"] == "Flint"
    assert bucket["metadata"]["lock_type"] == lock_type
    assert bucket["metadata"]["unlock_date"] == (
        PERMANENT_UNLOCK_DATE if lock_type == "permanent" else unlock
    )


@pytest.mark.asyncio
async def test_locked_proxy_write_is_rejected_but_does_not_echo_body(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "Flint")
    install_runtime(bucket_mgr)
    secret = "never echo this body"
    result = await letter_write(author="user", content=secret, lock_type="permanent")
    assert "不能替" in result
    assert secret not in result


@pytest.mark.asyncio
async def test_missing_ai_name_falls_back_to_author_label_instead_of_rejecting(bucket_mgr, monkeypatch):
    monkeypatch.delenv("AI_NAME", raising=False)
    install_runtime(bucket_mgr)
    result = await letter_write(author="ai", content="locked", lock_type="permanent")
    bucket = await bucket_mgr.get(created_id(result))
    assert bucket["metadata"]["lock_type"] == "permanent"
    assert bucket["metadata"]["writer_name"]


@pytest.mark.asyncio
async def test_owner_reads_locked_full_text_and_other_side_gets_only_metadata(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "Flint")
    install_runtime(bucket_mgr)
    result = await letter_write(
        author="ai", content="hidden body", title="hidden title", lock_type="permanent"
    )
    bucket = await bucket_mgr.get(created_id(result))
    owner_state = letter_lock_state(bucket, "ai")
    other_state = letter_lock_state(bucket, "human")
    output = await letter_read(limit=10)

    assert owner_state["locked"] is False
    assert other_state["locked"] is True
    assert "hidden title" in output and "hidden body" in output


@pytest.mark.asyncio
async def test_ai_sees_only_an_existence_line_for_a_human_locked_letter(bucket_mgr):
    install_runtime(bucket_mgr)
    letter_id = await bucket_mgr.create(content="her hidden body", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(
        letter_id,
        author="user",
        writer_name="Silvia",
        title="her hidden title",
        lock_type="timed",
        unlock_date="2099-01-01T00:00:00-08:00",
        locked_by="human",
    )
    output = await letter_read(limit=10)
    assert letter_id in output
    assert "Silvia" in output
    assert "锁到 2099-01-01" in output
    assert "her hidden body" not in output
    assert "her hidden title" not in output


@pytest.mark.asyncio
async def test_lock_owner_can_change_every_supported_transition_without_editing_original(
    bucket_mgr, monkeypatch
):
    monkeypatch.setenv("AI_NAME", "Flint")
    install_runtime(bucket_mgr)
    result = await letter_write(
        author="ai", content="immutable body", title="immutable title", lock_type="permanent"
    )
    letter_id = created_id(result)
    original = await bucket_mgr.get(letter_id)
    future = future_iso(3)

    assert "🔒" in await letter_lock_update(letter_id, "timed", future)
    assert "🔒" in await letter_lock_update(letter_id, "permanent")
    assert "🔓" in await letter_lock_update(letter_id, "none")
    assert (await bucket_mgr.get(letter_id))["metadata"].get("unlock_date") is None
    assert "🔒" in await letter_lock_update(letter_id, "timed", future)
    current = await bucket_mgr.get(letter_id)
    assert current["content"] == original["content"]
    for field in ("title", "author", "created"):
        assert current["metadata"].get(field) == original["metadata"].get(field)


@pytest.mark.asyncio
async def test_non_owner_cannot_change_lock(bucket_mgr):
    install_runtime(bucket_mgr)
    letter_id = await bucket_mgr.create(content="body", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(
        letter_id,
        author="user",
        writer_name="Silvia",
        lock_type="permanent",
        unlock_date=PERMANENT_UNLOCK_DATE,
        locked_by="human",
    )
    result = await letter_lock_update(letter_id, "none", caller_side="ai")
    assert "只有" in result
    assert (await bucket_mgr.get(letter_id))["metadata"]["lock_type"] == "permanent"


@pytest.mark.asyncio
async def test_pre_lock_letters_cannot_be_locked_afterwards(bucket_mgr):
    install_runtime(bucket_mgr)
    letter_id = await bucket_mgr.create(content="body", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(letter_id, author="Flint")
    result = await letter_lock_update(letter_id, "permanent", caller_side="ai")
    assert "锁主" in result
    assert (await bucket_mgr.get(letter_id))["metadata"].get("lock_type") in (None, "none")


def test_timed_validation_future_los_angeles_and_permanent_is_canonical():
    # 不带时区 → 洛杉矶（-08:00 / -07:00），存显式偏移
    stored = normalize_unlock_date("timed", "2099-01-01T09:00:00")
    assert stored.startswith("2099-01-01T09:00:00-0")
    # 只给日期 → 当天 00:00 洛杉矶
    stored_date = normalize_unlock_date("timed", "2099-01-01")
    assert stored_date.startswith("2099-01-01T00:00:00-0")
    # 带时区照原样
    assert normalize_unlock_date("timed", "2099-01-01T00:00:00+08:00") == "2099-01-01T00:00:00+08:00"
    with pytest.raises(ValueError, match="未来|future"):
        normalize_unlock_date("timed", "2020-01-01T00:00:00+08:00")
    with pytest.raises(ValueError):
        normalize_unlock_date("timed", "")
    with pytest.raises(ValueError):
        normalize_unlock_date("timed", "明年")
    assert normalize_unlock_date("permanent", "anything") == PERMANENT_UNLOCK_DATE
    assert normalize_unlock_date("none", "2099-01-01") is None


@pytest.mark.asyncio
async def test_expired_timed_lock_is_lazily_readable_and_normalized(bucket_mgr):
    install_runtime(bucket_mgr)
    letter_id = await bucket_mgr.create(content="released body", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(
        letter_id,
        author="user",
        writer_name="Silvia",
        lock_type="timed",
        unlock_date="2020-01-01T00:00:00+08:00",
        locked_by="human",
    )
    output = await letter_read(limit=10)
    current = await bucket_mgr.get(letter_id)
    assert "released body" in output
    assert current["metadata"]["lock_type"] == "none"
    assert current["metadata"].get("unlock_date") is None


@pytest.mark.asyncio
async def test_search_excludes_hidden_letter_before_embedding_ranking(bucket_mgr):
    hidden = await bucket_mgr.create(content="unique hidden nebula", bucket_type="letter", domain=["letter"])
    visible = await bucket_mgr.create(content="visible garden", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(
        hidden,
        author="user",
        writer_name="Silvia",
        title="unique hidden constellation",
        lock_type="permanent",
        unlock_date=PERMANENT_UNLOCK_DATE,
        locked_by="human",
    )
    embedding = RecordingEmbedding([(hidden, 0.99), (visible, 0.5)])
    install_runtime(bucket_mgr, embedding)
    result = await letter_read(query="unique hidden nebula", limit=10)

    assert hidden not in embedding.allowed
    assert "unique hidden" not in result
    assert "visible garden" in result


@pytest.mark.asyncio
async def test_keyword_search_does_not_find_hidden_letter_when_embedding_is_disabled(bucket_mgr):
    hidden = await bucket_mgr.create(content="unique hidden nebula", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(
        hidden, author="user", writer_name="Silvia",
        lock_type="permanent", unlock_date=PERMANENT_UNLOCK_DATE, locked_by="human",
    )
    install_runtime(bucket_mgr)
    result = await letter_read(query="nebula", limit=10)
    assert "nebula" not in result
    assert hidden not in result


# ---- Dashboard ----

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
    query_params = {}

    def __init__(self, body, letter_id=""):
        self.body = body
        self.path_params = {"letter_id": letter_id}

    async def json(self):
        return self.body


def _dashboard(monkeypatch, bucket_mgr):
    monkeypatch.setattr(letters.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(letters.sh, "_read_json_object", lambda request: request.json())
    monkeypatch.setattr(letters.sh, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(letters.sh, "dehydrator", MagicMock())
    mcp = FakeMCP()
    letters.register(mcp)
    return mcp


@pytest.mark.asyncio
async def test_dashboard_creates_human_lock_hides_it_from_ai_and_rejects_ai_proxy(
    bucket_mgr, monkeypatch
):
    monkeypatch.setenv("AI_NAME", "Flint")
    mcp = _dashboard(monkeypatch, bucket_mgr)
    create = mcp.routes[("POST", "/api/letter")]

    response = await create(JsonRequest({
        "author": "user", "user_name": "Silvia", "content": "dashboard secret",
        "title": "dashboard title", "lock_type": "timed", "unlock_date": "2099-03-01T09:00",
    }))
    data = json.loads(response.body)
    assert response.status_code == 200, data
    bucket = await bucket_mgr.get(data["id"])
    assert bucket["metadata"]["locked_by"] == "human"
    assert bucket["metadata"]["writer_name"] == "Silvia"
    assert bucket["metadata"]["unlock_date"].startswith("2099-03-01T09:00:00-0")
    assert letter_lock_state(bucket, "ai")["locked"] is True

    install_runtime(bucket_mgr)
    seen_by_ai = await letter_read(limit=10)
    assert "dashboard secret" not in seen_by_ai
    assert "锁到 2099-03-01 09:00" in seen_by_ai

    rejected = await create(JsonRequest({
        "author": "ai", "ai_name": "Flint", "content": "proxy", "lock_type": "permanent",
    }))
    assert rejected.status_code == 400


@pytest.mark.asyncio
async def test_dashboard_list_hides_locked_title_and_body_and_patch_only_changes_lock(
    bucket_mgr, monkeypatch
):
    letter_id = await bucket_mgr.create(content="hidden dashboard body", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(
        letter_id,
        author="Flint",
        writer_name="Flint",
        title="hidden dashboard title",
        lock_type="permanent",
        unlock_date=PERMANENT_UNLOCK_DATE,
        locked_by="ai",
    )
    mcp = _dashboard(monkeypatch, bucket_mgr)
    listed = await mcp.routes[("GET", "/api/letters")](JsonRequest({}))
    item = json.loads(listed.body)["letters"][0]
    assert item["locked"] is True
    assert item["writer_name"] == "Flint"
    assert "title" not in item and "content" not in item

    patch = mcp.routes[("PATCH", "/api/letter/{letter_id}")]
    patched = await patch(JsonRequest({"lock_type": "none"}, letter_id))
    assert patched.status_code == 403
    attempted_edit = await patch(JsonRequest({"content": "overwrite"}, letter_id))
    assert attempted_edit.status_code == 403
    assert (await bucket_mgr.get(letter_id))["content"] == "hidden dashboard body"


@pytest.mark.asyncio
async def test_dashboard_content_edit_is_separate_and_preserves_lock_metadata(
    bucket_mgr, monkeypatch
):
    letter_id = await bucket_mgr.create(content="original body", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(
        letter_id,
        author="user",
        user_name="Silvia",
        writer_name="Silvia",
        title="original title",
        lock_type="permanent",
        unlock_date=PERMANENT_UNLOCK_DATE,
        locked_by="human",
    )
    mcp = _dashboard(monkeypatch, bucket_mgr)
    patch = mcp.routes[("PATCH", "/api/letter/{letter_id}")]

    mixed = await patch(JsonRequest({"content": "mixed", "lock_type": "none"}, letter_id))
    assert mixed.status_code == 400

    edited = await patch(JsonRequest({"content": "edited body", "title": "edited title"}, letter_id))
    assert edited.status_code == 200
    current = await bucket_mgr.get(letter_id)
    assert current["content"] == "edited body"
    assert current["metadata"]["title"] == "edited title"
    assert current["metadata"]["lock_type"] == "permanent"
    assert current["metadata"]["locked_by"] == "human"
    assert current["metadata"]["writer_name"] == "Silvia"

    unlocked = await patch(JsonRequest({"lock_type": "none"}, letter_id))
    assert unlocked.status_code == 200
    assert (await bucket_mgr.get(letter_id))["metadata"]["lock_type"] == "none"

    install_runtime(bucket_mgr)
    assert "edited body" in await letter_read(limit=10)


@pytest.mark.asyncio
async def test_historical_and_unlocked_dashboard_letters_remain_editable(bucket_mgr, monkeypatch):
    historical = await bucket_mgr.create(content="historical body", bucket_type="letter", domain=["letter"])
    unlocked = await bucket_mgr.create(content="unlocked body", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(unlocked, author="user", lock_type="none", locked_by="human")
    mcp = _dashboard(monkeypatch, bucket_mgr)
    patch = mcp.routes[("PATCH", "/api/letter/{letter_id}")]

    assert (await patch(JsonRequest({"content": "historical edited"}, historical))).status_code == 200
    assert (await patch(JsonRequest({"content": "unlocked edited"}, unlocked))).status_code == 200
    assert (await bucket_mgr.get(historical))["content"] == "historical edited"
    assert (await bucket_mgr.get(unlocked))["content"] == "unlocked edited"
    # 老信没有锁主，Dashboard 也不能事后补锁
    assert (await patch(JsonRequest({"lock_type": "permanent"}, historical))).status_code == 403
