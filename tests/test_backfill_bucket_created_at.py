"""返修单一号 §4：created_at 回填脚本测试（v2，K 家 dry-run 打回后重写）。

K 家真实 dry-run 暴露的两个问题都要覆盖：
1) 标题日期解析要覆盖 YYYYMMDD / YYYY-MM-DD / dream_YYYY-MM-DD /
   xxx_YYYYMMDD，且从 id 和 name 两个字段都要找，不能只看 name。
2) 标准自动前缀（"YYYY-MM-DD HH-MM-SS"，跟 created 同一次 datetime.now()
   调用产生）不能当独立证据——对批量导入的病桶来说，前缀记录的正是导入
   时刻本身，跟 created 里的坏值逐秒相同；拿它去跟 created 比对"一致就
   跳过"等于让病灶自证清白。v2 只信"前缀之外"的日期痕迹（id/name 剩余
   部分、正文），没有独立证据的桶必须落进"未解析"交人工定，不能被
   自动前缀悄悄"确认正确"或"修"成另一个同样错误的值。
"""
import os
import sys
from datetime import datetime, timedelta

import frontmatter
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from backfill_bucket_created_at import _parse_real_date, main  # noqa: E402


# ============================================================
# 解析分支单测（不碰磁盘）—— _parse_real_date(bucket_id, name, content)
# ============================================================

def test_parse_finds_hyphenated_date_in_id():
    dt, source = _parse_real_date("dream_2026-07-31", "", "")
    assert source == "id_date"
    assert dt == datetime(2026, 7, 31)


def test_parse_finds_bare_yyyymmdd_in_id():
    dt, source = _parse_real_date("xxx_20260707", "", "")
    assert source == "id_date"
    assert dt == datetime(2026, 7, 7)


def test_parse_finds_bare_yyyymmdd_standalone():
    dt, source = _parse_real_date("20260707", "", "")
    assert source == "id_date"
    assert dt == datetime(2026, 7, 7)


def test_parse_bare_date_does_not_swallow_longer_digit_run():
    # feel 桶 id 常见形如 "feel_202506011423_V085"（12 位分钟级时间戳），
    # 不能被误截成 8 位日期 "20250601" —— 前后不能再挨着数字的边界必须守住。
    dt, source = _parse_real_date("feel_202506011423_V085", "", "")
    assert dt is None
    assert source == ""


def test_parse_id_takes_priority_over_name_and_content():
    dt, source = _parse_real_date(
        "dream_2026-07-31", "会议纪要 2026-03-05", "提到过 2026-01-01"
    )
    assert source == "id_date"
    assert dt == datetime(2026, 7, 31)


def test_parse_name_used_when_id_has_no_date():
    dt, source = _parse_real_date("a1b2c3d4e5f6", "会议纪要 2026-03-05 讨论要点", "")
    assert source == "title_date"
    assert dt == datetime(2026, 3, 5)


def test_parse_falls_back_to_earliest_content_date_when_id_and_name_empty():
    dt, source = _parse_real_date(
        "a1b2c3d4e5f6", "导入的旧记忆",
        "提到过 2026-05-20，后来又聊到 2026-03-01 的事",
    )
    assert source == "content_earliest"
    assert dt == datetime(2026, 3, 1)


def test_parse_returns_none_when_nothing_found():
    dt, source = _parse_real_date("a1b2c3d4e5f6", "没有日期的标题", "正文也完全没有日期痕迹")
    assert dt is None
    assert source == ""


def test_parse_rejects_future_content_date_as_noise():
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    dt, source = _parse_real_date(
        "a1b2c3d4e5f6", "没有日期的标题",
        f"提到过 {future} 的计划，也聊到 2026-01-01 的事",
    )
    assert source == "content_earliest"
    assert dt == datetime(2026, 1, 1), "未来日期不可能是创建时刻，必须当噪声丢弃"


# --- 核心回归：标准自动前缀不能当独立证据 ---

def test_parse_ignores_standard_auto_prefix_alone_as_evidence():
    """标准自动前缀（跟 created 同源）单独出现、且剩余标题/正文都没有
    独立日期时，必须判定为无法解析——不能被前缀本身"确认"成任何日期，
    那正是 v1 让 123 条病桶被"已正确跳过"放过的机制。"""
    dt, source = _parse_real_date(
        "a1b2c3d4e5f6", "2026-07-06 09-00-00 开会记录", "开了很久的会",
    )
    assert dt is None
    assert source == ""


def test_parse_uses_remainder_after_auto_prefix_when_present():
    """自动前缀之后如果标题剩余部分本身就带日期（LLM 起的标题恰好提到
    了另一个日期），那部分是独立证据，可以用。"""
    dt, source = _parse_real_date(
        "a1b2c3d4e5f6", "2026-07-06 09-00-00 谈到2026-01-15的计划", "",
    )
    assert source == "title_date"
    assert dt == datetime(2026, 1, 15)


def test_parse_rejects_invalid_calendar_date_and_keeps_trying():
    # 形似标准前缀但月份非法（13月），不匹配自动前缀模式，整串当剩余部分
    # 扫描；13-40 本身也不是合法连字符日期，应该继续往下试正文。
    dt, source = _parse_real_date(
        "a1b2c3d4e5f6", "2026-13-40 坏时间戳", "正文里有 2026-04-02 这个真实日期",
    )
    assert source == "content_earliest"
    assert dt == datetime(2026, 4, 2)


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
    # 批量导入病根复现：标准前缀是导入时刻（跟 created 相同，不可信），
    # 但正文里留着真实发生日期的痕迹——这才是能安全回填的独立证据。
    path = _write_bucket(
        buckets_dir, "bkt1", "2026-08-06 12-00-00 开会记录",
        "7月6号那天开了很久的会 2026-07-06",
        created="2026-08-06T12:00:00",
    )

    main(apply=False)

    post = frontmatter.load(path)
    assert post["created"] == "2026-08-06T12:00:00", "预演模式不该写盘"
    out = capsys.readouterr().out
    assert "bkt1" in out
    assert "改动 1 条" in out


def test_apply_rewrites_created_and_leaves_no_evidence_bucket_unresolved(fake_env, monkeypatch):
    buckets_dir = fake_env
    fixable = _write_bucket(
        buckets_dir, "bkt_fix", "2026-08-06 12-00-00 开会记录",
        "7月6号那天开了很久的会 2026-07-06",
        created="2026-08-06T12:00:00",
    )
    unresolvable = _write_bucket(
        buckets_dir, "bkt_stuck", "2026-08-06 12-00-00 没有日期痕迹", "正文也没有日期痕迹",
        created="2026-08-06T12:00:00",
    )

    import backfill_bucket_created_at as mod
    monkeypatch.setattr(mod, "_make_backup", lambda config: "/fake/backup.zip")

    main(apply=True)

    fixed_post = frontmatter.load(fixable)
    assert fixed_post["created"] == "2026-07-06T00:00:00"

    stuck_post = frontmatter.load(unresolvable)
    assert stuck_post["created"] == "2026-08-06T12:00:00", "没有独立证据的桶必须原样不动，不能被自动前缀确认或改写"


def test_sick_bucket_with_no_independent_evidence_is_unresolved_not_confirmed(fake_env, monkeypatch, capsys):
    """K 家 dry-run 打回的核心场景复现：标题标准前缀与 created 完全同源
    （批量导入病桶的典型形状），且没有任何其它日期痕迹。v1 会把这种桶
    静默判"已正确"放过；v2 必须落进未解析清单，绝不能被自动前缀自证。"""
    buckets_dir = fake_env
    _write_bucket(
        buckets_dir, "bkt_sick", "2026-07-06 09-00-00 导入的记忆",
        "没有任何日期痕迹的正文",
        created="2026-07-06T09:00:00",  # 跟标题前缀逐秒相同——批量导入的典型病灶
    )

    main(apply=False)
    out = capsys.readouterr().out

    assert "改动 0 条" in out
    assert "已正确跳过 0 条" in out
    assert "无法解析 1 条" in out
    assert "bkt_sick" in out


def test_rerun_after_apply_is_idempotent_zero_changes(fake_env, monkeypatch, capsys):
    buckets_dir = fake_env
    _write_bucket(
        buckets_dir, "bkt_fix", "2026-08-06 12-00-00 开会记录",
        "7月6号那天开了很久的会 2026-07-06",
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


def test_skip_report_lists_bucket_and_source_for_audit(fake_env, monkeypatch, capsys):
    """跳过清单必须带来源，方便人工审计"这条真的没病"而不是又被放过——
    直接回应 K 家的追问。"""
    buckets_dir = fake_env
    _write_bucket(
        buckets_dir, "bkt_fix", "2026-08-06 12-00-00 开会记录",
        "7月6号那天开了很久的会 2026-07-06",
        created="2026-08-06T12:00:00",
    )
    import backfill_bucket_created_at as mod
    monkeypatch.setattr(mod, "_make_backup", lambda config: "/fake/backup.zip")
    main(apply=True)
    capsys.readouterr()

    main(apply=True)
    out = capsys.readouterr().out
    assert "已正确跳过清单" in out
    assert "content_earliest" in out
    assert "bkt_fix" in out


def test_backup_failure_aborts_before_touching_any_file(fake_env, monkeypatch):
    buckets_dir = fake_env
    path = _write_bucket(
        buckets_dir, "bkt_fix", "2026-08-06 12-00-00 开会记录",
        "7月6号那天开了很久的会 2026-07-06",
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
