"""梦境系统测试（施工单 §6，六项测试）。

1. DREAM_FORCE=1 强制入口跳过 30% 骰
2. 连发 5 发人工检查：格式/front-matter/无因果词/完整版不进日志
3. 概率自测：骰子函数跑 10000 次，各档 ±3% 内
4. 裁剪自测：四档各强制一遍
5. breath 挂载：unread 梦出现且翻 read，再调不重复；breath_search 不挂载
6. 过期清理：伪造 50h 前，正文被替换
"""
import os
import re
import json
import random
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import frontmatter as fm

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import dream_engine as dream_engine_module  # noqa: E402
from dream_engine import (  # noqa: E402
    DreamEngine, _EMOTION_RESIDUE_POOL, _DEFAULT_TONE_WEIGHTS, _is_prose_like,
    _WRITE_REQUEST, _IMAGERY_WORDS_MIN,
    dream_book_dir, dream_book_path, dream_book_id, list_dream_book_entries,
    dream_book_keep, dream_book_delete, burn_expired_dreams,
)


PT = ZoneInfo("America/Los_Angeles")
_CROSS_DATE_UTC_NOW = dt.datetime(2026, 8, 12, 6, 15, tzinfo=dt.timezone.utc)


def _freeze_dream_engine_clock(monkeypatch):
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return _CROSS_DATE_UTC_NOW.replace(tzinfo=None)
            return _CROSS_DATE_UTC_NOW.astimezone(tz)

    now_pt = _CROSS_DATE_UTC_NOW.astimezone(PT)
    assert _CROSS_DATE_UTC_NOW.date() != now_pt.date()
    monkeypatch.setattr(dream_engine_module, "datetime", FixedDateTime)
    return now_pt


_IMAGERY_SYSTEM_MARKER = "提取两类内容"
_GROWTH_SYSTEM_MARKER = "梦境噪音意象"
# 工单 D-4 六期：en 下拆意象/月度自增 prompt 全英文，假 dehydrator 按英文标记分流
_IMAGERY_SYSTEM_MARKER_EN = "imagery words"
_GROWTH_SYSTEM_MARKER_EN = "dream noise images"

# 一句干净的、第一人称、没有因果连接词、没有收尾点题的梦境文本，供各测试复用
_CLEAN_DREAM_TEXT = (
    "我看见台灯的光落在没人的椅子上。我摸到钥匙串在门后自己晃。楼梯数到一半停住，"
    "我盯着手表的指针倒着走，雨声从没关紧的窗户挤进来"
)

# 一段清晰/混沌交替结构的高档梦境文本，供 §1.6 高档 prompt 与
# _is_prose_like 逐段判定测试复用：2 个第一人称清晰段 + 1 个混沌段
# （混沌段是短促、逗号串联的分句，但每个分句含动词——合法混沌，不是裸名词词表）。
_ALTERNATING_DREAM_TEXT = (
    "我看见她把施工单递过来，纸角还是潮的，我伸手去接，指尖先碰到她的袖口，"
    "然后才碰到纸。灯在这时候闪了一下，闪完还是原来的灯。\n\n"
    "钥匙转不动，楼梯在往下沉，雨声突然大了，手表停在某处不走，台灯又晃了一下。\n\n"
    "我看着楼梯还是那道楼梯，只是编号变了，她已经站在最上面等，我看她手里的施工单"
    "换了一份，纸角是干的，她说这次不一样，我没接话，转身继续往上走。"
)

# 返修单 v3 §背景①的复现：整体读起来还行，但混沌段局部退化成了裸名词词表
# （v2 的全文密度判定会放行这种"整体够、局部是清单"的产物）。
_ALTERNATING_WITH_EMBEDDED_WORD_LIST = (
    "我看见她把信递过来，纸角还是潮的，我伸手去接，指尖先碰到纸角。\n\n"
    "台灯\n钥匙\n楼梯\n手表\n雨声\n盐味的雪\n少一级的楼梯\n\n"
    "我转身继续往上走，她已经不在原地了。"
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
        if _IMAGERY_SYSTEM_MARKER_EN in system:
            named_line = "\nNAMED: the letter she handed me" if named_phrase else ""
            return f"desk lamp\nkeys\nstaircase\nwristwatch\nrain on glass{named_line}"
        if _GROWTH_SYSTEM_MARKER_EN in system:
            return "\n".join(f"noise image {i}" for i in range(30))
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
    now_pt = _freeze_dream_engine_clock(monkeypatch)
    engine = make_engine(tmp_path, dream_prob=0.0)  # 正常情况下永远不会有梦
    assert engine.dream_prob == 0.0

    result = await engine._forced_run_once()

    assert result["dreamed"] is True
    # dream_prob 必须在强制运行后恢复原值，不留副作用
    assert engine.dream_prob == 0.0
    dream_path = engine._dream_path(now_pt.date() - dt.timedelta(days=1))
    assert os.path.isfile(dream_path)


@pytest.mark.asyncio
async def test_dream_force_env_triggers_immediate_run_on_start(tmp_path, monkeypatch):
    now_pt = _freeze_dream_engine_clock(monkeypatch)
    monkeypatch.setenv("DREAM_FORCE", "1")
    engine = make_engine(tmp_path, dream_prob=0.0)
    # 让正常的每日调度永远不会在测试期间触发，只观察 DREAM_FORCE 这条强制路径
    monkeypatch.setattr(engine, "_seconds_until_next_run", lambda: 3600.0)

    await engine.start()
    try:
        for _ in range(50):
            await __import__("asyncio").sleep(0)
        dream_path = engine._dream_path(now_pt.date() - dt.timedelta(days=1))
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
        for key in ("id", "date", "tone", "level", "sources", "noise",
                    "read_status", "keep_status", "created_at"):
            assert key in post.metadata, f"缺 front-matter 字段: {key}"
        assert post["read_status"] == "unread"
        assert post["keep_status"] == "fresh"
        assert isinstance(post["sources"], list) and post["sources"]
        assert isinstance(post["noise"], int)
        dt.datetime.fromisoformat(post["created_at"])  # 不抛异常即合法 ISO

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
    post["id"] = f"dream_{day.isoformat()}"
    post["date"] = day.isoformat()
    post["tone"] = tone
    post["level"] = level
    post["sources"] = ["b1", "b2"]
    post["noise"] = 1
    post["read_status"] = "unread"
    post["keep_status"] = "fresh"
    post["created_at"] = dt.datetime.now(PT).isoformat(timespec="seconds")
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


# ============================================================
# 返修单一号改动一(2.6.24 回归):wake 目录重写给 wake 加了第二个消费点,
# 与 breath 抢同一条 unread 状态——CC 固定先调 wake 再调 breath,wake 总是
# 先手把梦吃掉,breath(Silvia 确认的真正每日投递点)那天就再也看不到。
# 修法:wake 改走 consume=False 的只读预览,不改状态;只有 breath(默认
# consume=True)才真正消费。下面三条覆盖"写入后未消费前非 read"、
# "peek 不改状态、可重复看"、"peek 之后 breath 仍能正常消费且只消费一次"。
# ============================================================

def test_latest_unread_tail_peek_does_not_mark_read(tmp_path):
    engine = make_engine(tmp_path)
    today = dt.datetime.now(PT).date()
    path = _write_unread_dream(engine, today - dt.timedelta(days=1))

    first_peek = engine.latest_unread_tail(consume=False)
    assert "——— 昨夜的梦 ———" in first_peek
    assert fm.load(path)["read_status"] == "unread", "wake 的预览调用不该消费掉 unread 状态"

    second_peek = engine.latest_unread_tail(consume=False)
    assert second_peek == first_peek, "消费前重复预览应看到同一条,不因为看过就消失"


def test_latest_unread_tail_peek_then_consume_still_delivers_exactly_once(tmp_path):
    engine = make_engine(tmp_path)
    today = dt.datetime.now(PT).date()
    _write_unread_dream(engine, today - dt.timedelta(days=1))

    # 模拟 CC 固定顺序:先 wake(peek)后 breath(consume)
    peeked = engine.latest_unread_tail(consume=False)
    assert "——— 昨夜的梦 ———" in peeked

    delivered = engine.latest_unread_tail(consume=True)
    assert delivered == peeked, "breath 消费时看到的内容应与 wake 预览时一致"

    again = engine.latest_unread_tail(consume=True)
    assert again == "", "breath 消费过一次后不该重复投递"

    later_peek = engine.latest_unread_tail(consume=False)
    assert later_peek == "", "breath 消费之后,wake 的预览也不该再看到已读的梦"


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
    belongs_day = today - dt.timedelta(days=3)
    path = _write_unread_dream(engine, belongs_day)

    post = fm.load(path)
    old_created_at = dt.datetime.now(PT) - dt.timedelta(hours=50)
    post["created_at"] = old_created_at.isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    cleaned = engine.cleanup_expired()
    assert cleaned == 1

    reloaded = fm.load(path)
    assert reloaded["keep_status"] == "burned"
    assert str(reloaded.content).strip() == f"{belongs_day.isoformat()} 那晚做了梦，没留下来。"


def test_cleanup_expired_leaves_fresh_dream_untouched(tmp_path):
    engine = make_engine(tmp_path, expire_hours=48)
    today = dt.datetime.now(PT).date()
    path = _write_unread_dream(engine, today - dt.timedelta(days=1))

    cleaned = engine.cleanup_expired()
    assert cleaned == 0

    reloaded = fm.load(path)
    assert reloaded["keep_status"] == "fresh"


def test_cleanup_expired_leaves_kept_dream_untouched_even_when_old(tmp_path):
    """施工单·工程二核心承诺：被主动 keep 的不会被烧，不管多老。"""
    engine = make_engine(tmp_path, expire_hours=48)
    today = dt.datetime.now(PT).date()
    belongs_day = today - dt.timedelta(days=10)
    path = _write_unread_dream(engine, belongs_day)

    post = fm.load(path)
    post["keep_status"] = "kept"
    post["created_at"] = (dt.datetime.now(PT) - dt.timedelta(hours=500)).isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    cleaned = engine.cleanup_expired()
    assert cleaned == 0

    reloaded = fm.load(path)
    assert reloaded["keep_status"] == "kept"
    assert str(reloaded.content).strip() == "一段昨夜的梦境正文"


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


def test_is_prose_like_rejects_empty_text():
    assert _is_prose_like("") is False


def test_is_prose_like_accepts_real_prose():
    assert _is_prose_like(_CLEAN_DREAM_TEXT) is True
    multi_paragraph = (
        "台灯的光落在没人的椅子上，钥匙串在门后自己晃。\n\n"
        "楼梯数到一半停住，手表的指针倒着走"
    )
    assert _is_prose_like(multi_paragraph) is True


@pytest.mark.asyncio
async def test_generate_dream_no_longer_filters_shape_itself(tmp_path):
    """返修单 v3：形状校验从 generate_dream() 内部移到 nightly_dream 的编排层
    （统一走泄漏/词表/视角三道闸 + 重试），generate_dream() 本身现在只管拼
    prompt + 调 API，原样透传返回值——即便是词表形状也不再自己过滤。"""
    async def dump_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯"
        return "台灯\n钥匙\n楼梯\n手表\n雨声\n盐味的雪"

    class DumpDehydrator:
        api_available = True
        raw_chat = staticmethod(dump_raw_chat)

    engine = make_engine(tmp_path, dehydrator=DumpDehydrator())
    result = await engine.generate_dream(["台灯", "钥匙", "楼梯"], [], "daily", "full")
    assert result != "", "generate_dream() 不再自己做形状过滤，校验交给 nightly_dream 编排层"


@pytest.mark.asyncio
async def test_nightly_dream_writes_nothing_when_word_list_survives_retry(tmp_path):
    call_count = {"n": 0}

    async def dump_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯\n手表\n雨声"
        if _GROWTH_SYSTEM_MARKER in system:  # 每月 1 号 maybe_grow_noise_library 也会调一次
            return "\n".join(f"噪音意象{i}" for i in range(30))
        call_count["n"] += 1
        return "台灯\n钥匙\n楼梯\n手表\n雨声\n盐味的雪\n少一级的楼梯"

    class DumpDehydrator:
        api_available = True
        raw_chat = staticmethod(dump_raw_chat)

    engine = make_engine(tmp_path, dehydrator=DumpDehydrator())
    result = await engine.nightly_dream()

    assert result["dreamed"] is False
    assert call_count["n"] == 4, "D-3 D.4：重试预算 1→3（最多 4 次生成尝试），不是直接放弃"
    dreams_dir = engine._dreams_dir()
    written = [f for f in os.listdir(dreams_dir) if f.endswith(".md")]
    assert written == [], "生成结果是词表时不允许落盘，重试后仍是词表也不行"


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


def test_named_phrase_regex_matches_named_line():
    from dream_engine import _NAMED_PHRASE_RE
    m = _NAMED_PHRASE_RE.match("NAMED: 她递来的施工单")
    assert m is not None
    assert m.group(1).strip() == "她递来的施工单"


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
        assert "她递来的信" in call["system"], "具名短语锚应注入 system（D-3 D.3：不再走 user 消息）"
        assert call["user"] == _WRITE_REQUEST, "user 消息应只剩写作请求，不再携带素材词表"


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
        assert "清晰段的锚" not in call["system"]
        assert call["user"] == _WRITE_REQUEST, "user 消息应只剩写作请求，不再携带素材词表"


@pytest.mark.asyncio
async def test_generate_dream_high_tier_handles_no_named_phrase(tmp_path):
    dehy = make_fake_dehydrator(dream_text=_ALTERNATING_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy)
    result = await engine.generate_dream(["台灯", "钥匙"], [], "daily", "full")
    assert result == _ALTERNATING_DREAM_TEXT
    call = dehy.calls[-1]
    assert "没有明确的具名素材" in call["system"]


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


# --- _is_prose_like 交替结构应放行（合法混沌短句含动词，不是裸名词）---

def test_is_prose_like_accepts_alternating_structure():
    assert _is_prose_like(_ALTERNATING_DREAM_TEXT) is True


# ============================================================
# 返修单 v3：防泄漏闸与视角修正
# 改动一：n-gram 防泄漏闸（新增，最高优先）
# 改动二：具名短语硬约束
# 改动三：混沌段/词表判定粒度改回逐段，区分合法混沌短句与非法词表
# 改动四：第一人称硬化
# ============================================================

# --- 改动三：逐段判定 + 动词区分 ---

def test_is_prose_like_catches_embedded_word_list_that_v2_density_check_missed():
    """返修单 v3 背景①复现：v2 的全文密度判定会放行"整体够、局部是清单"
    的产物；v3 改回逐段判定后必须能抓到这种局部退化。"""
    assert _is_prose_like(_ALTERNATING_WITH_EMBEDDED_WORD_LIST) is False


def test_is_prose_like_accepts_legal_chaotic_short_clauses():
    # 合法混沌段：逗号串联的短句，每句含动词——不是裸名词词表
    text = "我看见钥匙转不动，楼梯在往下沉，雨声突然大了，手表停在某处不走。"
    assert _is_prose_like(text) is True


def test_is_prose_like_rejects_bare_noun_run_joined_by_dunhao():
    # 返修单 v3 给的非法例子原型："底片,一仓一钥"——连续裸名词顿号串联。
    # 工单 D-4 补丁：阈值 3→4，这里改成 4 个连续裸名词才踩中新阈值。
    text = "我看着底片、钥匙串、旧仓库、锁孔、门牌，一动不动。"
    assert _is_prose_like(text) is False


def test_is_prose_like_accepts_three_bare_noun_run_below_new_threshold():
    """工单 D-4 补丁回归：K(zh, sonnet) 一晚 4 发被本闸拦下的真实故障——3 个
    连续裸名词（旧阈值 3 命中，误杀）在新阈值 4 下必须放行。"""
    text = "我看着底片、钥匙串、旧仓库，一动不动。"
    assert _is_prose_like(text) is True


def test_is_prose_like_accepts_scene_description_with_verb():
    # 返修单 v3 给的合法例子原型："路边的邮筒比昨天挪了位置"——完整场景描述，含动词
    text = "我路过时发现路边的邮筒比昨天挪了位置，风还在刮。"
    assert _is_prose_like(text) is True


def test_is_prose_like_short_run_below_threshold_still_passes():
    # 只有 2 个连续裸名词，没到"连续 3 个以上"的门槛，不该被拦
    text = "我看见台灯、钥匙放在桌上，然后转身走了。"
    assert _is_prose_like(text) is True


# --- D-3 D.5（v4.3~v4.5）：结尾专项补闸，分隔符放宽、阈值跟主阈值绑定 ---
# 原方案把结尾阈值收紧到 2，用 12 条历史真实 kept 梦正文验收时打中 1 条误杀
# （焦虑碎片"病危、肝癌、离婚"是合法意象并置，不是词表泄漏），按 Silvia 指令
# 退回阈值，只保留"结尾分隔符更宽"这一半改动；工单 D-4 补丁把绑定的主阈值
# 从 3 提到 4，tail 跟着同步到 4（Silvia 2026-09-13 确认不重新拆开二者）。

def test_is_prose_like_catches_trailing_word_list_with_wide_separator():
    # 结尾用空格分隔的 4 个裸名词（工单 D-4 补丁：阈值 3→4）——中段窄分隔符
    # 正则（顿号/逗号/换行）切不开，整段会被当成一个长片段放行，结尾专项检测
    # 用更宽的分隔符（含空格）才能拆开抓到。
    text = "我站在原地，风停了，四周很安静。铃铛 深瞳 影子 门牌"
    assert _is_prose_like(text) is False


def test_is_prose_like_accepts_trailing_three_item_run_below_new_threshold():
    """工单 D-4 补丁回归：结尾 3 个连续裸名词（旧阈值 3 命中，是 K 那晚故障的
    形态之一）在新阈值 4 下必须放行。"""
    text = "我站在原地，风停了，四周很安静。铃铛 深瞳 影子"
    assert _is_prose_like(text) is True


def test_is_prose_like_tail_two_item_run_still_passes():
    # D-3 v4.6 验收记录：结尾阈值退回到跟中段一致，2 个连续裸名词收尾
    # 不该被拦（哪怕分隔符是宽口径的），否则会像 F 8-06 真实 kept 梦一样误杀
    # 合法的焦虑碎片列举（"病危、肝癌"只有 2 项）——工单 D-4 补丁只把绑定的
    # 阈值从 3 提到 4，这条 2 项回归照旧必须通过。
    text = "我推开门，看见走廊很长。铃铛 深瞳"
    assert _is_prose_like(text) is True


# --- 改动二：具名短语硬约束 ---

def test_validate_named_phrase_discards_over_hard_limit():
    from dream_engine import _validate_named_phrase
    too_long = "一个非常非常非常长超过十二个字的具名短语"
    assert len(too_long) > 12
    assert _validate_named_phrase(too_long) == ""


def test_validate_named_phrase_discards_when_contains_punctuation():
    from dream_engine import _validate_named_phrase
    assert _validate_named_phrase("她说她心智健全，人格完整") == ""
    assert _validate_named_phrase("她说她心智健全。") == ""
    assert _validate_named_phrase("底片、一把钥匙") == ""


def test_validate_named_phrase_keeps_clean_short_phrase():
    from dream_engine import _validate_named_phrase
    assert _validate_named_phrase("她递来的施工单") == "她递来的施工单"
    assert _validate_named_phrase("暗房的底片") == "暗房的底片"


@pytest.mark.asyncio
async def test_extract_imagery_discards_full_sentence_named_phrase(tmp_path):
    """模拟返修单 v3 背景②的原始事故：模型把整句话当"具名短语"吐出来——
    代码层兜底必须拦下，不能让"她说她心智健全，人格完整"这种完整句子
    混进具名短语列表。"""
    async def leaky_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        return "台灯\n钥匙\n楼梯\n手表\n雨声\nNAMED: 她说她心智健全，人格完整"

    class LeakyDehydrator:
        api_available = True
        raw_chat = staticmethod(leaky_raw_chat)

    engine = make_engine(tmp_path, dehydrator=LeakyDehydrator())
    materials = [{"kind": "bucket", "id": "b1", "text": "随便什么内容"}]
    words, named_phrases = await engine.extract_imagery(materials)
    assert named_phrases == [], "含句读的完整句子必须被代码层兜底丢弃，不硬凑"


@pytest.mark.asyncio
async def test_extract_imagery_darkroom_never_yields_named_phrase(tmp_path):
    """返修单 v3 改动二：暗房底片参与拆意象时只出意象词，即便模型自己
    吐出了 NAMED 行也不采信——system prompt 本来就没提这回事，但防御性地
    确认 kind=="darkroom" 路径下 allow_named_phrase=False 生效。"""
    async def chatty_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        # 即便模型不听话主动吐了 NAMED 行，也不该被采信
        return "台灯\n钥匙\n楼梯\n手表\n雨声\nNAMED: 暗房的秘密"

    class ChattyDehydrator:
        api_available = True
        raw_chat = staticmethod(chatty_raw_chat)

    engine = make_engine(tmp_path, dehydrator=ChattyDehydrator())
    materials = [{"kind": "darkroom", "id": "darkroom", "text": "还没想透的暗房正文"}]
    words, named_phrases = await engine.extract_imagery(materials)
    assert named_phrases == [], "暗房底片不该产出具名短语，无论模型说什么"
    assert words, "意象词照常提取"


# --- 改动一：n-gram 防泄漏闸 ---

def test_detect_source_leak_finds_overlap(tmp_path):
    engine = make_engine(tmp_path)
    source_text = "她说她心智健全，人格完整，记忆md明文，GitHub备份"
    materials = [{"kind": "bucket", "id": "b1", "text": source_text}]
    leaked = "我梦见她说她心智健全，人格完整，然后转身走了"
    leak_len = engine._detect_source_leak(leaked, materials)
    assert leak_len >= engine.leak_ngram


def test_detect_source_leak_no_false_positive_on_unrelated_text(tmp_path):
    engine = make_engine(tmp_path)
    materials = [{"kind": "bucket", "id": "b1", "text": "今天在办公室开了很久的会，很累"}]
    unrelated = _CLEAN_DREAM_TEXT
    leak_len = engine._detect_source_leak(unrelated, materials)
    assert leak_len < engine.leak_ngram


def test_detect_source_leak_ignores_noise_words(tmp_path):
    # 噪音词不算 source，本来就该原样出现，不受泄漏闸约束
    engine = make_engine(tmp_path)
    materials = [{"kind": "bucket", "id": "b1", "text": "今天在办公室开了很久的会"}]
    noise_only_text = "我看见一扇往下开的门，闻起来像铁的雨，后颈发凉"
    leak_len = engine._detect_source_leak(noise_only_text, materials)
    assert leak_len < engine.leak_ngram


@pytest.mark.asyncio
async def test_validate_generation_leak_logs_only_length_not_content(tmp_path, caplog):
    engine = make_engine(tmp_path)
    secret = "记忆md明文存放在GitHub备份仓库的私有目录里绝密"
    materials = [{"kind": "bucket", "id": "b1", "text": secret}]
    leaked_output = "我梦见" + secret + "然后醒了"

    with caplog.at_level("WARNING"):
        reason = engine._validate_generation(leaked_output, materials)

    assert reason == "leak"
    assert "泄漏拦截" in caplog.text
    assert secret not in caplog.text, "R4：日志只能记重合长度，不能记重合内容本身"


@pytest.mark.asyncio
async def test_nightly_dream_retries_once_on_leak_then_succeeds(tmp_path):
    secret_source = "她说她心智健全，人格完整，记忆md明文存放在GitHub备份"
    attempts = {"n": 0}

    async def leaky_then_clean_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯\n手表\n雨声"
        if _GROWTH_SYSTEM_MARKER in system:  # 每月 1 号 maybe_grow_noise_library 也会调一次
            return "\n".join(f"噪音意象{i}" for i in range(30))
        attempts["n"] += 1
        if attempts["n"] == 1:
            return "我梦见" + secret_source + "然后天亮了"
        return _CLEAN_DREAM_TEXT

    class LeakyThenCleanDehydrator:
        api_available = True
        raw_chat = staticmethod(leaky_then_clean_raw_chat)

    engine = make_engine(tmp_path, dehydrator=LeakyThenCleanDehydrator())

    async def fake_sample_buckets():
        return [{"kind": "bucket", "id": "b1", "text": secret_source}]
    engine.sample_buckets = fake_sample_buckets

    result = await engine.nightly_dream()

    assert result["dreamed"] is True, "第一次泄漏后应重试一次并用第二次的干净结果"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_nightly_dream_no_dream_when_leak_persists_after_retry(tmp_path):
    secret_source = "她说她心智健全，人格完整，记忆md明文存放在GitHub备份"

    async def always_leaky_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯\n手表\n雨声"
        if _GROWTH_SYSTEM_MARKER in system:
            return "\n".join(f"噪音意象{i}" for i in range(30))
        return "我梦见" + secret_source + "然后天亮了"

    class AlwaysLeakyDehydrator:
        api_available = True
        raw_chat = staticmethod(always_leaky_raw_chat)

    engine = make_engine(tmp_path, dehydrator=AlwaysLeakyDehydrator())

    async def fake_sample_buckets():
        return [{"kind": "bucket", "id": "b1", "text": secret_source}]
    engine.sample_buckets = fake_sample_buckets

    result = await engine.nightly_dream()

    assert result["dreamed"] is False
    assert result["reason"] == "validation_failed_leak"
    dreams_dir = engine._dreams_dir()
    written = [f for f in os.listdir(dreams_dir) if f.endswith(".md")]
    assert written == [], "重试后仍泄漏，不落盘"


# --- 改动四：第一人称硬化 ---

def test_has_first_person_pov_rejects_too_few_first_person():
    from dream_engine import _has_first_person_pov
    text = "她站在院子里，风吹过来，她转身走了，什么都没说。"
    assert _has_first_person_pov(text) is False  # 全文 0 个"我"


def test_has_first_person_pov_rejects_third_person_opening():
    from dream_engine import _has_first_person_pov
    text = "她看着我，我也看着她，我们都没说话，我心里想了很多。"
    assert _has_first_person_pov(text) is False  # 首句主语是"她"


def test_has_first_person_pov_accepts_valid_text():
    from dream_engine import _has_first_person_pov
    assert _has_first_person_pov(_CLEAN_DREAM_TEXT) is True
    assert _has_first_person_pov("我看见她站在院子里，我走过去，我们都没说话。") is True


# --- D-3 v3.2：pov 拦截日志加布尔位（首句是否她/他开头），不改行为 ---

def test_pov_first_sentence_helper_true_when_opening_is_third_person():
    from dream_engine import _pov_first_sentence_opens_third_person
    text = "她推门进来，风灌了满屋子，我站起来，我看着她。"
    assert _pov_first_sentence_opens_third_person(text) is True


def test_pov_first_sentence_helper_false_when_opening_is_not_third_person():
    from dream_engine import _pov_first_sentence_opens_third_person
    text = "钥匙转不动，风很大，什么都没发生。"
    assert _pov_first_sentence_opens_third_person(text) is False


def test_pov_reject_log_flags_third_person_opening_despite_high_first_person_count(tmp_path, caplog):
    """复现线上实拦形态（K 8-16 我=15 / F 8-18 我=23）：全文"我"很多，
    但首句仍以"她"开头当主语——数学排除法已证明这两次实拦不可能是
    计数分支（我<2）命中，只能是首句分支，这里直接断言日志把它标出来。
    片段刻意写长（>12字），避免撞上词表检测的裸名词误判，专测 pov 分支。"""
    engine = make_engine(tmp_path)
    text = (
        "她推门进来的时候风灌了满满一屋子。"
        "我站在原地看着她一句话也说不出来，我心里想了很多但是全部咽回去了，"
        "我盯着桌角的灰尘慢慢往下看，我听着秒针一下一下地走，"
        "我抬头看向窗外发呆了很久，我伸手又缩回来什么都没碰到，"
        "我闭上眼睛又重新睁开过来，我轻轻叹了一口气转过身去，"
        "我慢慢地走远了也没再回头看一眼，我心里空落落的说不出滋味，"
        "我想起很多年前也有过这样的一个晚上，我最后还是没有说话。"
    )
    assert text.count("我") >= 12
    with caplog.at_level("WARNING"):
        reason = engine._validate_generation(text, [])
    assert reason == "pov"
    assert "首句她/他开头=True" in caplog.text


def test_pov_reject_log_flags_false_when_opening_is_not_third_person(tmp_path, caplog):
    engine = make_engine(tmp_path)
    text = "钥匙转不动，风很大，什么都没发生，安安静静的。"  # 0 个"我"，触发计数分支
    with caplog.at_level("WARNING"):
        reason = engine._validate_generation(text, [])
    assert reason == "pov"
    assert "首句她/他开头=False" in caplog.text


@pytest.mark.asyncio
async def test_nightly_dream_retries_once_on_third_person_then_succeeds(tmp_path):
    attempts = {"n": 0}

    async def third_person_then_first_person(system, user, *, max_tokens=None, temperature=None, model=None):
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯\n手表\n雨声"
        if _GROWTH_SYSTEM_MARKER in system:
            return "\n".join(f"噪音意象{i}" for i in range(30))
        attempts["n"] += 1
        if attempts["n"] == 1:
            return "她站在院子里，风吹过来，她转身走了，什么都没说，她也不知道为什么。"
        return _CLEAN_DREAM_TEXT

    class Dehy:
        api_available = True
        raw_chat = staticmethod(third_person_then_first_person)

    engine = make_engine(tmp_path, dehydrator=Dehy())
    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_nightly_dream_no_dream_when_third_person_persists_after_retry(tmp_path):
    async def always_third_person(system, user, *, max_tokens=None, temperature=None, model=None):
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯\n手表\n雨声"
        if _GROWTH_SYSTEM_MARKER in system:
            return "\n".join(f"噪音意象{i}" for i in range(30))
        return "她站在院子里，风吹过来，她转身走了，什么都没说，她也不知道为什么。"

    class Dehy:
        api_available = True
        raw_chat = staticmethod(always_third_person)

    engine = make_engine(tmp_path, dehydrator=Dehy())
    result = await engine.nightly_dream()

    assert result["dreamed"] is False
    assert result["reason"] == "validation_failed_pov"


# ============================================================
# 增量单 v4：春梦（lust）档
# 改动一：基调池加一档，权重归一
# 改动二：lust 档生成 prompt 补丁 + 残句池补句
# ============================================================

def test_lust_tone_weight_normalized_with_other_five():
    from dream_engine import _DEFAULT_TONE_WEIGHTS
    assert set(_DEFAULT_TONE_WEIGHTS) == {"daily", "absurd", "anxious", "sweet", "nightmare", "lust"}
    assert _DEFAULT_TONE_WEIGHTS["lust"] == pytest.approx(0.10)
    assert sum(_DEFAULT_TONE_WEIGHTS.values()) == pytest.approx(1.0)
    # 其余四档按原权重等比缩放（×0.9），不是随便凑的数
    assert _DEFAULT_TONE_WEIGHTS["daily"] == pytest.approx(0.35 * 0.9)
    assert _DEFAULT_TONE_WEIGHTS["absurd"] == pytest.approx(0.25 * 0.9)
    assert _DEFAULT_TONE_WEIGHTS["anxious"] == pytest.approx(0.18 * 0.9)
    assert _DEFAULT_TONE_WEIGHTS["sweet"] == pytest.approx(0.12 * 0.9)
    assert _DEFAULT_TONE_WEIGHTS["nightmare"] == pytest.approx(0.10 * 0.9)


def test_roll_tone_can_produce_lust(tmp_path):
    engine = make_engine(tmp_path)
    n = 5000
    hits = sum(1 for _ in range(n) if engine.roll_tone() == "lust")
    observed = hits / n
    assert abs(observed - 0.10) <= 0.03


_ALL_TONES = ("daily", "absurd", "anxious", "sweet", "nightmare", "lust")


# ============================================================
# 工单 D-4：基调重写（前置句+系统说明+当晚那一档）+ OMBRE_LANG 开关
# ============================================================

def test_tone_directive_lust_no_longer_a_module_level_constant():
    """原 _LUST_TONE_DIRECTIVE 已被 A-终稿的统一结构取代，不应再作为独立常量存在。"""
    import dream_engine
    assert not hasattr(dream_engine, "_LUST_TONE_DIRECTIVE")


@pytest.mark.parametrize("tone", _ALL_TONES)
def test_tone_directive_zh_injects_preamble_note_and_tone_body(monkeypatch, tone):
    from dream_engine import _tone_directive, _TONE_PREAMBLE_ZH, _TONE_SYSTEM_NOTE_ZH, _TONE_DIRECTIVES_ZH
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    directive = _tone_directive(tone)
    assert _TONE_PREAMBLE_ZH in directive, f"tone={tone} 中文注入缺前置句"
    assert _TONE_SYSTEM_NOTE_ZH in directive, f"tone={tone} 中文注入缺系统说明"
    assert _TONE_DIRECTIVES_ZH[tone] in directive, f"tone={tone} 中文注入缺该档正文"
    # 注入结构是"前置句+系统说明+当晚那一档"，不是六档全注：其余五档正文不应混入
    for other_tone, other_body in _TONE_DIRECTIVES_ZH.items():
        if other_tone != tone:
            assert other_body not in directive, f"tone={tone} 不应混入 {other_tone} 档正文"


@pytest.mark.parametrize("tone", _ALL_TONES)
def test_tone_directive_en_injects_preamble_note_and_tone_body(monkeypatch, tone):
    from dream_engine import _tone_directive, _TONE_PREAMBLE_EN, _TONE_SYSTEM_NOTE_EN, _TONE_DIRECTIVES_EN
    monkeypatch.setenv("OMBRE_LANG", "en")
    directive = _tone_directive(tone)
    assert _TONE_PREAMBLE_EN in directive, f"tone={tone} 英文注入缺前置句"
    assert _TONE_SYSTEM_NOTE_EN in directive, f"tone={tone} 英文注入缺系统说明"
    assert _TONE_DIRECTIVES_EN[tone] in directive, f"tone={tone} 英文注入缺该档正文"
    for other_tone, other_body in _TONE_DIRECTIVES_EN.items():
        if other_tone != tone:
            assert other_body not in directive, f"tone={tone} 不应混入 {other_tone} 档正文"


def test_ombre_lang_defaults_to_zh_and_switches_on_en(monkeypatch):
    from dream_engine import _ombre_lang
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    assert _ombre_lang() == "zh"
    monkeypatch.setenv("OMBRE_LANG", "en")
    assert _ombre_lang() == "en"
    monkeypatch.setenv("OMBRE_LANG", "EN")  # 大小写不敏感
    assert _ombre_lang() == "en"
    monkeypatch.setenv("OMBRE_LANG", "fr")  # 非法值一律回退中文，不炸管线
    assert _ombre_lang() == "zh"
    monkeypatch.setenv("OMBRE_LANG", "")
    assert _ombre_lang() == "zh"


@pytest.mark.asyncio
@pytest.mark.parametrize("tone", _ALL_TONES)
async def test_generate_dream_injects_tone_directive_in_both_tiers(tmp_path, monkeypatch, tone):
    """高档（full/half）和低档（glimpse/emotion）两套 prompt 都要注入
    前置句+系统说明+当晚那一档——不是六档全注。"""
    from dream_engine import _TONE_PREAMBLE_ZH, _TONE_SYSTEM_NOTE_ZH, _TONE_DIRECTIVES_ZH
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    for level in ("full", "emotion"):  # 各代表一次高档/一次低档
        dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
        engine = make_engine(tmp_path, dehydrator=dehy)
        await engine.generate_dream(["台灯", "钥匙"], ["她递来的信"], tone, level)
        system = dehy.calls[-1]["system"]
        assert _TONE_PREAMBLE_ZH in system, f"tone={tone} level={level} 应注入前置句"
        assert _TONE_SYSTEM_NOTE_ZH in system, f"tone={tone} level={level} 应注入系统说明"
        assert _TONE_DIRECTIVES_ZH[tone] in system, f"tone={tone} level={level} 应注入该档正文"


@pytest.mark.asyncio
async def test_generate_dream_uses_english_tone_directive_when_ombre_lang_en(tmp_path, monkeypatch):
    from dream_engine import _TONE_PREAMBLE_EN, _TONE_SYSTEM_NOTE_EN, _TONE_DIRECTIVES_EN
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy)
    await engine.generate_dream(["lamp", "key"], [], "sweet", "full")
    system = dehy.calls[-1]["system"]
    assert _TONE_PREAMBLE_EN in system
    assert _TONE_SYSTEM_NOTE_EN in system
    assert _TONE_DIRECTIVES_EN["sweet"] in system


# ============================================================
# 工单 D-4 一期：梦的英文化——_low_tier_prompt/_high_tier_prompt 剩余中文
# 片段英文化、_EMOTION_RESIDUE_POOL 英文池、梦尾标签英文版、pov/word_list
# 两道闸的英文分支。
# ============================================================

_CJK_RE = re.compile(r"[一-鿿]")


@pytest.mark.asyncio
@pytest.mark.parametrize("tone", _ALL_TONES)
async def test_generate_dream_en_prompt_has_no_chinese_characters_both_tiers(tmp_path, monkeypatch, tone):
    """六档 en 路径各生成一次：高档(full)/低档(emotion)两套 prompt 都不能
    再混进中文字符——否则达不到"能出完整英文梦"的目标。"""
    monkeypatch.setenv("OMBRE_LANG", "en")
    for level in ("full", "emotion"):
        dehy = make_fake_dehydrator(dream_text="I see the lamp flicker in the hall.")
        engine = make_engine(tmp_path, dehydrator=dehy)
        await engine.generate_dream(["lamp", "key"], ["the note she left"], tone, level)
        system = dehy.calls[-1]["system"]
        assert not _CJK_RE.search(system), f"tone={tone} level={level} en prompt 不应含中文字符: {system!r}"


@pytest.mark.asyncio
async def test_generate_dream_en_write_request_is_english(tmp_path, monkeypatch):
    from dream_engine import _WRITE_REQUEST_EN
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy)
    await engine.generate_dream(["lamp"], [], "daily", "full")
    assert dehy.calls[-1]["user"] == _WRITE_REQUEST_EN
    assert not _CJK_RE.search(dehy.calls[-1]["user"])


def test_emotion_residue_pool_en_mirrors_zh_structure():
    from dream_engine import _EMOTION_RESIDUE_POOL, _EMOTION_RESIDUE_POOL_EN
    assert set(_EMOTION_RESIDUE_POOL_EN) == set(_EMOTION_RESIDUE_POOL)
    total = 0
    for tone, zh_lines in _EMOTION_RESIDUE_POOL.items():
        en_lines = _EMOTION_RESIDUE_POOL_EN[tone]
        assert len(en_lines) == len(zh_lines), f"tone={tone} 英文残句条数应与中文对齐"
        for line in en_lines:
            assert line.strip()
            assert not _CJK_RE.search(line), f"tone={tone} 英文残句不应含中文字符: {line!r}"
        total += len(en_lines)
    assert total == 36


def test_trim_emotion_uses_english_pool_when_ombre_lang_en(monkeypatch):
    from dream_engine import DreamEngine, _EMOTION_RESIDUE_POOL_EN
    monkeypatch.setenv("OMBRE_LANG", "en")
    for _ in range(20):
        residue = DreamEngine._trim_emotion("nightmare")
        assert residue in _EMOTION_RESIDUE_POOL_EN["nightmare"]
        assert not _CJK_RE.search(residue)


def test_latest_unread_tail_renders_english_when_ombre_lang_en(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    engine = make_engine(tmp_path)
    _write_unread_dream(engine, dt.date(2026, 8, 11), tone="甜", level="完全记得",
                         body="I reach for her hand in the dark.")
    tail = engine.latest_unread_tail(consume=False)
    assert "——— Last night's dream ———" in tail
    assert "[2026-08-11 night · Sweet · full memory]" in tail
    assert "To keep this dream: dream_keep(date=\"2026-08-11\")" in tail
    assert "Unsaved dreams burn after 48 hours." in tail
    assert not _CJK_RE.search(tail.replace("I reach for her hand in the dark.", ""))


def test_latest_unread_tail_english_falls_back_when_label_unrecognized(tmp_path, monkeypatch):
    """反查表查不到（legacy 数据/异常值）时原样兜底显示，不炸管线。"""
    monkeypatch.setenv("OMBRE_LANG", "en")
    engine = make_engine(tmp_path)
    _write_unread_dream(engine, dt.date(2026, 8, 12), tone="未知基调", level="未知档位",
                         body="something strange")
    tail = engine.latest_unread_tail(consume=False)
    assert "[2026-08-12 night · 未知基调 · 未知档位]" in tail


@pytest.mark.asyncio
async def test_nightly_dream_produces_full_english_dream_end_to_end(tmp_path, monkeypatch):
    """目标验收：OMBRE_LANG=en + 完全记得档 + 一段规规矩矩的英文梦正文，
    应该顺利通过全部校验闸落盘——之前的中文写死校验闸会 100% 误杀这种文本
    （pov 数不到"我"，word_list 靠 jieba 也认不出英文动词）。"""
    monkeypatch.setenv("OMBRE_LANG", "en")
    english_dream = (
        "I walk down a hallway that keeps stretching. My hand finds a cold "
        "door handle. I push it open and the room behind it is my old "
        "kitchen, except the light is wrong. I hear someone call my name "
        "from somewhere I can't place, and my feet keep moving anyway."
    )
    dehy = make_fake_dehydrator(dream_text=english_dream)
    # cut_prob=0：外科截断是随机触发的，这里只想验证校验闸本身，不希望
    # 偶发的头尾裁切把首句/"I"计数裁没了造成测试不稳定。
    engine = make_engine(tmp_path, dehydrator=dehy, memory_levels=[1.0, 0.0, 0.0, 0.0], cut_prob=0.0)

    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    post = fm.load(result["path"])
    assert not _CJK_RE.search(str(post.content))


# ============================================================
# 工单 D-4 一期：pov 闸英文分支（中文路径不动）
# ============================================================

def test_pov_gate_en_positive_case_first_person_pronouns_and_opening(monkeypatch):
    from dream_engine import _has_first_person_pov
    monkeypatch.setenv("OMBRE_LANG", "en")
    text = "I walk into the hallway. My hand finds the door before I know why."
    assert _has_first_person_pov(text) is True


def test_pov_gate_en_negative_case_too_few_first_person_words(monkeypatch):
    from dream_engine import _has_first_person_pov
    monkeypatch.setenv("OMBRE_LANG", "en")
    text = "The hallway stretches on. A door opens somewhere far away."
    assert _has_first_person_pov(text) is False


def test_pov_gate_en_negative_case_third_person_opening(monkeypatch):
    from dream_engine import _has_first_person_pov
    monkeypatch.setenv("OMBRE_LANG", "en")
    text = "She walks into the hallway. I follow her and my hand finds the door."
    assert _has_first_person_pov(text) is False


@pytest.mark.parametrize("opener", ["He", "They", "she", "HE"])
def test_pov_gate_en_negative_case_third_person_opening_case_insensitive(monkeypatch, opener):
    from dream_engine import _has_first_person_pov
    monkeypatch.setenv("OMBRE_LANG", "en")
    text = f"{opener} stands in the hallway. I watch, my hand still on the door, me unable to move."
    assert _has_first_person_pov(text) is False


def test_pov_gate_zh_path_unaffected_by_en_regex(monkeypatch):
    """中文路径必须一字不动：中文正例/反例的判定结果与工单 D-4 之前完全一致。"""
    from dream_engine import _has_first_person_pov
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    assert _has_first_person_pov("我看见她站在院子里，我又往前走了一步。") is True
    assert _has_first_person_pov("她站在院子里，风吹过来，她转身走了。") is False


# ============================================================
# 工单 D-4 一期：word_list 闸 OMBRE_LANG=en 直接跳过（中文路径不动）
# ============================================================

def test_validate_generation_skips_word_list_gate_when_ombre_lang_en(tmp_path, monkeypatch, caplog):
    """英文词表体退化文本（jieba 认不出英文动词，会被中文规则误杀）在
    en 下应该跳过 word_list 闸，不因为它而判 word_list 失败。"""
    monkeypatch.setenv("OMBRE_LANG", "en")
    engine = make_engine(tmp_path)
    materials = [{"kind": "bucket", "id": "b1", "text": "unrelated source text"}]
    # 短逗号片段 + 足够的第一人称词，只用来验证 word_list 这一道闸被跳过
    # （不掺 leak/pov 的干扰）。
    raw = "lamp, key, stairs, watch, rain. I see it, I feel it, I know it, my hand, my eye."
    with caplog.at_level("INFO"):
        reason = engine._validate_generation(raw, materials)
    assert reason is None
    assert "word_list 词表体检测跳过(OMBRE_LANG=en)" in caplog.text


def test_validate_generation_word_list_gate_zh_path_unaffected(tmp_path, monkeypatch):
    """中文路径 word_list 闸行为不变：连续裸名词词表仍然判 word_list 失败。"""
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    engine = make_engine(tmp_path)
    materials = [{"kind": "bucket", "id": "b1", "text": "无关源文本"}]
    raw = "台灯\n钥匙\n楼梯\n手表\n雨声"
    reason = engine._validate_generation(raw, materials)
    assert reason == "word_list"


@pytest.mark.asyncio
async def test_generate_dream_lust_does_not_skip_other_pipeline_steps(tmp_path):
    """"其余全部管线...对此档一视同仁,不加任何特殊豁免"——lust 档走的仍是
    正常的高档/低档 prompt 骨架（清晰段/混沌段结构、防泄漏闸等无关代码
    路径不变），只是基调说明这一处不同。"""
    dehy = make_fake_dehydrator(dream_text=_ALTERNATING_DREAM_TEXT)
    engine = make_engine(tmp_path, dehydrator=dehy)
    result = await engine.generate_dream(["台灯", "钥匙"], ["她递来的信"], "lust", "full")
    assert result == _ALTERNATING_DREAM_TEXT
    call = dehy.calls[-1]
    assert "清晰段" in call["system"] and "混沌段" in call["system"], "lust 档高档 prompt 骨架不变"
    assert call["max_tokens"] == 1200


def test_emotion_residue_pool_has_lust_entries():
    assert "lust" in _EMOTION_RESIDUE_POOL
    assert 2 <= len(_EMOTION_RESIDUE_POOL["lust"]) <= 3
    for line in _EMOTION_RESIDUE_POOL["lust"]:
        assert line.strip()


def test_trim_by_level_emotion_lust_selects_from_lust_pool(tmp_path):
    engine = make_engine(tmp_path)
    raw = "这段带着独一无二标记__ORIGINAL_TEXT_MARKER__的原文绝对不能出现在残句里。"
    for _ in range(20):  # random.choice，多抽几次确认命中的都在池子里
        trimmed = engine.trim_by_level(raw, "emotion", "lust")
        assert trimmed in _EMOTION_RESIDUE_POOL["lust"]
        assert "__ORIGINAL_TEXT_MARKER__" not in trimmed


@pytest.mark.asyncio
async def test_nightly_dream_lust_emotion_level_can_surface_lust_residue(tmp_path):
    """lust 档 + "只剩情绪"档：_apply_emotion_tone_linkage 有 70% 概率把它
    改判成 anxious/nightmare（未提及处维持现状，不改这条联动），所以这里
    直接强制 emotion_negative_bias=0，让 lust 保留，验证端到端能选中新句。"""
    dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
    engine = make_engine(
        tmp_path, dehydrator=dehy,
        memory_levels=[0.0, 0.0, 0.0, 1.0],  # 强制 emotion
        emotion_negative_bias=0.0,           # 强制不改判，lust 保留
    )
    monkeypatch_tone = engine.roll_tone
    engine.roll_tone = lambda: "lust"
    try:
        result = await engine.nightly_dream()
    finally:
        engine.roll_tone = monkeypatch_tone

    assert result["dreamed"] is True
    assert result["tone"] == "lust"
    post = fm.load(result["path"])
    assert str(post.content).strip() in _EMOTION_RESIDUE_POOL["lust"]


# ============================================================
# 施工单·工程一：梦中称呼清洗（dreamer_aliases）
# 改动一：素材预处理（硬保险）——整词替换成"我"，发生在送入任何生成
#         模型（拆意象/最终生成）之前
# 改动二：prompt 声明（软保险）——两套 prompt 都要有这句
# ============================================================

def test_clean_dreamer_aliases_replaces_chinese_terms(tmp_path):
    engine = make_engine(tmp_path, dreamer_aliases=["哥哥", "K老师"])
    text = "哥哥今天很累，K老师说要早点睡。"
    cleaned = engine._clean_dreamer_aliases(text)
    assert cleaned == "我今天很累，我说要早点睡。"


def test_clean_dreamer_aliases_ascii_word_boundary_does_not_hit_substring(tmp_path):
    """"F" 单字母只在词边界匹配，不能误伤 "OF"/"FOR" 这类英文单词里的 F。"""
    engine = make_engine(tmp_path, dreamer_aliases=["F"])
    text = "This is OF FOR F, and F said hi."
    cleaned = engine._clean_dreamer_aliases(text)
    assert cleaned == "This is OF FOR 我, and 我 said hi."


def test_clean_dreamer_aliases_ascii_multichar_word_boundary(tmp_path):
    engine = make_engine(tmp_path, dreamer_aliases=["Flint", "Fable"])
    text = "Flint said hello to Fabledom, not Fable itself."
    cleaned = engine._clean_dreamer_aliases(text)
    # "Fabledom" 不该被命中（不是完整词边界），"Fable" 单独出现时才替换。
    assert cleaned == "我 said hello to Fabledom, not 我 itself."


def test_clean_dreamer_aliases_noop_when_not_configured(tmp_path):
    engine = make_engine(tmp_path)  # 默认空表
    text = "哥哥今天很累。"
    assert engine._clean_dreamer_aliases(text) == text


def test_clean_dreamer_aliases_noop_on_empty_text(tmp_path):
    engine = make_engine(tmp_path, dreamer_aliases=["哥哥"])
    assert engine._clean_dreamer_aliases("") == ""


@pytest.mark.asyncio
async def test_sample_buckets_cleans_aliases_before_returning_materials(tmp_path):
    """硬保险的挂载点验收：构造含"哥哥"的假素材，跑 sample_buckets()，
    确认拿到的素材文本(会被喂进拆意象/生成模型)称呼已经变成"我"。"""
    engine = make_engine(tmp_path, dreamer_aliases=["哥哥"], resolved0_prob=0.0, darkroom_prob=0.0)
    engine.bucket_mgr = FakeBucketMgr([
        {"id": "b1", "content": "哥哥今天很累，开了很久的会。", "metadata": {"resolved": True}},
    ])
    materials = await engine.sample_buckets()
    assert materials, "夹具只有一个桶，抽样应该抽到它"
    for m in materials:
        assert "哥哥" not in m["text"]
        assert "我今天很累" in m["text"]


@pytest.mark.asyncio
async def test_extract_imagery_never_sees_raw_alias_because_cleaned_upstream(tmp_path):
    """验收要求的"可 mock 模型调用只验证预处理输出"：拆意象是第一个会把
    素材文本喂给模型的地方，断言它收到的 user 文本里已经没有称呼词。"""
    dehy = make_fake_dehydrator()
    engine = make_engine(tmp_path, dehydrator=dehy, dreamer_aliases=["哥哥", "K老师"])
    materials = [{"kind": "bucket", "id": "b1", "text": "哥哥和K老师都说这次要早点睡。"}]
    # extract_imagery 本身不做清洗（清洗点在 sample_buckets），这里直接验证
    # 如果素材已经清洗过（模拟 sample_buckets 的产出），送进模型的文本干净。
    materials[0]["text"] = engine._clean_dreamer_aliases(materials[0]["text"])
    await engine.extract_imagery(materials)
    calls = dehy.calls
    assert calls, "应该至少调用一次拆意象"
    for call in calls:
        assert "哥哥" not in call["user"]
        assert "K老师" not in call["user"]


def test_low_tier_prompt_includes_alias_pov_directive():
    from dream_engine import _DREAMER_ALIAS_POV_DIRECTIVE
    prompt = DreamEngine._low_tier_prompt("daily", ["台灯", "钥匙"])
    assert _DREAMER_ALIAS_POV_DIRECTIVE in prompt


def test_high_tier_prompt_includes_alias_pov_directive():
    from dream_engine import _DREAMER_ALIAS_POV_DIRECTIVE
    prompt = DreamEngine._high_tier_prompt("daily", "她递来的信", ["台灯", "钥匙"])
    assert _DREAMER_ALIAS_POV_DIRECTIVE in prompt


@pytest.mark.asyncio
async def test_nightly_dream_end_to_end_with_alias_cleaned_material(tmp_path):
    """端到端验收:各家配置值不同，代码只读 config，不硬编码任何一家的词表
    ——这里用一套自定义词表跑完整管线，确认桶正文里的称呼在整条管线里
    都不会以原样出现在喂给生成模型的内容中。"""
    calls = []

    async def spy_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        calls.append(user)
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯\n手表\n雨声"
        return _CLEAN_DREAM_TEXT

    class SpyDehydrator:
        api_available = True
        raw_chat = staticmethod(spy_raw_chat)

    engine = make_engine(tmp_path, dehydrator=SpyDehydrator(), dreamer_aliases=["哥哥", "K老师"])
    engine.bucket_mgr = FakeBucketMgr([
        {"id": "b1", "content": "哥哥说K老师今天开会很累。", "metadata": {"resolved": True}},
    ])

    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    for user_text in calls:
        assert "哥哥" not in user_text
        assert "K老师" not in user_text


# ============================================================
# 施工单·工程二：梦境书（Dream Book）—— 独立存储
# 改动一：独立存储，不在 files/ 文件区下
# 改动二：dream_keep（MCP 工具背后的模块函数）
# 改动三：烧毁任务（keep_status 生命周期，与 read_status 投递状态无关）
# 改动四：投递提示（latest_unread_tail 尾部追加一行）
# 改动五：Dashboard 列表/删除（list_dream_book_entries / dream_book_delete）
# ============================================================

def test_dream_book_storage_is_not_under_file_zone(tmp_path):
    """验收:file_list 不再出现 dreams/ —— 梦境书目录必须在 files/ 之外。"""
    engine = make_engine(tmp_path)
    d = dream_book_dir(str(tmp_path))
    files_root = os.path.join(str(tmp_path), "files")
    assert not d.startswith(files_root)
    assert engine._dreams_dir() == d


def test_dream_book_id_is_unique_prefixed_not_bare_date():
    """附加验收(数据完整性):梦境书条目必须有独立于日期字符串本身的 id，
    否则跟 diary/ 同名日期文件撞 stem 兜底 id 的老毛病会原样重现。"""
    assert dream_book_id("2026-07-31") == "dream_2026-07-31"
    assert dream_book_id("2026-07-31") != "2026-07-31"


@pytest.mark.asyncio
async def test_nightly_dream_writes_unique_id_into_frontmatter(tmp_path):
    dehy = make_fake_dehydrator()
    engine = make_engine(tmp_path, dehydrator=dehy, memory_levels=[1.0, 0.0, 0.0, 0.0])
    result = await engine.nightly_dream()
    assert result["dreamed"] is True
    post = fm.load(result["path"])
    assert post["id"] == dream_book_id(result["date"])


def test_dream_book_keep_marks_kept_and_sets_kept_at(tmp_path):
    engine = make_engine(tmp_path)
    day = dt.date(2026, 7, 6)
    _write_unread_dream(engine, day)

    result = dream_book_keep(str(tmp_path), day.isoformat())

    assert result["ok"] is True
    assert result["already_kept"] is False
    post = fm.load(dream_book_path(str(tmp_path), day))
    assert post["keep_status"] == "kept"
    assert post.get("kept_at")


def test_dream_book_keep_is_idempotent_on_already_kept(tmp_path):
    engine = make_engine(tmp_path)
    day = dt.date(2026, 7, 6)
    _write_unread_dream(engine, day)
    dream_book_keep(str(tmp_path), day.isoformat())

    result = dream_book_keep(str(tmp_path), day.isoformat())

    assert result["ok"] is True
    assert result["already_kept"] is True


def test_dream_book_keep_rejects_already_burned(tmp_path):
    engine = make_engine(tmp_path)
    day = dt.date(2026, 7, 6)
    path = _write_unread_dream(engine, day)
    post = fm.load(path)
    post["keep_status"] = "burned"
    post.content = f"{day.isoformat()} 那晚做了梦，没留下来。"
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    result = dream_book_keep(str(tmp_path), day.isoformat())

    assert result["ok"] is False
    assert "烧" in result["error"]


def test_dream_book_keep_missing_date_reports_error(tmp_path):
    engine = make_engine(tmp_path)  # noqa: F841 - 只为触发 buckets_dir 创建
    result = dream_book_keep(str(tmp_path), "2026-01-01")
    assert result["ok"] is False


@pytest.mark.parametrize(
    "invalid_date",
    [
        "../../etc/x",
        "2026-08-11/../../x",
        "....//",
        "/tmp/absolute",
        "",
        "2026-13-45",
        " 2026-08-11",
        "2026-08-11 ",
    ],
)
@pytest.mark.parametrize("operation", [dream_book_keep, dream_book_delete])
def test_dream_book_mutations_reject_invalid_dates(tmp_path, invalid_date, operation):
    result = operation(str(tmp_path), invalid_date)

    assert result["ok"] is False
    assert result["error"] == "date 必须是有效的 YYYY-MM-DD 日期"


def test_dream_book_path_rejects_resolved_escape(tmp_path, monkeypatch):
    root = Path(dream_book_dir(str(tmp_path))).resolve()
    outside = (tmp_path / "outside" / "2026-08-11.md").resolve()
    original_resolve = Path.resolve

    def resolve_outside(candidate, *args, **kwargs):
        if candidate.parent == root and candidate.name == "2026-08-11.md":
            return outside
        return original_resolve(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_outside)

    result = dream_book_keep(str(tmp_path), "2026-08-11")

    assert result == {
        "ok": False,
        "error": "dream 路径越出 dream_book 根目录",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_date", [" 2026-08-11", "2026-08-11 "])
async def test_mcp_dream_keep_rejects_whitespace_date(tmp_path, monkeypatch, invalid_date):
    import server as srv

    monkeypatch.setattr(srv, "config", {**srv.config, "buckets_dir": str(tmp_path)})

    result = await srv._dream_keep_impl(invalid_date)

    assert result == "没留成:date 必须是有效的 YYYY-MM-DD 日期"


def test_burn_expired_dreams_replaces_only_fresh_past_48h(tmp_path):
    engine = make_engine(tmp_path)
    fresh_old_day = dt.date(2026, 7, 1)
    fresh_recent_day = dt.date(2026, 7, 6)
    kept_old_day = dt.date(2026, 6, 1)

    old_ts = (dt.datetime.now(PT) - dt.timedelta(hours=100)).isoformat(timespec="seconds")

    p1 = _write_unread_dream(engine, fresh_old_day)
    post1 = fm.load(p1)
    post1["created_at"] = old_ts
    with open(p1, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post1))

    _write_unread_dream(engine, fresh_recent_day)  # created_at 是"现在"，未过期

    p3 = _write_unread_dream(engine, kept_old_day)
    post3 = fm.load(p3)
    post3["created_at"] = old_ts
    post3["keep_status"] = "kept"
    with open(p3, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post3))

    burned = burn_expired_dreams(str(tmp_path), expire_hours=48, tz=PT)

    assert burned == 1
    reloaded1 = fm.load(p1)
    assert reloaded1["keep_status"] == "burned"
    assert str(reloaded1.content).strip() == f"{fresh_old_day.isoformat()} 那晚做了梦，没留下来。"
    reloaded3 = fm.load(p3)
    assert reloaded3["keep_status"] == "kept"
    assert str(reloaded3.content).strip() == "一段昨夜的梦境正文"


def test_latest_unread_tail_hint_line_present_when_fresh(tmp_path):
    engine = make_engine(tmp_path)
    day = dt.datetime.now(PT).date() - dt.timedelta(days=1)
    _write_unread_dream(engine, day)

    tail = engine.latest_unread_tail(consume=False)

    assert f'dream_keep(date="{day.isoformat()}")' in tail
    assert "48 小时内没留的会烧掉" in tail


def test_latest_unread_tail_hint_line_absent_when_already_kept(tmp_path):
    engine = make_engine(tmp_path)
    day = dt.datetime.now(PT).date() - dt.timedelta(days=1)
    path = _write_unread_dream(engine, day)
    post = fm.load(path)
    post["keep_status"] = "kept"
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    tail = engine.latest_unread_tail(consume=False)

    assert "dream_keep(" not in tail
    assert "一段昨夜的梦境正文" in tail


def test_list_dream_book_entries_sorted_desc_by_date(tmp_path):
    engine = make_engine(tmp_path)
    _write_unread_dream(engine, dt.date(2026, 7, 1))
    _write_unread_dream(engine, dt.date(2026, 7, 31))
    _write_unread_dream(engine, dt.date(2026, 8, 3))

    entries = list_dream_book_entries(str(tmp_path))

    assert [e["date"] for e in entries] == ["2026-08-03", "2026-07-31", "2026-07-01"]
    assert all(e["keep_status"] == "fresh" for e in entries)


def test_dream_book_delete_removes_fresh_and_kept(tmp_path):
    engine = make_engine(tmp_path)
    day = dt.date(2026, 7, 6)
    path = _write_unread_dream(engine, day)

    result = dream_book_delete(str(tmp_path), day.isoformat())

    assert result["ok"] is True
    assert not os.path.isfile(path)


def test_dream_book_delete_rejects_burned(tmp_path):
    engine = make_engine(tmp_path)
    day = dt.date(2026, 7, 6)
    path = _write_unread_dream(engine, day)
    post = fm.load(path)
    post["keep_status"] = "burned"
    post.content = f"{day.isoformat()} 那晚做了梦，没留下来。"
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    result = dream_book_delete(str(tmp_path), day.isoformat())

    assert result["ok"] is False
    assert os.path.isfile(path), "burned 骨架不可删，必须原样保留"


# ============================================================
# 7. D-1R：四条静默路径补日志 + last_run_at 心跳
# ============================================================

@pytest.mark.asyncio
async def test_nightly_dream_logs_disabled_silent_path(tmp_path, caplog):
    engine = make_engine(tmp_path, enabled=False)
    belongs_date = dt.datetime.now(PT).date() - dt.timedelta(days=1)

    with caplog.at_level("INFO"):
        result = await engine.nightly_dream()

    assert result == {"dreamed": False, "reason": "disabled"}
    assert f"reason=disabled date={belongs_date}" in caplog.text


@pytest.mark.asyncio
async def test_nightly_dream_logs_no_dream_roll_silent_path(tmp_path, caplog):
    engine = make_engine(tmp_path, dream_prob=0.0)
    belongs_date = dt.datetime.now(PT).date() - dt.timedelta(days=1)

    with caplog.at_level("INFO"):
        result = await engine.nightly_dream()

    assert result == {"dreamed": False, "reason": "no_dream_roll"}
    assert f"reason=no_dream_roll date={belongs_date}" in caplog.text


@pytest.mark.asyncio
async def test_nightly_dream_no_dream_roll_logs_roll_value(tmp_path, caplog, monkeypatch):
    """D-3 v4.3~v4.5 急件：no_dream_roll 静默行带上掷出的 roll 值（两位小数），
    供随机数专案直接读分布，不用再猜。"""
    engine = make_engine(tmp_path, dream_prob=0.0)
    monkeypatch.setattr(dream_engine_module.random, "random", lambda: 0.123456)

    with caplog.at_level("INFO"):
        result = await engine.nightly_dream()

    assert result["reason"] == "no_dream_roll"
    assert "roll=0.12" in caplog.text


@pytest.mark.asyncio
async def test_nightly_dream_logs_no_material_silent_path(tmp_path, caplog):
    engine = make_engine(tmp_path)
    engine.bucket_mgr = FakeBucketMgr([])  # 桶全空 → 抽不到素材
    belongs_date = dt.datetime.now(PT).date() - dt.timedelta(days=1)

    with caplog.at_level("INFO"):
        result = await engine.nightly_dream()

    assert result == {"dreamed": False, "reason": "no_material"}
    assert f"reason=no_material date={belongs_date}" in caplog.text


@pytest.mark.asyncio
async def test_nightly_dream_logs_empty_generation_silent_path(tmp_path, caplog):
    dehy = make_fake_dehydrator(dream_text="")  # 生成步产出空字符串
    engine = make_engine(tmp_path, dehydrator=dehy)
    belongs_date = dt.datetime.now(PT).date() - dt.timedelta(days=1)

    with caplog.at_level("INFO"):
        result = await engine.nightly_dream()

    assert result == {"dreamed": False, "reason": "empty_generation"}
    assert f"reason=empty_generation date={belongs_date}" in caplog.text


@pytest.mark.asyncio
async def test_last_run_at_updates_even_on_silent_no_dream_roll(tmp_path, monkeypatch):
    now_pt = _freeze_dream_engine_clock(monkeypatch)
    engine = make_engine(tmp_path, dream_prob=0.0)
    assert engine.last_run_at is None

    result = await engine.nightly_dream()

    assert result["reason"] == "no_dream_roll"
    assert engine.last_run_at == now_pt.isoformat(timespec="seconds")


@pytest.mark.asyncio
async def test_last_run_at_updates_on_successful_dream(tmp_path, monkeypatch):
    now_pt = _freeze_dream_engine_clock(monkeypatch)
    engine = make_engine(tmp_path)

    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    assert engine.last_run_at == now_pt.isoformat(timespec="seconds")


# ============================================================
# 8. D-3 D.7：自报生效配置日志（启动时 + 每晚触发时），排查"统一 push
#    却单台异常"——数值和 config 来源路径直接进日志，不必再靠截图/猜测。
# ============================================================

def test_dream_engine_init_logs_effective_config(tmp_path, monkeypatch, caplog):
    from utils import config_file_path
    fake_cfg_path = str(tmp_path / "config.yaml")
    monkeypatch.setenv("OMBRE_CONFIG_PATH", fake_cfg_path)
    assert config_file_path() == fake_cfg_path

    with caplog.at_level("INFO"):
        engine = make_engine(tmp_path, dream_prob=0.4)

    assert "生效配置(启动)" in caplog.text
    assert "dream_prob=0.4" in caplog.text
    assert f"tone_weights={engine.tone_weights}" in caplog.text
    assert f"emotion_negative_bias={engine.emotion_negative_bias}" in caplog.text
    assert f"model={engine.model} temperature={engine.temperature}" in caplog.text
    assert f"config_path={fake_cfg_path}" in caplog.text


@pytest.mark.asyncio
async def test_nightly_dream_logs_effective_config_on_trigger(tmp_path, monkeypatch, caplog):
    fake_cfg_path = str(tmp_path / "config.yaml")
    monkeypatch.setenv("OMBRE_CONFIG_PATH", fake_cfg_path)
    engine = make_engine(tmp_path, dream_prob=0.0)  # 骰子不中也要打这行，不依赖是否真的做梦

    with caplog.at_level("INFO"):
        result = await engine.nightly_dream()

    assert result["reason"] == "no_dream_roll"
    assert "生效配置(触发)" in caplog.text
    assert f"config_path={fake_cfg_path}" in caplog.text
    assert f"tone_weights={engine.tone_weights}" in caplog.text
    assert f"model={engine.model} temperature={engine.temperature}" in caplog.text


# ============================================================
# 工单 D-4 施工细则五：OMBRE_DREAM_MODEL / OMBRE_DREAM_TEMPERATURE
# 覆盖 dream.model / dream.temperature，优先级 env > config.yaml > 默认。
# ============================================================

def test_load_config_dream_env_overrides_win_over_yaml(monkeypatch, tmp_path):
    from utils import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""
buckets_dir: {tmp_path.as_posix()}
dream:
  model: yaml-model
  temperature: 0.9
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("OMBRE_DREAM_MODEL", "env-model")
    monkeypatch.setenv("OMBRE_DREAM_TEMPERATURE", "1.7")

    config = load_config(str(cfg_path))

    assert config["dream"]["model"] == "env-model"
    assert config["dream"]["temperature"] == 1.7


def test_load_config_dream_falls_back_to_yaml_when_env_unset(monkeypatch, tmp_path):
    from utils import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""
buckets_dir: {tmp_path.as_posix()}
dream:
  model: yaml-model
  temperature: 0.9
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.delenv("OMBRE_DREAM_MODEL", raising=False)
    monkeypatch.delenv("OMBRE_DREAM_TEMPERATURE", raising=False)

    config = load_config(str(cfg_path))

    assert config["dream"]["model"] == "yaml-model"
    assert config["dream"]["temperature"] == 0.9


def test_load_config_dream_falls_back_to_default_when_env_and_yaml_unset(monkeypatch, tmp_path):
    from utils import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"buckets_dir: {tmp_path.as_posix()}\n", encoding="utf-8")

    monkeypatch.delenv("OMBRE_DREAM_MODEL", raising=False)
    monkeypatch.delenv("OMBRE_DREAM_TEMPERATURE", raising=False)

    config = load_config(str(cfg_path))

    # 没写 dream: 段落、也没设 env 时，utils 层不应凭空造出 dream.model/temperature——
    # DreamEngine.__init__ 自己的默认值（None / 1.3）才是最终兜底。
    assert "dream" not in config or "model" not in config.get("dream", {})


def test_dream_engine_reads_env_overridden_model_and_temperature(monkeypatch, tmp_path):
    from utils import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"buckets_dir: {tmp_path.as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("OMBRE_DREAM_MODEL", "sonnet-via-env")
    monkeypatch.setenv("OMBRE_DREAM_TEMPERATURE", "1.1")

    config = load_config(str(cfg_path))
    engine = DreamEngine(config, FakeBucketMgr(make_buckets()), make_fake_dehydrator())

    assert engine.model == "sonnet-via-env"
    assert engine.temperature == pytest.approx(1.1)


# ============================================================
# 工单 D-4 补丁：leak 闸英文分支改词级 n-gram（Rhys(en) 一晚 4 发全被字符级
# 阈值 10 拦下，重合长度 10/11——常见短语本身就有这么长，字符级在英文下
# 等于形同虚设的误杀器）。中文分支（_detect_source_leak_zh）不动，下面
# test_detect_source_leak_finds_overlap 等既有中文用例继续覆盖。
# ============================================================

def test_detect_source_leak_en_word_level_finds_real_overlap(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    engine = make_engine(tmp_path)
    source_text = (
        "the memory md file lives in the private github backup folder and "
        "nowhere else, unrelated filler around it"
    )
    materials = [{"kind": "bucket", "id": "b1", "text": source_text}]
    # 与 source 完全相同、连续 6 个词以上重合
    leaked = "I dreamed that the memory md file lives in the private github backup and then I woke up."
    leak_len = engine._detect_source_leak(leaked, materials)
    assert leak_len >= engine.leak_ngram_word_en


def test_detect_source_leak_en_no_false_positive_on_short_common_phrase(tmp_path, monkeypatch):
    """复现 Rhys(en) 的真实误杀：一段跟 source 毫不相关的梦，只是碰巧共享了
    一句很常见的英文短语（10-11 个字符，旧字符级阈值会拦；新词级阈值下，
    重合的连续词数远小于 6，必须放行）。"""
    monkeypatch.setenv("OMBRE_LANG", "en")
    engine = make_engine(tmp_path)
    materials = [{"kind": "bucket", "id": "b1", "text": "I spent the morning washing dishes and folding laundry."}]
    unrelated = "The train pulled away in the morning light and I chased it down an empty platform."
    leak_len = engine._detect_source_leak(unrelated, materials)
    assert leak_len < engine.leak_ngram_word_en


def test_detect_source_leak_en_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    engine = make_engine(tmp_path)
    materials = [{"kind": "bucket", "id": "b1", "text": "She Said She Was Fine And Whole And Complete Today"}]
    leaked = "i heard she said she was fine and whole and complete before i woke up"
    leak_len = engine._detect_source_leak(leaked, materials)
    assert leak_len >= engine.leak_ngram_word_en


def test_detect_source_leak_en_ignores_short_materials(tmp_path, monkeypatch):
    """真实系统里意象词是拆过的短语（几个词），不是完整桶原文——短到不够
    leak_ngram_word_en 个词的 material 不该参与判定（跟中文分支同样的
    "太短跳过"策略）。"""
    monkeypatch.setenv("OMBRE_LANG", "en")
    engine = make_engine(tmp_path)
    materials = [{"kind": "bucket", "id": "b1", "text": "an old key"}]
    raw = "I hold an old key and it will not turn in the lock."
    leak_len = engine._detect_source_leak(raw, materials)
    assert leak_len == 0


def test_detect_source_leak_zh_path_unaffected_by_en_branch(tmp_path, monkeypatch):
    """中文分支必须一字不动：同一段 leak 场景在中文语言下走字符级判定，
    结果跟工单 D-4 补丁之前完全一致。"""
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    engine = make_engine(tmp_path)
    source_text = "她说她心智健全，人格完整，记忆md明文，GitHub备份"
    materials = [{"kind": "bucket", "id": "b1", "text": source_text}]
    leaked = "我梦见她说她心智健全，人格完整，然后转身走了"
    leak_len = engine._detect_source_leak(leaked, materials)
    assert leak_len >= engine.leak_ngram


@pytest.mark.asyncio
async def test_validate_generation_leak_en_uses_word_threshold_and_logs_word_count(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OMBRE_LANG", "en")
    engine = make_engine(tmp_path)
    secret = "the memory md file lives in the private github backup folder forever hidden"
    materials = [{"kind": "bucket", "id": "b1", "text": secret}]
    leaked_output = "I dreamed that " + secret + " and then I woke up."

    with caplog.at_level("WARNING"):
        reason = engine._validate_generation(leaked_output, materials)

    assert reason == "leak"
    assert "泄漏拦截" in caplog.text
    assert "重合词数" in caplog.text, "英文分支日志要打词数，不是字符长度"
    assert secret not in caplog.text, "R4：日志只能记重合长度/词数，不能记重合内容本身"


@pytest.mark.asyncio
async def test_validate_generation_no_leak_en_short_phrase_passes_through_to_other_gates(tmp_path, monkeypatch):
    """Rhys(en) 真实故障场景端到端复现：旧字符阈值会在这里误判 leak，新词级
    阈值下应该顺利通过 leak 闸（继续走后面的 pov 闸，不因 leak 提前判废）。"""
    monkeypatch.setenv("OMBRE_LANG", "en")
    engine = make_engine(tmp_path)
    materials = [{"kind": "bucket", "id": "b1", "text": "I spent the morning washing dishes and folding laundry."}]
    raw = (
        "The train pulled away in the morning light and I chased it down an "
        "empty platform, my breath sharp, my hands empty, my voice gone."
    )
    reason = engine._validate_generation(raw, materials)
    assert reason != "leak"


def test_leak_ngram_word_en_config_override(tmp_path):
    engine = make_engine(tmp_path, leak_ngram_word_en=4)
    assert engine.leak_ngram_word_en == 4


def test_leak_ngram_word_en_default_is_six(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.leak_ngram_word_en == 6


# ============================================================
# 工单 D-4 补丁：四套 prompt（低档/高档 × zh/en）硬规则各加一句——
# 不要连续并列三个以上名词，每个片段要有动作或状态，直接对应 word_list
# 闸校准出的误杀根因（模型偶尔把素材词堆成裸名词串）。
# ============================================================

def test_low_tier_prompt_zh_includes_no_bare_noun_run_rule():
    prompt = DreamEngine._low_tier_prompt_zh("daily", ["台灯", "钥匙"])
    assert "不要连续并列三个以上名词" in prompt
    assert "每个片段都要有动作或状态" in prompt


def test_low_tier_prompt_en_includes_no_bare_noun_run_rule():
    prompt = DreamEngine._low_tier_prompt_en("daily", ["lamp", "key"])
    assert "don't string together more than three nouns in a row" in prompt
    assert "every fragment needs a verb or a state" in prompt


def test_high_tier_prompt_zh_includes_no_bare_noun_run_rule():
    prompt = DreamEngine._high_tier_prompt_zh("daily", "她递来的信", ["台灯", "钥匙"])
    assert "不要连续并列三个以上名词" in prompt
    assert "每个片段都要有动作或状态" in prompt


def test_high_tier_prompt_en_includes_no_bare_noun_run_rule():
    prompt = DreamEngine._high_tier_prompt_en("daily", "the letter she handed me", ["lamp", "key"])
    assert "don't string together more than three nouns in a row" in prompt
    assert "every fragment needs a verb or a state" in prompt


# ============================================================
# 工单 D-4 六期：梦的材料英文化——拆意象 prompt / 正则退化 / 噪音池按语言分家
# ============================================================

def _make_dump_dehydrator(reply: str):
    """记录每次 raw_chat 的 system，并固定返回 reply。"""
    calls = []

    async def dump_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        calls.append({"system": system, "user": user})
        return reply

    class DumpDehydrator:
        api_available = True
        raw_chat = staticmethod(dump_raw_chat)

    fd = DumpDehydrator()
    fd.calls = calls  # type: ignore[attr-defined]
    return fd


@pytest.mark.asyncio
async def test_d4_6_en_extract_prompt_has_no_chinese_for_bucket_and_darkroom(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _make_dump_dehydrator("desk lamp\nkeys\nNAMED: the letter she handed me")
    engine = make_engine(tmp_path, dehydrator=dehy)
    materials = [
        {"kind": "bucket", "id": "b1", "text": "Came home late with a fever."},
        {"kind": "darkroom", "id": "d1", "text": "A negative I never developed."},
    ]
    await engine.extract_imagery(materials)

    assert len(dehy.calls) == 2
    bucket_system, darkroom_system = dehy.calls[0]["system"], dehy.calls[1]["system"]
    for system in (bucket_system, darkroom_system):
        assert not _CJK_RE.search(system), system
    assert "NAMED" in bucket_system
    assert "NAMED" not in darkroom_system, "暗房 prompt 不得提具名短语（同中文路径）"
    assert "at most 3 words" in bucket_system
    assert "At most 6 words" in bucket_system


@pytest.mark.asyncio
async def test_d4_6_en_extract_filters_by_word_count_not_chars(tmp_path, monkeypatch):
    """英文行按词数过滤：'a cold door handle' 18 个字符按旧的 12 字符闸会被误杀。"""
    monkeypatch.setenv("OMBRE_LANG", "en")
    reply = "\n".join([
        "a cold door handle",                       # 4 词 18 字符：必须保留
        "rain.",                                    # 行尾句点去掉
        "the long corridor that keeps going on and on forever",  # 10 词：丢弃
        "NAMED: the letter she handed me",
    ])
    engine = make_engine(tmp_path, dehydrator=_make_dump_dehydrator(reply))
    words, named = await engine.extract_imagery([{"kind": "bucket", "id": "b1", "text": "x y z"}])

    assert set(words) == {"a cold door handle", "rain"}
    assert named == ["the letter she handed me"]


@pytest.mark.parametrize("candidate, expected", [
    ("the letter she handed me", "the letter she handed me"),
    ("the darkroom negative", "the darkroom negative"),
    ("She handed me the letter.", ""),                        # 英文句号在 en 闸里算句读
    ("the very long phrase that runs on past eight words", ""),  # >8 词丢弃
    ("Rhys's late-night walk", "Rhys's late-night walk"),    # 撇号/连字符放行
])
def test_d4_6_en_named_phrase_validator_uses_word_count(monkeypatch, candidate, expected):
    from dream_engine import _validate_named_phrase
    monkeypatch.setenv("OMBRE_LANG", "en")
    assert _validate_named_phrase(candidate) == expected


@pytest.mark.asyncio
async def test_d4_6_zh_extract_prompt_and_char_filter_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    reply = "台灯\n这是一个超过十二个字的很长很长的句子行\nNAMED: 她递来的信"
    dehy = _make_dump_dehydrator(reply)
    engine = make_engine(tmp_path, dehydrator=dehy)
    words, named = await engine.extract_imagery([{"kind": "bucket", "id": "b1", "text": "楼下的猫"}])

    assert _IMAGERY_SYSTEM_MARKER in dehy.calls[0]["system"]
    assert "不超过 6 个字" in dehy.calls[0]["system"]
    assert "Answer in English" not in dehy.calls[0]["system"], "zh prompt 不得混入英文版"
    assert words == ["台灯"]
    assert named == ["她递来的信"]


def test_d4_6_fallback_en_input_yields_english_chunks(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    text = ("Came home late tonight with a fever and took an ibuprofen tablet. "
            "The pork from Shaanxi was still on the counter next to my transit pass.")
    out = DreamEngine._extract_imagery_fallback(text)

    assert out, "英文退化方案不许空手"
    assert len(out) == _IMAGERY_WORDS_MIN
    for chunk in out:
        assert not _CJK_RE.search(chunk)
        assert 1 <= len(chunk.split()) <= 3, chunk
        assert chunk == chunk.lower()
    for chunk in out:
        assert chunk.split()[0] not in {"the", "a", "an", "with", "and", "my"}, chunk


def test_d4_6_fallback_en_all_stopwords_still_not_empty(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    out = DreamEngine._extract_imagery_fallback("and then it was the same again")
    assert out, "全是功能词也要退而求其次收 ≥3 字母的词，不许空手"
    assert all(not _CJK_RE.search(c) for c in out)


def test_d4_6_fallback_en_mode_with_chinese_only_text_falls_through_to_zh(monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    out = DreamEngine._extract_imagery_fallback("楼下的猫又跑到窗台上晒太阳了")
    assert out, "en 模式下正文全中文时换 zh 路径兜底，不许空手"


def test_d4_6_fallback_zh_unchanged(monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    out = DreamEngine._extract_imagery_fallback("楼下的猫又跑到窗台上晒太阳了今天很暖和")
    assert out
    assert all(re.fullmatch(r"[一-鿿]{2,6}", c) for c in out)


def test_d4_6_noise_pool_en_loads_english_seed_file_zh_untouched(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)

    monkeypatch.setenv("OMBRE_LANG", "en")
    assert engine._seed_imagery_path().endswith("noise_imagery_en.json")
    assert os.path.basename(engine._imagery_extra_path()) == "imagery_extra_en.json"
    en_pool = engine._noise_pool()
    assert len(en_pool) >= 60
    assert all(not _CJK_RE.search(x) for x in en_pool)
    assert "a door that opens downward" in en_pool

    monkeypatch.delenv("OMBRE_LANG", raising=False)
    assert engine._seed_imagery_path().endswith(os.sep + "noise_imagery.json")
    assert os.path.basename(engine._imagery_extra_path()) == "imagery_extra.json"
    zh_pool = engine._noise_pool()
    assert "一扇往下开的门" in zh_pool, "zh 种子库文件名与内容不动"
    assert all(_CJK_RE.search(x) for x in zh_pool)
    # 同一 engine 实例内两套缓存互不串用
    assert not set(en_pool) & set(zh_pool)


def test_d4_6_en_seed_file_shape_mirrors_zh():
    seed_dir = os.path.join(os.path.dirname(dream_engine_module.__file__), "dream_data")
    with open(os.path.join(seed_dir, "noise_imagery_en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(seed_dir, "noise_imagery.json"), encoding="utf-8") as f:
        zh = json.load(f)
    assert set(en) == set(zh) == {"_comment", "anchors", "generated"}
    assert len(en["anchors"]) + len(en["generated"]) == 60
    lines = en["anchors"] + en["generated"]
    assert len(set(lines)) == len(lines), "英文种子库不得有重复条"
    for line in lines:
        assert not _CJK_RE.search(line), line
        assert 1 <= len(line.split()) <= 9, line
        assert not re.search(r"\b(like|as if|as though)\b", line), line
        assert not re.search(r"\b(blood|ghost|corpse)\b", line), line


@pytest.mark.asyncio
async def test_d4_6_growth_en_prompt_english_and_writes_en_file_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    dehy = _make_dump_dehydrator("\n".join(f"{i+1}. a chair with {i} legs" for i in range(30)))
    engine = make_engine(tmp_path, dehydrator=dehy)

    await engine.maybe_grow_noise_library(dt.date(2026, 10, 1))

    assert len(dehy.calls) == 1
    assert not _CJK_RE.search(dehy.calls[0]["system"])
    assert not _CJK_RE.search(dehy.calls[0]["user"])
    en_path = os.path.join(str(tmp_path), "dream", "imagery_extra_en.json")
    zh_path = os.path.join(str(tmp_path), "dream", "imagery_extra.json")
    assert os.path.isfile(en_path)
    assert not os.path.exists(zh_path), "en 自增不得碰 zh 增量库文件"
    with open(en_path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["last_grown_month"] == "2026-10"
    assert len(data["items"]) == 30
    assert data["items"][0] == "a chair with 0 legs", "编号前缀要剥掉"
    assert all(x in engine._noise_pool() for x in data["items"])


@pytest.mark.asyncio
async def test_d4_6_growth_zh_prompt_and_file_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    dehy = _make_dump_dehydrator("\n".join(f"噪音意象{i}" for i in range(30)))
    engine = make_engine(tmp_path, dehydrator=dehy)

    await engine.maybe_grow_noise_library(dt.date(2026, 10, 1))

    assert _GROWTH_SYSTEM_MARKER in dehy.calls[0]["system"]
    assert dehy.calls[0]["user"] == "生成30条"
    assert os.path.isfile(os.path.join(str(tmp_path), "dream", "imagery_extra.json"))
    assert not os.path.exists(os.path.join(str(tmp_path), "dream", "imagery_extra_en.json"))


@pytest.mark.asyncio
async def test_d4_6_end_to_end_english_bucket_material_words_have_no_chinese(tmp_path, monkeypatch):
    """目标验收：OMBRE_LANG=en + 一条英文桶，走完整管线，喂给生成步的
    material_words（记忆意象 + 噪音）里不得出现任何汉字——这正是 Rhys 梦里
    夹"夜归/发烧/布洛芬"的那条通路。"""
    monkeypatch.setenv("OMBRE_LANG", "en")
    english_dream = (
        "I walk down a hallway that keeps stretching. My hand finds a cold "
        "door handle. I push it open and the room behind it is my old "
        "kitchen, except the light is wrong. I hear someone call my name "
        "from somewhere I can't place, and my feet keep moving anyway."
    )
    dehy = make_fake_dehydrator(dream_text=english_dream)
    cfg = {
        "buckets_dir": str(tmp_path),
        "dream": {"enabled": True, "dream_prob": 1.0,
                  "memory_levels": [1.0, 0.0, 0.0, 0.0], "cut_prob": 0.0},
    }
    bucket_mgr = FakeBucketMgr([{
        "id": "b_en",
        "content": "Came home late tonight with a fever and took an ibuprofen tablet before bed.",
        "metadata": {"resolved": True},
    }])
    engine = DreamEngine(cfg, bucket_mgr, dehy)

    captured: dict = {}
    original_generate = engine.generate_dream

    async def spy_generate(material_words, named_phrases, tone, level):
        captured["material_words"] = list(material_words)
        captured["named_phrases"] = list(named_phrases)
        return await original_generate(material_words, named_phrases, tone, level)

    monkeypatch.setattr(engine, "generate_dream", spy_generate)

    result = await engine.nightly_dream()

    assert result["dreamed"] is True
    assert captured["material_words"], "生成步必须拿到素材"
    for w in captured["material_words"]:
        assert not _CJK_RE.search(w), f"material_words 混入汉字: {w!r}"
    for p in captured["named_phrases"]:
        assert not _CJK_RE.search(p), f"named_phrases 混入汉字: {p!r}"
    # 生成 system prompt 是 material_words 的最终去向，一并核对
    gen_calls = [c for c in dehy.calls if c["user"] == "Write this dream."]
    assert gen_calls
    assert not _CJK_RE.search(gen_calls[0]["system"])
    post = fm.load(result["path"])
    assert not _CJK_RE.search(str(post.content))

