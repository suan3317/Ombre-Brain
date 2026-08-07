"""返修单一号 §4：created_at 回填脚本测试。

验收要求：假桶夹具测试解析各分支；重复跑零改动（幂等）；真实数据跑前
必须先备份（--apply 路径覆盖备份调用，不在这里跑真实全量备份的 I/O）。
"""
import os
import sys
from datetime import datetime

import frontmatter
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from backfill_bucket_created_at import _parse_real_date, main  # noqa: E402


# ============================================================
# 解析分支单测（不碰磁盘）
# ============================================================

def test_parse_prefers_title_full_datetime():
    dt, source = _parse_real_date("2026-07-06 14-30-05 今天开了很久的会", "正文里提到 2026-01-01 别被这个带偏")
    assert source == "title_datetime"
    assert dt == datetime(2026, 7, 6, 14, 30, 5)


def test_parse_no_dashed_date_anywhere_returns_none():
    dt, source = _parse_real_date("feel_202506", "无关正文")  # 无连字符日期模式
    # feel_202506 里没有 YYYY-MM-DD 模式，走到正文；正文也没有 → 无法解析
    assert dt is None
    assert source == ""


def test_parse_title_bare_date_pattern():
    dt, source = _parse_real_date("会议纪要 2026-03-05 讨论要点", "")
    assert source == "title_date"
    assert dt == datetime(2026, 3, 5, 0, 0, 0)


def test_parse_falls_back_to_earliest_content_date():
    dt, source = _parse_real_date("导入的旧记忆", "提到过 2026-05-20，后来又聊到 2026-03-01 的事")
    assert source == "content_earliest"
    assert dt == datetime(2026, 3, 1, 0, 0, 0)


def test_parse_returns_none_when_nothing_found():
    dt, source = _parse_real_date("没有日期的标题", "正文也完全没有日期痕迹")
    assert dt is None
    assert source == ""


def test_parse_rejects_invalid_calendar_date_and_keeps_trying():
    # 标题前缀形似 "YYYY-MM-DD HH-MM-SS" 但月份非法（13月），应该继续往下找，
    # 而不是直接崩溃或误采信非法日期。
    dt, source = _parse_real_date("2026-13-40 25-99-99 坏时间戳", "正文里有 2026-04-02 这个真实日期")
    assert source == "content_earliest"
    assert dt == datetime(2026, 4, 2, 0, 0, 0)


# ============================================================
# 端到端：假桶夹具 + main()（--apply 路径打桩备份，不做真实全量备份 I/O）
# ============================================================

def _write_bucket(buckets_dir: str, bucket_id: str, name: str, content: str, created: str) -> str:
    post = frontmatter.Post(content)
    post["id"] = bucket_id
    post["name"] = name
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


def test_dry_run_does_not_write_and_reports_fix_candidate(fake_env, capsys):
    buckets_dir = fake_env
    # 批量导入病根复现：桶名带真实日期，created 却是导入时刻。
    path = _write_bucket(
        buckets_dir, "bkt1", "2026-07-06 09-00-00 开会记录", "开了很久的会",
        created="2026-08-06T12:00:00",
    )

    main(apply=False)

    post = frontmatter.load(path)
    assert post["created"] == "2026-08-06T12:00:00", "预演模式不该写盘"
    out = capsys.readouterr().out
    assert "bkt1" in out
    assert "改动 1 条" in out


def test_apply_rewrites_created_and_skips_unresolved(fake_env, monkeypatch):
    buckets_dir = fake_env
    fixable = _write_bucket(
        buckets_dir, "bkt_fix", "2026-07-06 09-00-00 开会记录", "开了很久的会",
        created="2026-08-06T12:00:00",
    )
    unresolvable = _write_bucket(
        buckets_dir, "bkt_stuck", "没有日期的标题", "正文也没有日期痕迹",
        created="2026-08-06T12:00:00",
    )

    import backfill_bucket_created_at as mod
    monkeypatch.setattr(mod, "_make_backup", lambda config: "/fake/backup.zip")

    main(apply=True)

    fixed_post = frontmatter.load(fixable)
    assert fixed_post["created"] == "2026-07-06T09:00:00"

    stuck_post = frontmatter.load(unresolvable)
    assert stuck_post["created"] == "2026-08-06T12:00:00", "无法解析的桶必须原样不动"


def test_rerun_after_apply_is_idempotent_zero_changes(fake_env, monkeypatch, capsys):
    buckets_dir = fake_env
    _write_bucket(
        buckets_dir, "bkt_fix", "2026-07-06 09-00-00 开会记录", "开了很久的会",
        created="2026-08-06T12:00:00",
    )

    import backfill_bucket_created_at as mod
    monkeypatch.setattr(mod, "_make_backup", lambda config: "/fake/backup.zip")

    main(apply=True)
    capsys.readouterr()  # 清空第一次运行的输出

    main(apply=True)
    out = capsys.readouterr().out
    assert "改动 0 条" in out
    assert "已正确跳过 1 条" in out


def test_backup_failure_aborts_before_touching_any_file(fake_env, monkeypatch):
    buckets_dir = fake_env
    path = _write_bucket(
        buckets_dir, "bkt_fix", "2026-07-06 09-00-00 开会记录", "开了很久的会",
        created="2026-08-06T12:00:00",
    )

    import backfill_bucket_created_at as mod
    from backup_archive import BackupArchiveError

    def _boom(config):
        raise BackupArchiveError("模拟备份失败")

    monkeypatch.setattr(mod, "_make_backup", _boom)

    main(apply=True)

    post = frontmatter.load(path)
    assert post["created"] == "2026-08-06T12:00:00", "备份失败必须整个中止，不能碰任何桶文件"
