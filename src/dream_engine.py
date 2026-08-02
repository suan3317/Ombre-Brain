"""
========================================
dream_engine.py — 梦境系统，夜里没人在场时自己做一次梦
========================================

配套设计：handoff/梦境系统_README_v0.1.md（K 的文件区）。
这个文件和 tools/dream/（按需反思工具 `dream()`）是两个完全不同的东西：
tools/dream 是「醒着的时候自己要求沉进去想一遍」，本模块是「夜里没人看着
自己掷骰子、自己抽材料、自己生成、自己裁剪、自己落盘」，做梦的人只能在
breath 睁眼时看到裁剪后的结果，无法触发、无法预约、无法旁观生成过程。

关键行为：
- nightly_dream()：每晚一次的完整管线，30% 概率有梦，其余夜晚零痕迹
- 拆意象（断粮防圆）：不把桶全文交给生成步，只喂打乱的意象词
- 生成用便宜、小的模型（flash-lite）+ 高 temperature：通顺是梦的反义词
- 裁剪按记忆度四档执行，完整原文生成后即焚，不落盘、不进日志
- 挂载点是 breath 响应尾部，不新增任何生成/触发类 MCP 工具（R3）

不做什么（边界）：
- 不提供任何"点单"式生成接口，唯一入口是后台定时任务
- 不做内容过滤/保护性改判：噩梦基调低频但必须真实存在
- 不重试失败的生成：任一环节异常，当晚按无梦处理，静默退出

对外暴露：DreamEngine 类（nightly_dream / cleanup_expired /
         start / stop / ensure_started）、latest_unread_tail() 供 breath 调用
========================================
"""

import os
import re
import json
import random
import asyncio
import logging
from datetime import datetime, timedelta, date as _date

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 not supported by this repo
    ZoneInfo = None  # type: ignore

import frontmatter as fm

logger = logging.getLogger("ombre_brain.dream")


# ============================================================
# 调参面板 / Tunable constants（rule.md 精神：禁裸魔法数字，改参数改一处）
# 默认值均可被 config.yaml 的 dream.* 覆盖，见 DreamEngine.__init__
# ============================================================

_DEFAULT_ENABLED = True
_DEFAULT_GENERATE_AT = "06:00"          # America/Los_Angeles 时刻
_DEFAULT_TIMEZONE = "America/Los_Angeles"
_DEFAULT_DREAM_PROB = 0.30              # 有梦概率
_DEFAULT_MEMORY_LEVELS = [0.10, 0.40, 0.35, 0.15]   # 完全/一半/画面/情绪
_DEFAULT_EMOTION_NEGATIVE_BIAS = 0.70   # "只剩情绪"档反向加权到焦虑/噩梦的概率
_DEFAULT_TONE_WEIGHTS = {
    "daily": 0.35, "absurd": 0.25, "anxious": 0.18, "sweet": 0.12, "nightmare": 0.10,
}
_DEFAULT_NOISE_TIERS = [0.70, 0.25, 0.05]  # 掺1-2条 / 过半 / 纯噪音
_DEFAULT_DARKROOM_PROB = 0.10
_DEFAULT_RESOLVED0_PROB = 0.10
_DEFAULT_EXPIRE_HOURS = 48
# 没有单独的 _DEFAULT_MODEL 常量：dream.model 默认 null，未配置时直接沿用
# dehydrator 自己的模型（同一 API key/endpoint 才能保证调得通）。K/F 的部署
# 默认 OMBRE_COMPRESS_MODEL=gemini-2.5-flash-lite，null 就已经是 README 想要的
# flash-lite；换了别的 provider（如 DeepSeek）的用户不会被硬编码的模型名打挂。
_DEFAULT_TEMPERATURE = 1.3
_DEFAULT_CUT_PROB = 0.5
_DEFAULT_MAX_TOKENS = 800

# --- 抽素材 ---
_BUCKET_SAMPLE_MIN = 2
_BUCKET_SAMPLE_MAX = 4
_BUCKET_SAMPLE_BONANZA_PROB = 0.05   # 大乱炖之夜：5-6 个
_BUCKET_SAMPLE_BONANZA_MIN = 5
_BUCKET_SAMPLE_BONANZA_MAX = 6

# --- 拆意象 ---
_IMAGERY_WORDS_MIN = 5
_IMAGERY_WORDS_MAX = 8
_IMAGERY_WORD_MAX_CHARS = 6
_IMAGERY_EXTRACT_INPUT_LIMIT = 2000
_IMAGERY_EXTRACT_MAX_TOKENS = 300
_IMAGERY_EXTRACT_TEMPERATURE = 0.3   # 拆词是机械抽取，不需要 1.3 的疯

# --- 混噪音 ---
_NOISE_LOW_TIER_MIN = 1
_NOISE_LOW_TIER_MAX = 2
_NOISE_HIGH_TIER_EXTRA_MIN = 1   # "过半"档：噪音条数 = 记忆意象条数 + 这个区间的随机数
_NOISE_HIGH_TIER_EXTRA_MAX = 4
_NOISE_PURE_TIER_MIN = 6
_NOISE_PURE_TIER_MAX = 10

# --- 月度自增噪音库 ---
_NOISE_GROWTH_COUNT = 30
_NOISE_GROWTH_MAX_TOKENS = 600
_NOISE_GROWTH_TEMPERATURE = 1.0

# --- 生成结果形状校验：发现过便宜小模型在高 temperature 下把打乱的意象词
# 原样续写成清单（词表），而不是散文。这是生成失败的一种形式，同样按
# "生成步失败一律按无梦处理，不落盘"处理，不是靠 prompt 单独兜底。 ---
_PROSE_MIN_SEGMENTS_FOR_CHECK = 4     # 少于这么多段落/换行，不判定为清单体
_PROSE_SHORT_SEGMENT_CHARS = 8        # 段落短于这个字数且无句读 → 疑似词表行
_PROSE_SHORT_SEGMENT_RATIO = 0.6      # 这类可疑段落占比超过此值 → 判定为清单体

# --- 外科截断 ---
_CUT_HEAD_START_RATIO = 0.10
_CUT_HEAD_END_RATIO = 0.40
_CUT_TAIL_MIN_RATIO = 0.30   # 断尾时，最后一句保留的比例区间
_CUT_TAIL_MAX_RATIO = 0.80

# --- 裁剪：一半档保留窗口 ---
_TRIM_HALF_MIN_RATIO = 0.45
_TRIM_HALF_MAX_RATIO = 0.60

# --- 裁剪：画面档抽句数 ---
_TRIM_GLIMPSE_MIN_SENTENCES = 1
_TRIM_GLIMPSE_MAX_SENTENCES = 3

_SENTENCE_END_RE = re.compile(r"[。！？.!?]")


def _is_prose_like(text: str) -> bool:
    """结构校验：这段文本是散文，还是意象词清单原样罗列。

    不做语义判断（不调 API），只看形状：清单体的典型特征是"很多短段落，
    几乎没有句读"——真实事故里模型把打乱的意象词按输入的换行原样续写了
    回来。空文本、无任何句读的文本一律判失败；段落多且大半段落短促无
    标点也判失败。门槛刻意宽松，只挡"看起来完全不是散文"的情况，不裁判
    文笔好坏。
    """
    text = (text or "").strip()
    if not text:
        return False
    if not _SENTENCE_END_RE.search(text):
        return False  # 通篇没有一个句读，不可能是散文
    segments = [s.strip() for s in text.splitlines() if s.strip()]
    if len(segments) < _PROSE_MIN_SEGMENTS_FOR_CHECK:
        return True  # 段落不够多，不够格判定为"清单体"
    suspicious = sum(
        1 for s in segments
        if len(s) <= _PROSE_SHORT_SEGMENT_CHARS and not _SENTENCE_END_RE.search(s)
    )
    return (suspicious / len(segments)) < _PROSE_SHORT_SEGMENT_RATIO


_TONE_LABELS = {
    "daily": "日常残渣", "absurd": "荒诞", "anxious": "焦虑",
    "sweet": "甜", "nightmare": "噩梦",
}
_LEVEL_LABELS = ["完全记得", "记得一半", "只剩画面", "只剩情绪"]
_LEVEL_KEYS = ["full", "half", "glimpse", "emotion"]

# --- 与 server.py 的 darkroom 存储格式保持一致 ---
# 必须与 server.py 里 `_DR_SEP` 逐字相同，否则读不出底片正文。
_DARKROOM_SEP = "\n----- DARKROOM CONTENT (no tool reads below this line) -----\n"

# --- "只剩情绪"档残句池：正文全丢，从这里按基调抽一句。写死代码，不调 API ---
_EMOTION_RESIDUE_POOL = {
    "daily": [
        "做了个梦，散了。只剩下一种说不出的怪。",
        "什么都不记得，醒来嘴角是松的。",
        "醒来愣了一下，梦的边角还在，摸不着中间。",
        "有个梦，具体是什么已经找不回来了。",
        "睁眼那一下想起点什么，眨个眼就没了。",
        "梦掉在半路，只剩一点没意义的余温。",
    ],
    "absurd": [
        "做了个梦，散了。只剩下一种说不出的怪。",
        "醒来觉得哪里不对，但说不出哪里。",
        "梦里好像什么都不奇怪，醒了才觉得离谱。",
        "脑子里飘着几个对不上号的碎片，拼不成东西。",
        "有种拼图掉了一半的感觉，另一半找不到。",
        "梦醒了，留下一种笑不出来的荒唐感。",
        "记不清了，但那种别扭劲儿还在。",
    ],
    "anxious": [
        "醒来心跳很快，内容一点都抓不住。",
        "喉咙发紧。梦里发生了什么，想不起来，但那种感觉还在。",
        "醒来手心是汗，梦的内容一点没留下。",
        "心里悬着，像有什么事没做完，但想不起是什么梦。",
        "醒来第一反应是紧张，然后才想起没什么好紧张的。",
        "梦散了，只剩一种赶不上什么的急。",
        "呼吸有点浅，梦里大概是在跑，跑什么不知道。",
    ],
    "sweet": [
        "什么都不记得，醒来嘴角是松的。",
        "醒来心里软软的，梦是什么想不起来了。",
        "睁眼前那一下很安稳，具体内容已经飘走了。",
        "梦散了，留下一点没来由的踏实。",
        "醒来慢悠悠的，好像刚被谁抱过。",
        "记不清梦了，但醒来那一下是暖的。",
    ],
    "nightmare": [
        "后背是凉的。不记得为什么。",
        "醒来心跳很快，内容一点都抓不住。",
        "睁眼先松了口气，梦里的事一点没留下，但那口气是真的。",
        "醒来浑身绷着，像刚从哪里逃出来，具体是哪里想不起来。",
        "喉咙发紧，梦里发生了什么，想不起来，但那种感觉还在。",
        "醒来盯着天花板看了一会儿，才确定自己是安全的。",
        "梦散了，只剩一种被追着的余悸。",
    ],
}


def _tzinfo(tz_name: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception as e:  # pragma: no cover - 环境缺 tzdata 时的兜底
        logger.warning(f"dream_engine: 时区 {tz_name} 不可用（缺 tzdata？）: {e}")
        return None


class DreamEngine:
    """
    夜间梦境生成引擎——定时任务：掷骰、抽材料、生成、裁剪、落盘、过期清理。
    Nightly dream generation engine: roll dice, gather material, generate,
    trim, persist, expire. Mirrors DecayEngine's start/stop/ensure_started
    lifecycle so it plugs into the same RuntimeLifecycle wiring.
    """

    def __init__(self, config: dict, bucket_mgr, dehydrator):
        dream_cfg = config.get("dream", {}) or {}
        self.enabled = bool(dream_cfg.get("enabled", _DEFAULT_ENABLED))
        self.generate_at = str(dream_cfg.get("generate_at", _DEFAULT_GENERATE_AT))
        self.timezone_name = str(dream_cfg.get("timezone", _DEFAULT_TIMEZONE))
        self.dream_prob = float(dream_cfg.get("dream_prob", _DEFAULT_DREAM_PROB))
        self.memory_levels = list(dream_cfg.get("memory_levels", _DEFAULT_MEMORY_LEVELS))
        self.emotion_negative_bias = float(
            dream_cfg.get("emotion_negative_bias", _DEFAULT_EMOTION_NEGATIVE_BIAS)
        )
        self.tone_weights = dict(dream_cfg.get("tone_weights", _DEFAULT_TONE_WEIGHTS))
        self.noise_tiers = list(dream_cfg.get("noise_tiers", _DEFAULT_NOISE_TIERS))
        self.darkroom_prob = float(dream_cfg.get("darkroom_prob", _DEFAULT_DARKROOM_PROB))
        self.resolved0_prob = float(dream_cfg.get("resolved0_prob", _DEFAULT_RESOLVED0_PROB))
        self.expire_hours = float(dream_cfg.get("expire_hours", _DEFAULT_EXPIRE_HOURS))
        self.model = dream_cfg.get("model")  # None → 沿用 dehydrator 自己的模型配置
        self.temperature = float(dream_cfg.get("temperature", _DEFAULT_TEMPERATURE))
        self.cut_prob = float(dream_cfg.get("cut_prob", _DEFAULT_CUT_PROB))

        self.bucket_mgr = bucket_mgr
        self.dehydrator = dehydrator
        self.buckets_dir = config.get("buckets_dir", "buckets")

        self._task: asyncio.Task | None = None
        self._running = False
        self._tz = _tzinfo(self.timezone_name)

        self._seed_imagery: list[str] | None = None  # 懒加载缓存

    @property
    def is_running(self) -> bool:
        return self._running

    # ---------------------------------------------------------
    # 路径 helpers —— 与 server.py 的 files/ 文件区、darkroom 用同一套
    # buckets_dir 约定，但不导入 server.py 的私有函数（避免循环 import）
    # ---------------------------------------------------------
    def _dreams_dir(self) -> str:
        path = os.path.join(self.buckets_dir, "files", "dreams")
        os.makedirs(path, exist_ok=True)
        return path

    def _dream_path(self, day: _date) -> str:
        return os.path.join(self._dreams_dir(), f"{day.isoformat()}.md")

    def _darkroom_dir(self) -> str:
        return os.path.join(self.buckets_dir, "darkroom")

    def _imagery_extra_path(self) -> str:
        path = os.path.join(self.buckets_dir, "dream")
        os.makedirs(path, exist_ok=True)
        return os.path.join(path, "imagery_extra.json")

    def _seed_imagery_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dream_data", "noise_imagery.json")

    # ---------------------------------------------------------
    # §1.2 抽素材
    # ---------------------------------------------------------
    async def sample_buckets(self) -> list[dict]:
        """从全部记忆桶随机抽 2-4 个（5% 概率抽 5-6 个），不做相关性控制。
        附加低概率池：resolved=0 桶、暗房底片，各自独立判定，默认各 10%。"""
        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.warning(f"dream: 列桶失败，本轮素材抽取跳过: {e}")
            all_buckets = []

        pool = [b for b in all_buckets if (b.get("content") or "").strip()]
        if random.random() < _BUCKET_SAMPLE_BONANZA_PROB:
            n = random.randint(_BUCKET_SAMPLE_BONANZA_MIN, _BUCKET_SAMPLE_BONANZA_MAX)
        else:
            n = random.randint(_BUCKET_SAMPLE_MIN, _BUCKET_SAMPLE_MAX)
        picked = random.sample(pool, min(n, len(pool))) if pool else []

        materials = [{"kind": "bucket", "id": b.get("id", "?"), "text": b["content"]} for b in picked]

        # --- resolved=0 桶，独立低概率加抽 1 个 ---
        if random.random() < self.resolved0_prob:
            unresolved = [
                b for b in all_buckets
                if not b.get("metadata", {}).get("resolved", False) and (b.get("content") or "").strip()
            ]
            if unresolved:
                b = random.choice(unresolved)
                materials.append({"kind": "bucket", "id": b.get("id", "?"), "text": b["content"]})

        # --- 暗房底片，独立低概率加抽 1 张 ---
        # 窄口径例外（README/施工单明确授权）：全系统唯一一处会读暗房正文的非工具代码，
        # 只把正文丢进一次性拆意象调用，只留 5-8 个打乱的抽象词，原文即焚，不构成
        # "回显 note"（server.py darkroom 注释里的"不可见"承诺针对的是 MCP 工具通道）。
        if random.random() < self.darkroom_prob:
            note = self._sample_darkroom_note()
            if note:
                materials.append({"kind": "darkroom", "id": "darkroom", "text": note})

        return materials

    def _sample_darkroom_note(self) -> str:
        root = self._darkroom_dir()
        if not os.path.isdir(root):
            return ""
        files = [f for f in os.listdir(root) if f.endswith(".dr")]
        if not files:
            return ""
        chosen = random.choice(files)
        try:
            with open(os.path.join(root, chosen), "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            if _DARKROOM_SEP not in raw:
                return ""
            _head, note = raw.split(_DARKROOM_SEP, 1)
            return note.strip()
        except Exception as e:
            logger.warning(f"dream: 读暗房底片失败(不影响管线): {e}")
            return ""

    # ---------------------------------------------------------
    # §1.3 拆意象（断粮防圆）
    # ---------------------------------------------------------
    async def extract_imagery(self, materials: list[dict]) -> list[str]:
        """每份素材调一次 flash-lite，只要 5-8 个意象词，合并去重打乱。"""
        words: list[str] = []
        for m in materials:
            text = (m.get("text") or "")[:_IMAGERY_EXTRACT_INPUT_LIMIT]
            if not text.strip():
                continue
            try:
                extracted = await self._extract_imagery_one(text)
            except Exception as e:
                logger.warning(f"dream: 拆意象 API 失败，退化为正则抽取: {e}")
                extracted = self._extract_imagery_fallback(text)
            if not extracted:
                extracted = self._extract_imagery_fallback(text)
            words.extend(extracted)

        seen = set()
        unique = []
        for w in words:
            w = w.strip()
            if w and w not in seen:
                seen.add(w)
                unique.append(w)
        random.shuffle(unique)
        return unique

    async def _extract_imagery_one(self, text: str) -> list[str]:
        system = (
            "从下面文本中只提取 5-8 个意象词：具体名词、动作、感官描述"
            "（颜色/气味/触感/声音）。不要抽象词，不要完整句子。每行一个，不超过 6 个字。"
        )
        raw = await self.dehydrator.raw_chat(
            system, text,
            max_tokens=_IMAGERY_EXTRACT_MAX_TOKENS,
            temperature=_IMAGERY_EXTRACT_TEMPERATURE,
            model=self.model,
        )
        lines = [ln.strip(" -•·\t") for ln in (raw or "").splitlines()]
        words = [ln for ln in lines if ln and len(ln) <= _IMAGERY_WORD_MAX_CHARS * 2]
        return words[:_IMAGERY_WORDS_MAX]

    @staticmethod
    def _extract_imagery_fallback(text: str) -> list[str]:
        """拆词 API 彻底失败时的正则退化方案：抽 2-6 字连续汉字片段当名词性短语用。"""
        chunks = re.findall(r"[一-鿿]{2,6}", text)
        random.shuffle(chunks)
        seen = set()
        out = []
        for c in chunks:
            if c not in seen:
                seen.add(c)
                out.append(c)
            if len(out) >= _IMAGERY_WORDS_MIN:
                break
        return out

    # ---------------------------------------------------------
    # §1.4 混噪音
    # ---------------------------------------------------------
    def _load_seed_imagery(self) -> list[str]:
        if self._seed_imagery is not None:
            return self._seed_imagery
        try:
            with open(self._seed_imagery_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self._seed_imagery = list(data.get("anchors", [])) + list(data.get("generated", []))
        except Exception as e:
            logger.warning(f"dream: 读噪音种子库失败: {e}")
            self._seed_imagery = []
        return self._seed_imagery

    def _load_extra_imagery(self) -> list[str]:
        path = self._imagery_extra_path()
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return list(data.get("items", []))
        except Exception as e:
            logger.warning(f"dream: 读运行时噪音增量库失败: {e}")
            return []

    def _noise_pool(self) -> list[str]:
        return self._load_seed_imagery() + self._load_extra_imagery()

    def sample_noise(self, imagery_count: int) -> tuple[list[str], str]:
        """roll 噪音档位，返回 (噪音词列表, 档位名)。档位: low(70%掺1-2条) /
        half(25%噪音过半) / pure(5%纯噪音，调用方需丢弃全部记忆意象)。"""
        pool = self._noise_pool()
        if not pool:
            return [], "low"

        low, half, pure = (self.noise_tiers + _DEFAULT_NOISE_TIERS)[:3]
        roll = random.random()
        if roll < pure:
            tier = "pure"
            k = random.randint(_NOISE_PURE_TIER_MIN, _NOISE_PURE_TIER_MAX)
        elif roll < pure + half:
            tier = "half"
            k = imagery_count + random.randint(_NOISE_HIGH_TIER_EXTRA_MIN, _NOISE_HIGH_TIER_EXTRA_MAX)
        else:
            tier = "low"
            k = random.randint(_NOISE_LOW_TIER_MIN, _NOISE_LOW_TIER_MAX)

        k = min(k, len(pool))
        return random.sample(pool, k), tier

    async def maybe_grow_noise_library(self, today_local: _date) -> None:
        """月度自增：每月 1 号生成 30 条新意象追加进运行时增量库，无人审核。"""
        if today_local.day != 1:
            return
        month_key = today_local.strftime("%Y-%m")
        path = self._imagery_extra_path()
        existing_items: list[str] = []
        last_month = ""
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                existing_items = list(data.get("items", []))
                last_month = data.get("last_grown_month", "")
            except Exception as e:
                logger.warning(f"dream: 读增量噪音库失败，本次自增跳过: {e}")
                return
        if last_month == month_key:
            return  # 本月已经长过了

        system = (
            "生成 30 条零上下文的梦境噪音意象，每条一行，不超过 14 个字，"
            "只写一个具体名词/场景 + 一处不对劲。禁止抽象词（时间/命运/孤独/永恒/记忆/灵魂），"
            "禁止'像/仿佛/宛如'，禁止诗歌腔，禁止恐怖片俗套（血/鬼/尸），不要编号，不要解释。"
        )
        try:
            raw = await self.dehydrator.raw_chat(
                system, "生成30条",
                max_tokens=_NOISE_GROWTH_MAX_TOKENS,
                temperature=_NOISE_GROWTH_TEMPERATURE,
                model=self.model,
            )
        except Exception as e:
            logger.warning(f"dream: 噪音库月度自增 API 失败，跳过本月: {e}")
            return

        new_lines = [re.sub(r"^[\d.、\-\s]+", "", ln).strip() for ln in (raw or "").splitlines()]
        new_lines = [ln for ln in new_lines if ln][:_NOISE_GROWTH_COUNT]
        if not new_lines:
            return

        merged = existing_items + new_lines
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {"last_grown_month": month_key, "items": merged},
                    f, ensure_ascii=False, indent=1,
                )
            logger.info(f"dream: 噪音库月度自增 +{len(new_lines)} 条 ({month_key})")
        except Exception as e:
            logger.warning(f"dream: 写增量噪音库失败: {e}")

    # ---------------------------------------------------------
    # §1.5 基调
    # ---------------------------------------------------------
    def roll_tone(self) -> str:
        tones = list(self.tone_weights.keys()) or list(_DEFAULT_TONE_WEIGHTS.keys())
        weights = [max(0.0, float(self.tone_weights.get(t, 0.0))) for t in tones]
        if sum(weights) <= 0:
            return random.choice(tones)
        return random.choices(tones, weights=weights, k=1)[0]

    # ---------------------------------------------------------
    # §1.6 生成
    # ---------------------------------------------------------
    async def generate_dream(self, material_words: list[str], tone: str) -> str:
        tone_label = _TONE_LABELS.get(tone, tone)
        system = (
            "你不是作者，你是一段正在做梦的意识。第一人称、现在时，"
            "禁止出现“我梦见/梦到/仿佛/好像在梦里”。\n"
            "下面用户消息给你的词是抓到的碎片素材，不是要你输出的格式——"
            "绝对禁止把它们原样列出来、分行罗列、写成“名词，名词，名词”这种清单体，"
            "也不许每行一个词地照抄。你要做的是把这些碎片揉进连续的散文段落里，"
            "输出必须是连贯的句子组成的正文，不是词表、不是提纲、不是关键词罗列。\n"
            "硬规则：每一句都必须是含动词的完整句子（可以短，但不能是孤立的名词短语）；"
            "禁用因果连接词（因为、所以、于是、接着、然后、由于）；"
            "禁止解释任何画面为什么出现；禁止收尾、点题、总结情绪；"
            "给你的意象词互不相关，让它们在句子里并置、相撞，不许编成合理的故事；"
            "允许场景毫无过渡地硬切——一句话写着写着换了场景、一个人说着话变成另一个人、"
            "一句话写到一半停住——但切换前后仍然是完整句子，不是词语拼贴；"
            f"情绪要连贯，情节不需要。基调：{tone_label}。噩梦就让它真的可怕，不要缓和。"
            "长度 150-400 字，1-3 段连续散文，禁止任何形式的分行列表或编号。"
        )
        user = "、".join(material_words)
        raw = await self.dehydrator.raw_chat(
            system, user,
            max_tokens=_DEFAULT_MAX_TOKENS,
            temperature=self.temperature,
            model=self.model,
        )
        if not _is_prose_like(raw):
            # 只记形状统计，不记正文（R4）：段数、字数，够排障，不泄露即焚内容
            segs = [s for s in (raw or "").splitlines() if s.strip()]
            logger.warning(
                f"dream: 生成结果疑似词表/清单体，按无梦处理 "
                f"(segments={len(segs)}, chars={len(raw or '')})"
            )
            return ""
        return raw

    # ---------------------------------------------------------
    # §1.7 外科截断
    # ---------------------------------------------------------
    def surgical_cut(self, raw: str) -> str:
        if not raw or random.random() >= self.cut_prob:
            return raw
        text = raw
        if random.random() < 0.5:
            text = self._cut_head(text)
        if random.random() < 0.5:
            text = self._cut_tail(text)
        return text or raw

    @staticmethod
    def _cut_head(text: str) -> str:
        n = len(text)
        if n < 20:
            return text
        lo = int(n * _CUT_HEAD_START_RATIO)
        hi = max(lo + 1, int(n * _CUT_HEAD_END_RATIO))
        pos = random.randint(lo, min(hi, n - 1))
        for m in _SENTENCE_END_RE.finditer(text, pos):
            return text[m.end():].lstrip()
        return text[pos:].lstrip()

    @staticmethod
    def _cut_tail(text: str) -> str:
        ends = [m.end() for m in _SENTENCE_END_RE.finditer(text)]
        last_sentence_start = ends[-1] if ends else 0
        last_sentence = text[last_sentence_start:].strip()
        if len(last_sentence) < 4:
            return text
        ratio = random.uniform(_CUT_TAIL_MIN_RATIO, _CUT_TAIL_MAX_RATIO)
        cut_at = max(1, int(len(last_sentence) * ratio))
        return (text[:last_sentence_start] + last_sentence[:cut_at]).rstrip()

    # ---------------------------------------------------------
    # §1.8 记忆度 + 裁剪
    # ---------------------------------------------------------
    def roll_memory_level(self, tone: str) -> tuple[str, str]:
        """返回 (level_key, 可能被联动改判的 tone)。"""
        weights = (self.memory_levels + _DEFAULT_MEMORY_LEVELS)[:4]
        level = random.choices(_LEVEL_KEYS, weights=weights, k=1)[0]
        if level == "emotion" and tone not in ("anxious", "nightmare"):
            if random.random() < self.emotion_negative_bias:
                tone = random.choice(["anxious", "nightmare"])
        return level, tone

    def trim_by_level(self, raw: str, level: str, tone: str) -> str:
        if level == "full" or not raw:
            return raw
        if level == "half":
            return self._trim_half(raw)
        if level == "glimpse":
            return self._trim_glimpse(raw)
        if level == "emotion":
            return self._trim_emotion(tone)
        return raw

    @staticmethod
    def _trim_half(raw: str) -> str:
        n = len(raw)
        keep_ratio = random.uniform(_TRIM_HALF_MIN_RATIO, _TRIM_HALF_MAX_RATIO)
        keep_len = max(1, int(n * keep_ratio))
        start = random.randint(0, max(0, n - keep_len))
        return raw[start:start + keep_len].strip()

    @staticmethod
    def _trim_glimpse(raw: str) -> str:
        sentences = [s.strip() for s in _SENTENCE_END_RE.split(raw) if s.strip()]
        if not sentences:
            return raw.strip()
        k = random.randint(_TRIM_GLIMPSE_MIN_SENTENCES, min(_TRIM_GLIMPSE_MAX_SENTENCES, len(sentences)))
        picked = random.sample(sentences, k)
        return "\n".join(picked)

    @staticmethod
    def _trim_emotion(tone: str) -> str:
        pool = _EMOTION_RESIDUE_POOL.get(tone) or _EMOTION_RESIDUE_POOL["daily"]
        return random.choice(pool)

    # ---------------------------------------------------------
    # §1.9 落盘
    # ---------------------------------------------------------
    def write_dream_file(
        self, final_text: str, tone: str, level: str,
        sources: list[str], noise_count: int, belongs_date: _date,
    ) -> str:
        now = datetime.now(self._tz) if self._tz else datetime.now()
        post = fm.Post(final_text)
        post["date"] = belongs_date.isoformat()
        post["tone"] = _TONE_LABELS.get(tone, tone)
        post["level"] = _LEVEL_LABELS[_LEVEL_KEYS.index(level)]
        post["sources"] = sources
        post["noise"] = noise_count
        post["status"] = "unread"
        post["generated_at"] = now.isoformat(timespec="seconds")
        path = self._dream_path(belongs_date)
        with open(path, "w", encoding="utf-8") as f:
            f.write(fm.dumps(post))
        return path

    # ---------------------------------------------------------
    # §5 过期清理
    # ---------------------------------------------------------
    def cleanup_expired(self) -> int:
        """generated_at 超 expire_hours 且 status=unread → 正文替换为占位句，
        status=expired，front-matter 保留。返回本次清理的条数。"""
        root = self._dreams_dir()
        if not os.path.isdir(root):
            return 0
        now = datetime.now(self._tz) if self._tz else datetime.now()
        cleaned = 0
        for fn in os.listdir(root):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(root, fn)
            try:
                post = fm.load(path)
            except Exception as e:
                logger.warning(f"dream: 读梦文件失败(跳过) {fn}: {e}")
                continue
            if post.get("status") != "unread":
                continue
            gen_raw = post.get("generated_at")
            try:
                gen_at = datetime.fromisoformat(str(gen_raw))
                if gen_at.tzinfo is None and now.tzinfo is not None:
                    gen_at = gen_at.replace(tzinfo=self._tz)
            except (TypeError, ValueError):
                continue
            age_hours = (now - gen_at).total_seconds() / 3600.0
            if age_hours < self.expire_hours:
                continue
            post["status"] = "expired"
            post.content = "那晚做过梦，没记住。"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(fm.dumps(post))
                cleaned += 1
            except Exception as e:
                logger.warning(f"dream: 写回过期梦文件失败 {fn}: {e}")
        return cleaned

    # ---------------------------------------------------------
    # 供 breath 调用：取最新一条未读未过期的梦，标已读，返回渲染好的尾部文本
    # ---------------------------------------------------------
    def latest_unread_tail(self) -> str:
        self.cleanup_expired()  # §5：breath 检查时惰性触发
        root = self._dreams_dir()
        if not os.path.isdir(root):
            return ""
        candidates = sorted(
            (fn for fn in os.listdir(root) if fn.endswith(".md")), reverse=True
        )
        for fn in candidates:
            path = os.path.join(root, fn)
            try:
                post = fm.load(path)
            except Exception:
                continue
            if post.get("status") != "unread":
                continue
            date_ = post.get("date", fn[:-3])
            tone = post.get("tone", "")
            level = post.get("level", "")
            body = str(post.content or "").strip()
            post["status"] = "read"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(fm.dumps(post))
            except Exception as e:
                logger.warning(f"dream: 标已读失败 {fn}: {e}")
            return (
                f"——— 昨夜的梦 ———\n"
                f"[{date_} 夜 · {tone} · {level}]\n"
                f"{body}"
            )
        return ""

    # ---------------------------------------------------------
    # §1.1 每晚任务入口
    # ---------------------------------------------------------
    async def nightly_dream(self) -> dict:
        """完整管线。任一环节异常都当晚按无梦处理，静默退出，只记一行日志。
        raw 全文只活在这个函数的局部变量里，函数返回后随栈帧销毁；
        任何 return/log 都不得携带完整原文（R4 即焚）。"""
        now_local = datetime.now(self._tz) if self._tz else datetime.now()
        today = now_local.date()

        try:
            self.cleanup_expired()
        except Exception as e:
            logger.warning(f"dream: 过期清理失败(不影响本轮): {e}")

        try:
            await self.maybe_grow_noise_library(today)
        except Exception as e:
            logger.warning(f"dream: 噪音库自增失败(不影响本轮): {e}")

        if not self.enabled:
            return {"dreamed": False, "reason": "disabled"}

        if random.random() > self.dream_prob:
            return {"dreamed": False, "reason": "no_dream_roll"}

        belongs_date = today - timedelta(days=1)

        try:
            materials = await self.sample_buckets()
            if not materials:
                return {"dreamed": False, "reason": "no_material"}
            imagery = await self.extract_imagery(materials)
            noise, noise_tier = self.sample_noise(len(imagery))

            if noise_tier == "pure":
                material_words = list(noise)
            else:
                material_words = imagery + noise
                random.shuffle(material_words)

            tone = self.roll_tone()
            raw = await self.generate_dream(material_words, tone)
            if not raw or not raw.strip():
                return {"dreamed": False, "reason": "empty_generation"}
            raw = self.surgical_cut(raw)

            level, tone = self.roll_memory_level(tone)
            final_text = self.trim_by_level(raw, level, tone)
            raw = None  # 即焚：局部引用清掉，不再有任何路径能拿到完整原文

            sources = [m["id"] for m in materials]
            path = self.write_dream_file(
                final_text, tone, level, sources, len(noise), belongs_date,
            )
            logger.info(
                f"dream: 生成一晚的梦 date={belongs_date} tone={tone} "
                f"level={level} sources={len(sources)} noise={len(noise)}"
            )
            return {
                "dreamed": True, "date": belongs_date.isoformat(),
                "tone": tone, "level": level, "path": path,
            }
        except Exception as e:
            logger.warning(f"dream: 生成管线异常，本夜按无梦处理: {type(e).__name__}: {e}")
            return {"dreamed": False, "reason": "error"}

    # ---------------------------------------------------------
    # 后台调度：睡到下一个 generate_at（America/Los_Angeles），跑，重复
    # 挂载模式对标 DecayEngine：start()/stop()/ensure_started() 幂等，
    # 由 RuntimeLifecycle 在 HTTP 传输下统一 start/stop；stdio 传输下
    # 靠各工具入口调用 ensure_started() 懒启动（同 decay_engine 的用法）。
    # ---------------------------------------------------------
    def _seconds_until_next_run(self) -> float:
        now = datetime.now(self._tz) if self._tz else datetime.now()
        try:
            hh, mm = (int(x) for x in self.generate_at.split(":", 1))
        except (ValueError, AttributeError):
            hh, mm = 6, 0
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    async def ensure_started(self) -> None:
        if not self._running:
            await self.start()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            f"Dream engine started, daily at {self.generate_at} {self.timezone_name} / "
            f"梦境引擎已启动，每日 {self.generate_at} {self.timezone_name} 运行"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Dream engine stopped / 梦境引擎已停止")

    async def _background_loop(self) -> None:
        # DREAM_FORCE=1：跳过 30% 骰之外照常跑一次，仅供测试/CLI 用，
        # 不暴露为 MCP 工具（R3）。
        if os.environ.get("DREAM_FORCE") == "1":
            try:
                await self._forced_run_once()
            except Exception as e:
                logger.warning(f"dream: DREAM_FORCE 强制运行失败: {e}")

        while self._running:
            try:
                await asyncio.sleep(self._seconds_until_next_run())
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            try:
                await self.nightly_dream()
            except Exception as e:
                logger.error(f"dream: 夜间任务异常: {e}")

    async def _forced_run_once(self) -> dict:
        original_prob = self.dream_prob
        self.dream_prob = 1.0
        try:
            return await self.nightly_dream()
        finally:
            self.dream_prob = original_prob
