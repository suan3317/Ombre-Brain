"""N-1 #4(2026-11-10):trace 参数收口。

- 对外公布的 schema 只有 bucket_id + 六个显式参数 + changes;
- 旧写法(直接传没公布的显式参数)照样生效;
- changes 写法与显式写法等价;
- changes 里的未知键、与显式参数冲突的值:整次调用拒绝,桶不变。
"""
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.trace.changes import CHANGES_KEYS, PUBLIC_EXPLICIT_PARAMS, merge_changes, slim_schema


class EchoDehydrator:
    async def dehydrate(self, content, meta=None):
        return content


def install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.dehydrator = EchoDehydrator()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


def _text(result) -> str:
    """mcp.call_tool 的返回在不同 mcp 小版本里是 list[Content] 或 (list, dict)。"""
    blocks = result[0] if isinstance(result, tuple) else result
    return "".join(getattr(b, "text", "") for b in blocks)


# ---------------------------------------------------------------- 纯函数

def _explicit(**over):
    base = {k: "" for k in ("name", "domain", "tags", "content", "status", "why_remembered",
                            "meaning_append", "delete_reason", "cited", "event_at",
                            "superseded_by", "supersede_type", "supersede_effective_at")}
    base.update({k: -1 for k in ("valence", "arousal", "importance", "resolved", "pinned",
                                 "digested", "weight", "dont_surface", "seed")})
    base.update({"delete": False, "hard_delete": False, "meaning_replace": None,
                 "media_append": None, "media_replace": None})
    base.update(over)
    return base


def test_public_and_changes_keys_cover_every_trace_field_exactly_once():
    assert len(PUBLIC_EXPLICIT_PARAMS) == 6
    assert len(CHANGES_KEYS) == 21
    assert not set(PUBLIC_EXPLICIT_PARAMS) & set(CHANGES_KEYS)
    assert set(PUBLIC_EXPLICIT_PARAMS) | set(CHANGES_KEYS) == set(_explicit())


def test_merge_none_or_empty_changes_is_identity():
    e = _explicit(importance=7)
    assert merge_changes(e, None) == (e, None)
    assert merge_changes(e, {}) == (e, None)


def test_merge_flattens_changes_into_fields():
    merged, err = merge_changes(_explicit(pinned=1), {"domain": "工作", "seed": 1, "meaning_replace": ["a"]})
    assert err is None
    assert merged["pinned"] == 1 and merged["domain"] == "工作" and merged["seed"] == 1
    assert merged["meaning_replace"] == ["a"]


def test_merge_accepts_json_string_changes():
    merged, err = merge_changes(_explicit(), '{"tags": "a,b"}')
    assert err is None and merged["tags"] == "a,b"


def test_merge_allows_public_params_inside_changes_and_zero_values():
    merged, err = merge_changes(_explicit(), {"pinned": 0, "resolved": 1, "delete": True})
    assert err is None
    assert merged["pinned"] == 0 and merged["resolved"] == 1 and merged["delete"] is True


def test_merge_rejects_unknown_key_and_lists_valid_ones():
    e = _explicit()
    merged, err = merge_changes(e, {"domian": "工作", "seed": 1})
    assert merged == e, "拒绝时不能带出任何半截改动"
    assert "domian" in err and "什么都没改" in err and "domain" in err


def test_merge_rejects_bucket_id_in_changes():
    _, err = merge_changes(_explicit(), {"bucket_id": "abc"})
    assert "bucket_id 不能放进 changes" in err


def test_merge_rejects_conflict_but_allows_same_value():
    e = _explicit(importance=8)
    merged, err = merge_changes(e, {"importance": 3, "domain": "x"})
    assert merged == e and "importance" in err and "不同的值" in err
    merged, err = merge_changes(e, {"importance": 8})
    assert err is None and merged["importance"] == 8


@pytest.mark.parametrize("bad", [["domain", "x"], 5, "not json"])
def test_merge_rejects_non_object_changes(bad):
    e = _explicit()
    merged, err = merge_changes(e, bad)
    assert merged == e and "changes 要传" in err


def test_slim_schema_keeps_only_public_properties():
    full = {"type": "object", "title": "traceArguments", "required": ["bucket_id"],
            "properties": {k: {"title": k} for k in ("bucket_id", *_explicit(), "changes")}}
    slim = slim_schema(full)
    assert list(slim["properties"]) == ["bucket_id", *PUBLIC_EXPLICIT_PARAMS, "changes"]
    assert slim["required"] == ["bucket_id"] and slim["type"] == "object"
    assert len(full["properties"]) == 29, "原 schema 不被就地修改"


# ---------------------------------------------------------------- 经 server.trace / MCP 层

@pytest.mark.asyncio
async def test_tools_list_publishes_exactly_eight_trace_params(bucket_mgr):
    import server as srv
    tools = {t.name: t for t in await srv.mcp.list_tools()}
    props = tools["trace"].inputSchema["properties"]
    assert list(props) == ["bucket_id", "content", "importance", "pinned", "resolved",
                           "delete", "meaning_append", "changes"]
    assert tools["trace"].inputSchema["required"] == ["bucket_id"]
    # 描述里必须列出 changes 的键名——schema 不再约束它们,模型只能从这里知道。
    for key in CHANGES_KEYS:
        assert key in tools["trace"].description, key


@pytest.mark.asyncio
async def test_legacy_explicit_params_not_in_public_schema_still_apply(bucket_mgr):
    import server as srv
    install_runtime(bucket_mgr)
    bid = await bucket_mgr.create(content="旧写法:直接传 domain / seed / why_remembered。", importance=5, domain=["日常"])

    out = _text(await srv.mcp.call_tool("trace", {
        "bucket_id": bid, "domain": "工作", "seed": 1, "why_remembered": "因为是旧写法",
    }))
    meta = (await bucket_mgr.get(bid))["metadata"]

    assert bid in out
    assert meta["domain"] == ["工作"]
    assert meta.get("seed") in (1, True)
    assert meta["why_remembered"] == "因为是旧写法"


@pytest.mark.asyncio
async def test_changes_form_is_equivalent_to_explicit_form(bucket_mgr):
    import server as srv
    install_runtime(bucket_mgr)
    a = await bucket_mgr.create(content="显式写法的桶,内容随便写一句。", importance=5, domain=["日常"])
    b = await bucket_mgr.create(content="changes 写法的桶,内容随便写一句。", importance=5, domain=["日常"])

    await srv.mcp.call_tool("trace", {"bucket_id": a, "importance": 8, "domain": "工作", "tags": "甲,乙", "seed": 1})
    await srv.mcp.call_tool("trace", {"bucket_id": b, "importance": 8,
                                      "changes": {"domain": "工作", "tags": "甲,乙", "seed": 1}})
    ma = (await bucket_mgr.get(a))["metadata"]
    mb = (await bucket_mgr.get(b))["metadata"]

    for key in ("importance", "domain", "tags", "seed"):
        assert ma.get(key) == mb.get(key), key
    assert mb["importance"] == 8 and mb["domain"] == ["工作"]


@pytest.mark.asyncio
async def test_unknown_changes_key_rejects_whole_call_and_leaves_bucket_untouched(bucket_mgr):
    import server as srv
    install_runtime(bucket_mgr)
    bid = await bucket_mgr.create(content="键名写错的时候什么都不该改。", importance=5, domain=["日常"])

    out = _text(await srv.mcp.call_tool("trace", {
        "bucket_id": bid, "importance": 9, "changes": {"domian": "工作"},
    }))
    meta = (await bucket_mgr.get(bid))["metadata"]

    assert "不认识的键" in out and "domian" in out
    assert meta["importance"] == 5 and meta["domain"] == ["日常"]


@pytest.mark.asyncio
async def test_conflicting_explicit_and_changes_rejected(bucket_mgr):
    import server as srv
    install_runtime(bucket_mgr)
    bid = await bucket_mgr.create(content="同一字段两处给了不同的值。", importance=5, domain=["日常"])

    out = _text(await srv.mcp.call_tool("trace", {
        "bucket_id": bid, "importance": 9, "changes": {"importance": 3},
    }))

    assert "不同的值" in out and "importance" in out
    assert (await bucket_mgr.get(bid))["metadata"]["importance"] == 5


@pytest.mark.asyncio
async def test_plain_public_param_call_unchanged(bucket_mgr):
    import server as srv
    install_runtime(bucket_mgr)
    bid = await bucket_mgr.create(content="最普通的调用:只改 importance。", importance=5, domain=["日常"])

    out = _text(await srv.mcp.call_tool("trace", {"bucket_id": bid, "importance": 8}))

    assert "importance=8" in out
    assert (await bucket_mgr.get(bid))["metadata"]["importance"] == 8
