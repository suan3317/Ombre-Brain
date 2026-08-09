#!/usr/bin/env python3
"""
tools/replay_analysis.py — 记忆动力学二期 · 回放调参分析脚本
========================================
只读，分析用，不属于施工。

本脚本绝对只读：只用 frontmatter.load() 读桶文件，不调用任何 .dump()/写文件
接口，不 os.utime()，不改一个字节；输出仅打印到 stdout。跑坏了也只是打印错误，
不会碰坏任何生产数据——这是设计定稿（工单/设计定稿_记忆动力学二期_20260808.md）
施工前的回放依据，由 Silvia 在 K/F/G 三家终端各跑一次，报告贴回，供三家确认
band 宽度、配额比例、seed floor 等数值参数，不代替人工判断，也不做任何自动化
处理。

范围口径（贯穿全脚本，除非小节里另有说明）：
- 只看 buckets_dir 下 permanent/ + dynamic/ 两个子目录，不含 archive/——已归档
  桶不在当前浮现/继承候选池里，不参与"现在该怎么调参"的讨论。
- "现行 weight" = decay_engine.calculate_score(metadata)，即 Dashboard 上
  "活跃度分 / Activity score" 那一列（旧称"权重分 / Weight"）——直接复用生产
  代码里的同一份公式实例算分，不重新发明一套，避免跟真实衰减行为脱节。
- "浮现候选池"（b/c 的 weight 离散度、d 的配额模拟）限定为 type=dynamic 且非
  pinned/protected/非 anchor 的桶：pinned/protected 走"核心准则"通道、
  anchor 是坐标系永不主动浮现（tools/breath/surface.py 的防御性排除）、
  permanent/feel/plan/letter 在 calculate_score() 里直接返回固定分——这些
  桶的"weight"不随衰减变化，混进配额模拟的排序里会把统计弄脏。
  e（seed 候选清单）例外：那一节看的是"哪些桶户主可能想圈成种子"，范围是
  permanent+dynamic 全量，不做候选池过滤。

用法：
  python3 tools/replay_analysis.py
  python3 tools/replay_analysis.py --import-window "2026-07-06T02:08:37" "2026-07-06T02:33:32"
  python3 tools/replay_analysis.py --quota 6,3,1 --vault-dir /path/to/buckets
========================================
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import frontmatter as fm  # noqa: E402

from decay_engine import DecayEngine  # noqa: E402
from utils import load_config, parse_bool, parse_iso_datetime  # noqa: E402


_ACTIVE_SUBDIRS = ("permanent", "dynamic")

# band 边界：与设计定稿口径一致——seed 候选阈值 importance>=8（高档下限）、
# 负反馈适用范围 importance<6（低档上限），中档即两者之间的 6-7。
BAND_HIGH_MIN = 8
BAND_MID_MIN = 6
BAND_MID_MAX = 7

# "沉底桶"（§2.4 不对称规则的受益者）判定：importance>=8，且现行 weight 落在
# 候选池当次得分中位数以下。数值口径未定稿，用中位数是脚本的默认取法，实际
# 阈值分数会打印出来供人工核对/换算成别的百分位。
SINK_IMPORTANCE_MIN = 8

# "当月新桶"：脚本运行时刻往前 30 天内 created 的桶（K 家关心指标专用）。
RECENT_DAYS = 30

# 配额模拟内置对照组（"两三种备选"）；CLI 传的 --quota 会作为第一组，
# 与默认提案不同时追加为"CLI 指定"。
_BUILTIN_QUOTA_PRESETS = [
    ("提案默认 5/3/2", (5, 3, 2)),
    ("中档+1让高 4/4/2", (4, 4, 2)),
    ("中档+2让高低 3/5/2", (3, 5, 2)),
]


def _iter_bucket_files(vault_dir: str, subdirs=_ACTIVE_SUBDIRS):
    for sub in subdirs:
        root = os.path.join(vault_dir, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.endswith(".md"):
                    yield os.path.join(dirpath, fn)


def load_buckets(vault_dir: str) -> list[dict]:
    """只读扫描 permanent/ + dynamic/，返回 metadata 字典列表（附 id/path）。

    解析失败的文件跳过并计入 parse_errors（附在返回列表末尾以外，由调用方
    自行用 --vault-dir 核对），不让个别坏文件中断整次回放。
    """
    buckets = []
    errors = []
    for path in _iter_bucket_files(vault_dir):
        try:
            post = fm.load(path)
        except Exception as e:  # noqa: BLE001 - 只读扫描，坏文件不该中断整次回放
            errors.append({"path": path, "error": f"{type(e).__name__}: {e}"})
            continue
        meta = dict(post.metadata)
        meta.setdefault("id", os.path.splitext(os.path.basename(path))[0])
        meta["_path"] = path
        buckets.append(meta)
    return buckets, errors


def _is_pool_candidate(meta: dict) -> bool:
    """是否属于"正常浮现候选池"（见文件头范围口径）。"""
    if meta.get("type", "dynamic") != "dynamic":
        return False
    if meta.get("pinned") or meta.get("protected"):
        return False
    if parse_bool(meta.get("anchor"), default=False):
        return False
    return True


def _safe_importance(meta: dict) -> int:
    try:
        return max(1, min(10, int(meta.get("importance", 5))))
    except (TypeError, ValueError):
        return 5


def _band_of(importance: int) -> str:
    if importance >= BAND_HIGH_MIN:
        return "high"
    if importance >= BAND_MID_MIN:
        return "mid"
    return "low"


def _parse_dt(raw) -> datetime | None:
    try:
        return parse_iso_datetime(raw)
    except (ValueError, TypeError):
        return None


def _is_recent(meta: dict, now: datetime, days: int = RECENT_DAYS) -> bool:
    dt = _parse_dt(meta.get("created"))
    if dt is None:
        return False
    return (now - dt).total_seconds() <= days * 86400


def score_buckets(buckets: list[dict], engine: DecayEngine) -> dict[str, float]:
    """id -> calculate_score(metadata)，只算一次供各小节复用。"""
    scores = {}
    for meta in buckets:
        scores[meta["id"]] = engine.calculate_score(meta)
    return scores


# ============================================================
# a. importance 分布直方图
# ============================================================
def section_a_importance_histogram(buckets: list[dict]) -> dict:
    pool = [b for b in buckets if _is_pool_candidate(b)]
    hist = {i: 0 for i in range(1, 11)}
    for meta in pool:
        hist[_safe_importance(meta)] += 1
    by_type = {}
    for meta in buckets:
        t = meta.get("type", "dynamic")
        by_type[t] = by_type.get(t, 0) + 1
    return {"pool_size": len(pool), "histogram": hist, "by_type_all": by_type}


# ============================================================
# b. weight 分布 vs importance 散点摘要 + 沉底桶数量
# ============================================================
def section_b_weight_vs_importance(buckets: list[dict], scores: dict[str, float]) -> dict:
    pool = [b for b in buckets if _is_pool_candidate(b)]
    per_importance = {i: [] for i in range(1, 11)}
    for meta in pool:
        per_importance[_safe_importance(meta)].append(scores[meta["id"]])

    scatter = {}
    for imp, vals in per_importance.items():
        if not vals:
            continue
        scatter[imp] = {
            "count": len(vals),
            "min": min(vals),
            "median": statistics.median(vals),
            "max": max(vals),
        }

    all_scores = [scores[b["id"]] for b in pool]
    if all_scores:
        median_score = statistics.median(all_scores)
    else:
        median_score = 0.0

    sinkers = [
        b for b in pool
        if _safe_importance(b) >= SINK_IMPORTANCE_MIN and scores[b["id"]] <= median_score
    ]
    return {
        "pool_size": len(pool),
        "scatter_by_importance": scatter,
        "median_score": median_score,
        "sink_importance_min": SINK_IMPORTANCE_MIN,
        "sinkers_count": len(sinkers),
        "sinkers_sample_ids": [b["id"] for b in sinkers[:20]],
    }


# ============================================================
# c. cohort 模拟（导入窗口内桶数与现行权重离散度）
# ============================================================
def section_c_cohort(buckets: list[dict], scores: dict[str, float], window) -> dict | None:
    if window is None:
        return None
    start, end = window
    in_window = []
    for meta in buckets:
        dt = _parse_dt(meta.get("created"))
        if dt is not None and start <= dt <= end:
            in_window.append(meta)

    by_type = {}
    for meta in in_window:
        t = meta.get("type", "dynamic")
        by_type[t] = by_type.get(t, 0) + 1

    scoreable = [b for b in in_window if _is_pool_candidate(b)]
    vals = [scores[b["id"]] for b in scoreable]
    dispersion = None
    if vals:
        dispersion = {
            "count": len(vals),
            "min": min(vals),
            "median": statistics.median(vals),
            "max": max(vals),
            "stdev": statistics.stdev(vals) if len(vals) > 1 else 0.0,
        }
    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "total_in_window": len(in_window),
        "by_type": by_type,
        "excluded_special_count": len(in_window) - len(scoreable),
        "weight_dispersion": dispersion,
    }


# ============================================================
# d. 浮现配额模拟
# ============================================================
def _simulate_one_quota(pool: list[dict], scores: dict[str, float], quota) -> dict:
    bands = {"high": [], "mid": [], "low": []}
    for meta in pool:
        bands[_band_of(_safe_importance(meta))].append(meta)

    result = {}
    for band_name, n in zip(("high", "mid", "low"), quota):
        ranked = sorted(bands[band_name], key=lambda b: scores[b["id"]], reverse=True)
        selected = ranked[:n]
        result[band_name] = {
            "pool_size": len(ranked),
            "quota": n,
            "selected_ids": [b["id"] for b in selected],
        }
    return result


def section_d_quota_simulation(
    buckets: list[dict], scores: dict[str, float], primary_quota, now: datetime
) -> dict:
    pool = [b for b in buckets if _is_pool_candidate(b)]

    presets = list(_BUILTIN_QUOTA_PRESETS)
    if primary_quota not in {q for _, q in presets}:
        presets = [("CLI 指定 %d/%d/%d" % primary_quota, primary_quota)] + presets

    mid_pool = [b for b in pool if _band_of(_safe_importance(b)) == "mid"]
    mid_recent = [b for b in mid_pool if _is_recent(b, now)]

    scenarios = {}
    for name, quota in presets:
        sim = _simulate_one_quota(pool, scores, quota)
        mid_selected_ids = set(sim["mid"]["selected_ids"])
        recent_selected = [b for b in mid_recent if b["id"] in mid_selected_ids]
        recent_excluded = [b for b in mid_recent if b["id"] not in mid_selected_ids]
        scenarios[name] = {
            "quota": quota,
            "bands": sim,
            "k_mid_metric": {
                "mid_pool_size": len(mid_pool),
                "mid_recent_new_count": len(mid_recent),
                "mid_recent_new_selected_count": len(recent_selected),
                "mid_recent_new_selected_ids": [b["id"] for b in recent_selected],
                "mid_recent_new_excluded_ids": [b["id"] for b in recent_excluded],
            },
        }
    return {"pool_size": len(pool), "recent_days": RECENT_DAYS, "scenarios": scenarios}


# ============================================================
# e. seed 候选辅助清单（importance>=8 或 pinned；非自动资格）
# ============================================================
def section_e_seed_candidates(buckets: list[dict]) -> dict:
    candidates = [
        b for b in buckets
        if _safe_importance(b) >= 8 or b.get("pinned")
    ]
    rows = [
        {
            "id": b["id"],
            "name": b.get("name", ""),
            "importance": _safe_importance(b),
            "pinned": bool(b.get("pinned")),
            "type": b.get("type", "dynamic"),
        }
        for b in candidates
    ]
    return {"count": len(rows), "candidates": rows}


# ============================================================
# f. activation_count / last_active 分布现状
# ============================================================
def section_f_activation_and_last_active(buckets: list[dict], now: datetime) -> dict:
    activation_counts = []
    missing_activation = 0
    last_active_ages_days = []
    missing_last_active = 0

    for meta in buckets:
        raw_ac = meta.get("activation_count")
        if raw_ac is None:
            missing_activation += 1
        else:
            try:
                activation_counts.append(float(raw_ac))
            except (TypeError, ValueError):
                missing_activation += 1

        raw_la = meta.get("last_active")
        if not raw_la:
            missing_last_active += 1
        else:
            dt = _parse_dt(raw_la)
            if dt is None:
                missing_last_active += 1
            else:
                last_active_ages_days.append((now - dt).total_seconds() / 86400.0)

    def _stats(vals):
        if not vals:
            return None
        return {
            "count": len(vals),
            "min": min(vals),
            "median": statistics.median(vals),
            "max": max(vals),
        }

    return {
        "total_buckets": len(buckets),
        "activation_count": {
            "missing": missing_activation,
            "stats": _stats(activation_counts),
        },
        "last_active_age_days": {
            "missing": missing_last_active,
            "stats": _stats(last_active_ages_days),
        },
    }


# ============================================================
# CLI / 报告渲染
# ============================================================
def _parse_quota(raw: str):
    parts = raw.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("--quota 必须是三个逗号分隔的整数，如 5,3,2")
    try:
        a, b, c = (int(p.strip()) for p in parts)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"--quota 解析失败: {e}") from e
    if a < 0 or b < 0 or c < 0:
        raise argparse.ArgumentTypeError("--quota 三个值必须是非负整数")
    return (a, b, c)


def _parse_window_arg(raw: str) -> datetime:
    try:
        return parse_iso_datetime(raw)
    except (ValueError, TypeError) as e:
        raise argparse.ArgumentTypeError(f"无法解析时间 {raw!r}: {e}") from e


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "只读回放分析：记忆动力学二期数值参数（band 宽度/配额/seed）的"
            "历史数据依据。不修改任何桶数据。"
        )
    )
    parser.add_argument("--vault-dir", default=None, help="覆盖 config.yaml 里的 buckets_dir")
    parser.add_argument(
        "--import-window",
        nargs=2,
        metavar=("START", "END"),
        default=None,
        help="导入窗口起止时间（ISO 8601），如 2026-07-06T02:08:37 2026-07-06T02:33:32",
    )
    parser.add_argument(
        "--quota",
        type=_parse_quota,
        default=(5, 3, 2),
        help="高/中/低三档浮现配额，逗号分隔，默认 5,3,2",
    )
    return parser


def _fmt_scatter(scatter: dict) -> str:
    lines = []
    for imp in range(1, 11):
        row = scatter.get(imp)
        if not row:
            continue
        lines.append(
            f"  importance={imp:>2}: n={row['count']:>4}  "
            f"min={row['min']:>8.4f}  median={row['median']:>8.4f}  max={row['max']:>8.4f}"
        )
    return "\n".join(lines) if lines else "  (候选池为空)"


def render_report(result: dict) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("记忆动力学二期 · 回放分析报告（只读，不代替人工判断）")
    lines.append(f"vault_dir = {result['vault_dir']}")
    lines.append(f"扫描时刻 = {result['now'].isoformat()}")
    if result["parse_errors"]:
        lines.append(f"解析失败 {len(result['parse_errors'])} 个文件（未纳入统计）：")
        for err in result["parse_errors"][:10]:
            lines.append(f"  - {err['path']}: {err['error']}")
    lines.append("=" * 70)

    a = result["a"]
    lines.append("\n--- a. importance 分布直方图（浮现候选池，n=%d） ---" % a["pool_size"])
    for i in range(1, 11):
        lines.append(f"  importance={i:>2}: {'#' * a['histogram'][i]} ({a['histogram'][i]})")
    lines.append("  全量按 type 分布（含 permanent/pinned/anchor 等，供背景参考）：")
    for t, n in sorted(a["by_type_all"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    {t}: {n}")

    b = result["b"]
    lines.append(f"\n--- b. weight(现行 score) vs importance 散点摘要（n={b['pool_size']}） ---")
    lines.append(_fmt_scatter(b["scatter_by_importance"]))
    lines.append(
        f"  全池 median score = {b['median_score']:.4f}；"
        f"importance>={b['sink_importance_min']} 且 score<=中位数的\"沉底桶\" "
        f"= {b['sinkers_count']} 条"
    )
    if b["sinkers_sample_ids"]:
        lines.append(f"  样本 id（至多 20 条）：{', '.join(b['sinkers_sample_ids'])}")

    c = result["c"]
    lines.append("\n--- c. cohort 模拟（导入窗口） ---")
    if c is None:
        lines.append("  未提供 --import-window，跳过。")
    else:
        lines.append(f"  窗口 [{c['window_start']}, {c['window_end']}]")
        lines.append(f"  窗口内桶数 = {c['total_in_window']}，按 type 分布 = {c['by_type']}")
        lines.append(f"  其中 {c['excluded_special_count']} 条不计入 weight 离散度（非浮现候选池）")
        if c["weight_dispersion"]:
            d = c["weight_dispersion"]
            lines.append(
                f"  候选池内 weight 离散度：n={d['count']} min={d['min']:.4f} "
                f"median={d['median']:.4f} max={d['max']:.4f} stdev={d['stdev']:.4f}"
            )
        else:
            lines.append("  候选池内窗口桶数为 0，无离散度可算。")

    d_ = result["d"]
    lines.append(f"\n--- d. 浮现配额模拟（候选池 n={d_['pool_size']}，当月新=近 {d_['recent_days']} 天） ---")
    for name, scenario in d_["scenarios"].items():
        lines.append(f"  [{name}]  quota(高/中/低)={scenario['quota']}")
        for band in ("high", "mid", "low"):
            bd = scenario["bands"][band]
            lines.append(
                f"    {band:>4}: pool={bd['pool_size']:>4} quota={bd['quota']:>2} "
                f"选中={len(bd['selected_ids'])}"
            )
        km = scenario["k_mid_metric"]
        lines.append(
            "    K 家指标 · 中档(importance 6-7)当月新桶入选情况："
            f"当月新={km['mid_recent_new_count']}/中档总={km['mid_pool_size']}，"
            f"入选={km['mid_recent_new_selected_count']}"
        )
        if km["mid_recent_new_excluded_ids"]:
            lines.append(f"      未入选样本: {', '.join(km['mid_recent_new_excluded_ids'][:10])}")

    e = result["e"]
    lines.append(f"\n--- e. seed 候选辅助清单（importance>=8 或 pinned，n={e['count']}） ---")
    lines.append("  仅供户主圈 seed 参考，非自动资格——importance/pinned 不等于 seed。")
    for row in e["candidates"][:50]:
        lines.append(
            f"  [{row['id']}] {row['name']} | importance={row['importance']} "
            f"pinned={row['pinned']} type={row['type']}"
        )
    if e["count"] > 50:
        lines.append(f"  ...其余 {e['count'] - 50} 条未展示，用 --json 或直接读账本核对全量。")

    f_ = result["f"]
    lines.append(f"\n--- f. activation_count / last_active 分布现状（全量 n={f_['total_buckets']}） ---")
    ac = f_["activation_count"]
    lines.append(f"  activation_count 缺失 = {ac['missing']}")
    if ac["stats"]:
        s = ac["stats"]
        lines.append(f"    n={s['count']} min={s['min']:.1f} median={s['median']:.1f} max={s['max']:.1f}")
    la = f_["last_active_age_days"]
    lines.append(f"  last_active 缺失/无法解析 = {la['missing']}")
    if la["stats"]:
        s = la["stats"]
        lines.append(
            f"    距今天数：n={s['count']} min={s['min']:.1f} "
            f"median={s['median']:.1f} max={s['max']:.1f}"
        )

    lines.append("\n" + "=" * 70)
    lines.append("本脚本只读，未修改任何桶数据。以上仅为回放参考，数值参数最终由三家人工确认。")
    return "\n".join(lines)


def run_analysis(vault_dir: str, config: dict, quota, window, now: datetime) -> dict:
    buckets, parse_errors = load_buckets(vault_dir)
    engine = DecayEngine(config, bucket_mgr=None)
    scores = score_buckets(buckets, engine)

    return {
        "vault_dir": vault_dir,
        "now": now,
        "parse_errors": parse_errors,
        "a": section_a_importance_histogram(buckets),
        "b": section_b_weight_vs_importance(buckets, scores),
        "c": section_c_cohort(buckets, scores, window),
        "d": section_d_quota_simulation(buckets, scores, quota, now),
        "e": section_e_seed_candidates(buckets),
        "f": section_f_activation_and_last_active(buckets, now),
    }


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    config = load_config()
    vault_dir = args.vault_dir or config.get("buckets_dir")
    if not vault_dir or not os.path.isdir(vault_dir):
        print(f"找不到 buckets 目录: {vault_dir!r}", file=sys.stderr)
        return 1

    window = None
    if args.import_window:
        start = _parse_window_arg(args.import_window[0])
        end = _parse_window_arg(args.import_window[1])
        if start > end:
            print("--import-window 的 START 晚于 END", file=sys.stderr)
            return 1
        window = (start, end)

    now = datetime.now()
    result = run_analysis(vault_dir, config, args.quota, window, now)
    print(render_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
