"""工单 D-4 二期：脱水压缩 4 个 prompt（DEHYDRATE/DIGEST/MERGE/ANALYZE）英文化。

背景：Rhys 记忆入库正文是英文，但桶标题/摘要全是中文——4 个 prompt 中文写死，
按 OMBRE_LANG 给英文版；JSON 字段名（core_facts/summary/domain/tags/...）两版
完全一致，解析代码（_normalize_dehydration_result/_parse_analysis/_parse_digest）
不需要跟着改，这里只验证"选对了 prompt + 解析仍然工作"，不重复其余文件已经
覆盖的解析细节。
"""

import json
import re

import pytest

from dehydrator import (
    ANALYZE_PROMPT,
    ANALYZE_PROMPT_EN,
    DEHYDRATE_PROMPT,
    DEHYDRATE_PROMPT_EN,
    DIGEST_PROMPT,
    DIGEST_PROMPT_EN,
    Dehydrator,
    MERGE_PROMPT,
    MERGE_PROMPT_EN,
    _perspective_rule,
)

_CJK_RE = re.compile(r"[一-鿿]")

_ALL_EN_PROMPTS = {
    "DEHYDRATE_PROMPT_EN": DEHYDRATE_PROMPT_EN,
    "DIGEST_PROMPT_EN": DIGEST_PROMPT_EN,
    "MERGE_PROMPT_EN": MERGE_PROMPT_EN,
    "ANALYZE_PROMPT_EN": ANALYZE_PROMPT_EN,
}


def _dehydrator(tmp_path, **dehy_overrides) -> Dehydrator:
    cfg = {
        "buckets_dir": str(tmp_path / "vault"),
        "human": "Rhys",
        "dehydration": {
            "api_key": "test-key",
            "api_format": "anthropic",
            "base_url": "https://api.anthropic.com",
            "model": "claude-3-5-haiku-latest",
            **dehy_overrides,
        },
    }
    return Dehydrator(cfg)


def _capture_chat(dehy):
    """monkeypatch dehy._chat，记录每次调用的 system/user，返回固定 fake 结果的工厂。"""
    calls = []

    async def fake_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        calls.append({"system": system, "user": user})
        return fake_chat.result

    fake_chat.result = ""
    dehy._chat = fake_chat
    return calls, fake_chat


# ============================================================
# 1. 四个 EN prompt 本身：不含中文字符
# ============================================================

@pytest.mark.parametrize("name", list(_ALL_EN_PROMPTS))
def test_en_prompt_has_no_chinese_characters(name):
    prompt = _ALL_EN_PROMPTS[name]
    assert not _CJK_RE.search(prompt), f"{name} 不应含中文字符"


def test_perspective_rule_en_has_no_chinese_characters(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    rule = _perspective_rule("Rhys")
    assert not _CJK_RE.search(rule)
    assert "Rhys" in rule


def test_perspective_rule_zh_unaffected_by_en_addition(monkeypatch):
    """中文路径一字不动：默认/zh 时仍是原视角铁律文案。"""
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    rule = _perspective_rule("测试者")
    assert "视角铁律" in rule
    assert "测试者" in rule


# ============================================================
# 2. 四个 API 调用点按 OMBRE_LANG 选对 prompt（中文路径回归）
# ============================================================

@pytest.mark.asyncio
async def test_api_dehydrate_selects_en_prompt_when_ombre_lang_en(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)
    calls, fake_chat = _capture_chat(dehy)
    fake_chat.result = json.dumps({
        "core_facts": ["met at the cafe"],
        "emotion_state": "calm",
        "todos": [],
        "keywords": ["cafe"],
        "summary": "A calm afternoon at the cafe.",
    })
    await dehy._api_dehydrate("some long english content " * 20)
    system = calls[-1]["system"]
    assert DEHYDRATE_PROMPT_EN in system
    assert DEHYDRATE_PROMPT not in system
    assert not _CJK_RE.search(system.replace("Rhys", ""))
    dehy._cache_conn.close()


@pytest.mark.asyncio
async def test_api_dehydrate_selects_zh_prompt_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = _dehydrator(tmp_path)
    calls, fake_chat = _capture_chat(dehy)
    fake_chat.result = json.dumps({
        "core_facts": ["原文事实"], "emotion_state": "平静",
        "todos": [], "keywords": ["测试"], "summary": "中文摘要",
    }, ensure_ascii=False)
    await dehy._api_dehydrate("一段足够长的中文原文" * 20)
    system = calls[-1]["system"]
    assert DEHYDRATE_PROMPT in system
    assert DEHYDRATE_PROMPT_EN not in system
    dehy._cache_conn.close()


@pytest.mark.asyncio
async def test_api_merge_selects_en_prompt_and_labels_when_ombre_lang_en(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)
    calls, fake_chat = _capture_chat(dehy)
    fake_chat.result = "merged english text"
    await dehy._api_merge("old english memory", "new english content")
    call = calls[-1]
    assert MERGE_PROMPT_EN in call["system"]
    assert "Old memory:" in call["user"]
    assert "New content:" in call["user"]
    assert "旧记忆" not in call["user"]
    dehy._cache_conn.close()


@pytest.mark.asyncio
async def test_api_merge_selects_zh_prompt_and_labels_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = _dehydrator(tmp_path)
    calls, fake_chat = _capture_chat(dehy)
    fake_chat.result = "合并后的文本"
    await dehy._api_merge("旧记忆内容", "新内容")
    call = calls[-1]
    assert MERGE_PROMPT in call["system"]
    assert "旧记忆：" in call["user"]
    assert "新内容：" in call["user"]
    dehy._cache_conn.close()


@pytest.mark.asyncio
async def test_api_analyze_selects_en_prompt_when_ombre_lang_en(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)
    calls, fake_chat = _capture_chat(dehy)
    fake_chat.result = json.dumps({
        "domain": ["romance"], "valence": 0.8, "arousal": 0.6,
        "tags": ["cafe", "first date"], "suggested_name": "First date at the cafe",
    })
    await dehy._api_analyze("we met at the cafe for the first time")
    assert ANALYZE_PROMPT_EN in calls[-1]["system"]
    assert ANALYZE_PROMPT not in calls[-1]["system"]
    dehy._cache_conn.close()


@pytest.mark.asyncio
async def test_api_analyze_selects_zh_prompt_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = _dehydrator(tmp_path)
    calls, fake_chat = _capture_chat(dehy)
    fake_chat.result = json.dumps({
        "domain": ["恋爱"], "valence": 0.8, "arousal": 0.6,
        "tags": ["咖啡厅"], "suggested_name": "第一次约会",
    }, ensure_ascii=False)
    await dehy._api_analyze("我们第一次在咖啡厅见面")
    assert ANALYZE_PROMPT in calls[-1]["system"]
    dehy._cache_conn.close()


@pytest.mark.asyncio
async def test_api_digest_selects_en_prompt_when_ombre_lang_en(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)
    calls, fake_chat = _capture_chat(dehy)
    fake_chat.result = json.dumps([{
        "name": "Cafe meeting", "content": "We met at the cafe today, it was calm and nice.",
        "domain": ["romance"], "valence": 0.8, "arousal": 0.5, "tags": ["cafe"], "importance": 6,
    }])
    await dehy._api_digest("today I went to the cafe and met someone new " * 5)
    assert DIGEST_PROMPT_EN in calls[-1]["system"]
    assert DIGEST_PROMPT not in calls[-1]["system"]
    dehy._cache_conn.close()


@pytest.mark.asyncio
async def test_api_digest_selects_zh_prompt_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = _dehydrator(tmp_path)
    calls, fake_chat = _capture_chat(dehy)
    fake_chat.result = json.dumps([{
        "name": "咖啡厅偶遇", "content": "今天去咖啡厅遇到了新朋友。",
        "domain": ["恋爱"], "valence": 0.8, "arousal": 0.5, "tags": ["咖啡厅"], "importance": 6,
    }], ensure_ascii=False)
    await dehy._api_digest("今天去咖啡厅遇到了新朋友" * 5)
    assert DIGEST_PROMPT in calls[-1]["system"]
    dehy._cache_conn.close()


# ============================================================
# 3. 英文输入端到端：dehydrate()/analyze()/digest() 输出字段可解析
# ============================================================

@pytest.mark.asyncio
async def test_dehydrate_english_input_end_to_end_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)
    english_content = (
        "Today I finally told her how I felt after months of holding it in. "
        "We talked for hours in the kitchen. I still need to call her back "
        "tomorrow to plan the trip. It felt calm and a little scary at the same time."
    ) * 3

    async def fake_api_dehydrate(_content):
        return json.dumps({
            "core_facts": [
                "Told her how I felt after months of holding it in",
                "Talked for hours in the kitchen",
            ],
            "emotion_state": "calm and a little scared",
            "todos": ["call her back tomorrow to plan the trip"],
            "keywords": ["confession", "kitchen", "trip"],
            "summary": "Finally opened up to her; we talked for hours and I still owe her a call about the trip.",
        })

    monkeypatch.setattr(dehy, "_api_dehydrate", fake_api_dehydrate)
    output = await dehy.dehydrate(english_content)
    dehy._cache_conn.close()

    assert not _CJK_RE.search(output)
    assert "Finally opened up to her" in output
    assert "Todos: call her back tomorrow to plan the trip" in output
    assert "待办" not in output


@pytest.mark.asyncio
async def test_analyze_english_input_end_to_end_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)

    async def fake_api_analyze(_content):
        raw = json.dumps({
            "domain": ["romance", "inner life"],
            "valence": 0.75,
            "arousal": 0.55,
            "tags": ["first time", "vulnerability", "trust"],
            "suggested_name": "Told her the truth",  # 19 字符，卡在 _NAME_MAX_CHARS=20 之内
        })
        return dehy._parse_analysis(raw)

    monkeypatch.setattr(dehy, "_api_analyze", fake_api_analyze)
    result = await dehy.analyze("today I finally told her how I felt")

    assert result["domain"] == ["romance", "inner life"]
    assert result["suggested_name"] == "Told her the truth"
    assert not _CJK_RE.search(result["suggested_name"])
    assert not any(_CJK_RE.search(t) for t in result["tags"])


@pytest.mark.asyncio
async def test_digest_english_input_end_to_end_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)

    async def fake_api_digest(_content):
        raw = json.dumps([
            {
                "name": "Cafe meeting",
                "content": "We met at the cafe today and talked for hours.",
                "domain": ["romance"],
                "valence": 0.8,
                "arousal": 0.5,
                "tags": ["cafe", "first meeting"],
                "importance": 6,
            }
        ])
        return dehy._parse_digest(raw)

    monkeypatch.setattr(dehy, "_api_digest", fake_api_digest)
    entries = await dehy.digest("today I went to the cafe and met someone new, we talked for hours")

    assert len(entries) == 1
    assert entries[0]["name"] == "Cafe meeting"
    assert not _CJK_RE.search(entries[0]["name"])
    assert not _CJK_RE.search(entries[0]["content"])


# ============================================================
# 3b. _render_dehydrated 的"待办："标签/分隔符（不是 4 个 prompt 之一，但是
# 同一份 JSON 渲染成可读文本的最后一步，留中文会在英文摘要里冒出一个中文词）
# ============================================================

def test_render_dehydrated_todos_label_english_when_ombre_lang_en(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    raw = json.dumps({
        "summary": "A calm afternoon.",
        "core_facts": [],
        "todos": ["call her back", "book the trip"],
    })
    rendered = Dehydrator._render_dehydrated(raw)
    assert "Todos: call her back; book the trip" in rendered
    assert "待办" not in rendered


def test_render_dehydrated_todos_label_chinese_by_default(monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    raw = json.dumps({
        "summary": "平静的一个下午。",
        "core_facts": [],
        "todos": ["回电话", "订行程"],
    }, ensure_ascii=False)
    rendered = Dehydrator._render_dehydrated(raw)
    assert "待办：回电话；订行程" in rendered


# ============================================================
# 4. domain 兜底值（打标失败/未返回）按 OMBRE_LANG 切换
# ============================================================

def test_default_analysis_domain_is_english_when_ombre_lang_en(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)
    assert dehy._default_analysis()["domain"] == ["unclassified"]
    dehy._cache_conn.close()


def test_default_analysis_domain_is_chinese_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = _dehydrator(tmp_path)
    assert dehy._default_analysis()["domain"] == ["未分类"]
    dehy._cache_conn.close()


def test_parse_analysis_missing_domain_falls_back_by_language(tmp_path, monkeypatch):
    dehy = _dehydrator(tmp_path)
    raw = json.dumps({"valence": 0.5, "arousal": 0.3, "tags": [], "suggested_name": ""})

    monkeypatch.setenv("OMBRE_LANG", "en")
    assert dehy._parse_analysis(raw)["domain"] == ["unclassified"]

    monkeypatch.delenv("OMBRE_LANG", raising=False)
    assert dehy._parse_analysis(raw)["domain"] == ["未分类"]
    dehy._cache_conn.close()


# ============================================================
# 工单 D-4 三期：二期留下的 _format_output 桶头标签
# （"记忆桶:"/"[主题:]"/"[情感:V/A]"/"[已消化]"）英文化
# ============================================================

def test_en_format_output_header_has_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)
    out = dehy._format_output("plain body text", {
        "name": "First date", "type": "dynamic", "domain": ["romance"],
        "digested": True, "model_valence": 0.8,
    })
    header = out.splitlines()[0]
    assert not _CJK_RE.search(header)
    assert header == "💭 Memory: First date [topic:romance] [emotion:V0.5/A0.3] [my perspective:V0.8] [digested]"
    dehy._cache_conn.close()


def test_en_format_output_unnamed_fallback_has_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)
    out = dehy._format_output("plain body text", {"type": "dynamic"})
    header = out.splitlines()[0]
    assert not _CJK_RE.search(header)
    assert "unnamed" in header
    dehy._cache_conn.close()


def test_zh_default_format_output_header_unaffected(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = _dehydrator(tmp_path)
    out = dehy._format_output("正文内容", {"name": "普通事", "type": "dynamic", "domain": ["工作"]})
    assert out.splitlines()[0].startswith("💭 记忆桶: 普通事")
    dehy._cache_conn.close()


# ============================================================
# 工单 D-4 三期：self.human 未配置时的默认值（"用户"）同样按 OMBRE_LANG
# 切换——它会原样嵌进 _perspective_rule_en/_zh，不切换就会在英文视角铁律
# 里冒出一个中文「用户」。
# ============================================================

def test_en_default_human_fallback_is_english(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = Dehydrator({"buckets_dir": str(tmp_path / "vault")})
    assert dehy.human == "the user"
    assert not _CJK_RE.search(_perspective_rule(dehy.human))
    dehy._cache_conn.close()


def test_zh_default_human_fallback_unaffected(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = Dehydrator({"buckets_dir": str(tmp_path / "vault")})
    assert dehy.human == "用户"
    dehy._cache_conn.close()


# ============================================================
# 工单 D-4 四期：dehydrator.py 内部报错 + judge_plan_resolution +
# dehydrate() 空内容固定输出
# ============================================================

def _dehydrator_no_api(tmp_path) -> Dehydrator:
    return Dehydrator({"buckets_dir": str(tmp_path / "vault")})  # 无 api_key


@pytest.mark.asyncio
async def test_en_require_api_error_has_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator_no_api(tmp_path)

    with pytest.raises(RuntimeError) as exc_info:
        await dehy.merge("old", "new")

    assert not _CJK_RE.search(str(exc_info.value))
    assert "Dehydration API unavailable" in str(exc_info.value)
    dehy._cache_conn.close()


@pytest.mark.asyncio
async def test_en_merge_analyze_digest_wrapped_errors_have_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)

    async def failing(*_a, **_k):
        raise TimeoutError("upstream down")

    monkeypatch.setattr(dehy, "_api_merge", failing)
    with pytest.raises(RuntimeError) as merge_exc:
        await dehy.merge("old", "new")
    assert not _CJK_RE.search(str(merge_exc.value))
    assert "API merge failed" in str(merge_exc.value)

    monkeypatch.setattr(dehy, "_api_analyze", failing)
    with pytest.raises(RuntimeError) as analyze_exc:
        await dehy.analyze("some content")
    assert not _CJK_RE.search(str(analyze_exc.value))
    assert "API tagging failed" in str(analyze_exc.value)
    dehy._cache_conn.close()


def test_en_dehydrate_empty_content_has_no_chinese_characters(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)

    out = asyncio.run(dehy.dehydrate(""))

    assert not _CJK_RE.search(out)
    assert out == "(empty memory)"
    dehy._cache_conn.close()


def test_en_judge_plan_resolution_system_prompt_has_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator(tmp_path)
    captured = {}

    async def fake_chat(system, user, **kwargs):
        captured["system"] = system
        return '{"resolved": false, "confidence": 0.1, "reason": "not yet"}'

    monkeypatch.setattr(dehy, "_chat", fake_chat)
    import asyncio
    result = asyncio.run(dehy.judge_plan_resolution("plan text", "new event text"))

    assert not _CJK_RE.search(captured["system"])
    assert result["reason"] == "not yet"
    dehy._cache_conn.close()


def test_en_judge_plan_resolution_api_unavailable_reason_is_english(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _dehydrator_no_api(tmp_path)

    result = asyncio.run(dehy.judge_plan_resolution("plan text", "new event text"))

    assert result == {"resolved": False, "confidence": 0.0, "reason": "API unavailable"}
    dehy._cache_conn.close()


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

@pytest.mark.asyncio
async def test_zh_default_require_api_error_unaffected(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = _dehydrator_no_api(tmp_path)

    with pytest.raises(RuntimeError) as exc_info:
        await dehy.merge("old", "new")

    assert str(exc_info.value) == "脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置"
    dehy._cache_conn.close()


def test_zh_default_dehydrate_empty_content_unaffected(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = _dehydrator(tmp_path)

    out = asyncio.run(dehy.dehydrate(""))

    assert out == "（空记忆 / empty memory）"
    dehy._cache_conn.close()
