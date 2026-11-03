"""U-1 第 1 条(上游 08243d1 手工移植):一条核心准则装不下,不能把普通浮现整段带走。

我们这版核心准则段默认是目录行(stage2),全文放不下会退化成目录行(stage4),
所以"装不下"只在目录行本身超预算时发生;测试用超长标题把目录行撑大来触发。
"""
from __future__ import annotations

import pytest

from tools.breath._verbatim import catalog_line, render_stored_bucket
from utils import count_tokens_approx


def _core(bucket_id, content, name=None):
    meta = {"type": "permanent", "importance": 10, "pinned": True, "domain": []}
    if name is not None:
        meta["name"] = name
    return {"id": bucket_id, "content": content, "metadata": meta}


def _ordinary(bucket_id, content, importance=10):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "type": "dynamic",
            "importance": importance,
            "activation_count": 1,
            "domain": [],
        },
    }


def _line_cost(bucket):
    return count_tokens_approx(catalog_line(bucket, prefix="📌 "))


def _full_cost(bucket, header):
    return render_stored_bucket(bucket, header)[1]


def _setup(monkeypatch):
    from tests.test_breath_verbatim_patch import OrderedBucketManager, _install_runtime

    fits = _core("fits", "Core rule that fits.")
    huge = _core("huge", "Oversized core rule.", name="超长核心准则标题" * 60)
    ordinary = _ordinary("ordinary", "An ordinary memory that fits the leftovers.")
    _install_runtime(OrderedBucketManager([fits, huge, ordinary]))
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)
    ordinary_cost = _full_cost(ordinary, "[权重:10.00] [bucket_id:ordinary]")
    assert _line_cost(huge) > ordinary_cost, "测试前提:超长目录行必须比普通条目贵"
    return fits, huge, ordinary, _line_cost(fits) + ordinary_cost


@pytest.mark.asyncio
async def test_ordinary_memories_still_surface_when_a_core_rule_is_too_big(monkeypatch):
    from tools.breath.surface import surface_default

    fits, huge, ordinary, budget = _setup(monkeypatch)
    output = await surface_default(max_results=1, max_tokens=budget, tag_filter=[])

    assert "[fits]" in output
    assert "[bucket_id:ordinary]" in output
    assert ordinary["content"] in output
    assert "[huge]" not in output
    assert "token 预算不足" in output


@pytest.mark.asyncio
async def test_the_omitted_core_rule_is_still_reported(monkeypatch):
    from tools.breath.surface import surface_default

    _, _, _, budget = _setup(monkeypatch)
    output = await surface_default(max_results=1, max_tokens=budget, tag_filter=[])

    assert "核心准则" in output
    assert "omitted=1" in output
    assert "surfacing.breath_max_tokens" in output
    # 旧提示会把核心准则的省略错报成"浮现记忆未返回"
    assert "条浮现记忆未返回" not in output


@pytest.mark.asyncio
async def test_core_rules_still_get_the_budget_first(monkeypatch):
    from tests.test_breath_verbatim_patch import OrderedBucketManager, _install_runtime
    from tools.breath.surface import surface_default

    core = _core("core", "Core rule wins the budget.")
    ordinary = _ordinary("ordinary", "Ordinary memory must not crowd it out.")
    _install_runtime(OrderedBucketManager([core, ordinary]))
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    output = await surface_default(max_results=5, max_tokens=_line_cost(core), tag_filter=[])

    assert "[core]" in output
    assert "[bucket_id:ordinary]" not in output


# ---- Dashboard 诊断 ----

class _FakeManager:
    def __init__(self, buckets):
        self._buckets = buckets

    async def list_all(self, include_archive=False):
        return list(self._buckets)


async def _report(monkeypatch, buckets, limit):
    import web._shared as sh
    import web.system as system

    monkeypatch.setattr(sh, "bucket_mgr", _FakeManager(buckets), raising=False)
    monkeypatch.setattr(sh, "config", {"surfacing": {"breath_max_tokens": limit}}, raising=False)
    return await system._pinned_budget_report()


@pytest.mark.asyncio
async def test_report_counts_only_pinned_buckets(monkeypatch):
    report = await _report(
        monkeypatch,
        [_core("a", "rule"), _core("b", "rule"), _ordinary("c", "not pinned")],
        10000,
    )
    assert report["pinned_count"] == 2
    assert report["limit_tokens"] == 10000
    assert report["required_tokens"] > 0
    assert report["full_text_tokens"] >= report["required_tokens"]


@pytest.mark.asyncio
async def test_report_is_zero_without_pinned_buckets(monkeypatch):
    report = await _report(monkeypatch, [_ordinary("c", "not pinned")], 10000)
    assert report["pinned_count"] == 0
    assert report["required_tokens"] == 0


@pytest.mark.asyncio
async def test_report_notices_when_pins_exceed_the_budget(monkeypatch):
    report = await _report(monkeypatch, [_core("a", "rule", name="准" * 3000)], 1000)
    assert report["required_tokens"] > report["limit_tokens"]
    assert report["largest_entry_tokens"] > report["limit_tokens"]
