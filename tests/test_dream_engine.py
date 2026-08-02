"""梦境系统测试（施工单 §6，六项测试）。

1. DREAM_FORCE=1 强制入口跳过 30% 骰
2. 连发 5 发人工检查：格式/front-matter/无因果词/完整版不进日志
3. 概率自测：骰子函数跑 10000 次，各档 ±3% 内
4. 裁剪自测：四档各强制一遍
5. breath 挂载：unread 梦出现且翻 read，再调不重复；breath_search 不挂载
6. 过期清理：伪造 50h 前，正文被替换
"""
import os
import random
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
import frontmatter as fm

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dream_engine import (  # noqa: E402
    DreamEngine, _EMOTION_RESIDUE_POOL, _DEFAULT_TONE_WEIGHTS, _is_prose_like,
)


PT = ZoneInfo("America/Los_Angeles")

_IMAGERY_SYSTEM_MARKER = "提取两类内容"
_GROWTH_SYSTEM_MARKER = "梦境噪音意象"

# 一句干净的、没有因果连接词、没有收尾点题的梦境文本，供各测试复用
_CLEAN_DREAM_TEXT = (
    "台灯的光落在没人的椅子上。钥匙串在门后自己晃。楼梯数到一半停住，"
    "手表的指针倒着走，雨声从没关紧的窗户挤进来"
)

# 一段像清晰/混沌交替结构的高档梦境文本，供 §1.6 高档 prompt 与
# _is_prose_like 全文粒度测试复用：2 个有标点的清晰段 + 混沌段里夹着
# 短促无标点的行（v1 的逐段判定会误杀这种输出，v2 改成全文密度判定）。
_ALTERNATING_DREAM_TEXT = (
    "她把施工单递过来，纸角还是潮的，我伸手去接，指尖先碰到她的袖口，"
    "然后才碰到纸。灯在这时候闪了一下，闪完还是原来的灯。\n\n"
    "台灯\n钥匙\n楼梯\n手表\n雨声\n盐味的雪\n"
    "少一级的楼梯\n\n"
    "楼梯还是那道楼梯，只是编号变了，她已经站在最上面等，手里的施工单"
    "换了一份，纸角是干的，她说这次不一样，我没接话，转身继续往上走。"
)


class FakeBucketMgr:
    def __init__(self, buckets):
        self._buckets = buckets

    async def list_all(self, include_archive=False):
        return [dict(b) for b in self._buckets]


def make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT, raise_on_generate=False, named_phrase="她递来的信"):
    calls = []

    async def fake_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                       "temperature": temperature, "model": model})
        if _IMAGERY_SYSTEM_MARKER in system:
            named_line = f"\nNAMED: {named_phrase}" if named_phrase else ""
            return f"台灯\n钥匙\n楼梯\n手表\n雨声{named_line}"
        if _GROWTH_SYSTEM_MARKER in system:
            return "\n".join(f"噪音意象{i}" for i in range(30))
        if raise_on_generate:
            raise RuntimeError("模拟生成失败")
        return dream_text

    class FakeDehydrator:
        api_available = True
        raw_chat = staticmethod(fake_raw_chat)

    fd = FakeDehydrator()
    fd.calls = calls  # type: ignore[attr-defined]
    return fd


def make_buckets():
    return [
        {"id": "b1", "content": "今天在办公室开了很久的会，很累。", "metadata": {"resolved": True}},
        {"id": "b2", "content": "楼下的猫又跑到窗台上晒太阳了。", "metadata": {"resolved": False}},
        {"id": "b3", "content": "晚上煮了一锅汤，忘了放盐。", "metadata": {"resolved": True}},
    ]


def make_engine(tmp_path, dehydrator=None, **cfg_overrides):
    cfg = {
        "buckets_dir": str(tmp_path),
        "dream": {
            "enabled": True,
            "dream_prob": 1.0,
            **cfg_overrides,
        },
    }
    bucket_mgr = FakeBucketMgr(make_buckets())
    dehydrator = dehydrator or make_fake_dehydrator()
    return DreamEngine(cfg, bucket_mgr, dehydrator)


# ============================================================
# 1. DREAM_FORCE=1 强制入口
# ============================================================

@pytest.mark.asyncio
async def test_forced_run_bypasses_probability_roll(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, dream_prob=0.0)  # 正常情况下永远不会有梦
    assert engine.dream_prob == 0.0

    result = await engine._forced_run_once()

    assert result["dreamed"] is True
    # dream_prob 必须在强制运行后恢复原值，不留副作用
    assert engine.dream_prob == 0.0
    dream_path = engine._dream_path(dt.date.today() - dt.timedelta(days=1))
    assert os.path.isfile(dream_path)


@pytest.mark.asyncio
async def test_dream_force_env_triggers_immediate_run_on_start(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAM_FORCE", "1")
    engine = make_engine(tmp_path, dream_prob=0.0)
    # 让正常的每日调度永远不会在测试期间触发，只观察 DREAM_FORCE 这条强制路径
    monkeypatch.setattr(engine, "_seconds_until_next_run", lambda: 3600.0)

    await engine.start()
    try:
        for _ in range(50):
            await __import__("asyncio").sleep(0)
        dream_path = engine._dream_path(dt.date.today() - dt.timedelta(days=1))
        assert os.path.isfile(dream_path), "DREAM_FORCE=1 应在 start() 后立即生成一晚的梦，不受 dream_prob=0 影响"
    finally:
        await engine.stop()


# ============================================================
# 2. 连发 5 发人工检查
# ============================================================

@pytest.mark.asyncio
async def test_five_dreams_manual_check(tmp_path, caplog):
    forbidden_causal = ["因为", "所以", "于是", "接着然后", "由于"]
    results = []
    for i in range(5):
        marker = f"__RAW_MARKER_{i}__绝对不能进日志__"
        text = _CLEAN_DREAM_TEXT + marker
        dehy = make_fake_dehydrator(dream_text=text)
        # 强制"完全记得"档，这样 file body == raw，能验证"完整版"字符串本身
        # 不含因果词/不做收尾点题；但只有落盘允许含 raw，日志绝不允许。
        engine = make_engine(tmp_path, dehydrator=dehy, memory_levels=[1.0, 0.0, 0.0, 0.0])

        with caplog.at_level("DEBUG"):
            result = await engine.nightly_dream()
        results.append(result)

        assert result["dreamed"] is True
        path = result["path"]
        post = fm.load(path)

        # --- front-matter 格式检查 ---
        for key in ("date", "tone", "level", "sources", "noise", "status", "generated_at"):
            assert key in post.metadata, f"缺 front-matter 字段: {key}"
        assert post["status"] == "unread"
        assert isinstance(post["sources"], list) and post["sources"]
        assert isinstance(post["noise"], int)
        dt.datetime.fromisoformat(post["generated_at"])  # 不抛异常即合法 ISO

        # --- 无因果连接词 ---
        body = str(post.content)
        for w in forbidden_causal:
            assert w not in body, f"正文不应含因果连接词 {w}: {body}"

        # --- 完整版原文绝不进日志（R4）---
        assert marker not in caplog.text, "完整原文标记出现在日志里，违反 R4 即焚"

    assert len(results) == 5


# ============================================================
# 3. 概率自测：10000 次，各档 ±3%
# ============================================================

def test_probability_self_test(tmp_path, capsys):
    engine = make_engine(tmp_path)
    n = 10000
    tol = 0.03

    # --- 基调分布 ---
    tone_counts = {t: 0 for t in _DEFAULT_TONE_WEIGHTS}
    for _ in range(n):
        tone_counts[engine.roll_tone()] += 1

    # --- 记忆度分布（用中性基调，避免"只剩情绪"联动改判干扰统计）---
    level_counts = {"full": 0, "half": 0, "glimpse": 0, "emotion": 0}
    for _ in range(n):
        level, _tone = engine.roll_memory_level("daily")
        level_counts[level] += 1

    # --- 噪音档位分布 ---
    tier_counts = {"low": 0, "half": 0, "pure": 0}
    for _ in range(n):
        _noise, tier = engine.sample_noise(imagery_count=3)
        tier_counts[tier] += 1

    # --- 有梦骰 ---
    dream_hits = sum(1 for _ in range(n) if random.random() <= engine.dream_prob)

    lines = ["\n=== 梦境系统概率自测（n=10000，容差 ±3%）===", "基调分布:"]
    for t, w in _DEFAULT_TONE_WEIGHTS.items():
        observed = tone_counts[t] / n
        lines.append(f"  {t:10s} 期望={w:.2f} 实际={observed:.4f}")
        assert abs(observed - w) <= tol, f"tone {t}: {observed} vs {w}"

    lines.append("记忆度分布:")
    for key, w in zip(["full", "half", "glimpse", "emotion"], engine.memory_levels):
        observed = level_counts[key] / n
        lines.append(f"  {key:10s} 期望={w:.2f} 实际={observed:.4f}")
        assert abs(observed - w) <= tol, f"level {key}: {observed} vs {w}"

    lines.append("噪音档位分布:")
    for key, w in zip(["low", "half", "pure"], engine.noise_tiers):
        observed = tier_counts[key] / n
        lines.append(f"  {key:10s} 期望={w:.2f} 实际={observed:.4f}")
        assert abs(observed - w) <= tol, f"noise tier {key}: {observed} vs {w}"

    observed_dream = dream_hits / n
    lines.append(f"有梦骰: 期望={engine.dream_prob:.2f} 实际={observed_dream:.4f}")
    assert abs(observed_dream - engine.dream_prob) <= tol

    table = "\n".join(lines)
    print(table)
    with capsys.disabled():
        pass  # 表格已经打进 stdout；`pytest -s` 可见


# ============================================================
# 4. 裁剪自测：四档各强制一遍
# ============================================================

def test_trim_by_level_full(tmp_path):
    engine = make_engine(tmp_path)
    raw = _CLEAN_DREAM_TEXT
    assert engine.trim_by_level(raw, "full", "daily") == raw


def test_trim_by_level_half_is_contiguous_window(tmp_path):
    engine = make_engine(tmp_path)
    raw = _CLEAN_DREAM_TEXT * 3  # 拉长，避免窗口计算被短文本边界吃掉
    trimmed = engine.trim_by_level(raw, "half", "daily")
    assert trimmed in raw  # 必须是原文的连续子串
    ratio = len(trimmed) / len(raw)
    assert 0.40 <= ratio <= 0.65  # 45%-60% 目标区间，留一点边界宽容度


def test_trim_by_level_glimpse_is_subset_of_sentences(tmp_path):
    engine = make_engine(tmp_path)
    raw = "这是第一句。这是第二句！这是第三句？这是第四句。"
    trimmed = engine.trim_by_level(raw, "glimpse", "daily")
    lines = trimmed.split("\n")
    assert 1 <= len(lines) <= 3
    for line in lines:
        assert line in raw


def test_trim_by_level_emotion_is_from_residue_pool_and_raw_unrecoverable(tmp_path):
    engine = make_engine(tmp_path)
    raw = "这段带着独一无二标记__ORIGINAL_TEXT_MARKER__的原文绝对不能出现在残句里。"
    for tone in _EMOTION_RESIDUE_POOL:
        trimmed = engine.trim_by_level(raw, "emotion", tone)
        assert trimmed in _EMOTION_RESIDUE_POOL[tone]
        assert "__ORIGINAL_TEXT_MARKER__" not in trimmed
        assert raw not in trimmed and trimmed not in raw


# ============================================================
# 5. breath 挂载
# ============================================================

def _write_unread_dream(engine, day, tone="荒诞", level="只剩画面", body="一段昨夜的梦境正文"):
    post = fm.Post(body)
    post["date"] = day.isoformat()
    post["tone"] = tone
    post["level"] = level
    post["sources"] = ["b1", "b2"]
    post["noise"] = 1
    post["status"] = "unread"
    post["generated_at"] = dt.datetime.now(PT).isoformat(timespec="seconds")
    path = engine._dream_path(day)
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))
    return path


@pytest.fixture
def clean_rt():
    import tools._runtime as rt
    keys = ("config", "bucket_mgr", "dehydrator", "decay_engine", "dream_engine",
            "embedding_engine", "import_engine", "logger", "fire_webhook", "mark_op")
    saved = {k: getattr(rt, k, None) for k in keys}
    yield rt
    for k, v in saved.items():
        setattr(rt, k, v)


class _NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None


@pytest.mark.asyncio
async def test_breath_mount_shows_unread_dream_then_marks_read(tmp_path, clean_rt, bucket_mgr):
    from unittest.mock import MagicMock
    from tools.breath import dispatch

    engine = make_engine(tmp_path)
    today = dt.datetime.now(PT).date()
    _write_unread_dream(engine, today - dt.timedelta(days=1))

    clean_rt.config = {"surfacing": {}}
    clean_rt.bucket_mgr = bucket_mgr
    clean_rt.decay_engine = _NoopDecay()
    clean_rt.dream_engine = engine
    clean_rt.dehydrator = None
    clean_rt.embedding_engine = None
    clean_rt.logger = MagicMock()
    clean_rt.fire_webhook = None
    clean_rt.mark_op = None

    first = await dispatch()
    assert "——— 昨夜的梦 ———" in first
    assert "一段昨夜的梦境正文" in first

    second = await dispatch()
    assert "——— 昨夜的梦 ———" not in second, "已读的梦不该再出现在下一次 breath 里"


@pytest.mark.asyncio
async def test_breath_search_does_not_mount_dream_tail(tmp_path, clean_rt, bucket_mgr):
    from unittest.mock import MagicMock
    from tools.breath import dispatch

    engine = make_engine(tmp_path)
    today = dt.datetime.now(PT).date()
    _write_unread_dream(engine, today - dt.timedelta(days=1))

    clean_rt.config = {"surfacing": {}}
    clean_rt.bucket_mgr = bucket_mgr
    clean_rt.decay_engine = _NoopDecay()
    clean_rt.dream_engine = engine
    clean_rt.dehydrator = None
    clean_rt.embedding_engine = None
    clean_rt.logger = MagicMock()
    clean_rt.fire_webhook = None
    clean_rt.mark_op = None

    # breath_search 在 server.py 里显式传 include_dream=False；这里直接测那条契约。
    result = await dispatch(query="随便什么", include_dream=False)
    assert "——— 昨夜的梦 ———" not in result


# ============================================================
# 6. 过期清理
# ============================================================

def test_cleanup_expired_replaces_body_past_48h(tmp_path):
    engine = make_engine(tmp_path, expire_hours=48)
    today = dt.datetime.now(PT).date()
    path = _write_unread_dream(engine, today - dt.timedelta(days=3))

    post = fm.load(path)
    old_generated_at = dt.datetime.now(PT) - dt.timedelta(hours=50)
    post["generated_at"] = old_generated_at.isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    cleaned = engine.cleanup_expired()
    assert cleaned == 1

    reloaded = fm.load(path)
    assert reloaded["status"] == "expired"
    assert str(reloaded.content).strip() == "那晚做过梦，没记住。"


def test_cleanup_expired_leaves_fresh_dream_untouched(tmp_path):
    engine = make_engine(tmp_path, expire_hours=48)
    today = dt.datetime.now(PT).date()
    path = _write_unread_dream(engine, today - dt.timedelta(days=1))

    cleaned = engine.cleanup_expired()
    assert cleaned == 0

    reloaded = fm.load(path)
    assert reloaded["status"] == "unread"


# ============================================================
# 额外覆盖：架构张力点——暗房/resolved=0 低概率入池、拆词失败退化
# ============================================================

@pytest.mark.asyncio
async def test_sample_buckets_includes_resolved0_and_darkroom_when_forced(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, darkroom_prob=1.0, resolved0_prob=1.0)
    darkroom_dir = os.path.join(str(tmp_path), "darkroom")
    os.makedirs(darkroom_dir, exist_ok=True)
    sep = "\n----- DARKROOM CONTENT (no tool reads below this line) -----\n"
    with open(os.path.join(darkroom_dir, "dr_20260101000000_deadbeef.dr"), "w", encoding="utf-8") as f:
        f.write('{"entry_id": "dr_1"}' + sep + "还没想透的暗房正文")

    materials = await engine.sample_buckets()
    kinds = [m["kind"] for m in materials]
    assert "darkroom" in kinds
    # resolved0_prob=1.0：b2（resolved=False）应该被加抽进来（可能重复于主抽样，无妨）
    ids = [m["id"] for m in materials]
    assert "b2" in ids


@pytest.mark.asyncio
async def test_extract_imagery_falls_back_when_api_fails(tmp_path):
    async def failing_raw_chat(system, user, **kwargs):
        raise RuntimeError("API 挂了")

    class FailingDehydrator:
        api_available = True
        raw_chat = staticmethod(failing_raw_chat)

    engine = make_engine(tmp_path, dehydrator=FailingDehydrator())
    materials = [{"kind": "bucket", "id": "b1", "text": "楼下的猫又跑到窗台上晒太阳了今天很暖和"}]
    words, named_phrases = await engine.extract_imagery(materials)
    assert words, "拆词 API 失败时正则退化方案必须仍然产出词，管线不能断"
    assert named_phrases == [], "正则退化方案抽不出具名短语，不硬凑"


@pytest.mark.asyncio
async def test_nightly_dream_no_dream_on_generation_failure(tmp_path):
    dehy = make_fake_dehydrator(raise_on_generate=True)
    engine = make_engine(tmp_path, dehydrator=dehy)
    result = await engine.nightly_dream()
    assert result["dreamed"] is False


# ============================================================
# 回归：首发实测发现生成结果是意象词原样罗列，不是叙事散文
# （生产上 dreams/2026-07-31.md 的正文是清单体）。修复：①§1.6 prompt 明确
# 要求连续散文、每句含动词、禁止罗列；②形状校验兜底，清单体一律按生成
# 失败处理，不落盘。
# ============================================================

def test_is_prose_like_rejects_word_list_dump():
    word_list_dump = "台灯\n钥匙\n楼梯\n手表\n雨声\n盐味的雪\n少一级的楼梯"
    assert _is_prose_like(word_list_dump) is False


def test_is_prose_like_rejects_empty_and_no_punctuation_text():
    assert _is_prose_like("") is False
    assert _is_prose_like("台灯 钥匙 楼梯 手表 雨声 没有任何句读的一长串文字") is False


def test_is_prose_like_accepts_real_prose():
    assert _is_prose_like(_CLEAN_DREAM_TEXT) is True
    multi_paragraph = (
        "台灯的光落在没人的椅子上，钥匙串在门后自己晃。\n\n"
        "楼梯数到一半停住，手表的指针倒着走"
    )
    assert _is_prose_like(multi_paragraph) is True


@pytest.mark.asyncio
async def test_generate_dream_returns_empty_when_llm_dumps_word_list(tmp_path):
    async def dump_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯"
        # 模拟首发事故：生成步把打乱的意象词原样续写回来
        return "\n".join(["台灯", "钥匙", "楼梯", "手表", "雨声", "盐味的雪"])

    class DumpDehydrator:
        api_available = True
        raw_chat = staticmethod(dump_raw_chat)

    engine = make_engine(tmp_path, dehydrator=DumpDehydrator())
    result = await engine.generate_dream(["台灯", "钥匙", "楼梯"], [], "daily", "full")
    assert result == ""


@pytest.mark.asyncio
async def test_nightly_dream_writes_nothing_when_generation_is_word_list_shaped(tmp_path):
    async def dump_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯\n手表\n雨声"
        return "台灯\n钥匙\n楼梯\n手表\n雨声\n盐味的雪\n少一级的楼梯"

    class DumpDehydrator:
        api_available = True
        raw_chat = staticmethod(dump_raw_chat)

    engine = make_engine(tmp_path, dehydrator=DumpDehydrator())
    result = await engine.nightly_dream()

    assert result["dreamed"] is False
    dreams_dir = engine._dreams_dir()
    written = [f for f in os.listdir(dreams_dir) if f.endswith(".md")]
    assert written == [], "生成结果是词表时不允许落盘，哪怕退化路径也不行"


# ============================================================
# 返修单 v2：清晰/混沌交替结构
# 改动一：管线顺序（level 在 generate 前已知）
# 改动二：拆意象加具名短语
# 改动三：生成 prompt 按档分两套
# 改动四：记得一半档改按段落粒度裁剪
# 改动五：DREAM_FORCE_LEVEL 测试变量
# + _is_prose_like 判定粒度改为全文，不误杀交替结构里的短行混沌段
# ============================================================

# --- 改动二：拆意象加具名短语 ---

@pytest.mark.asyncio
async def test_extract_imagery_returns_named_phrase_separately(tmp_path):
    dehy = make_fake_dehydrator(named_phrase="她递来的施工单")
    engine = make_engine(tmp_path, dehydrator=dehy)
    materials = [{"kind": "bucket", "id": "b1", "text": "今天很累，开了很久的会"}]
    words, named_phrases = await engine.extract_imagery(materials)
    assert "她递来的施工单" in named_phrases
    assert "她递来的施工单" not in words, "具名短语要单独成列表，不混进意象词里"


@pytest.mark.asyncio
async def test_extract_imagery_no_named_phrase_when_bucket_has_none(tmp_path):
    dehy = make_fake_dehydrator(named_phrase="")  # 桶内无专名，不硬凑
    engine = make_engine(tmp_path, dehydrator=dehy)
    materials = [{"kind": "bucket", "id": "b1", "text": "楼下的猫又跑到窗台上晒太阳了"}]
    words, named_phrases = await engine.extract_imagery(materials)
    assert named_phrases == []
    assert words  # 意象词照常有


def test_named_phrase_truncated_to_max_chars():
    from dream_engine import _NAMED_PHRASE_RE, _NAMED_PHRASE_MAX_CHARS
    m = _NAMED_PHRASE_RE.match("NAMED: 一个非常非常非常长超过十个字的具名短语肯定会被截断")
    assert m is not None
    assert len(m.group(1).strip()[:_NAMED_PHRASE_MAX_CHARS]) == _NAMED_PHRASE_MAX_CHARS


# --- 改动三：生成 prompt 按档分两套 ---

@pytest.mark.asyncio
async def test_generate_dream_uses_high_tier_prompt_for_full_and_half(tmp_path):
    for level in ("full", "half"):
        dehy = make_fake_dehydrator(dream_text=_ALTERNATING_DREAM_TEXT)
        engine = make_engine(tmp_path, dehydrator=dehy)
        result = await engine.generate_dream(["台灯", "钥匙"], ["她递来的信"], "daily", level)
        assert result == _ALTERNATING_DREAM_TEXT
        call = dehy.calls[-1]
        assert "清晰段" in call["system"] and "混沌段" in call["system"], f"level={level} 应走高档交替结构 prompt"
        assert call["max_tokens"] == 1200, f"level={level} 高档 max_tokens 应上调至 1200"
        assert "清晰段的锚" in call["user"]
        assert "她递来的信" in call["user"]


@pytest.mark.asyncio
async def test_generate_dream_uses_low_tier_prompt_for_glimpse_and_emotion(tmp_path):
    for level in ("glimpse", "emotion"):
        dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
        engine = make_engine(tmp_path, dehydrator=dehy)
        result = await engine.generate_dream(["台灯", "钥匙"], ["她递来的信"], "daily", level)
        assert result == _CLEAN_DREAM_TEXT
        call = dehy.calls[-1]
        assert "清晰段" not in call["system"], f"level={level} 不该走高档 prompt"
        assert call["max_tokens"] == 800, f"level={level} 低档 max_tokens 应维持 800"
        assert "清晰段的锚" not in call["user"]


@pytest.mark.asyncio
async def test_generate_dream_high_tier_handles_no_named_phrase(tmp_path):
    dehy = make_fake_dehydrator(dream_text=_ALTERNATING_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy)
    result = await engine.generate_dream(["台灯", "钥匙"], [], "daily", "full")
    assert result == _ALTERNATING_DREAM_TEXT
    call = dehy.calls[-1]
    assert "没有明确的具名素材" in call["user"]


# --- 改动一：管线顺序（level 在 generate 前已知）---

@pytest.mark.asyncio
async def test_nightly_dream_pipeline_passes_level_to_generate_before_trim(tmp_path, monkeypatch):
    dehy = make_fake_dehydrator(dream_text=_ALTERNATING_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy, memory_levels=[0.0, 1.0, 0.0, 0.0])  # 强制 half

    seen_levels = []
    original = engine.generate_dream

    async def spy_generate_dream(material_words, named_phrases, tone, level):
        seen_levels.append(level)
        return await original(material_words, named_phrases, tone, level)

    monkeypatch.setattr(engine, "generate_dream", spy_generate_dream)
    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    assert seen_levels == ["half"], "roll_memory_level 必须在 generate_dream 之前完成，档位要传给生成步"
    assert result["level"] == "half"


@pytest.mark.asyncio
async def test_pure_noise_tier_drops_named_phrases(tmp_path, monkeypatch):
    dehy = make_fake_dehydrator(dream_text=_ALTERNATING_DREAM_TEXT, named_phrase="她递来的信")
    engine = make_engine(tmp_path, dehydrator=dehy)
    # 强制纯噪音档：sample_noise 固定返回 pure
    monkeypatch.setattr(engine, "sample_noise", lambda imagery_count: (["一扇往下开的门", "少一级的楼梯"], "pure"))

    seen_named = []
    original = engine.generate_dream

    async def spy_generate_dream(material_words, named_phrases, tone, level):
        seen_named.append(list(named_phrases))
        return await original(material_words, named_phrases, tone, level)

    monkeypatch.setattr(engine, "generate_dream", spy_generate_dream)
    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    assert seen_named == [[]], "纯噪音梦丢弃全部记忆意象时，具名短语（同样来自记忆桶）也该一起丢"


# --- 改动四：记得一半档改按段落粒度裁剪 ---

def test_trim_half_paragraph_granularity_keeps_whole_paragraphs(tmp_path):
    engine = make_engine(tmp_path)
    paragraphs = [
        "第一段。" * 10,   # 40 字
        "第二段。" * 10,   # 40 字
        "第三段。" * 10,   # 40 字
        "第四段。" * 10,   # 40 字
    ]
    raw = "\n\n".join(paragraphs)
    trimmed = engine.trim_by_level(raw, "half", "daily")

    # 保留的内容必须由完整段落（或对某一段的窗口截断）拼成，不是逐字符乱切
    kept_parts = trimmed.split("\n\n")
    for part in kept_parts:
        assert any(part in p or p.startswith(part) for p in paragraphs), (
            f"裁剪结果里的片段不是某个原段落的整段或前缀窗口: {part!r}"
        )
    total_len = sum(len(p) for p in paragraphs)
    ratio = len(trimmed.replace("\n\n", "")) / total_len
    assert 0.35 <= ratio <= 0.70  # 45%-60% 目标区间，留边界宽容度


def test_trim_half_falls_back_to_char_window_without_paragraph_breaks(tmp_path):
    engine = make_engine(tmp_path)
    raw = _CLEAN_DREAM_TEXT * 3  # 没有空行分段
    trimmed = engine.trim_by_level(raw, "half", "daily")
    assert trimmed in raw  # 退化到旧的整篇字符窗口裁法，必须是连续子串
    ratio = len(trimmed) / len(raw)
    assert 0.40 <= ratio <= 0.65


# --- 改动五：DREAM_FORCE_LEVEL ---

@pytest.mark.asyncio
async def test_dream_force_level_scene_alias_forces_glimpse(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAM_FORCE", "1")
    monkeypatch.setenv("DREAM_FORCE_LEVEL", "scene")
    dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy, memory_levels=[1.0, 0.0, 0.0, 0.0])  # 骰子本该永远是 full

    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    assert result["level"] == "glimpse", "DREAM_FORCE_LEVEL=scene 应别名映射到内部 glimpse 档，跳过骰子"


@pytest.mark.asyncio
async def test_dream_force_level_requires_dream_force_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("DREAM_FORCE", raising=False)
    monkeypatch.setenv("DREAM_FORCE_LEVEL", "scene")  # 没有 DREAM_FORCE=1，不该生效
    dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy, memory_levels=[1.0, 0.0, 0.0, 0.0])

    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    assert result["level"] == "full", "没有 DREAM_FORCE=1 时 DREAM_FORCE_LEVEL 不该生效"


@pytest.mark.asyncio
async def test_dream_force_level_invalid_value_falls_back_to_roll(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DREAM_FORCE", "1")
    monkeypatch.setenv("DREAM_FORCE_LEVEL", "not_a_real_level")
    dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy, memory_levels=[1.0, 0.0, 0.0, 0.0])

    with caplog.at_level("WARNING"):
        result = await engine.nightly_dream()

    assert result["dreamed"] is True
    assert result["level"] == "full", "非法档位应忽略并照常走骰子，不该炸管线"
    assert "不是合法档位" in caplog.text


@pytest.mark.asyncio
async def test_dream_force_level_emotion_still_applies_tone_linkage(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAM_FORCE", "1")
    monkeypatch.setenv("DREAM_FORCE_LEVEL", "emotion")
    dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy, emotion_negative_bias=1.0)  # 100% 触发联动改判
    monkeypatch.setattr(engine, "roll_tone", lambda: "daily")  # 骰子照常但结果固定，方便断言联动

    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    assert result["level"] == "emotion"
    assert result["tone"] in ("anxious", "nightmare"), (
        "DREAM_FORCE_LEVEL 强制指定档位时，只剩情绪档的基调联动改判仍要照常生效"
    )


# --- _is_prose_like 判定粒度改为全文，不误杀交替结构 ---

def test_is_prose_like_accepts_alternating_structure_with_short_chaotic_lines():
    assert _is_prose_like(_ALTERNATING_DREAM_TEXT) is True


def test_is_prose_like_full_text_granularity_not_per_segment():
    # 构造一段整体密度足够、但夹杂几行短促无标点混沌行的合法输出——
    # v1 的逐段/逐行判定会因为这些短行把整篇误杀；v2 只看全文密度。
    text = (
        "她把信递过来，我伸手去接，指尖先碰到纸角，风从门缝里钻进来，"
        "灯光晃了一下又稳住，桌上的杯子还是温的。\n"
        "台灯\n钥匙\n楼梯\n手表\n雨声\n"
        "楼下的邻居还在搬东西，声音断断续续传上来，我没有回头。"
    )
    assert _is_prose_like(text) is True
