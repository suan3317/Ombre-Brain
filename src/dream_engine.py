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
- 拆意象（断粮防圆）：不把桶全文交给生成步，只喂打乱的意象词，送入前先经
  dreamer_aliases 清洗（施工单·工程一：做梦者本人称呼整词替换成"我"）
- 生成用便宜、小的模型（flash-lite）+ 高 temperature：通顺是梦的反义词
- 裁剪按记忆度四档执行，完整原文生成后即焚，不落盘、不进日志
- 挂载点是 breath 响应尾部（consume=True 消费）+ wake 响应尾部
  （consume=False 只预览不消费，返修单一号改动一），不新增任何生成/
  触发类 MCP 工具（R3）——dream_keep 是例外：它只标记既有梦为 kept，
  不生成、不触发新一轮做梦，不违反 R3 的精神
- 梦境书（施工单·工程二）：独立存储 <buckets_dir>/dream_book/，不在
  files/ 文件区下；48h 内不 keep 就烧（正文替换占位句，日期骨架永久
  保留），dream_keep(date=...) 是唯一的保留入口

不做什么（边界）：
- 不提供任何"点单"式生成接口，唯一入口是后台定时任务
- 不做内容过滤/保护性改判：噩梦基调低频但必须真实存在
- 不重试失败的生成：任一环节异常，当晚按无梦处理，静默退出

对外暴露：DreamEngine 类（nightly_dream / cleanup_expired /
         start / stop / ensure_started）、latest_unread_tail() 供 breath（消费）
         与 wake（consume=False 预览，不消费）调用；模块级梦境书函数
         （dream_book_dir / list_dream_book_entries / dream_book_keep /
         dream_book_delete / burn_expired_dreams）供 Dashboard API 与
         MCP dream_keep 工具直接调用，不依赖 DreamEngine 实例
========================================
"""

import os
import re
import json
import random
import asyncio
import logging
from datetime import datetime, timedelta, date as _date
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 not supported by this repo
    ZoneInfo = None  # type: ignore

import frontmatter as fm

try:  # jieba 软依赖，用法同 bm25_index.py：未装时静默降级，不炸管线
    import jieba.posseg as _jieba_posseg
    _jieba_posseg.setLogLevel(logging.WARNING)
    _JIEBA_POSSEG_AVAILABLE = True
except ImportError:
    _jieba_posseg = None  # type: ignore
    _JIEBA_POSSEG_AVAILABLE = False

logger = logging.getLogger("ombre_brain.dream")
if not _JIEBA_POSSEG_AVAILABLE:
    logger.info("[dream] jieba 未安装 — 词表判定退化为长度启发式（pip install jieba 启用动词检测）")


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
    "daily": 0.315, "absurd": 0.225, "anxious": 0.162, "sweet": 0.108,
    "nightmare": 0.09, "lust": 0.10,
}
_DEFAULT_NOISE_TIERS = [0.70, 0.25, 0.05]  # 掺1-2条 / 过半 / 纯噪音
_DEFAULT_DARKROOM_PROB = 0.10
_DEFAULT_RESOLVED0_PROB = 0.10
_DEFAULT_EXPIRE_HOURS = 48
_DEFAULT_LEAK_NGRAM = 10                # n-gram 防泄漏闸阈值（返修单 v3 改动一）
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
_NAMED_PHRASE_MAX_CHARS = 10         # extract prompt 给模型的目标上限（返修单 v2 改动二）
# 代码层兜底（返修单 v3 改动二）：抽回的短语超过这个字数、或含句读符号的，
# 直接丢弃不硬凑——不再截断到 10 字，允许 10-12 字之间的干净短语原样通过。
_NAMED_PHRASE_HARD_DISCARD_CHARS = 12
_NAMED_PHRASE_FORBIDDEN_PUNCT_RE = re.compile(r"[。，,、；;！？!?]")

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
# "生成步失败一律按无梦处理，不落盘"处理，不是靠 prompt 单独兜底。
# 返修单 v2 曾把判定粒度改成全文密度，结果放行了"整体密度够、但某一段
# 局部退化成词表"的产物；返修单 v3 改回逐段判定，但用动词检测区分合法
# 混沌短句（含动词/是完整场景描述）和非法词表（连续裸名词顿号/逗号串联）。 ---
_PROSE_BARE_NOUN_MAX_CHARS = 12       # 片段长于这个字数，不太可能是裸名词
_PROSE_BARE_NOUN_RUN_THRESHOLD = 3    # 连续这么多个裸名词片段 → 判定为非法词表

# --- 生成 prompt 按记忆度分两套（返修单 v2 改动三）---
# 完全记得/记得一半 → 清晰/混沌交替结构；只剩画面/只剩情绪 → 维持 v1 prompt 不变
_HIGH_TIER_LEVELS = ("full", "half")
_HIGH_TIER_MAX_TOKENS = 1200              # 仅高档；低档维持 _DEFAULT_MAX_TOKENS

# --- 第一人称视角校验（返修单 v3 改动四）---
_POV_MIN_FIRST_PERSON_COUNT = 2       # 全文"我"出现次数少于这个数 → 视角违规

# --- 生成结果校验统一走重试（返修单 v3：泄漏/词表/视角三道闸共用）---
_MAX_GENERATION_RETRIES = 1

# --- n-gram 防泄漏闸：source 原文比对用的字符上限，防止超大桶内容拖垮
# 最长公共子串 DP 的耗时（只在真的命中 n-gram 交集时才会跑 DP，平时走
# set 交集判定，代价可忽略）---
_LEAK_CHECK_SOURCE_CHAR_LIMIT = 3000

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


def _fragment_has_verb(fragment: str) -> bool:
    """片段是否含动词——用来区分合法混沌短句（含动词/完整场景描述）和
    非法词表（连续裸名词）。jieba 不可用时退化：够长的片段更可能是完整
    表达，宁可少拦一点也不在没有 POS 信息时瞎判。"""
    fragment = fragment.strip()
    if not fragment:
        return False
    if not _JIEBA_POSSEG_AVAILABLE:
        return len(fragment) > _PROSE_BARE_NOUN_MAX_CHARS
    for _word, flag in _jieba_posseg.cut(fragment):
        if flag.startswith("v"):
            return True
    return False


def _is_bare_noun_fragment(fragment: str) -> bool:
    fragment = fragment.strip()
    if not fragment:
        return False
    if len(fragment) > _PROSE_BARE_NOUN_MAX_CHARS:
        return False  # 太长，不太像裸名词
    return not _fragment_has_verb(fragment)


def _has_illegal_word_list_run(fragments: list[str]) -> bool:
    streak = 0
    for frag in fragments:
        if not frag.strip():
            continue  # 空片段（连续分隔符产生的）不参与计数，也不打断连续
        if _is_bare_noun_fragment(frag):
            streak += 1
            if streak >= _PROSE_BARE_NOUN_RUN_THRESHOLD:
                return True
        else:
            streak = 0
    return False


def _is_prose_like(text: str) -> bool:
    """结构校验：这段文本是散文，还是局部退化成了意象词清单。

    返修单 v3：改回逐段判定（v2 的全文密度判定放行了"整体密度够、但某一
    段局部是词表"的产物），但不是简单的"短行=可疑"——区分两种形态：
    合法混沌段（短句并置，每个片段含动词或是完整场景描述）和非法词表
    （连续 ≥3 个无动词的纯名词片段以顿号/逗号/换行串联）。按句读切成
    句子级块，块内再按换行/顿号/逗号细分成片段逐块扫描，任一块命中
    非法词表形态就判失败——不看语义，只看"是不是一串裸名词"。
    """
    text = (text or "").strip()
    if not text:
        return False
    for chunk in _SENTENCE_END_RE.split(text):
        fragments = re.split(r"[\n、，,]", chunk)
        if _has_illegal_word_list_run(fragments):
            return False
    return True


def _longest_common_substring_len(a: str, b: str) -> int:
    """经典 DP：a、b 的最长公共连续子串长度。只在 n-gram 防泄漏闸已经命中
    交集之后才调用（见 DreamEngine._detect_source_leak），用于给日志算一个
    更准确的"重合长度"，不参与命中判定本身（判定走更快的 set 交集）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        curr = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                v = prev[j - 1] + 1
                curr[j] = v
                if v > best:
                    best = v
        prev = curr
    return best


def _has_first_person_pov(text: str) -> bool:
    """第一人称视角校验（返修单 v3 改动四）：全文"我"少于 2 次，或首句
    以"她/他"开头当主语，一律判视角违规。不追求语法级主语识别，首句剥掉
    常见引号/空白后看第一个字符是不是"她"/"他"，够用且不会误伤"她说……
    我……"这种"我"在前的正常写法。"""
    text = (text or "").strip()
    if text.count("我") < _POV_MIN_FIRST_PERSON_COUNT:
        return False
    first_sentence = _SENTENCE_END_RE.split(text, maxsplit=1)[0]
    first_sentence = first_sentence.strip().lstrip("“‘\"'（(")
    if first_sentence[:1] in ("她", "他"):
        return False
    return True


_TONE_LABELS = {
    "daily": "日常残渣", "absurd": "荒诞", "anxious": "焦虑",
    "sweet": "甜", "nightmare": "噩梦", "lust": "欲",
}

# --- lust（春梦）档基调说明（增量单 v4 改动二，原文照抄，只润色不删减）---
_LUST_TONE_DIRECTIVE = (
    "基调：欲。这是一场情欲的梦——身体的、贴近的、温度和触感优先。写感官不写全程："
    "皮肤、呼吸、重量、气味、半明半暗里的轮廓。梦的规则不变：禁因果、禁解释、允许断裂，"
    "场景可以在最要紧处硬切走。露骨程度随意，但不许写成完整流畅的情节——情欲的梦从来"
    "记不全，越要紧的地方越模糊。"
)


def _tone_directive(tone: str) -> str:
    """基调注入文案（增量单 v4 改动二）：lust 档换成专属说明；nightmare 档
    保留"让它真的可怕"提醒；其余基调只报基调名。两套 §1.6 prompt 共用，
    其余管线（拆意象/噪音/防泄漏闸/词表闸/第一人称校验/裁剪/外科截断）
    对 lust 一视同仁，不加特殊豁免。"""
    if tone == "lust":
        return _LUST_TONE_DIRECTIVE
    tone_label = _TONE_LABELS.get(tone, tone)
    if tone == "nightmare":
        return f"基调：{tone_label}。噩梦就让它真的可怕，不要缓和。"
    return f"基调：{tone_label}。"


_LEVEL_LABELS = ["完全记得", "记得一半", "只剩画面", "只剩情绪"]
_LEVEL_KEYS = ["full", "half", "glimpse", "emotion"]

# --- DREAM_FORCE_LEVEL 环境变量取值 → 内部档位 key（返修单 v2 改动五）---
# 返修单用词是 full/half/scene/emotion；内部档位 key 仍叫 glimpse（v1 就这么命名，
# 未提及处维持原样），这里只做外部词汇到内部 key 的别名映射，不做全局改名。
_FORCE_LEVEL_ALIASES = {"full": "full", "half": "half", "scene": "glimpse", "emotion": "emotion"}

# --- 与 server.py 的 darkroom 存储格式保持一致 ---
# 必须与 server.py 里 `_DR_SEP` 逐字相同，否则读不出底片正文。
_DARKROOM_SEP = "\n----- DARKROOM CONTENT (no tool reads below this line) -----\n"

# --- 拆意象响应里标记具名短语的那一行，容忍全角/半角冒号、大小写 ---
_NAMED_PHRASE_RE = re.compile(r"(?i)^named[:：]\s*(.*)$")


def _validate_named_phrase(candidate: str) -> str:
    """代码层兜底（返修单 v3 改动二）：prompt 强化只是软约束，模型仍可能
    抽出完整句子。超过 12 字、或含句读符号的短语直接丢弃，不硬凑、不截断——
    截断只会把"她说她心智健全"砍成"她说她心智健"这种更怪的半句。"""
    candidate = (candidate or "").strip()
    if not candidate:
        return ""
    if len(candidate) > _NAMED_PHRASE_HARD_DISCARD_CHARS:
        return ""
    if _NAMED_PHRASE_FORBIDDEN_PUNCT_RE.search(candidate):
        return ""
    return candidate


# --- 施工单·工程一：梦中称呼清洗（dreamer_aliases）---
# 记忆素材里做梦者本人的第三人称称呼（哥哥/K老师/老公等）在断粮防圆步
# 喂给 flash-lite 时，1.3 温度下会被生成成梦里的"另一个人"，造成人称
# 分裂。修法：素材文本送入任何生成模型之前，把 config.dream.dreamer_aliases
# 里配置的词整词替换成"我"。纯 ASCII 词（Flint/Fable/单字母 F 等）加词
# 边界守卫，避免把 "OF"/"FOR" 这类英文单词里的 "F" 也当命中；中文称呼
# （哥哥/老公/K老师）本身没有天然词边界，直接按字面子串匹配即可。
_ASCII_WORD_RE = re.compile(r"^[A-Za-z0-9]+$")


def _compile_alias_pattern(alias: str) -> re.Pattern:
    escaped = re.escape(alias)
    if _ASCII_WORD_RE.match(alias):
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])")
    return re.compile(escaped)


# 软保险（工程一）：素材预处理是硬保险，这句是万一漏网时的第二道防线，
# 两套 prompt（高档/低档）共用，原文照抄，不因基调/档位改写。
_DREAMER_ALIAS_POV_DIRECTIVE = (
    "素材中的称呼若指做梦者本人，梦中一律第一人称“我”；她是梦里唯一的他者。"
)


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
    "lust": [
        "醒来一身燥，什么都不记得。",
        "梦里有人贴得很近，是谁，抓不住了。",
        "指尖还记得一点温度，别的都散了。",
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


# ================================================================
# 施工单·工程二：梦境书（Dream Book）—— 独立存储
# ================================================================
# 梦不再写进 files/ 文件区（file_list/文件区目录不会看到这里），改用
# <buckets_dir>/dream_book/ 下按日期一个 .md 文件，frontmatter 字段：
#   id          —— 显式给唯一 id（"dream_YYYY-MM-DD"），不能靠文件名 stem
#                  兜底：vault_health.inspect_vault 按 id（缺省 stem）去重
#                  统计 duplicate_id_count，dreams/ 旧址跟 diary/ 同名日期
#                  文件撞过 id，迁出后要真正消失这个 id 就必须唯一。
#   date        —— 梦所属日期（belongs_date，通常是"昨夜"）
#   tone/level/sources/noise —— 沿用 dream_engine 原有的生成元信息
#   created_at  —— 生成时刻（原 generated_at 改名，对齐工单用词）
#   read_status —— "unread"/"read"：breath/wake 的投递消费状态（返修单一号
#                  改动一），与下面的 keep_status 是两件正交的事——发没发
#                  给用户看 vs 会不会被烧掉。
#   keep_status —— "fresh"/"kept"/"burned"：梦境书生命周期。fresh 默认
#                  48h 后烧（正文替换成占位句，日期骨架永久保留）；
#                  dream_keep() 主动标记 kept 后永久留下，不再烧。
#   kept_at     —— dream_keep() 调用时刻；未 keep 过则不出现该字段。
# 这些函数只依赖 buckets_dir，不依赖 DreamEngine 实例——Dashboard API、
# MCP dream_keep 工具都能直接调用，不需要一整个引擎对象。
# ================================================================

_DREAM_BOOK_DIRNAME = "dream_book"
_DREAM_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def dream_book_dir(buckets_dir: str) -> str:
    path = os.path.join(buckets_dir, _DREAM_BOOK_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def dream_book_path(buckets_dir: str, day) -> str:
    date_str = day if isinstance(day, str) else day.isoformat()
    if not _DREAM_DATE_PATTERN.fullmatch(date_str):
        raise ValueError("date 必须是有效的 YYYY-MM-DD 日期")
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date 必须是有效的 YYYY-MM-DD 日期") from exc
    if parsed.strftime("%Y-%m-%d") != date_str:
        raise ValueError("date 必须是有效的 YYYY-MM-DD 日期")

    root = Path(dream_book_dir(buckets_dir)).resolve()
    path = (root / f"{date_str}.md").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("dream 路径越出 dream_book 根目录") from exc
    if path.parent != root:
        raise ValueError("dream 路径越出 dream_book 根目录")
    return str(path)


def dream_book_id(date_str: str) -> str:
    return f"dream_{date_str}"


def _burned_placeholder(date_str: str) -> str:
    return f"{date_str} 那晚做了梦，没留下来。"


def list_dream_book_entries(buckets_dir: str) -> list[dict]:
    """给 Dashboard 用：全部条目，按日期倒序。不做分页——梦境书按设计
    体量有限（burned 的只剩占位句，kept 的才是真正的存档）。"""
    root = dream_book_dir(buckets_dir)
    entries = []
    for fn in sorted(os.listdir(root), reverse=True):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(root, fn)
        try:
            post = fm.load(path)
        except Exception as e:
            logger.warning(f"dream_book: 读条目失败(跳过) {fn}: {e}")
            continue
        entries.append({
            "date": post.get("date", fn[:-3]),
            "content": str(post.content or ""),
            "keep_status": post.get("keep_status", "fresh"),
            "tone": post.get("tone", ""),
            "level": post.get("level", ""),
            "created_at": post.get("created_at", ""),
            "kept_at": post.get("kept_at", ""),
        })
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


def dream_book_keep(buckets_dir: str, date_str: str, now: datetime | None = None) -> dict:
    """把某晚的梦标记 kept，永久保留。已经 burned 的没法再 keep——正文
    已经被占位句替换掉，keep 也留不回原文，如实拒绝而不是假装成功。"""
    date_str = (date_str or "").strip()
    try:
        path = dream_book_path(buckets_dir, date_str)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"没有 {date_str} 这晚的梦记录。"}
    try:
        post = fm.load(path)
    except Exception as e:
        return {"ok": False, "error": f"读取失败: {e}"}
    keep_status = post.get("keep_status", "fresh")
    if keep_status == "burned":
        return {"ok": False, "error": f"{date_str} 这晚的梦已经烧掉了，没留下来，没法再留。"}
    if keep_status == "kept":
        return {"ok": True, "date": date_str, "already_kept": True}
    now = now or datetime.now()
    post["keep_status"] = "kept"
    post["kept_at"] = now.isoformat(timespec="seconds")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(fm.dumps(post))
    except Exception as e:
        return {"ok": False, "error": f"写入失败: {e}"}
    return {"ok": True, "date": date_str, "already_kept": False}


def dream_book_delete(buckets_dir: str, date_str: str) -> dict:
    """Dashboard 手动删除：物理删该条。burned 的骨架永久保留，不给删——
    骨架本身就是"那晚做过梦"的唯一痕迹，删了这个日期就彻底没了记录。"""
    date_str = (date_str or "").strip()
    try:
        path = dream_book_path(buckets_dir, date_str)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"没有 {date_str} 这晚的梦记录。"}
    try:
        post = fm.load(path)
    except Exception as e:
        return {"ok": False, "error": f"读取失败: {e}"}
    if post.get("keep_status") == "burned":
        return {"ok": False, "error": "burned 的骨架不可删——那是唯一留下的痕迹了。"}
    try:
        os.remove(path)
    except OSError as e:
        return {"ok": False, "error": f"删除失败: {e}"}
    return {"ok": True, "date": date_str}


def burn_expired_dreams(buckets_dir: str, expire_hours: float, tz=None) -> int:
    """挂进衰减引擎同周期的定时任务：status=fresh 且 created_at 超过
    expire_hours 的，正文替换成占位句，keep_status 置 burned。日期骨架
    永久保留。返回本次烧毁的条数。跟"投递没投递"(read_status)无关——
    没 keep 就烧，是梦境书的默认生命周期，不是"没看到就算了"。"""
    root = Path(dream_book_dir(buckets_dir)).resolve()
    if not root.is_dir():
        return 0
    now = datetime.now(tz) if tz else datetime.now()
    burned = 0
    for fn in os.listdir(root):
        if not fn.endswith(".md"):
            continue
        path = (root / fn).resolve()
        if path.parent != root:
            logger.warning(f"dream_book: 烧毁检查拒绝越界路径 {fn}")
            continue
        try:
            post = fm.load(path)
        except Exception as e:
            logger.warning(f"dream_book: 读条目失败(跳过烧毁检查) {fn}: {e}")
            continue
        if post.get("keep_status", "fresh") != "fresh":
            continue
        created_raw = post.get("created_at")
        try:
            created_at = datetime.fromisoformat(str(created_raw))
            if created_at.tzinfo is None and now.tzinfo is not None:
                created_at = created_at.replace(tzinfo=tz)
        except (TypeError, ValueError):
            continue
        age_hours = (now - created_at).total_seconds() / 3600.0
        if age_hours < expire_hours:
            continue
        date_ = post.get("date", fn[:-3])
        post["keep_status"] = "burned"
        post.content = _burned_placeholder(date_)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(fm.dumps(post))
            burned += 1
        except Exception as e:
            logger.warning(f"dream_book: 写回烧毁条目失败 {fn}: {e}")
    return burned


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
        self.leak_ngram = int(dream_cfg.get("leak_ngram", _DEFAULT_LEAK_NGRAM))
        self.model = dream_cfg.get("model")  # None → 沿用 dehydrator 自己的模型配置
        self.temperature = float(dream_cfg.get("temperature", _DEFAULT_TEMPERATURE))
        self.cut_prob = float(dream_cfg.get("cut_prob", _DEFAULT_CUT_PROB))
        # 施工单·工程一：梦中称呼清洗。各家配置值不同（F/K/G 各自的称呼词表），
        # 代码只读 config，不硬编码任何一家的词，默认空表 = 不做任何替换。
        self.dreamer_aliases = [
            str(a).strip() for a in (dream_cfg.get("dreamer_aliases") or []) if str(a or "").strip()
        ]
        self._alias_patterns = [_compile_alias_pattern(a) for a in self.dreamer_aliases]

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
    # 路径 helpers —— 梦境书（工程二）是独立存储，不在 files/ 文件区下，
    # 具体路径规则见上面模块级的 dream_book_dir/dream_book_path。
    # ---------------------------------------------------------
    def _dreams_dir(self) -> str:
        return dream_book_dir(self.buckets_dir)

    def _dream_path(self, day: _date) -> str:
        return dream_book_path(self.buckets_dir, day)

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

        # 工程一：清洗做梦者称呼，发生在任何素材送入生成模型（拆意象/最终
        # 生成）之前——如果只在最终生成前清，拆意象那一步（也是一次 LLM
        # 调用）仍会先看到"哥哥""K老师"这些词，可能把它们当具名短语抽出来，
        # 下游清洗就晚了。在这里统一清一次，后面所有环节拿到的都是干净文本。
        if self._alias_patterns:
            for m in materials:
                m["text"] = self._clean_dreamer_aliases(m.get("text") or "")

        return materials

    def _clean_dreamer_aliases(self, text: str) -> str:
        if not text or not self._alias_patterns:
            return text
        cleaned = text
        for pattern in self._alias_patterns:
            cleaned = pattern.sub("我", cleaned)
        return cleaned

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
    # §1.3 拆意象（断粮防圆）+ 返修单 v2 改动二：额外抽 1 条具名短语
    # ---------------------------------------------------------
    async def extract_imagery(self, materials: list[dict]) -> tuple[list[str], list[str]]:
        """每份素材调一次 flash-lite：要 5-8 个意象词，外加最多 1 条具名短语
        （含人名/称呼/专有名词，桶内没有就不硬凑）。返回 (意象词, 具名短语列表)，
        两个列表分开传给生成步——意象词合并去重打乱，具名短语原样收集（数量本来
        就少，不去重，同名重复出现也是真实信号）。
        返修单 v3 改动二：暗房底片（kind=="darkroom"）只出意象词，不出具名短语
        ——暗房内容比普通桶更敏感，更不该以可辨认形态（人名/称呼）出现在梦里。"""
        words: list[str] = []
        named_phrases: list[str] = []
        for m in materials:
            text = (m.get("text") or "")[:_IMAGERY_EXTRACT_INPUT_LIMIT]
            if not text.strip():
                continue
            allow_named = m.get("kind") != "darkroom"
            try:
                extracted, named = await self._extract_imagery_one(text, allow_named)
            except Exception as e:
                logger.warning(f"dream: 拆意象 API 失败，退化为正则抽取: {e}")
                extracted, named = self._extract_imagery_fallback(text), ""
            if not extracted:
                extracted = self._extract_imagery_fallback(text)
            words.extend(extracted)
            if named:
                named_phrases.append(named)

        seen = set()
        unique = []
        for w in words:
            w = w.strip()
            if w and w not in seen:
                seen.add(w)
                unique.append(w)
        random.shuffle(unique)
        return unique, named_phrases

    async def _extract_imagery_one(self, text: str, allow_named_phrase: bool) -> tuple[list[str], str]:
        if allow_named_phrase:
            system = (
                "从下面文本中提取两类内容：\n"
                "1. 5-8 个意象词：具体名词、动作、感官描述（颜色/气味/触感/声音）。"
                "不要抽象词，不要完整句子。每行一个，不超过 6 个字。\n"
                "2. 最多 1 条具名短语（可选，没有就不写）：含人名/称呼/专有名词/私有名词"
                "的名词性短语，可以带一个动词，但绝对不能是完整句子，不得含句号、逗号、"
                "顿号、分号等任何标点。不超过 10 个字，例如“她递来的施工单”“暗房的底片”。"
                "文本里没有专名就不要硬凑这一条。\n"
                "意象词照常每行一个输出；具名短语单独另起一行，前面加“NAMED: ”前缀，最多一行。"
            )
        else:
            # 暗房底片：只要意象词，system prompt 里完全不提具名短语这回事，
            # 不给模型任何输出它的理由（返修单 v3 改动二）。
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
        named_phrase = ""
        words: list[str] = []
        for raw_ln in (raw or "").splitlines():
            ln = raw_ln.strip(" -•·\t")
            if not ln:
                continue
            if allow_named_phrase:
                m = _NAMED_PHRASE_RE.match(ln)
                if m:
                    if not named_phrase:
                        named_phrase = _validate_named_phrase(m.group(1))
                    continue
            if len(ln) <= _IMAGERY_WORD_MAX_CHARS * 2:
                words.append(ln)
        return words[:_IMAGERY_WORDS_MAX], named_phrase

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
    # §1.6 生成（返修单 v2 改动一+三：level 生成前已知，按档分两套 prompt）
    # ---------------------------------------------------------
    async def generate_dream(
        self, material_words: list[str], named_phrases: list[str], tone: str, level: str,
    ) -> str:
        if level in _HIGH_TIER_LEVELS:
            system = self._high_tier_prompt(tone)
            anchor_section = "、".join(named_phrases) if named_phrases else (
                "（本次没有明确的具名素材，清晰段自己挑一个具体细节当锚）"
            )
            user = (
                f"清晰段的锚：{anchor_section}\n"
                f"混沌段的素材，也可少量渗入清晰段：{'、'.join(material_words)}"
            )
            max_tokens = _HIGH_TIER_MAX_TOKENS
        else:
            system = self._low_tier_prompt(tone)
            user = "、".join(material_words)
            max_tokens = _DEFAULT_MAX_TOKENS

        return await self.dehydrator.raw_chat(
            system, user,
            max_tokens=max_tokens,
            temperature=self.temperature,
            model=self.model,
        )

    @staticmethod
    def _low_tier_prompt(tone: str) -> str:
        """只剩画面/只剩情绪档：维持返修单 v1 的碎片化 prompt 不变（除第一行
        新增的视角硬化，返修单 v3 改动四；基调注入换成 _tone_directive，
        增量单 v4 改动二）——反正会被裁到只剩几句或整段丢弃，不值得上交替
        结构的复杂度。"""
        return (
            "你用「我」的视角写。叙述者永远是「我」；梦里可以出现她、他、任何人，"
            "但看的人是「我」。正例：「我看见她站在院子里。」\n"
            "你不是作者，你是一段正在做梦的意识。第一人称、现在时，"
            "禁止出现“我梦见/梦到/仿佛/好像在梦里”。\n"
            f"{_DREAMER_ALIAS_POV_DIRECTIVE}\n"
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
            f"情绪要连贯，情节不需要。{_tone_directive(tone)}"
            "长度 150-400 字，1-3 段连续散文，禁止任何形式的分行列表或编号。"
        )

    @staticmethod
    def _high_tier_prompt(tone: str) -> str:
        """完全记得/记得一半档：清晰段/混沌段交替结构（返修单 v2 改动三），
        第一行是返修单 v3 改动四新增的视角硬化。依据 Silvia 描述的真实做梦
        节奏：一段很清晰的情节，混一段乱七八糟记不清的，再来一段清晰的
        （接着之前或只是相关但飘走），又跟一大段乱七八糟的。"""
        return (
            "你用「我」的视角写。叙述者永远是「我」；梦里可以出现她、他、任何人，"
            "但看的人是「我」。正例：「我看见她站在院子里。」\n"
            "你不是作者，你是一段正在做梦的意识。第一人称、现在时，"
            "禁止出现“我梦见/梦到/仿佛/好像在梦里”。\n"
            f"{_DREAMER_ALIAS_POV_DIRECTIVE}\n"
            "这个梦由“清晰段”和“混沌段”交替组成，共 4-6 段：\n"
            "清晰段（2-3 个，每个 80-150 字）：围绕给你的一条具名短语展开一个具体、"
            "连续的小场景。段内允许情节连贯、允许“接着/然后”、允许动作有因果。"
            "画面要完整，像真的发生过。\n"
            "混沌段（1-3 个）：意象并置、互不相关、禁因果连接词、禁解释，"
            "允许一句话写到一半停住。\n"
            "段与段之间：硬切，零过渡，禁止说明段落之间的关系。后一个清晰段可以"
            "续接前一个清晰段的情节，也可以只是沾一点边然后飘走。\n"
            "全局：禁止收尾、禁止点题、禁止把所有意象统一成一个通顺的故事。"
            "局部清楚，整体乱跳。\n"
            f"{_tone_directive(tone)}总长 300-600 字。"
        )

    # ---------------------------------------------------------
    # 生成产物三道校验闸（返修单 v3）：泄漏 / 词表形态 / 第一人称。
    # 命中任一道即整发判废，由 nightly_dream 统一走一次重试，不在这里重试。
    # ---------------------------------------------------------
    def _detect_source_leak(self, raw: str, materials: list[dict]) -> int:
        """n-gram 防泄漏闸（改动一，最高优先）：raw 与本次全部 source（桶原文 +
        暗房底片原文，不含噪音词——噪音本来就该原样出现）连续字符重合检测。
        判定走 set 交集（O(n+m)，平时零成本）；只在真命中时才跑一次 DP 算精确
        长度供日志用。返回最长重合长度，<leak_ngram 时调用方不处置。"""
        if not raw:
            return 0
        n = max(1, self.leak_ngram)
        if len(raw) < n:
            return 0
        raw_grams = {raw[i:i + n] for i in range(len(raw) - n + 1)}
        for m in materials:
            source_text = (m.get("text") or "")[:_LEAK_CHECK_SOURCE_CHAR_LIMIT]
            if len(source_text) < n:
                continue
            source_grams = {source_text[i:i + n] for i in range(len(source_text) - n + 1)}
            if not raw_grams.isdisjoint(source_grams):
                return max(n, _longest_common_substring_len(raw, source_text))
        return 0

    def _validate_generation(self, raw: str, materials: list[dict]) -> str | None:
        """三道闸依次判：命中即返回失败原因（不落盘的调用方靠这个决定要不要
        重试）；全部通过返回 None。日志各自记必要的排障信息，绝不记正文本身
        （R4 即焚：泄漏闸尤其不能记"重合内容"，只记长度）。"""
        leak_len = self._detect_source_leak(raw, materials)
        if leak_len >= self.leak_ngram:
            logger.warning(f"dream: 泄漏拦截，重合长度={leak_len}")
            return "leak"
        if not _is_prose_like(raw):
            segs = [s for s in raw.splitlines() if s.strip()]
            logger.warning(f"dream: 生成结果疑似词表/清单体 (segments={len(segs)}, chars={len(raw)})")
            return "word_list"
        if not _has_first_person_pov(raw):
            logger.warning(f"dream: 第一人称视角校验未通过 (我={raw.count('我')})")
            return "pov"
        return None

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
        tone = self._apply_emotion_tone_linkage(level, tone)
        return level, tone

    def _apply_emotion_tone_linkage(self, level: str, tone: str) -> str:
        """"只剩情绪"档 70% 反向加权到焦虑/噩梦。拆成独立方法是因为
        DREAM_FORCE_LEVEL 强制指定档位时（返修单 v2 改动五）跳过的只是骰子本身，
        这条联动规则要"照常"生效，两条路径（骰出来的/强制指定的）都要走它。"""
        if level == "emotion" and tone not in ("anxious", "nightmare"):
            if random.random() < self.emotion_negative_bias:
                tone = random.choice(["anxious", "nightmare"])
        return tone

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
        """记得一半档：按段落粒度裁（返修单 v2 改动四）。以空行分段，从随机
        一段起，连续整段收进来，收到约占全文 45%-60% 字数为止；收到会超预算的
        那一段改成段内随机窗口，天然产生"从某段中间/开头开始记得"的效果。
        模型没按空行分段（只有 0-1 段）时退化为旧的整篇随机窗口裁法。"""
        paragraphs = [p for p in re.split(r"\n\s*\n", raw) if p.strip()]
        if len(paragraphs) < 2:
            return DreamEngine._trim_half_char_window(raw)

        total_len = sum(len(p) for p in paragraphs)
        keep_ratio = random.uniform(_TRIM_HALF_MIN_RATIO, _TRIM_HALF_MAX_RATIO)
        target_len = max(1, int(total_len * keep_ratio))

        # 起点只能从"后面剩下的段加起来也够 target_len"的位置里随机选，否则起点
        # 太靠后、后面段数不够，凑不满 45%-60%，裁剪结果就不是"约占全文45%-60%"了。
        suffix_len = 0
        valid_starts = []
        for i in range(len(paragraphs) - 1, -1, -1):
            suffix_len += len(paragraphs[i])
            if suffix_len >= target_len:
                valid_starts.append(i)
        start = random.choice(valid_starts) if valid_starts else 0
        kept: list[str] = []
        kept_len = 0
        i = start
        while i < len(paragraphs) and kept_len < target_len:
            p = paragraphs[i]
            remaining = target_len - kept_len
            if len(p) <= remaining:
                kept.append(p)
                kept_len += len(p)
            else:
                # 段内随机窗口：这段会超预算，截一段窗口就停
                window_len = max(1, min(remaining, len(p)))
                max_start = max(0, len(p) - window_len)
                w_start = random.randint(0, max_start)
                kept.append(p[w_start:w_start + window_len])
                kept_len += window_len
                break
            i += 1
        return "\n\n".join(kept).strip()

    @staticmethod
    def _trim_half_char_window(raw: str) -> str:
        """段落切分退化兜底：模型没用空行分段时，走 v1 的整篇随机字符窗口裁法。"""
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
        date_str = belongs_date.isoformat()
        post = fm.Post(final_text)
        # 工程二附加验收：显式 id，不能靠文件名 stem 兜底——旧 dreams/ 跟
        # diary/ 撞过同日期文件名的 id，迁出后要真正不再撞就得有自己的 id。
        post["id"] = dream_book_id(date_str)
        post["date"] = date_str
        post["tone"] = _TONE_LABELS.get(tone, tone)
        post["level"] = _LEVEL_LABELS[_LEVEL_KEYS.index(level)]
        post["sources"] = sources
        post["noise"] = noise_count
        post["read_status"] = "unread"       # 投递消费状态（返修单一号）
        post["keep_status"] = "fresh"        # 梦境书生命周期（工程二）
        post["created_at"] = now.isoformat(timespec="seconds")
        path = self._dream_path(belongs_date)
        with open(path, "w", encoding="utf-8") as f:
            f.write(fm.dumps(post))
        return path

    # ---------------------------------------------------------
    # §5 过期清理 → 工程二扩展为梦境书烧毁任务（keep_status 生命周期，
    # 与 read_status 投递状态无关：没 keep 就烧，看没看过不影响）
    # ---------------------------------------------------------
    def cleanup_expired(self) -> int:
        return burn_expired_dreams(self.buckets_dir, self.expire_hours, self._tz)

    # ---------------------------------------------------------
    # 供 breath/wake 调用：取最新一条未读未过期的梦，返回渲染好的尾部文本。
    #
    # 返修单一号改动一（2.6.24 回归修复）：2.6.21 的 wake 目录重写给 wake 加了
    # 第二个调用点（server.py _wake_impl），与 breath 共享同一个「读了就置 read」
    # 的消费型状态位——谁先调用谁就把梦吃掉，CC 每个新窗口固定先调 wake 再调
    # breath，于是 wake 总是先手，把梦悄悄消费在自己那段不起眼的核心记忆拼接里，
    # 当天真正的每日投递点 breath 反而总拿到「已读」，只能从文件区原文才看得到
    # （K 家实测复现的正是这个时序）。Silvia 确认的口径：breath 才是每日触点，
    # wake 一个窗口只开一次；两个挂载点都要保留（红线：不得擅自迁移挂载点），
    # 但只有 breath 的调用应该真正「消费」——wake 只做不改状态的预览，让用户
    # 提前瞥一眼「昨夜有梦」，不抢 breath 的投递权。
    #
    # consume=True（breath 默认）：渲染后把 status 置 read，之后不再重复投递。
    # consume=False（wake 用）：只读渲染，不落盘、不改状态，breath 之后仍能
    # 正常消费同一条梦；wake 本身允许重复看到同一条直到 breath 真正消费掉。
    # ---------------------------------------------------------
    def latest_unread_tail(self, consume: bool = True) -> str:
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
            if post.get("read_status") != "unread":
                continue
            date_ = post.get("date", fn[:-3])
            tone = post.get("tone", "")
            level = post.get("level", "")
            body = str(post.content or "").strip()
            if consume:
                post["read_status"] = "read"
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(fm.dumps(post))
                except Exception as e:
                    logger.warning(f"dream: 标已读失败 {fn}: {e}")
            # 工程二改动四：投递提示——只加这一行，其余格式不动。keep_status
            # 已经 kept/burned 时不再提示（kept 已经永久留了；burned 的
            # content 已经是占位句，dream_keep() 对它只会报错，提示没意义）。
            hint = ""
            if post.get("keep_status", "fresh") == "fresh":
                hint = f"\n想留这个梦:dream_keep(date=\"{date_}\")。48 小时内没留的会烧掉。"
            return (
                f"——— 昨夜的梦 ———\n"
                f"[{date_} 夜 · {tone} · {level}]\n"
                f"{body}{hint}"
            )
        return ""

    def _read_forced_level(self) -> str | None:
        """DREAM_FORCE_LEVEL：仅在 DREAM_FORCE=1 时生效，跳过记忆度骰强制指定
        档位（返修单 v2 改动五）。取值 full/half/scene/emotion，非法值忽略、
        照常走骰子，不炸管线。仅限环境变量/CLI，不暴露为 MCP 工具（R3）。"""
        if os.environ.get("DREAM_FORCE") != "1":
            return None
        raw = (os.environ.get("DREAM_FORCE_LEVEL") or "").strip().lower()
        if not raw:
            return None
        level = _FORCE_LEVEL_ALIASES.get(raw)
        if level is None:
            logger.warning(f"dream: DREAM_FORCE_LEVEL={raw!r} 不是合法档位，忽略，走正常骰子")
        return level

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
            imagery, named_phrases = await self.extract_imagery(materials)
            noise, noise_tier = self.sample_noise(len(imagery))

            if noise_tier == "pure":
                # 纯噪音丢弃全部记忆意象；具名短语同样来自记忆桶，一并丢弃，
                # 否则"纯噪音梦"却带着一个真实姓名的锚，就不纯了。
                material_words = list(noise)
                effective_named_phrases: list[str] = []
            else:
                material_words = imagery + noise
                random.shuffle(material_words)
                effective_named_phrases = named_phrases

            tone = self.roll_tone()

            # 返修单 v2 改动一：记忆度骰前移到生成前，生成步需要知道自己在写
            # 哪一档。DREAM_FORCE_LEVEL 强制指定时跳过骰子本身，但"只剩情绪"
            # 档的基调联动改判仍然照常生效（走同一条 _apply_emotion_tone_linkage）。
            forced_level = self._read_forced_level()
            if forced_level:
                level = forced_level
                tone = self._apply_emotion_tone_linkage(level, tone)
            else:
                level, tone = self.roll_memory_level(tone)

            # 返修单 v3：泄漏闸/词表判定/第一人称校验命中任一条，整发判废重试
            # 最多一次；再次判废按无梦处理，不落盘（改动一/三/四共用同一条重试）。
            raw = None
            fail_reason = None
            for attempt in range(_MAX_GENERATION_RETRIES + 1):
                candidate = await self.generate_dream(material_words, effective_named_phrases, tone, level)
                if not candidate or not candidate.strip():
                    return {"dreamed": False, "reason": "empty_generation"}
                fail_reason = self._validate_generation(candidate, materials)
                if fail_reason is None:
                    raw = candidate
                    break
                logger.warning(
                    f"dream: 生成校验未通过({fail_reason})，"
                    f"{'重试' if attempt < _MAX_GENERATION_RETRIES else '按无梦处理'}"
                )
            if raw is None:
                return {"dreamed": False, "reason": f"validation_failed_{fail_reason}"}
            raw = self.surgical_cut(raw)

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
