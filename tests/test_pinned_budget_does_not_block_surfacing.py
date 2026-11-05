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


# ---- U-1 第 2 条(上游 58c2e32):默认预算必须装得下钉满的核心准则,六处 fallback 钉在一起 ----

_DEFAULT_BREATH_TOKENS = 20000


def _fallbacks():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    found = {}
    for rel in (
        "src/tools/breath/__init__.py",
        "src/web/config_api.py",
        "src/web/system.py",
        "frontend/dashboard.html",
        # T-2 四(2026-11-04):breath_advanced 描述瘦身后不再写具体 fallback 数字("不传走 config"),
        # server.py 里没有可比对的值,从一致性检查里拿掉;真正的 fallback 仍在 breath/__init__.py 等处核对。
    ):
        text = (root / rel).read_text(encoding="utf-8")
        found[rel] = {
            int(n)
            for n in re.findall(r"breath_max_tokens[^\n]*?\|\|\s*(\d+)", text)
            + re.findall(r'breath_max_tokens"\)\s*or\s*(\d+)', text)
            + re.findall(r"breath_max_tokens,fallback (\d+)", text)
        }
    return found


def test_every_fallback_agrees_on_the_default():
    for rel, values in _fallbacks().items():
        assert values, f"{rel} 里没找到 breath_max_tokens 的 fallback"
        assert values == {_DEFAULT_BREATH_TOKENS}, f"{rel} 的 fallback 是 {values}"


def test_config_example_matches_the_code_default():
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(
        encoding="utf-8"
    )
    match = re.search(r"breath_max_tokens:\s*(\d+)", text)
    assert match is not None
    assert int(match.group(1)) == _DEFAULT_BREATH_TOKENS


def test_the_default_fits_a_full_set_of_core_rules():
    from tools._common import _DEFAULT_MAX_PINNED

    # 我们这版核心准则默认是目录行(≤ 约 120 token/条);按上游口径一条 250 字
    # 全文准则约 427 token,钉满时也必须还剩得下普通浮现。
    per_rule = 427
    needed = _DEFAULT_MAX_PINNED * per_rule
    assert needed < _DEFAULT_BREATH_TOKENS, f"钉满要 {needed}，预算只有 {_DEFAULT_BREATH_TOKENS}"
    assert _DEFAULT_BREATH_TOKENS - needed >= per_rule * 5
