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
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import frontmatter as fm

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import dream_engine as dream_engine_module  # noqa: E402
from dream_engine import (  # noqa: E402
    DreamEngine, _EMOTION_RESIDUE_POOL, _DEFAULT_TONE_WEIGHTS, _is_prose_like,
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
    assert call_count["n"] == 2, "词表形状要重试一次（最多 2 次生成尝试），不是直接放弃"
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
    # 返修单 v3 给的非法例子原型："底片,一仓一钥"——连续裸名词顿号串联
    text = "我看着底片、钥匙串、旧仓库、锁孔，一动不动。"
    assert _is_prose_like(text) is False


def test_is_prose_like_accepts_scene_description_with_verb():
    # 返修单 v3 给的合法例子原型："路边的邮筒比昨天挪了位置"——完整场景描述，含动词
    text = "我路过时发现路边的邮筒比昨天挪了位置，风还在刮。"
    assert _is_prose_like(text) is True


def test_is_prose_like_short_run_below_threshold_still_passes():
    # 只有 2 个连续裸名词，没到"连续 3 个以上"的门槛，不该被拦
    text = "我看见台灯、钥匙放在桌上，然后转身走了。"
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


def test_tone_directive_lust_matches_spec_text_verbatim():
    from dream_engine import _tone_directive, _LUST_TONE_DIRECTIVE
    directive = _tone_directive("lust")
    assert directive == _LUST_TONE_DIRECTIVE
    for phrase in ("基调：欲", "情欲的梦", "禁因果", "禁解释", "允许断裂", "越要紧的地方越模糊"):
        assert phrase in directive


def test_tone_directive_nightmare_and_others_unchanged():
    from dream_engine import _tone_directive
    assert _tone_directive("nightmare") == "基调：噩梦。噩梦就让它真的可怕，不要缓和。"
    assert _tone_directive("daily") == "基调：日常残渣。"
    assert _tone_directive("sweet") == "基调：甜。"


@pytest.mark.asyncio
async def test_generate_dream_injects_lust_directive_in_both_tiers(tmp_path):
    for level in ("full", "half", "glimpse", "emotion"):
        dehy = make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT)
        engine = make_engine(tmp_path, dehydrator=dehy)
        await engine.generate_dream(["台灯", "钥匙"], [], "lust", level)
        call = dehy.calls[-1]
        assert "情欲的梦" in call["system"], f"level={level} 的 system prompt 应注入 lust 基调说明"
        assert "禁因果" in call["system"] and "越要紧的地方越模糊" in call["system"]


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
    prompt = DreamEngine._low_tier_prompt("daily")
    assert _DREAMER_ALIAS_POV_DIRECTIVE in prompt


def test_high_tier_prompt_includes_alias_pov_directive():
    from dream_engine import _DREAMER_ALIAS_POV_DIRECTIVE
    prompt = DreamEngine._high_tier_prompt("daily")
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
