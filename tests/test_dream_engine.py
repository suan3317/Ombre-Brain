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

from dream_engine import DreamEngine, _EMOTION_RESIDUE_POOL, _DEFAULT_TONE_WEIGHTS  # noqa: E402


PT = ZoneInfo("America/Los_Angeles")

_IMAGERY_SYSTEM_MARKER = "只提取"
_GROWTH_SYSTEM_MARKER = "梦境噪音意象"

# 一句干净的、没有因果连接词、没有收尾点题的梦境文本，供各测试复用
_CLEAN_DREAM_TEXT = (
    "台灯的光落在没人的椅子上。钥匙串在门后自己晃。楼梯数到一半停住，"
    "手表的指针倒着走，雨声从没关紧的窗户挤进来"
)


class FakeBucketMgr:
    def __init__(self, buckets):
        self._buckets = buckets

    async def list_all(self, include_archive=False):
        return [dict(b) for b in self._buckets]


def make_fake_dehydrator(dream_text=_CLEAN_DREAM_TEXT, raise_on_generate=False):
    calls = []

    async def fake_raw_chat(system, user, *, max_tokens=None, temperature=None, model=None):
        calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                       "temperature": temperature, "model": model})
        if _IMAGERY_SYSTEM_MARKER in system:
            return "台灯\n钥匙\n楼梯\n手表\n雨声"
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
    words = await engine.extract_imagery(materials)
    assert words, "拆词 API 失败时正则退化方案必须仍然产出词，管线不能断"


@pytest.mark.asyncio
async def test_nightly_dream_no_dream_on_generation_failure(tmp_path):
    dehy = make_fake_dehydrator(raise_on_generate=True)
    engine = make_engine(tmp_path, dehydrator=dehy)
    result = await engine.nightly_dream()
    assert result["dreamed"] is False
