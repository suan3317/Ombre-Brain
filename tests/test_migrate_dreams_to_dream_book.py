"""施工单·工程二 §6：迁移脚本测试。验收要求：迁移脚本在假 file zone 上
跑通；重复跑不重复导（幂等）；导入后 file_list 不再出现 dreams/（用
os.path.isdir 直接验证旧目录清空/文件消失，等价于 file_list 扫不到）。
"""
import os
import sys

import frontmatter as fm
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from migrate_dreams_to_dream_book import main  # noqa: E402


def _write_old_dream(buckets_dir, date_str, status="read", body="旧梦正文", generated_at="2026-07-31T06:00:00"):
    old_dir = os.path.join(buckets_dir, "files", "dreams")
    os.makedirs(old_dir, exist_ok=True)
    post = fm.Post(body)
    post["date"] = date_str
    post["tone"] = "荒诞"
    post["level"] = "只剩画面"
    post["sources"] = ["b1"]
    post["noise"] = 1
    post["status"] = status
    post["generated_at"] = generated_at
    path = os.path.join(old_dir, f"{date_str}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))
    return path


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    buckets_dir = str(tmp_path / "buckets")
    os.makedirs(buckets_dir, exist_ok=True)
    import migrate_dreams_to_dream_book as mod
    monkeypatch.setattr(mod, "load_config", lambda: {"buckets_dir": buckets_dir})
    return buckets_dir


def test_no_old_dir_is_a_clean_noop(fake_env, capsys):
    main(apply=True)
    out = capsys.readouterr().out
    assert "没有找到旧址" in out


def test_dry_run_does_not_write_or_delete(fake_env):
    old_path = _write_old_dream(fake_env, "2026-07-31")

    main(apply=False)

    assert os.path.isfile(old_path), "预演模式不该删除旧文件"
    from dream_engine import dream_book_path
    assert not os.path.isfile(dream_book_path(fake_env, "2026-07-31")), "预演模式不该写盘"


def test_apply_imports_with_kept_status_and_deletes_old_file(fake_env):
    old_path = _write_old_dream(fake_env, "2026-07-31", status="read", body="旧梦正文", generated_at="2026-07-31T06:00:00")

    main(apply=True)

    from dream_engine import dream_book_path, dream_book_id
    new_path = dream_book_path(fake_env, "2026-07-31")
    assert os.path.isfile(new_path)
    assert not os.path.isfile(old_path), "导入成功后旧文件应被删除"

    post = fm.load(new_path)
    assert post["id"] == dream_book_id("2026-07-31")
    assert post["keep_status"] == "kept", "头一批产出，档案价值，迁移时不追溯烧"
    assert post["read_status"] == "read"
    assert post["created_at"] == "2026-07-31T06:00:00"
    assert post.get("kept_at")
    assert str(post.content).strip() == "旧梦正文"


def test_apply_maps_old_unread_status_to_new_unread(fake_env):
    _write_old_dream(fake_env, "2026-08-01", status="unread")

    main(apply=True)

    from dream_engine import dream_book_path
    post = fm.load(dream_book_path(fake_env, "2026-08-01"))
    assert post["read_status"] == "unread"


def test_apply_maps_old_expired_status_to_new_read(fake_env):
    """旧 schema 的 expired 语义已经脱钩(工程二烧毁不再看投递状态)，
    迁移只关心"读没读过"，expired 当已读处理，不引入第三种取值。"""
    _write_old_dream(fake_env, "2026-08-02", status="expired")

    main(apply=True)

    from dream_engine import dream_book_path
    post = fm.load(dream_book_path(fake_env, "2026-08-02"))
    assert post["read_status"] == "read"


def test_rerun_after_apply_is_idempotent(fake_env, capsys):
    _write_old_dream(fake_env, "2026-07-31")
    main(apply=True)
    capsys.readouterr()

    # 旧文件已经被删了，第二次跑应该是"没有旧址/空目录"或者 0 条改动，
    # 不会去覆盖已经迁移过的梦境书条目。
    from dream_engine import dream_book_path
    new_path = dream_book_path(fake_env, "2026-07-31")
    before_mtime = os.path.getmtime(new_path)

    main(apply=True)

    assert os.path.getmtime(new_path) == before_mtime, "重复跑不应该改动已迁移的条目"


def test_apply_skips_when_target_already_exists_without_deleting_old(fake_env, capsys):
    """幂等的另一面:如果梦境书那边已经有同日期条目(比如手工建过)，旧文件
    还在，脚本不能覆盖目标，也不能误删旧文件（导入没有"发生"）。"""
    from dream_engine import dream_book_path, dream_book_id
    new_path = dream_book_path(fake_env, "2026-07-31")
    os.makedirs(os.path.dirname(new_path), exist_ok=True)
    existing = fm.Post("已经手工存在的梦境书条目")
    existing["id"] = dream_book_id("2026-07-31")
    existing["date"] = "2026-07-31"
    existing["keep_status"] = "kept"
    with open(new_path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(existing))

    old_path = _write_old_dream(fake_env, "2026-07-31", body="旧文件里的正文")

    main(apply=True)

    assert os.path.isfile(old_path), "目标已存在时不该删除旧文件"
    reloaded = fm.load(new_path)
    assert str(reloaded.content).strip() == "已经手工存在的梦境书条目", "不该覆盖已存在的梦境书条目"


def test_multiple_dreams_migrate_independently(fake_env):
    _write_old_dream(fake_env, "2026-07-31")
    _write_old_dream(fake_env, "2026-08-01")
    _write_old_dream(fake_env, "2026-08-03")

    main(apply=True)

    from dream_engine import dream_book_path
    for date_str in ("2026-07-31", "2026-08-01", "2026-08-03"):
        assert os.path.isfile(dream_book_path(fake_env, date_str))
    old_dir = os.path.join(fake_env, "files", "dreams")
    assert os.listdir(old_dir) == [], "全部导入成功后旧目录应清空"


def test_old_files_dreams_dir_absent_from_file_zone_after_migration(fake_env):
    """附加验收落地:迁移后 file_list（扫描 <buckets_dir>/files/）不会再
    看到 dreams/ 下的任何文件——旧目录里已经没有 .md 了。"""
    _write_old_dream(fake_env, "2026-07-31")

    main(apply=True)

    old_dir = os.path.join(fake_env, "files", "dreams")
    remaining_md = [f for f in os.listdir(old_dir) if f.endswith(".md")]
    assert remaining_md == []
