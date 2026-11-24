import sqlite3

import frontmatter
import pytest

from vault_health import inspect_vault


def _write(path, bucket_id, content="memory"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        frontmatter.dumps(frontmatter.Post(content, id=bucket_id, type="dynamic")),
        encoding="utf-8",
    )


def _db(path, ids=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT ''
            )"""
        )
        connection.executemany(
            "INSERT INTO embeddings VALUES (?, '[0.1]', 'now', 'hash')",
            [(item,) for item in ids],
        )


def test_vault_health_reports_clean_source_and_projection(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "one.md", "one")
    db = vault / "embeddings.db"
    _db(db, ["one"])

    report = inspect_vault(str(vault), str(db))

    assert report["status"] == "ok"
    assert report["markdown"]["file_count"] == 1
    assert report["sqlite"]["quick_check_ok"] is True
    assert report["sqlite"]["missing_unqueued_count"] == 0


def test_vault_health_distinguishes_pending_missing_and_orphan_vectors(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "one.md", "one")
    _write(vault / "dynamic" / "general" / "two.md", "two")
    db = vault / "embeddings.db"
    _db(db, ["one", "gone"])

    queued = inspect_vault(str(vault), str(db), pending_ids={"two"})
    assert queued["status"] == "warning"
    assert queued["sqlite"]["orphan_ids"] == ["gone"]
    assert queued["sqlite"]["missing_active_ids"] == ["two"]
    assert queued["sqlite"]["missing_unqueued_count"] == 0

    unqueued = inspect_vault(str(vault), str(db))
    assert unqueued["sqlite"]["missing_unqueued_ids"] == ["two"]


def test_vault_health_reports_parse_errors_and_duplicate_ids(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "one.md", "same")
    _write(vault / "archive" / "general" / "old.md", "same")
    bad = vault / "dynamic" / "general" / "bad.md"
    bad.write_bytes(b"\xff\xfe")

    report = inspect_vault(str(vault), str(vault / "missing.db"))

    assert report["status"] == "error"
    assert report["markdown"]["duplicate_id_count"] == 1
    assert report["markdown"]["parse_error_count"] == 1


def test_diary_and_old_dreams_same_date_filename_no_longer_scanned(tmp_path):
    """原先这条复现的是：diary/ 与旧 files/dreams/ 同日期文件名、都不写 id，
    stem 兜底撞成同一个 id（duplicate_id_count == 1）。2026-11-24 起体检只扫
    桶目录，files/ 整个不进体检，同名文件不再计数，也不再撞 id。"""
    vault = tmp_path / "vault"
    _write_no_id(vault / "files" / "diary" / "2026-07-31.md", "日记正文")
    _write_no_id(vault / "files" / "dreams" / "2026-07-31.md", "旧梦正文")

    report = inspect_vault(str(vault), str(vault / "missing.db"))

    assert report["markdown"]["file_count"] == 0
    assert report["markdown"]["duplicate_id_count"] == 0


def test_file_zone_markdown_with_rule_and_star_is_not_parsed(tmp_path):
    """Rio 体检报错的复现：文件区普通 .md 第一行是 "---"、第二行 "*" 开头，
    按头信息解析会报 YAML alias 错。体检不扫 files/，不报错、不计数。"""
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "one.md", "one")
    board = vault / "files" / "留言板.md"
    board.parent.mkdir(parents=True, exist_ok=True)
    board.write_text("---\n*加粗* 第二行\n---\n正文\n", encoding="utf-8")
    # 前提：这份文本按头信息解析确实会炸（line 2, column 1 的 alias 错）
    with pytest.raises(Exception, match="alias"):
        frontmatter.loads(board.read_text(encoding="utf-8"))
    db = vault / "embeddings.db"
    _db(db, ["one"])

    report = inspect_vault(str(vault), str(db))

    assert report["status"] == "ok"
    assert report["markdown"]["file_count"] == 1
    assert report["markdown"]["parse_error_count"] == 0


def test_dream_book_and_hidden_dirs_are_not_scanned(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "one.md", "one")
    for rel in ("dream_book/2026-07-31.md", ".import_backups/x.md", "files/a/b.md"):
        bad = vault / rel
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"\xff\xfe")

    report = inspect_vault(str(vault), str(vault / "missing.db"))

    assert report["markdown"]["file_count"] == 1
    assert report["markdown"]["parse_error_count"] == 0


def test_broken_header_in_bucket_dir_still_reports_error(tmp_path):
    """桶目录里真有坏头信息的，照旧报 error。"""
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "one.md", "one")
    bad = vault / "dynamic" / "general" / "bad.md"
    bad.write_text("---\n*坏头信息\n---\n正文\n", encoding="utf-8")
    (vault / "files").mkdir(parents=True, exist_ok=True)
    (vault / "files" / "留言板.md").write_text("---\n*加粗*\n---\n", encoding="utf-8")

    report = inspect_vault(str(vault), str(vault / "missing.db"))

    assert report["status"] == "error"
    assert report["markdown"]["file_count"] == 2
    assert report["markdown"]["parse_error_count"] == 1
    assert report["markdown"]["parse_errors"][0]["path"].endswith("bad.md")


def test_scan_list_matches_bucket_manager_dirs(bucket_mgr):
    """体检的目录名单必须与 BucketManager 遍历的（活跃目录 + archive）一致。"""
    import os

    from vault_health import _BUCKET_TOP_LEVELS

    expected = {
        os.path.basename(path)
        for path in list(bucket_mgr._active_dirs) + [bucket_mgr.archive_dir]
    }
    assert set(_BUCKET_TOP_LEVELS) == expected


def test_dream_book_explicit_id_does_not_collide_with_diary(tmp_path):
    """迁移后：梦境书条目有显式 id("dream_YYYY-MM-DD")，不再靠文件名 stem
    兜底，即使跟 diary/ 同一天的文件名撞了，id 也不会撞——duplicate_id_count
    应该归零。"""
    from dream_engine import dream_book_id

    vault = tmp_path / "vault"
    _write_no_id(vault / "files" / "diary" / "2026-07-31.md", "日记正文")
    _write(
        vault / "dream_book" / "2026-07-31.md",
        dream_book_id("2026-07-31"),
        content="梦境书正文",
    )

    report = inspect_vault(str(vault), str(vault / "missing.db"))

    assert report["markdown"]["duplicate_id_count"] == 0


def _write_no_id(path, content):
    """模拟旧数据：完全没有 id frontmatter 字段，post.get("id") 只能靠
    path.stem 兜底——这正是 inspect_vault 里 duplicate_id_count 统计的
    那一分支（vault_health.py: bucket_id = str(post.get("id") or resolved.stem)）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(content)), encoding="utf-8")
