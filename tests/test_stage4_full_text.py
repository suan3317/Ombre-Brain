"""任务书阶段4：breath 浮现条数降至10、超长条目默认截首段、
full_text 可选参数、核心准则段 core_limit=3。"""
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath import dispatch
from tools.breath.surface import surface_default


class DummyDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        if meta.get("pinned") or meta.get("protected") or meta.get("type") == "permanent":
            return 999.0
        return float(meta.get("importance") or 5)


class NoopEmbedding:
    enabled = False

    async def search_similar(self, query, top_k=20):
        return []


def install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = DummyDecay()
    rt.dehydrator = MagicMock()
    rt.embedding_engine = NoopEmbedding()
    rt.dream_engine = None
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_a, **_k: None


LONG_CONTENT = (
    "第一段是这条记忆最重要的部分，讲清楚了发生了什么、为什么重要，长度刻意拉到超过三十个字。\n\n"
    "第二段是补充细节，正常情况下默认模式不应该出现在输出里，因为已经超过三百字的截断阈值了。"
    + "补充" * 200
)


@pytest.mark.asyncio
async def test_default_breath_max_results_is_10_not_20(bucket_mgr):
    for i in range(15):
        await bucket_mgr.create(content=f"第 {i} 条待浮现记忆，用来验证默认条数上限。")
    install_runtime(bucket_mgr)

    result = await dispatch()

    weight_lines = [ln for ln in result.splitlines() if ln.startswith("[权重:")]
    assert len(weight_lines) <= 10


@pytest.mark.asyncio
async def test_long_entry_default_shows_only_first_paragraph_not_second(bucket_mgr):
    await bucket_mgr.create(content=LONG_CONTENT, meaning="这条记忆的重量")
    install_runtime(bucket_mgr)

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    assert "第一段是这条记忆最重要的部分" in result
    assert "第二段是补充细节" not in result
    assert "仅显示首段" in result
    assert "💭 meaning: 这条记忆的重量" in result


@pytest.mark.asyncio
async def test_full_text_true_restores_complete_body(bucket_mgr):
    await bucket_mgr.create(content=LONG_CONTENT, meaning="这条记忆的重量")
    install_runtime(bucket_mgr)

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[], full_text=True)

    assert "第一段是这条记忆最重要的部分" in result
    assert "第二段是补充细节" in result
    assert "仅显示首段" not in result


@pytest.mark.asyncio
async def test_short_entry_not_truncated_even_by_default(bucket_mgr):
    short = "很短的一条记忆，远不到三百字。"
    await bucket_mgr.create(content=short)
    install_runtime(bucket_mgr)

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    assert short in result
    assert "仅显示首段" not in result


@pytest.mark.asyncio
async def test_core_segment_stays_catalog_only_by_default_even_with_full_text_param_absent(bucket_mgr):
    full_tail = "内容" * 60  # 120 字，远超目录行 50 字截断上限——只有全文渲染才会把它整段带出来
    for i in range(5):
        await bucket_mgr.create(content=f"核心准则第{i}条，正文比较长，" + full_tail, pinned=True)
    install_runtime(bucket_mgr)

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    core_section = result.split("=== 浮现记忆 ===")[0]
    assert full_tail not in core_section  # 没有任何一条给了全文（目录行只截 50 字）


@pytest.mark.asyncio
async def test_full_text_true_guarantees_core_limit_3_full_entries(bucket_mgr):
    # 字符串本身远超目录行 50 字截断上限：只有真正被全文渲染的条目才会把它
    # 完整地、原样地带出来；目录行只能截到其中一小段。
    long_tail = "字数填充" * 60
    for i in range(5):
        await bucket_mgr.create(content=f"核心准则正文，{long_tail}（第{i}条）", pinned=True, importance=5 + i)
    install_runtime(bucket_mgr)

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[], full_text=True)

    core_section = result.split("=== 浮现记忆 ===")[0] if "=== 浮现记忆 ===" in result else result
    full_count = core_section.count(long_tail)
    assert full_count >= 3, f"至少 3 条应给全文，实际 {full_count} 条"
    # 目录行（没有全文）的条目仍然要出现，不能整条消失——5 条都应该在，只是有的全文有的目录行
    assert core_section.count("📌 ") == 5
