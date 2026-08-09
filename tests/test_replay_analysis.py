"""记忆动力学二期 · 回放分析脚本（tools/replay_analysis.py）测试。

只读脚本，不依赖真实生产数据——构造合成桶目录覆盖 a-f 各输出分支，并用
字节+mtime 双重哈希验证脚本严格只读（红线要求，参照
tests/test_list_ren_engineering_buckets.py 的同款只读校验手法）。
"""
import datetime
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

import pytest  # noqa: E402

import replay_analysis as ra  # noqa: E402


def _write_bucket(root, subdir, filename, frontmatter_lines, body=""):
    d = os.path.join(root, subdir)
    os.makedirs(d, exist_ok=True)
    text = "---\n" + "\n".join(frontmatter_lines) + "\n---\n" + body
    with open(os.path.join(d, filename), "w", encoding="utf-8") as f:
        f.write(text)


NOW = datetime.datetime(2026, 8, 8, 12, 0, 0)
OLD = (NOW - datetime.timedelta(days=200)).isoformat()
RECENT = (NOW - datetime.timedelta(days=5)).isoformat()
WINDOW_START = "2026-07-06T02:08:37"
WINDOW_END = "2026-07-06T02:33:32"
IN_WINDOW = "2026-07-06T02:15:00"


def _seed_vault(root):
    # 高 importance、老且低激活 → 现行 weight 应该很低（沉底候选）。
    _write_bucket(
        root, "dynamic", "sinker.md",
        ["id: sinker", "name: 高重要低权重", "importance: 9", "type: dynamic",
         f"created: {OLD}", f"last_active: {OLD}", "activation_count: 1"],
    )
    # 中档、本月新 → K 家指标的目标对象。
    _write_bucket(
        root, "dynamic", "midnew.md",
        ["id: midnew", "name: 中档新桶", "importance: 6", "type: dynamic",
         f"created: {RECENT}", f"last_active: {RECENT}", "activation_count: 5"],
    )
    # 中档、导入窗口内。
    _write_bucket(
        root, "dynamic", "inwindow.md",
        ["id: inwindow", "name: 导入窗口内", "importance: 7", "type: dynamic",
         f"created: {IN_WINDOW}", f"last_active: {IN_WINDOW}", "activation_count: 2"],
    )
    # 低档普通桶，无 activation_count/last_active（评估迁移起点用）。
    _write_bucket(
        root, "dynamic", "lowplain.md",
        ["id: lowplain", "name: 低档普通", "importance: 3", "type: dynamic",
         f"created: {OLD}"],
    )
    # anchor 桶：type=dynamic 但不该进候选池。
    _write_bucket(
        root, "dynamic", "anchor1.md",
        ["id: anchor1", "name: 坐标桶", "importance: 5", "type: dynamic",
         "anchor: true", f"created: {OLD}"],
    )
    # pinned + permanent：seed 候选清单该收，但不进浮现候选池。
    _write_bucket(
        root, "permanent", "core1.md",
        ["id: core1", "name: 核心准则", "importance: 10", "type: permanent",
         "pinned: true", f"created: {OLD}"],
    )
    # archive 桶：不应被任何一节统计到。
    _write_bucket(
        root, "archive", "dead1.md",
        ["id: dead1", "name: 已归档", "importance: 8", "type: dynamic",
         f"created: {OLD}"],
    )


@pytest.fixture
def config():
    return {"decay": {"lambda": 0.05, "threshold": 0.3, "emotion_weights": {"base": 0.5, "arousal_boost": 0.5}}}


def test_load_buckets_excludes_archive(tmp_path):
    _seed_vault(str(tmp_path))
    buckets, errors = ra.load_buckets(str(tmp_path))
    ids = {b["id"] for b in buckets}
    assert "dead1" not in ids
    assert ids == {"sinker", "midnew", "inwindow", "lowplain", "anchor1", "core1"}
    assert errors == []


def test_is_pool_candidate_excludes_anchor_pinned_and_non_dynamic():
    assert ra._is_pool_candidate({"type": "dynamic"}) is True
    assert ra._is_pool_candidate({"type": "dynamic", "anchor": True}) is False
    assert ra._is_pool_candidate({"type": "dynamic", "anchor": "true"}) is False
    assert ra._is_pool_candidate({"type": "dynamic", "pinned": True}) is False
    assert ra._is_pool_candidate({"type": "dynamic", "protected": True}) is False
    assert ra._is_pool_candidate({"type": "permanent"}) is False
    assert ra._is_pool_candidate({"type": "feel"}) is False


def test_section_a_importance_histogram_counts_pool_only(tmp_path):
    _seed_vault(str(tmp_path))
    buckets, _ = ra.load_buckets(str(tmp_path))
    result = ra.section_a_importance_histogram(buckets)

    # 候选池 = sinker(9) + midnew(6) + inwindow(7) + lowplain(3)；
    # anchor1/core1/dead1 都不计入。
    assert result["pool_size"] == 4
    assert result["histogram"][9] == 1
    assert result["histogram"][6] == 1
    assert result["histogram"][7] == 1
    assert result["histogram"][3] == 1
    assert result["histogram"][10] == 0
    assert result["by_type_all"]["dynamic"] == 5  # 含 anchor1，不含 archive 的 dead1
    assert result["by_type_all"]["permanent"] == 1


def test_section_b_flags_high_importance_low_weight_as_sinker(tmp_path, config):
    _seed_vault(str(tmp_path))
    buckets, _ = ra.load_buckets(str(tmp_path))
    engine = ra.DecayEngine(config, bucket_mgr=None)
    scores = ra.score_buckets(buckets, engine)
    result = ra.section_b_weight_vs_importance(buckets, scores)

    assert result["pool_size"] == 4
    assert "sinker" in result["sinkers_sample_ids"]
    assert result["sinkers_count"] >= 1
    assert 9 in result["scatter_by_importance"]
    assert result["scatter_by_importance"][9]["count"] == 1


def test_section_c_filters_by_created_window_and_computes_dispersion(tmp_path, config):
    _seed_vault(str(tmp_path))
    buckets, _ = ra.load_buckets(str(tmp_path))
    engine = ra.DecayEngine(config, bucket_mgr=None)
    scores = ra.score_buckets(buckets, engine)
    window = (ra.parse_iso_datetime(WINDOW_START), ra.parse_iso_datetime(WINDOW_END))

    result = ra.section_c_cohort(buckets, scores, window)

    assert result["total_in_window"] == 1
    assert result["by_type"] == {"dynamic": 1}
    assert result["weight_dispersion"]["count"] == 1


def test_section_c_returns_none_without_window(tmp_path, config):
    _seed_vault(str(tmp_path))
    buckets, _ = ra.load_buckets(str(tmp_path))
    engine = ra.DecayEngine(config, bucket_mgr=None)
    scores = ra.score_buckets(buckets, engine)

    assert ra.section_c_cohort(buckets, scores, None) is None


def test_section_d_quota_simulation_reports_k_mid_metric(tmp_path, config):
    _seed_vault(str(tmp_path))
    buckets, _ = ra.load_buckets(str(tmp_path))
    engine = ra.DecayEngine(config, bucket_mgr=None)
    scores = ra.score_buckets(buckets, engine)

    result = ra.section_d_quota_simulation(buckets, scores, (5, 3, 2), NOW)

    assert result["pool_size"] == 4
    # CLI 指定的配额与内置"提案默认"相同，不应重复出现。
    assert list(result["scenarios"].keys()) == [name for name, _ in ra._BUILTIN_QUOTA_PRESETS]

    baseline = result["scenarios"]["提案默认 5/3/2"]
    km = baseline["k_mid_metric"]
    assert km["mid_pool_size"] == 2  # midnew(6) + inwindow(7)
    assert km["mid_recent_new_count"] == 1  # 只有 midnew 是近 30 天新桶
    assert "midnew" in km["mid_recent_new_selected_ids"]
    # mid quota=3 >= mid pool=2，两条全部入选。
    assert km["mid_recent_new_selected_count"] == 1


def test_section_d_adds_cli_quota_when_different_from_presets(tmp_path, config):
    _seed_vault(str(tmp_path))
    buckets, _ = ra.load_buckets(str(tmp_path))
    engine = ra.DecayEngine(config, bucket_mgr=None)
    scores = ra.score_buckets(buckets, engine)

    result = ra.section_d_quota_simulation(buckets, scores, (7, 1, 1), NOW)

    names = list(result["scenarios"].keys())
    assert names[0] == "CLI 指定 7/1/1"
    assert len(names) == len(ra._BUILTIN_QUOTA_PRESETS) + 1


def test_section_e_seed_candidates_includes_high_importance_and_pinned_only(tmp_path):
    _seed_vault(str(tmp_path))
    buckets, _ = ra.load_buckets(str(tmp_path))

    result = ra.section_e_seed_candidates(buckets)
    ids = {row["id"] for row in result["candidates"]}

    # sinker: importance=9 → 命中；core1: pinned=True → 命中。
    assert ids == {"sinker", "core1"}
    assert result["count"] == 2
    by_id = {row["id"]: row for row in result["candidates"]}
    assert by_id["core1"]["pinned"] is True
    assert by_id["sinker"]["pinned"] is False


def test_section_f_reports_missing_activation_and_last_active(tmp_path):
    _seed_vault(str(tmp_path))
    buckets, _ = ra.load_buckets(str(tmp_path))

    result = ra.section_f_activation_and_last_active(buckets, NOW)

    # lowplain 没有 activation_count/last_active；anchor1/core1 也没有。
    assert result["activation_count"]["missing"] == 3
    assert result["activation_count"]["stats"]["count"] == 3
    assert result["last_active_age_days"]["missing"] == 3
    assert result["last_active_age_days"]["stats"]["count"] == 3


def test_run_analysis_is_strictly_read_only(tmp_path, config):
    _seed_vault(str(tmp_path))

    def _fingerprint():
        digest = {}
        for dirpath, _dirs, files in os.walk(str(tmp_path)):
            for fn in files:
                p = os.path.join(dirpath, fn)
                with open(p, "rb") as f:
                    content_hash = hashlib.sha256(f.read()).hexdigest()
                digest[p] = (content_hash, os.stat(p).st_mtime)
        return digest

    before = _fingerprint()
    result = ra.run_analysis(str(tmp_path), config, (5, 3, 2), None, NOW)
    after = _fingerprint()

    assert before == after
    # 顺带验证渲染不炸，且关键指标文案出现。
    report = ra.render_report(result)
    assert "K 家指标" in report
    assert "沉底桶" in report


def test_run_analysis_handles_empty_vault(tmp_path, config):
    result = ra.run_analysis(str(tmp_path), config, (5, 3, 2), None, NOW)
    assert result["a"]["pool_size"] == 0
    assert result["e"]["count"] == 0
    report = ra.render_report(result)
    assert "候选池为空" in report


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5,3,2", (5, 3, 2)),
        (" 6 , 3 , 1 ", (6, 3, 1)),
    ],
)
def test_parse_quota_accepts_valid_input(raw, expected):
    assert ra._parse_quota(raw) == expected


@pytest.mark.parametrize("raw", ["5,3", "a,b,c", "5,3,-1", "5,3,2,1"])
def test_parse_quota_rejects_invalid_input(raw):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        ra._parse_quota(raw)


def test_parse_window_arg_rejects_unparseable_string():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        ra._parse_window_arg("not-a-date")
