"""返修单一号 §4：created_at 回填脚本测试（v3，F 家 dry-run 两轮打回后重写）。

v3 判据核心：原值非空 = 默认可信。真实 dry-run 暴露过 v2 的伤害——
content_earliest 拿正文提到的"内容日期"去覆盖本来健康的"创建日期"
（"p模式教程"被填成 2026-02-12、966be76b7507 被从 07-08 错改到 07-09）。
只有三种情况允许动一个非空原值：
    1. 原值为空
    2. 原值命中"同一精确时刻出现 ≥5 次"的批量导入聚集特征
    3. 结构化证据（id/title，不含正文）与原值相差超过 7 天 → 只进矛盾
       清单，不自动改
其余非空原值一律跳过，标注 healthy。
"""
import os
import sys
from datetime import datetime, timedelta

import frontmatter
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from backfill_bucket_created_at import (  # noqa: E402
    _structured_evidence, _earliest_content_date, _fill_priority_date, main,
)


# ============================================================
# 解析分支单测（不碰磁盘）
# ============================================================

def test_structured_evidence_finds_hyphenated_date_in_id():
    dt, source = _structured_evidence("dream_2026-07-31", "")
    assert source == "id_date"
    assert dt == datetime(2026, 7, 31)


def test_structured_evidence_finds_bare_yyyymmdd_in_id():
    dt, source = _structured_evidence("xxx_20260707", "")
    assert source == "id_date"
    assert dt == datetime(2026, 7, 7)


def test_structured_evidence_bare_date_does_not_swallow_longer_digit_run():
    dt, source = _structured_evidence("feel_202506011423_V085", "")
    assert dt is None
    assert source == ""


def test_structured_evidence_id_takes_priority_over_name():
    dt, source = _structured_evidence("dream_2026-07-31", "会议纪要 2026-03-05")
    assert source == "id_date"
    assert dt == datetime(2026, 7, 31)


def test_structured_evidence_uses_name_when_id_has_no_date():
    dt, source = _structured_evidence("a1b2c3d4e5f6", "会议纪要 2026-03-05 讨论要点")
    assert source == "title_date"
    assert dt == datetime(2026, 3, 5)


def test_structured_evidence_ignores_standard_auto_prefix_alone():
    """标准自动前缀（跟 created 同源）单独出现时不算独立证据。"""
    dt, source = _structured_evidence("a1b2c3d4e5f6", "2026-07-06 09-00-00 开会记录")
    assert dt is None
    assert source == ""


def test_structured_evidence_uses_remainder_after_auto_prefix():
    dt, source = _structured_evidence("a1b2c3d4e5f6", "2026-07-06 09-00-00 谈到2026-01-15的计划")
    assert source == "title_date"
    assert dt == datetime(2026, 1, 15)


def test_earliest_content_date_finds_earliest_mention():
    dt = _earliest_content_date("提到过 2026-05-20，后来又聊到 2026-03-01 的事")
    assert dt == datetime(2026, 3, 1)


def test_earliest_content_date_rejects_future_as_noise():
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    dt = _earliest_content_date(f"提到过 {future} 的计划，也聊到 2026-01-01 的事")
    assert dt == datetime(2026, 1, 1)


def test_earliest_content_date_none_when_nothing_found():
    assert _earliest_content_date("正文里完全没有日期痕迹") is None


def test_fill_priority_prefers_structured_over_content():
    dt, source = _fill_priority_date("dream_2026-07-31", "", "提到过 2026-01-01")
    assert source == "id_date"
    assert dt == datetime(2026, 7, 31)


def test_fill_priority_falls_back_to_content_when_no_structured_evidence():
    dt, source = _fill_priority_date("a1b2c3d4e5f6", "没有日期的标题", "提到过 2026-03-01 的事")
    assert source == "content_earliest"
    assert dt == datetime(2026, 3, 1)


def test_fill_priority_none_when_nothing_found():
    dt, source = _fill_priority_date("a1b2c3d4e5f6", "没有日期的标题", "正文也没有日期")
    assert dt is None
    assert source == ""


# ============================================================
# 端到端：假桶夹具 + main()
# ============================================================

def _write_bucket(buckets_dir, bucket_id, name, content, created):
    post = frontmatter.Post(content)
    post["id"] = bucket_id
    post["name"] = name
    if created is not None:
        post["created"] = created
    path = os.path.join(buckets_dir, f"{bucket_id}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    return path


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    buckets_dir = str(tmp_path / "buckets")
    os.makedirs(buckets_dir, exist_ok=True)
    import backfill_bucket_created_at as mod
    monkeypatch.setattr(mod, "load_config", lambda: {"buckets_dir": buckets_dir, "embedding": {}})
    return buckets_dir


# --- 场景 1：原值为空 → 按优先级回填 ---

def test_empty_original_filled_from_id(fake_env, monkeypatch):
    path = _write_bucket(fake_env, "dream_2026-07-31", "", "梦境书条目", created=None)
    import backfill_bucket_created_at as mod
    monkeypatch.setattr(mod, "_make_backup", lambda config: "/fake/backup.zip")

    main(apply=True)

    post = frontmatter.load(path)
    assert post["created"] == "2026-07-31T00:00:00"


def test_empty_original_with_no_evidence_is_unresolved(fake_env, capsys):
    _write_bucket(fake_env, "a1b2c3d4e5f6", "没有日期的标题", "正文也没有日期", created=None)

    main(apply=False)
    out = capsys.readouterr().out

    assert "改动 0 条" in out
    assert "未解析 1 条" in out
    assert "a1b2c3d4e5f6" in out


# --- 场景 2：核心回归——非空原值默认可信，content_earliest 不得覆盖 ---

def test_non_empty_original_not_clustered_is_never_overwritten_by_content_date(fake_env, capsys):
    """复现 F 家 dry-run 的真实事故："p模式教程"桶正文提到的是教程内容
    的日期，不是这条记忆的创建日期；原值健康就必须原样不动。"""
    path = _write_bucket(
        fake_env, "p_mode_tutorial", "P模式教程整理",
        "这篇教程记录的是 2026-02-12 发布的功能，跟这条记忆本身是哪天记的无关。",
        created="2026-07-20T10:00:00",
    )

    main(apply=True)

    post = frontmatter.load(path)
    assert post["created"] == "2026-07-20T10:00:00", "健康原值不能被正文提到的内容日期覆盖"
    out = capsys.readouterr().out
    assert "健康跳过" in out
    assert "p_mode_tutorial" in out


def test_non_empty_original_with_small_title_gap_stays_healthy(fake_env):
    """结构化证据与原值差距在 7 天以内视为噪声，不进矛盾清单。"""
    path = _write_bucket(
        fake_env, "a1b2c3d4e5f6", "2026-07-06 09-00-00 谈到2026-07-10的事",
        "正文", created="2026-07-06T09:00:00",
    )

    main(apply=True)

    post = frontmatter.load(path)
    assert post["created"] == "2026-07-06T09:00:00"


# --- 场景 3：结构化证据矛盾超过 7 天 → 人工清单，不自动改 ---

def test_structured_contradiction_over_7_days_is_flagged_not_fixed(fake_env, capsys):
    path = _write_bucket(
        fake_env, "a1b2c3d4e5f6", "2026-07-06 09-00-00 谈到2026-01-01的事",
        "正文", created="2026-07-06T09:00:00",
    )

    main(apply=True)

    post = frontmatter.load(path)
    assert post["created"] == "2026-07-06T09:00:00", "矛盾清单不自动改"
    out = capsys.readouterr().out
    assert "矛盾清单" in out
    assert "a1b2c3d4e5f6" in out
    assert "矛盾待人工 1 条" in out


# --- 场景 4：批量导入聚集侦测 ---

def test_cluster_of_5_identical_timestamps_gets_backfilled(fake_env, capsys):
    shared = "2026-07-06T09:00:00"
    for i in range(5):
        _write_bucket(
            fake_env, f"sick{i}", f"2026-07-06 09-00-00 导入的记忆{i}",
            f"这件事其实发生在 2026-0{(i % 6) + 1}-15。", created=shared,
        )

    main(apply=True)
    out = capsys.readouterr().out

    assert "侦测到 1 个聚集时间戳" in out
    assert "聚集批次 5" in out
    for i in range(5):
        post = frontmatter.load(os.path.join(fake_env, f"sick{i}.md"))
        assert post["created"] != shared, f"sick{i} 应该被聚集侦测回填，不该原样保留导入时刻"


def test_cluster_below_threshold_of_5_is_not_flagged(fake_env, capsys):
    shared = "2026-07-06T09:00:00"
    for i in range(4):  # 只有 4 条，不到 5 条阈值
        _write_bucket(
            fake_env, f"sick{i}", f"2026-07-06 09-00-00 导入的记忆{i}",
            "没有日期痕迹的正文。", created=shared,
        )

    main(apply=True)
    out = capsys.readouterr().out

    assert "侦测到 0 个聚集时间戳" in out
    for i in range(4):
        post = frontmatter.load(os.path.join(fake_env, f"sick{i}.md"))
        assert post["created"] == shared, "4 条不到聚集阈值，不该被动"


def test_cluster_bucket_with_no_evidence_is_listed_separately(fake_env, capsys):
    shared = "2026-07-06T09:00:00"
    for i in range(5):
        _write_bucket(
            fake_env, f"sick{i}", f"2026-07-06 09-00-00 导入的记忆{i}",
            "没有任何日期痕迹的正文。", created=shared,
        )

    main(apply=False)
    out = capsys.readouterr().out

    assert "聚集但无证据 5 条" in out
    assert "聚集批次但找不到任何回填证据" in out
    for i in range(5):
        assert f"sick{i}" in out


def test_cluster_fix_is_idempotent_on_rerun(fake_env, monkeypatch, capsys):
    shared = "2026-07-06T09:00:00"
    for i in range(5):
        _write_bucket(
            fake_env, f"sick{i}", f"2026-07-06 09-00-00 导入的记忆{i}",
            f"这件事其实发生在 2026-0{(i % 6) + 1}-1{i}。", created=shared,
        )
    import backfill_bucket_created_at as mod
    monkeypatch.setattr(mod, "_make_backup", lambda config: "/fake/backup.zip")

    main(apply=True)
    capsys.readouterr()

    main(apply=True)
    out = capsys.readouterr().out
    # 修完之后每条桶的新 created 各不相同，不再共享同一精确时刻，
    # 重跑不会再被判定为聚集、也不会再被回填。
    assert "侦测到 0 个聚集时间戳" in out
    assert "改动 0 条" in out


# --- 报告/备份基础设施 ---

def test_backup_failure_aborts_before_touching_any_file(fake_env, monkeypatch):
    path = _write_bucket(
        fake_env, "a1b2c3d4e5f6", "2026-07-06 09-00-00 开会记录",
        "开了很久的会", created="2026-08-06T12:00:00",
    )
    import backfill_bucket_created_at as mod
    from backup_archive import BackupArchiveError

    def _boom(config):
        raise BackupArchiveError("模拟备份失败")

    monkeypatch.setattr(mod, "_make_backup", _boom)

    main(apply=True)

    post = frontmatter.load(path)
    assert post["created"] == "2026-08-06T12:00:00", "备份失败必须整个中止，不能碰任何桶文件"


def test_dry_run_does_not_write(fake_env):
    path = _write_bucket(fake_env, "dream_2026-07-31", "", "梦境书条目", created=None)

    main(apply=False)

    post = frontmatter.load(path)
    assert post.get("created") is None, "预演模式不该写盘"
