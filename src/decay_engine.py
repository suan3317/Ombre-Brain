"""
========================================
decay_engine.py — 记忆衰减引擎，模拟人类遗忘曲线
========================================

这个文件负责给每个桶算「现在还有多重」的权重分，然后把分数掉到阈值以下
的桶搬到 archive。后台一个 asyncio 任务每隔 N 小时跑一次。

关键行为：
- 打分公式（改进版艾宾浩斯 + 情感坐标）：
    Score = Importance × (activation_count^0.3) × e^(-λ×days) × emotion_weight
- 情感权重 = base + arousal × arousal_boost；唤醒度高的记忆衰减得慢
- anchor / pinned / protected 桶不参与衰减、不被归档
- ensure_started() 幂等启动后台循环；可被测试 monkeypatch 成 noop

不做什么（边界）：
- 不删除桶（只把分数低的搬到 archive）
- 不做内容修改、不打标、不调用 LLM
- 不决定「该不该 hold/grow」，只对已有桶打分

对外暴露：DecayEngine 类（calculate_score / run_once / ensure_started）
========================================
"""

import math
import asyncio
import logging
from datetime import datetime

from utils import parse_bool, parse_iso_datetime

logger = logging.getLogger("ombre_brain.decay")


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §⑩：禁止裸魔法数字。下面这些常量原本散落在 calculate_score()
# 和 run_decay_cycle() 各处，集中后：① 公式可读性大幅提升；
# ② 任何调参改一处即可；③ 单元测试可直接 import 这些常量做断言。
#
# ⚠️ 改这些数字前先读 rule.md §1.0 哲学："记忆只会淡去，不会消失"。
# decay 不是删除，是分数下沉。改 threshold/lambda 会直接影响"多少天后被遗忘"。
# ============================================================

# --- DecayEngine 默认值（被 config.yaml 的 decay.* 覆盖）---
_DEFAULT_LAMBDA = 0.05            # 指数衰减率：每过一天分数 × e^(-λ)
_DEFAULT_THRESHOLD = 0.3          # 低于此分数 → 归档
_DEFAULT_CHECK_INTERVAL_HRS = 24  # 后台循环间隔（小时）
_DEFAULT_EMOTION_BASE = 1.0       # 情感权重基准
_DEFAULT_AROUSAL_BOOST = 0.8      # arousal 每 +1 → 情感权重 +0.8

# --- 锁分：某些桶不参与衰减 ---
_SCORE_PINNED = 999.0    # pinned / protected / permanent 桶恒高分（永不归档）
_SCORE_FEEL = 50.0       # feel / plan / letter 桶固定中分（生命周期由 status 控制）

# --- v3 Commit A：seed 打分 —— floor 不是冻结（设计定稿推论四）---
# seed 待遇跟 pinned/permanent 那种"恒 999"的冻结不一样：floor 是硬下限，
# 不是天花板，活动信号仍能往上加一点（但受 importance 递减约束，宪法推论一
# "高权重桶不需要更多分"）。floor 是保守初值，不写死：config.seed.floor
# 可调，未配置时用这里的默认值。
# 初值理由：floor=3.0——比归档阈值(_DEFAULT_THRESHOLD=0.3)高一个数量级，
# 明确"不会被误判成快归档的桶"，但远低于 _SCORE_FEEL(50)，不会在排序里
# 假装成"跟 feel/plan/letter 一个量级的活跃桶"。
# v3 Commit C 更新：activity_bonus 部分已改用正式的 activity_bonus()（见
# 下方"排序结构"小节），不再是 activation_count 代理；
# config.seed.activity_bonus_scale 这个专属 seed 的旧配置项已退役，改用
# 通用的 config.activity_bonus.scale（所有桶共用同一套 activity_bonus 曲线，
# seed 不该有自己单独一份——两正交轴设计的整个意义就是让 seed 和普通桶
# 用同一套 activity_bonus 语言，只是 retention 轴待遇不同）。
_DEFAULT_SEED_FLOOR = 3.0

# ============================================================
# v3 Commit C：排序结构（retention + activity_bonus 双轴）
# ------------------------------------------------------------
# 架构裁定（F，2026-08-09）：双轴只接管"排序面"——band 配额浮现(4/4/2)、
# breath 默认浮现排序、检索排序的 band 不可越界约束（这条是定稿不变量，
# 本期必须落地）。archiving 阈值判定与 Dashboard"活跃度分"显示继续走
# 老 calculate_score() 不动，本期不切：归档是有数据后果的动作，公式一夜
# 切换可能造成批量误归档；归档链路迁移到 retention 是独立后续项，届时
# 先出 dry-run 对比报告再切（见 run_decay_cycle() 里对应 TODO）。
# ============================================================

# band 划分：施工单定案，回放定案（K/F 两家 importance>=8 沉底桶近半、
# 5/3/2 下中档当月新桶漏出严重），不复议。
_BAND_HIGH_MIN = 8
_BAND_MID_MIN = 6
_BAND_ORDER = ("low", "mid", "high")  # 低→高，用于分配不重叠的分数区间
_BAND_SCORE_RANGE = 1000.0  # 每个 band 独占的分数宽度——"检索排序最终分不得
                             # 越band"这条不变量，靠区间物理不重叠保证，不
                             # 是靠排序逻辑"尽量不越界"。

# 浮现配额：施工单定案 4/4/2（高/中/低），回放数据支持，config.band_quota.*
# 可覆盖（万一后续再调，不用改代码）。
DEFAULT_BAND_QUOTA = {"high": 4, "mid": 4, "low": 2}

# 负反馈两段式：设计定稿已拍板的数字（不是待回放参数）。适用范围
# importance<6 且非 pinned/protected/permanent/anchor/seed——同一条边界线
# bucket_manager.py 的 record_semantic_recall_without_use() 独立定义了一份
# （避免跨模块硬耦合导入），两处必须保持一致，改一处记得改另一处。
_NEG_FEEDBACK_STREAK_GRACE_ENDS = 5     # 连续 5 次未使用 → 取消止衰
_NEG_FEEDBACK_STREAK_EXTRA_DECAY = 10   # 连续 10 次未使用 → 额外衰减
_NEG_FEEDBACK_EXTRA_DECAY_FRACTION = 0.125  # 1/8
_NEG_FEEDBACK_IMPORTANCE_MAX = 6

# "止衰"的具体实现（CC 设计，设计定稿只给了"连续5次未使用→取消止衰"这句
# 结论，没给机制）：触发负反馈前，适用范围内的桶衰减率打折（宽限期，给
# "边缘记忆"一点容错，呼应宪法"分数优先奖励边缘记忆"）；触发后打折取消，
# 回到全额衰减率。未定参数，config.retention.grace_lambda_discount 可调。
# 初值理由：0.5——打对折是"明显更慢但仍在衰减"，不是止住不动（宪法推论三：
# 任何富者愈富闭环设计都是错的，止衰不能做成事实上的免死金牌）。
_DEFAULT_RETENTION_GRACE_LAMBDA_DISCOUNT = 0.5

# activity_bonus 衰减曲线：未定参数，config.activity_bonus.* 可调。
# 初值理由：half_life=72h——比 decay 模块自身"新鲜度加成"的半衰期(36h，
# 见下方 _FRESHNESS_HALF_LIFE_HRS)更慢，因为 activity_bonus 承载的是"最近
# 一次真正被用上"这件事的分量，不该跟"最近浮现过"一样快消退；scale=2.0，
# 配合 importance 递减（宪法推论一）后，量级跟 retention 轴大致可比，不会
# 单方面压过/被压过对方。
_DEFAULT_ACTIVITY_BONUS_HALF_LIFE_HRS = 72.0
_DEFAULT_ACTIVITY_BONUS_SCALE = 2.0


def band_of(importance) -> str:
    """importance 决定 band：高 8-10 / 中 6-7 / 低 1-5。

    这条边界线跟 bucket_manager.py 的 seed 阈值(importance>=8 不自动成为
    种子)、负反馈阈值(importance<6 才适用)共用同一组数字不是巧合——"中档"
    本来就是从这两条既有边界之间切出来的。
    """
    try:
        imp = int(importance)
    except (TypeError, ValueError):
        imp = _DEFAULT_IMPORTANCE
    if imp >= _BAND_HIGH_MIN:
        return "high"
    if imp >= _BAND_MID_MIN:
        return "mid"
    return "low"


def band_floor(band: str) -> float:
    """band 的分数区间下限。不同 band 的最终分数永远落在各自独占的
    [floor, floor+_BAND_SCORE_RANGE) 区间——band 内怎么排都不会跨到别的
    band 头上，物理上不重叠，不依赖排序逻辑"尽量不越界"。
    """
    try:
        idx = _BAND_ORDER.index(band)
    except ValueError:
        idx = 0
    return idx * _BAND_SCORE_RANGE


def apply_band_quota(ranked_buckets: list, quota: dict) -> list:
    """按配额截取 DecayEngine.band_ranked() 的输出。

    quota 例：{"high": 4, "mid": 4, "low": 2}。band 内已经排好序，这里只是
    分段截断——"各配额 band 均有代表"这条不变量，只要该 band 池非空、
    配额>=1 就自然满足，不需要额外"至少一条"特判。

    返回顺序：high 段 → mid 段 → low 段（按 band 优先级摆放，不是把三个
    band 的分数交叉在一起排——band 之间的相对次序由"重要度"这个更高层的
    语义决定，不是由 band_floor 的数值大小顺便决定的实现细节）。
    """
    by_band: dict[str, list] = {"high": [], "mid": [], "low": []}
    for b in ranked_buckets:
        by_band.setdefault(b.get("_band", "low"), []).append(b)
    result: list = []
    for band in ("high", "mid", "low"):
        n = max(0, int(quota.get(band, 0)))
        result.extend(by_band.get(band, [])[:n])
    return result

# --- 周期自愈：每轮衰减最多补多少条缺失向量（防一次性打爆 embedding API）---
# 活跃桶落盘了但 embeddings.db 没它的向量 → breath 向量通道会漏掉它（permanent
# 尤其常见，见 #6）。剩余的下一轮继续补。
_BACKFILL_MAX_PER_CYCLE = 50

# --- Freshness bonus：bonus = 1 + e^(-hours/HALF_LIFE) ---
_FRESHNESS_HALF_LIFE_HRS = 36.0  # 36h 半衰：刚存 ×2.0，36h 后 ×1.5，72h 后 ≈×1.14
_FRESHNESS_AMPLITUDE = 1.0       # bonus 上限增量（0 → 无加成；1 → 最多 ×2）

# --- 短期 vs 长期权重分配（核心心理模型）---
# 短期：刚发生的事 time 占主导（"印象很新"）
# 长期：超过这个分界后 emotion 占主导（"刻骨铭心 vs 已经无所谓"）
_SHORT_TERM_DAYS = 3.0
_SHORT_TERM_TIME_RATIO = 0.7
_LONG_TERM_EMOTION_RATIO = 0.7

# --- Activation count 的次线性放大：访问越多越鲜活，但不线性 ---
_ACTIVATION_EXPONENT = 0.3

# --- Resolved/digested 衰减加速因子 ---
_FACTOR_RESOLVED_DIGESTED = 0.02  # 已处理 + 已写 feel → 加速淡化到背景
_FACTOR_RESOLVED_ONLY = 0.05      # 仅已处理（未写 feel）→ 中度淡化

# --- Urgency boost：高 arousal 且未处理 → 临时加重，避免被错误归档 ---
_AROUSAL_URGENCY_THRESHOLD = 0.7
_URGENCY_BOOST = 1.5

# --- Auto-resolve 触发条件 ---
_AUTO_RESOLVE_IMPORTANCE_MAX = 4   # 重要度 ≤ 4 才允许自动结案
_AUTO_RESOLVE_DAYS_MIN = 30        # 且 30 天未被激活
_AUTO_RESOLVE_FALLBACK_DAYS = 999  # 时间字段坏掉时，按"很久以前"对待，触发自动结案

# --- Arousal/importance 兜底 ---
_DEFAULT_AROUSAL = 0.3
_DEFAULT_IMPORTANCE = 5
_DEFAULT_DAYS_FALLBACK = 30  # calculate_score 时间字段坏 → 按 30 天处理（保守）

# --- 时间换算 ---
_SECONDS_PER_DAY = 86400
_SECONDS_PER_HOUR = 3600


def _days_since_active(meta: dict, fallback_days: float = _DEFAULT_DAYS_FALLBACK) -> float:
    """从 metadata 解析"距上次激活的天数"。

    抽出来的原因：原文件里 calculate_score / run_decay_cycle 各写了一遍
    同样的 "fromisoformat → 求差 → 兜底" 三段式，且兜底值还不一样
    （前者 30、后者 999）。统一成一个函数，由调用方传 fallback_days
    决定坏数据怎么处理：
      * calculate_score 用默认 30：保守地按"一个月没动"算分
      * run_decay_cycle 的 auto-resolve 路径传 999：让坏数据顺利触发结案

    边界（rule.md §⑨）：
      * meta 不是 dict / 字段缺失 / 字符串无法解析 → 返回 fallback_days
      * 永远返回 ≥ 0 的浮点数（防止时钟漂移产生负数）
    """
    if not isinstance(meta, dict):
        return fallback_days
    raw = meta.get("last_active") or meta.get("created") or ""
    try:
        last_active = parse_iso_datetime(raw)
        return max(0.0, (datetime.now() - last_active).total_seconds() / _SECONDS_PER_DAY)
    except (ValueError, TypeError):
        return float(fallback_days)


class DecayEngine:
    """
    Memory decay engine — periodically scans all dynamic buckets,
    calculates decay scores, auto-archives low-activity buckets
    to simulate natural forgetting.
    记忆衰减引擎 —— 定期扫描所有动态桶，
    计算衰减得分，将低活跃桶自动归档，模拟自然遗忘。
    """

    def __init__(self, config: dict, bucket_mgr):
        # --- Load decay parameters / 加载衰减参数 ---
        decay_cfg = config.get("decay", {})
        self.decay_lambda = decay_cfg.get("lambda", _DEFAULT_LAMBDA)
        self.threshold = decay_cfg.get("threshold", _DEFAULT_THRESHOLD)
        self.check_interval = decay_cfg.get("check_interval_hours", _DEFAULT_CHECK_INTERVAL_HRS)

        # --- Emotion weight params (continuous arousal coordinate) ---
        # --- 情感权重参数（基于连续 arousal 坐标）---
        emotion_cfg = decay_cfg.get("emotion_weights", {})
        self.emotion_base = emotion_cfg.get("base", _DEFAULT_EMOTION_BASE)
        self.arousal_boost = emotion_cfg.get("arousal_boost", _DEFAULT_AROUSAL_BOOST)

        # --- v3 Commit A: seed floor + activity bonus（config.seed.* 可调，见上方常量注释）---
        seed_cfg = config.get("seed", {})
        self.seed_floor = seed_cfg.get("floor", _DEFAULT_SEED_FLOOR)

        # --- v3 Commit C: retention 的"止衰宽限" + import cohort 中性化窗口 ---
        retention_cfg = config.get("retention", {})
        self.retention_grace_lambda_discount = retention_cfg.get(
            "grace_lambda_discount", _DEFAULT_RETENTION_GRACE_LAMBDA_DISCOUNT
        )
        # import_cohort_windows: [{"start": iso, "end": iso, "exempt": [iso, ...]}, ...]
        # 各家自己的批量导入窗口是实例数据，不进代码默认值——config.yaml 里配，
        # config.example.yaml 只给注释示例（K 家窗口）。
        self.import_cohort_windows = retention_cfg.get("import_cohort_windows", [])

        # --- v3 Commit C: activity_bonus 衰减曲线（config.activity_bonus.* 可调）---
        activity_bonus_cfg = config.get("activity_bonus", {})
        self.activity_bonus_half_life_hrs = activity_bonus_cfg.get(
            "half_life_hours", _DEFAULT_ACTIVITY_BONUS_HALF_LIFE_HRS
        )
        self.activity_bonus_scale = activity_bonus_cfg.get(
            "scale", _DEFAULT_ACTIVITY_BONUS_SCALE
        )

        # --- v3 Commit C: 浮现配额（config.band_quota.* 可调，施工单定案 4/4/2）---
        band_quota_cfg = config.get("band_quota", {})
        self.band_quota = {
            "high": int(band_quota_cfg.get("high", DEFAULT_BAND_QUOTA["high"])),
            "mid": int(band_quota_cfg.get("mid", DEFAULT_BAND_QUOTA["mid"])),
            "low": int(band_quota_cfg.get("low", DEFAULT_BAND_QUOTA["low"])),
        }

        self.bucket_mgr = bucket_mgr

        # --- Background task control / 后台任务控制 ---
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the decay engine is running in the background.
        衰减引擎是否正在后台运行。"""
        return self._running

    # ---------------------------------------------------------
    # Core: calculate decay score for a single bucket
    # 核心：计算单个桶的衰减得分
    #
    # Higher score = more vivid memory; below threshold → archive
    # 得分越高 = 记忆越鲜活，低于阈值则归档
    # Permanent buckets never decay / 固化桶永远不衰减
    # ---------------------------------------------------------
    # ---------------------------------------------------------
    # Freshness bonus: continuous exponential decay
    # 新鲜度加成：连续指数衰减
    # bonus = 1.0 + 1.0 × e^(-t/36), t in hours
    # t=0 → 2.0×, t≈25h(半衰) → 1.5×, t≈72h → ≈1.14×, t→∞ → 1.0×
    # ---------------------------------------------------------
    @staticmethod
    def _calc_time_weight(days_since: float) -> float:
        """
        Freshness bonus multiplier: 1.0 + e^(-t/36), t in hours.
        新鲜度加成乘数：刚存入×2.0，~36小时半衰，72小时后趋近×1.0。
        """
        hours = days_since * 24.0
        return 1.0 + _FRESHNESS_AMPLITUDE * math.exp(-hours / _FRESHNESS_HALF_LIFE_HRS)

    def calculate_score(self, metadata: dict) -> float:
        """
        Calculate current activity score for a memory bucket.
        计算一个记忆桶的当前活跃度得分。

        New model: short-term vs long-term weight separation.
        新模型：短期/长期权重分离。
        - Short-term (≤3 days): time_weight dominates, emotion amplifies
        - Long-term (>3 days): emotion_weight dominates, time decays to floor
        短期（≤3天）：时间权重主导，情感放大
        长期（>3天）：情感权重主导，时间衰减到底线
        """
        if not isinstance(metadata, dict):
            return 0.0

        # --- Pinned/protected buckets: never decay, importance locked to 10 ---
        if metadata.get("pinned") or metadata.get("protected"):
            return _SCORE_PINNED

        # --- Permanent buckets never decay ---
        if metadata.get("type") == "permanent":
            return _SCORE_PINNED

        # --- Feel buckets: never decay, fixed moderate score ---
        if metadata.get("type") == "feel":
            return _SCORE_FEEL

        # --- Plan / letter buckets: never decay (status-driven, not time-driven) ---
        # --- plan / letter 桶不衰减；plan 由 status 字段控制生命周期，letter 永久保存 ---
        if metadata.get("type") in ("plan", "letter"):
            return _SCORE_FEEL

        # --- Seed buckets: floor not frozen（放在 pinned/permanent/feel 之后——
        # 若一个桶同时是 pinned/permanent/feel 又是 seed，前面那几种"冻结"
        # 待遇优先，seed 的 floor 只管非冻结的普通 dynamic 桶）---
        if parse_bool(metadata.get("seed"), default=False):
            return self._calc_seed_score(metadata)

        try:
            importance = max(1, min(10, int(metadata.get("importance", _DEFAULT_IMPORTANCE))))
        except (TypeError, ValueError):
            importance = _DEFAULT_IMPORTANCE
        activation_count = max(1.0, float(metadata.get("activation_count") or 1))

        # --- Days since last activation ---
        days_since = _days_since_active(metadata, fallback_days=_DEFAULT_DAYS_FALLBACK)

        # --- Emotion weight ---
        try:
            arousal = max(0.0, min(1.0, float(metadata.get("arousal", _DEFAULT_AROUSAL))))
        except (ValueError, TypeError):
            arousal = _DEFAULT_AROUSAL
        emotion_weight = self.emotion_base + arousal * self.arousal_boost

        # --- Time weight ---
        time_weight = self._calc_time_weight(days_since)

        # --- Short-term vs Long-term weight separation ---
        # 短期（≤3天）：time_weight 占 70%，emotion 占 30%
        # 长期（>3天）：emotion 占 70%，time_weight 占 30%
        if days_since <= _SHORT_TERM_DAYS:
            # Short-term: time dominates, emotion amplifies
            combined_weight = (
                time_weight * _SHORT_TERM_TIME_RATIO
                + emotion_weight * (1.0 - _SHORT_TERM_TIME_RATIO)
            )
        else:
            # Long-term: emotion dominates, time provides baseline
            combined_weight = (
                emotion_weight * _LONG_TERM_EMOTION_RATIO
                + time_weight * (1.0 - _LONG_TERM_EMOTION_RATIO)
            )

        # --- Base score ---
        base_score = (
            importance
            * (activation_count ** _ACTIVATION_EXPONENT)
            * math.exp(-self.decay_lambda * days_since)
            * combined_weight
        )

        # --- Weight pool modifiers ---
        # resolved + digested (has feel) → 加速淡化
        # resolved only → 中度淡化
        resolved = metadata.get("resolved", False)
        digested = metadata.get("digested", False)  # set when feel is written for this memory
        if resolved and digested:
            resolved_factor = _FACTOR_RESOLVED_DIGESTED
        elif resolved:
            resolved_factor = _FACTOR_RESOLVED_ONLY
        else:
            resolved_factor = 1.0
        urgency_boost = (
            _URGENCY_BOOST
            if (arousal > _AROUSAL_URGENCY_THRESHOLD and not resolved)
            else 1.0
        )

        return round(base_score * resolved_factor * urgency_boost, 4)

    def _calc_seed_score(self, metadata: dict) -> float:
        """
        seed 桶打分：floor + activity_bonus。不受 age_decay、不受负反馈
        （resolved_factor/urgency_boost 均不适用——这两个只在上面普通路径
        里算）。

        v3 Commit C：Commit A 里那条"按访问次数凑活动感"的过渡代理路径已
        整体移除（原实现见 git 历史 30a4bc1..d1d3aa7 一带 v3 Commit A 提交），
        现在直接调用正式的 activity_bonus()：last_meaningful_at 从未设置
        （从未有过强信号）时返回 0，对应验收 4a"从未检索"→ 停在 floor；
        有强信号时按新鲜度曲线给分，同样受 importance 递减约束（宪法
        推论一），语义与过渡实现一致，只是数据来源换成了正式的
        last_meaningful_at 强信号时间戳。
        """
        return round(self.seed_floor + self.activity_bonus(metadata, for_seed=True), 4)

    def _encoded_age_days(self, metadata: dict, now: datetime) -> float:
        """v3 Commit C：retention() 的年龄输入。年龄基准是 created（"神圣不可
        改写"），不是 last_active——那是给 activity_bonus 算新鲜度用的。

        import cohort 中性化：metadata["created"] 落在
        self.import_cohort_windows 任一窗口内（且不在该窗口的 exempt 名单
        里）→ 年龄改按"窗口结束时刻"算，不按各自(可能是聚集导入产物、不
        可信)的 created 算——同一批导入的桶不因为不可考的真实创建时间产生
        档内差异；窗口外正常走 created。之后仍随时间正常衰减，这不是
        永久覆盖，只是给这批桶一个统一、不编造的起点。
        """
        created_raw = str(metadata.get("created") or "")
        try:
            created = parse_iso_datetime(created_raw)
        except (ValueError, TypeError):
            return float(_DEFAULT_DAYS_FALLBACK)

        base = created
        for window in self.import_cohort_windows:
            if created_raw in (window.get("exempt") or []):
                continue
            try:
                start = parse_iso_datetime(window.get("start"))
                end = parse_iso_datetime(window.get("end"))
            except (ValueError, TypeError):
                continue
            if start <= created <= end:
                base = end
                break

        return max(0.0, (now - base).total_seconds() / _SECONDS_PER_DAY)

    def retention(self, metadata: dict, now: datetime | None = None) -> float:
        """v3 Commit C：排序结构第一根轴。可降，承载 age_decay/负反馈/遗忘
        语义。只服务"排序面"（band 配额浮现 / breath 默认浮现排序 / 检索
        排序）——archiving 阈值判定继续用 calculate_score()，本函数不参与
        （见文件顶部"排序结构"说明块，F 2026-08-09 架构裁定）。

        seed：retention = self.seed_floor（不参与年龄/负反馈——"不受
        age_decay、不受一切负反馈"是 Commit A 定的规矩，原样沿用）。
        pinned/protected/permanent/feel/plan/letter/anchor：这些桶各自有
        自己的展示通道，不进 band 配额候选池，这里仍给出对应的锁分值，
        避免误用本函数时把年龄偏见带进来。

        负反馈两段式（设计定稿已拍板的数字，适用范围 importance<6 且非
        pinned/protected/permanent/anchor/seed）：连续 5 次未使用 → 取消
        "止衰宽限"（衰减率打折的宽限期结束，回到全额衰减率）；连续 10 次
        → 额外 1/8 衰减，直接乘在结果上。
        """
        if not isinstance(metadata, dict):
            return 0.0
        now = now or datetime.now()

        if parse_bool(metadata.get("seed"), default=False):
            return self.seed_floor
        if metadata.get("pinned") or metadata.get("protected") or metadata.get("type") == "permanent":
            return _SCORE_PINNED
        if metadata.get("type") in ("feel", "plan", "letter"):
            return _SCORE_FEEL
        if parse_bool(metadata.get("anchor"), default=False):
            return _SCORE_FEEL

        try:
            importance = max(1, min(10, int(metadata.get("importance", _DEFAULT_IMPORTANCE))))
        except (TypeError, ValueError):
            importance = _DEFAULT_IMPORTANCE

        age_days = self._encoded_age_days(metadata, now)

        streak = int(metadata.get("semantic_unused_streak") or 0)
        applies_negative_feedback = importance < _NEG_FEEDBACK_IMPORTANCE_MAX
        effective_lambda = self.decay_lambda
        if applies_negative_feedback and streak < _NEG_FEEDBACK_STREAK_GRACE_ENDS:
            effective_lambda *= self.retention_grace_lambda_discount

        value = importance * math.exp(-effective_lambda * age_days)

        if applies_negative_feedback and streak >= _NEG_FEEDBACK_STREAK_EXTRA_DECAY:
            value *= (1.0 - _NEG_FEEDBACK_EXTRA_DECAY_FRACTION)

        return round(value, 4)

    def activity_bonus(
        self, metadata: dict, now: datetime | None = None, *, for_seed: bool = False
    ) -> float:
        """v3 Commit C：排序结构第二根轴。≥0，非负贡献，不承担惩罚；数值
        随 last_meaningful_at 新鲜度衰减回 0；强信号不构成永久累积优势——
        不是 monotonic counter，纯粹是"上次强信号多久以前"的衰减函数，
        时间一过就回落，不会因为强信号发生过越多次就越高。

        last_meaningful_at 从未设置（从未有过 hold 追加 / meaning_append /
        citation_credit 强信号，见 bucket_manager.record_strong_signal）
        → 0.0。

        Commit E 修正：importance 递减只对 for_seed=True（_calc_seed_score
        的种子路径）生效——那是设计定稿"种子条款"里明写的"activity 仍可
        小幅正向增益（受高 importance 递减）"，专属种子。通用排序轴（
        band_ranked() 走的非 seed 默认路径，for_seed=False）不递减：设计
        定稿"不对称原则"明写"加分类规则看 weight（捞沉底）……importance
        10 / weight 低位的非 seed 桶必须能吃到反向匹配与全额 activity_
        bonus"——两条要求主体不同，此前实现把种子专属的递减曲线套用到了
        全部桶（含非 seed）上，是 Commit C 遗留的不对称原则违规，回归 e
        验收时发现并在此修正。
        """
        if not isinstance(metadata, dict):
            return 0.0
        now = now or datetime.now()
        raw = metadata.get("last_meaningful_at")
        if not raw:
            return 0.0
        try:
            last = parse_iso_datetime(raw)
        except (ValueError, TypeError):
            return 0.0

        if for_seed:
            try:
                importance = max(1, min(10, int(metadata.get("importance", _DEFAULT_IMPORTANCE))))
            except (TypeError, ValueError):
                importance = _DEFAULT_IMPORTANCE
            # 宪法推论一 × 种子条款：种子的 importance=10 时递减到 0。
            diminish = max(0.0, (10 - importance) / 9.0)
        else:
            # 不对称原则：非 seed 桶的 activity_bonus 不看 importance，
            # 一律全额——importance 只用来定 band，不用来打折加分。
            diminish = 1.0

        hours = max(0.0, (now - last).total_seconds() / _SECONDS_PER_HOUR)
        if self.activity_bonus_half_life_hrs <= 0:
            decay = 0.0
        else:
            decay = math.exp(-hours / self.activity_bonus_half_life_hrs)
        return round(self.activity_bonus_scale * diminish * decay, 4)

    def band_ranked(self, buckets: list, now: datetime | None = None) -> list:
        """v3 Commit C：band 内先归一化（min-max）再排序，band 之间用不
        重叠的分数区间（band_floor）隔开——检索排序最终分不得越band。

        buckets: [{"id":..., "metadata": {...}}, ...]（跟 bucket_mgr 各处
        返回的形状一致）。就地给每个 dict 加 "_rank_score" / "_band" 两个
        键，按 _rank_score 降序返回同一批对象（不拷贝，调用方本来就是拿
        list_all() 现造的临时列表，原地加字段没有副作用风险）。

        tie-breaker：bucket_id 字典序——"仅稳定排序不入年龄"，不用任何跟
        created/last_active 相关的字段打破平局，避免把年龄偏见从后门带回来。
        """
        now = now or datetime.now()
        by_band: dict[str, list] = {"high": [], "mid": [], "low": []}
        for b in buckets:
            band = band_of((b.get("metadata") or {}).get("importance"))
            by_band[band].append(b)

        ranked: list = []
        for band, group in by_band.items():
            if not group:
                continue
            raw = [
                self.retention(b["metadata"], now) + self.activity_bonus(b["metadata"], now)
                for b in group
            ]
            lo, hi = min(raw), max(raw)
            span = (hi - lo) or 1.0
            floor = band_floor(band)
            for b, value in zip(group, raw):
                normalized = (value - lo) / span  # [0, 1]
                b["_rank_score"] = floor + normalized * (_BAND_SCORE_RANGE - 1.0)
                b["_band"] = band
            ranked.extend(group)

        ranked.sort(key=lambda b: (b["_rank_score"], str(b.get("id") or "")), reverse=True)
        return ranked

    # ---------------------------------------------------------
    # Execute one decay cycle
    # 执行一轮衰减周期
    # Scan all dynamic buckets → score → archive those below threshold
    # 扫描所有动态桶 → 算分 → 低于阈值的归档
    # ---------------------------------------------------------
    async def run_decay_cycle(self) -> dict:
        """
        Execute one decay cycle: iterate dynamic buckets, archive those
        scoring below threshold.
        执行一轮衰减：遍历动态桶，归档得分低于阈值的桶。

        Returns stats: {"checked": N, "archived": N, "lowest_score": X}
        """
        try:
            buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for decay / 衰减周期列桶失败: {e}")
            return {"checked": 0, "archived": 0, "lowest_score": 0, "error": str(e)}

        checked = 0
        archived = 0
        auto_resolved = 0
        lowest_score = float("inf")

        demoted_orphans = 0
        for bucket in buckets:
            meta = bucket.get("metadata", {})

            # Skip anchor / seed / permanent / pinned / protected / feel / i buckets
            # 跳过 anchor、seed、固化桶、钉选/保护桶、feel 桶和 i（自我认知）桶
            # i 桶承诺永不衰减（tools/i/core.py 注释）——必须在此显式排除
            # anchor 是 dynamic 桶上的 bool 标记，不锁高 importance，必须显式排除
            # v3 Commit A：seed 同样是 dynamic 桶上的 bool 标记，不受一切负
            # 反馈（含这里的自动归档 *和* 下面的自动结案），必须显式排除。
            if (
                meta.get("type") in ("permanent", "feel", "i")
                or meta.get("pinned")
                or meta.get("protected")
                or parse_bool(meta.get("anchor"), default=False)
                or parse_bool(meta.get("seed"), default=False)
            ):
                continue

            checked += 1

            # --- Auto-resolve: imp≤4 + >30 days old + not resolved → auto resolve ---
            # --- 自动结案：重要度≤4 + 超过30天 + 未解决 → 自动 resolve ---
            if not meta.get("resolved", False):
                imp = int(meta.get("importance") or _DEFAULT_IMPORTANCE)
                # auto-resolve 路径上时间字段坏 → 按 999 天处理（加速会被结案）
                days_since = _days_since_active(
                    meta, fallback_days=_AUTO_RESOLVE_FALLBACK_DAYS
                )
                if imp <= _AUTO_RESOLVE_IMPORTANCE_MAX and days_since > _AUTO_RESOLVE_DAYS_MIN:
                    try:
                        await self.bucket_mgr.update(bucket["id"], resolved=True)
                        meta["resolved"] = True  # refresh local meta so resolved_factor applies this cycle
                        auto_resolved += 1
                        logger.info(
                            f"Auto-resolved / 自动结案: "
                            f"{meta.get('name', bucket['id'])} "
                            f"(imp={imp}, days={days_since:.0f})"
                        )
                    except Exception as e:
                        logger.warning(f"Auto-resolve failed / 自动结案失败: {e}")

            try:
                # TODO(归档链路迁移到 retention，独立后续项，非本期范围)：
                # v3 Commit C 架构裁定（F，2026-08-09）：归档阈值判定继续用
                # calculate_score()，暂不切到 retention()——归档是有数据
                # 后果的动作（低于阈值就搬 archive），公式一夜切换可能造成
                # 批量误归档。等要切的时候，先跑 dry-run 对比新旧公式在
                # 全量数据上的归档判定差异出报告，人工过一遍再切，不能
                # 直接改这一行了事。
                score = self.calculate_score(meta)
            except Exception as e:
                logger.warning(
                    f"Score calculation failed for {bucket.get('id', '?')} / "
                    f"计算得分失败: {e}"
                )
                continue

            lowest_score = min(lowest_score, score)

            # --- Below threshold → archive (simulate forgetting) ---
            # --- 低于阈值 → 归档（模拟遗忘）---
            if score < self.threshold:
                try:
                    success = await self.bucket_mgr.archive(bucket["id"])
                    if success:
                        archived += 1
                        logger.info(
                            f"Decay archived / 衰减归档: "
                            f"{meta.get('name', bucket['id'])} "
                            f"(score={score:.4f}, threshold={self.threshold})"
                        )
                except Exception as e:
                    logger.warning(
                        f"Archive failed for {bucket.get('id', '?')} / "
                        f"归档失败: {e}"
                    )

        # --- Self-heal: 补齐缺失向量（周期性，详见 _self_heal_embeddings）---
        backfilled_embeddings = await self._self_heal_embeddings(buckets)

        result = {
            "checked": checked,
            "archived": archived,
            "auto_resolved": auto_resolved,
            "demoted_orphans": demoted_orphans,
            "backfilled_embeddings": backfilled_embeddings,
            "lowest_score": lowest_score if checked > 0 else 0,
        }
        logger.info(f"Decay cycle complete / 衰减周期完成: {result}")
        return result

    async def _self_heal_embeddings(self, buckets: list) -> int:
        """周期自愈：给「落盘了但 embeddings.db 里没向量」的活跃桶补向量。

        背景（#6）：permanent 桶常因批量导入 / dashboard 钉选而漏建向量，
        breath 的向量通道就检索不到它们，表现为「只读得到 dynamic」。衰减循环
        每轮顺手补齐，无需人工跑 backfill_embeddings.py。

        边界：embedding 未启用 → 跳过；每轮最多补 _BACKFILL_MAX_PER_CYCLE 条
        （防打爆 API），剩余下一轮继续；单条失败仅 warning（rule.md §1.5 允许降级）。
        只处理活跃桶（buckets 不含 archive），不在此删孤儿向量（删除走专用脚本，
        避免把 archive 桶的有效向量误判为孤儿）。"""
        outbox = getattr(self.bucket_mgr, "embedding_outbox", None)
        if outbox is not None and getattr(outbox, "running", False):
            try:
                queued = await outbox.reconcile(
                    buckets=buckets,
                    include_archive=False,
                )
                if queued:
                    logger.info(
                        "Decay self-heal queued / 衰减自愈已加入向量队列: %s 条",
                        queued,
                    )
                return queued
            except Exception as e:
                logger.warning(f"self-heal embeddings: 投递后台队列失败: {e}")
                return 0

        ee = getattr(self.bucket_mgr, "embedding_engine", None)
        if not ee or not getattr(ee, "enabled", False):
            return 0
        try:
            index_ids = set(ee.list_all_ids())
        except Exception as e:
            logger.warning(f"self-heal embeddings: 读取向量索引失败: {e}")
            return 0
        missing = [b for b in buckets if b["id"] not in index_ids and (b.get("content") or "").strip()]
        if not missing:
            return 0
        healed = 0
        for b in missing[:_BACKFILL_MAX_PER_CYCLE]:
            try:
                if await ee.generate_and_store(b["id"], b["content"]):
                    healed += 1
            except Exception as e:
                logger.warning(f"self-heal embeddings: 补 {b['id']} 失败: {e}")
        if healed:
            remaining = len(missing) - healed
            logger.info(
                f"Decay self-heal / 自愈补向量: {healed} 条"
                + (f"（本轮上限 {_BACKFILL_MAX_PER_CYCLE}，剩 {remaining} 下轮继续）"
                   if remaining > 0 else "")
            )
        return healed

    # ---------------------------------------------------------
    # Background decay task management
    # 后台衰减任务管理
    # ---------------------------------------------------------
    async def ensure_started(self) -> None:
        """
        Ensure the decay engine is started (lazy init on first call).
        确保衰减引擎已启动（懒加载，首次调用时启动）。
        """
        if not self._running:
            await self.start()

    async def start(self) -> None:
        """Start the background decay loop.
        启动后台衰减循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            f"Decay engine started, interval: {self.check_interval}h / "
            f"衰减引擎已启动，检查间隔: {self.check_interval} 小时"
        )

    async def stop(self) -> None:
        """Stop the background decay loop.
        停止后台衰减循环。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Decay engine stopped / 衰减引擎已停止")

    async def _background_loop(self) -> None:
        """Background loop: run decay → sleep → repeat.
        后台循环体：执行衰减 → 睡眠 → 重复。"""
        while self._running:
            try:
                await self.run_decay_cycle()
            except Exception as e:
                logger.error(f"Decay cycle error / 衰减周期出错: {e}")
            # --- Wait for next cycle / 等待下一个周期 ---
            try:
                await asyncio.sleep(self.check_interval * _SECONDS_PER_HOUR)
            except asyncio.CancelledError:
                break
