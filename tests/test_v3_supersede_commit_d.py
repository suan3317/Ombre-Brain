"""
记忆动力学二期 · Commit D：supersede + derived_freshness sidecar + legacy
迁移。

范围（F 裁定 2026-08-09）：
1/2/4/5 认可原方案；Dashboard 独立文件查看器（纯字节流）本期跳过。
3 两级精度：目标文件正文里有字面 [bucket_id:xxx] 标记的，sidecar
  location 记录该标记所在行号（"段"级）；只有 provenance edge（cited
  参数）、正文里没有字面标记的，退化到"引用行为"级（诚实条款承认的
  当前颗粒度上限，claim/span 细粒度列三期）。两级都遵守"禁止整份文档级
  模糊警示"红线。

覆盖：
- bucket_manager：SUPERSEDE_TYPES/find_marker_line/format_staleness_warning
  纯函数；derived_freshness sidecar 的标记/读取/校准（幂等、多条独立）；
  mark_superseded() 的校验与旧正文不改；_propagate_supersede_staleness
  的两级精度分流。
- trace_core：superseded_by/supersede_type/supersede_effective_at 参数。
- 三入口：tools/i/core.py 的 _read_i、wake 留言板段、file_read（_fz_read）。
- tools/migrate_legacy_claim_index.py：字面标记扫描 + 候选队列，不自动
  标 stale，不碰任何文件/桶本体。
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

import tools._runtime as rt
from bucket_manager import (
    SUPERSEDE_TYPES,
    find_marker_line,
    format_staleness_warning,
)
from tools.trace.core import trace_core


def install_runtime(bucket_mgr):
    from decay_engine import DecayEngine

    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = DecayEngine({}, bucket_mgr)
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


# ============================================================
# 纯函数：find_marker_line / format_staleness_warning
# ============================================================
class TestFindMarkerLine:
    def test_finds_marker_on_correct_line(self):
        text = "第一行\n第二行 [bucket_id:abc123] 提到\n第三行"
        assert find_marker_line(text, "abc123") == 2

    def test_returns_none_when_absent(self):
        assert find_marker_line("没有标记的正文", "abc123") is None

    def test_does_not_match_different_bucket_id(self):
        text = "这里是 [bucket_id:xyz789]"
        assert find_marker_line(text, "abc123") is None

    def test_first_occurrence_wins(self):
        text = "[bucket_id:abc123]\n中间\n[bucket_id:abc123]"
        assert find_marker_line(text, "abc123") == 1

    def test_handles_empty_text(self):
        assert find_marker_line("", "abc123") is None
        assert find_marker_line(None, "abc123") is None


class TestFormatStalenessWarning:
    def test_empty_entries_returns_empty_string(self):
        assert format_staleness_warning([]) == ""

    def test_paragraph_precision_names_the_line(self):
        entries = [{
            "stale_by": "oldid", "replaced_by": "newid", "location": "line:5",
            "precision": "paragraph", "supersede_type": "contradiction",
        }]
        text = format_staleness_warning(entries)
        assert "第 5 行" in text
        assert "oldid" in text  # 哪个旧事实过期了
        assert "newid" in text  # 被谁取代
        assert "被推翻" in text

    def test_citation_precision_names_the_citing_act(self):
        entries = [{
            "stale_by": "oldid", "replaced_by": "newid", "location": "hold",
            "precision": "citation", "supersede_type": "state_transition",
        }]
        text = format_staleness_warning(entries)
        assert "hold" in text
        assert "状态已变化" in text
        assert "第" not in text  # 不该冒充有行号

    def test_multiple_entries_each_on_own_line_not_merged(self):
        entries = [
            {"stale_by": "a", "replaced_by": "x", "location": "line:1", "precision": "paragraph", "supersede_type": "contradiction"},
            {"stale_by": "b", "replaced_by": "y", "location": "line:9", "precision": "paragraph", "supersede_type": "plan_completed"},
        ]
        text = format_staleness_warning(entries)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == 2  # 每条独立一行，不合并成一句笼统提示


# ============================================================
# derived_freshness sidecar：标记 / 读取 / 校准
# ============================================================
class TestDerivedFreshnessSidecar:
    @pytest.mark.asyncio
    async def test_mark_and_get(self, bucket_mgr):
        await bucket_mgr.mark_derived_stale(
            "file:note.md", stale_by="oldid", location="line:3",
            supersede_type="contradiction", precision="paragraph",
        )
        entries = await bucket_mgr.get_derived_stale("file:note.md")
        assert len(entries) == 1
        assert entries[0]["stale_by"] == "oldid"
        assert entries[0]["location"] == "line:3"

    @pytest.mark.asyncio
    async def test_get_empty_when_never_marked(self, bucket_mgr):
        assert await bucket_mgr.get_derived_stale("file:never_marked.md") == []

    @pytest.mark.asyncio
    async def test_mark_is_idempotent_same_stale_by_and_location(self, bucket_mgr):
        for _ in range(3):
            await bucket_mgr.mark_derived_stale(
                "file:note.md", stale_by="oldid", location="line:3",
                supersede_type="contradiction", precision="paragraph",
            )
        entries = await bucket_mgr.get_derived_stale("file:note.md")
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_multiple_distinct_stale_markers_coexist(self, bucket_mgr):
        await bucket_mgr.mark_derived_stale(
            "file:note.md", stale_by="old1", location="line:3",
            supersede_type="contradiction", precision="paragraph",
        )
        await bucket_mgr.mark_derived_stale(
            "file:note.md", stale_by="old2", location="line:9",
            supersede_type="plan_completed", precision="paragraph",
        )
        entries = await bucket_mgr.get_derived_stale("file:note.md")
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_clear_all_for_path(self, bucket_mgr):
        await bucket_mgr.mark_derived_stale(
            "file:note.md", stale_by="old1", location="line:3",
            supersede_type="contradiction", precision="paragraph",
        )
        removed = await bucket_mgr.clear_derived_stale("file:note.md")
        assert removed == 1
        assert await bucket_mgr.get_derived_stale("file:note.md") == []

    @pytest.mark.asyncio
    async def test_clear_scoped_to_one_stale_by_keeps_others(self, bucket_mgr):
        await bucket_mgr.mark_derived_stale(
            "file:note.md", stale_by="old1", location="line:3",
            supersede_type="contradiction", precision="paragraph",
        )
        await bucket_mgr.mark_derived_stale(
            "file:note.md", stale_by="old2", location="line:9",
            supersede_type="plan_completed", precision="paragraph",
        )
        removed = await bucket_mgr.clear_derived_stale("file:note.md", stale_by="old1")
        assert removed == 1
        remaining = await bucket_mgr.get_derived_stale("file:note.md")
        assert len(remaining) == 1
        assert remaining[0]["stale_by"] == "old2"

    @pytest.mark.asyncio
    async def test_clear_nonexistent_returns_zero(self, bucket_mgr):
        assert await bucket_mgr.clear_derived_stale("file:never.md") == 0

    @pytest.mark.asyncio
    async def test_sidecar_never_touches_bucket_files(self, bucket_mgr):
        bid = await bucket_mgr.create(content="正文不该被碰", importance=5, domain=["测试"])
        before = (await bucket_mgr.get(bid))["content"]

        await bucket_mgr.mark_derived_stale(
            bid, stale_by="other", location="hold", supersede_type="contradiction", precision="citation",
        )

        after = (await bucket_mgr.get(bid))["content"]
        assert before == after


# ============================================================
# mark_superseded()：校验 + 旧正文不改
# ============================================================
class TestMarkSuperseded:
    def test_all_four_types_are_recognized(self):
        assert SUPERSEDE_TYPES == {"contradiction", "state_transition", "plan_completed", "plan_abandoned"}

    @pytest.mark.asyncio
    async def test_rejects_invalid_type(self, bucket_mgr):
        old = await bucket_mgr.create(content="旧事实", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])
        result = await bucket_mgr.mark_superseded(old, superseded_by=new, supersede_type="not_a_real_type")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_rejects_self_supersede(self, bucket_mgr):
        bid = await bucket_mgr.create(content="自己", importance=5, domain=["测试"])
        result = await bucket_mgr.mark_superseded(bid, superseded_by=bid, supersede_type="contradiction")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_rejects_when_target_missing(self, bucket_mgr):
        old = await bucket_mgr.create(content="旧事实", importance=5, domain=["测试"])
        result = await bucket_mgr.mark_superseded(old, superseded_by="does_not_exist", supersede_type="contradiction")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_rejects_when_source_missing(self, bucket_mgr):
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])
        result = await bucket_mgr.mark_superseded("does_not_exist", superseded_by=new, supersede_type="contradiction")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_success_sets_metadata_without_touching_content(self, bucket_mgr):
        old = await bucket_mgr.create(content="旧事实原文一字不能改", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])

        result = await bucket_mgr.mark_superseded(
            old, superseded_by=new, supersede_type="contradiction", effective_at="2026-05-01T00:00:00",
        )

        assert result["ok"] is True
        bucket = await bucket_mgr.get(old)
        assert bucket["content"] == "旧事实原文一字不能改"
        assert bucket["metadata"]["superseded_by"] == new
        assert bucket["metadata"]["supersede_type"] == "contradiction"
        assert bucket["metadata"]["supersede_effective_at"] == "2026-05-01T00:00:00"

    @pytest.mark.asyncio
    async def test_effective_at_is_optional(self, bucket_mgr):
        old = await bucket_mgr.create(content="旧事实", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])

        result = await bucket_mgr.mark_superseded(old, superseded_by=new, supersede_type="plan_abandoned")

        assert result["ok"] is True
        bucket = await bucket_mgr.get(old)
        assert "supersede_effective_at" not in bucket["metadata"]


# ============================================================
# _propagate_supersede_staleness：两级精度
# ============================================================
class TestPropagateSupersedeStaleness:
    @pytest.mark.asyncio
    async def test_paragraph_precision_when_citer_file_has_literal_marker(self, bucket_mgr, tmp_path):
        old = await bucket_mgr.create(content="旧事实", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])

        # 引用者是一个文件，正文里有字面 [bucket_id:xxx] 标记。
        files_dir = os.path.join(bucket_mgr.base_dir, "files")
        os.makedirs(files_dir, exist_ok=True)
        note_path = os.path.join(files_dir, "note.md")
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(f"第一行\n提到过 [bucket_id:{old}] 这条\n第三行")
        await bucket_mgr.record_citation(old, source="file:note.md", location="file_save")

        result = await bucket_mgr.mark_superseded(old, superseded_by=new, supersede_type="contradiction")

        assert result["marked_stale_count"] == 1
        entries = await bucket_mgr.get_derived_stale("file:note.md")
        assert len(entries) == 1
        assert entries[0]["precision"] == "paragraph"
        assert entries[0]["location"] == "line:2"

    @pytest.mark.asyncio
    async def test_citation_precision_when_no_literal_marker(self, bucket_mgr):
        old = await bucket_mgr.create(content="旧事实", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])
        citer = await bucket_mgr.create(content="另一条桶，正文里没有字面标记", importance=5, domain=["测试"])
        await bucket_mgr.record_citation(old, source=citer, location="trace")

        result = await bucket_mgr.mark_superseded(old, superseded_by=new, supersede_type="state_transition")

        assert result["marked_stale_count"] == 1
        entries = await bucket_mgr.get_derived_stale(citer)
        assert len(entries) == 1
        assert entries[0]["precision"] == "citation"
        assert entries[0]["location"] == "trace"

    @pytest.mark.asyncio
    async def test_no_citers_means_zero_marked(self, bucket_mgr):
        old = await bucket_mgr.create(content="从没被引用过", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])

        result = await bucket_mgr.mark_superseded(old, superseded_by=new, supersede_type="contradiction")

        assert result["marked_stale_count"] == 0


# ============================================================
# trace_core: superseded_by / supersede_type / supersede_effective_at
# ============================================================
class TestTraceSupersede:
    @pytest.mark.asyncio
    async def test_sets_supersede_via_trace(self, bucket_mgr):
        old = await bucket_mgr.create(content="旧事实", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        result = await trace_core(old, superseded_by=new, supersede_type="plan_completed")

        assert "已标记" in result
        bucket = await bucket_mgr.get(old)
        assert bucket["metadata"]["superseded_by"] == new

    @pytest.mark.asyncio
    async def test_requires_supersede_type(self, bucket_mgr):
        old = await bucket_mgr.create(content="旧事实", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        result = await trace_core(old, superseded_by=new)

        assert "supersede_type" in result
        bucket = await bucket_mgr.get(old)
        assert "superseded_by" not in bucket["metadata"]

    @pytest.mark.asyncio
    async def test_invalid_type_surfaces_readable_error(self, bucket_mgr):
        old = await bucket_mgr.create(content="旧事实", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新事实", importance=5, domain=["测试"])
        install_runtime(bucket_mgr)

        result = await trace_core(old, superseded_by=new, supersede_type="nonsense_type")

        assert "标记 superseded_by 失败" in result


# ============================================================
# 三入口：wake 的"自我"段 / 留言板段 / file_read
# ============================================================
class TestThreeRenderEntryPoints:
    @pytest.mark.asyncio
    async def test_i_bucket_content_shows_staleness_warning(self, bucket_mgr):
        """场景是"这条自我认知引用过的事实过期了"（I 桶是引用者/citer），
        不是"这条自我认知本身被 supersede"——后者读 I 桶自己的
        superseded_by 元数据就够了，不需要 sidecar。derived_freshness
        只管前一种：VIEW 的内容里引用了什么现在该核实的东西。"""
        from tools.i.core import i_core

        install_runtime(bucket_mgr)
        result = await i_core(content="我以前觉得自己不擅长这个", aspect="patterns")
        i_bucket_id = result.split("→")[1].strip()

        old = await bucket_mgr.create(content="被这条自我认知引用过的旧结论", importance=5, domain=["测试"])
        new = await bucket_mgr.create(content="新结论", importance=5, domain=["测试"])
        await bucket_mgr.record_citation(old, source=i_bucket_id, location="i_manual")
        await bucket_mgr.mark_superseded(old, superseded_by=new, supersede_type="state_transition")

        text = await i_core(read=True)

        assert "⚠️" in text
        assert old in text  # 哪个旧事实过期了（stale_by）
        assert new in text  # 被谁取代（replaced_by）

    @pytest.mark.asyncio
    async def test_i_bucket_without_stale_marker_shows_no_warning(self, bucket_mgr):
        from tools.i.core import i_core

        install_runtime(bucket_mgr)
        await i_core(content="正常的自我认知，没被取代", aspect="values")

        text = await i_core(read=True)

        assert "⚠️" not in text

    @pytest.mark.asyncio
    async def test_wake_board_section_shows_staleness_warning(self, test_config, fake_embedding_engine, monkeypatch):
        import server as srv
        from bucket_manager import BucketManager

        mgr = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        monkeypatch.setattr(srv, "bucket_mgr", mgr)
        monkeypatch.setattr(srv, "dream_engine", None)

        board_path = srv._fz_safe("留言板.md")
        os.makedirs(os.path.dirname(board_path), exist_ok=True)
        with open(board_path, "w", encoding="utf-8") as f:
            f.write("给下个窗口的话")

        old = await mgr.create(content="被引用的旧结论", importance=5, domain=["测试"])
        new = await mgr.create(content="新结论", importance=5, domain=["测试"])
        await mgr.record_citation(old, source="file:留言板.md", location="file_save")
        await mgr.mark_superseded(old, superseded_by=new, supersede_type="contradiction")

        text = await srv._wake_impl(48)

        board_section = text.split("## 三、留言板")[1].split("## 四、")[0]
        assert "⚠️" in board_section
        assert old in board_section  # 哪个旧事实过期了（stale_by）
        assert new in board_section  # 被谁取代（replaced_by）

    @pytest.mark.asyncio
    async def test_file_read_shows_staleness_warning(self, test_config, fake_embedding_engine, monkeypatch):
        import server as srv
        from bucket_manager import BucketManager

        mgr = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        monkeypatch.setattr(srv, "bucket_mgr", mgr)

        note_path = srv._fz_safe("笔记.md")
        os.makedirs(os.path.dirname(note_path), exist_ok=True)
        with open(note_path, "w", encoding="utf-8") as f:
            f.write("这是笔记内容")

        old = await mgr.create(content="被笔记引用的旧结论", importance=5, domain=["测试"])
        new = await mgr.create(content="新结论", importance=5, domain=["测试"])
        await mgr.record_citation(old, source="file:笔记.md", location="file_save")
        await mgr.mark_superseded(old, superseded_by=new, supersede_type="contradiction")

        text = await srv._fz_read("笔记.md", 0)

        assert "⚠️" in text
        assert old in text  # 哪个旧事实过期了（stale_by）
        assert new in text  # 被谁取代（replaced_by）

    @pytest.mark.asyncio
    async def test_file_read_without_stale_marker_shows_no_warning(self, test_config, fake_embedding_engine, monkeypatch):
        import server as srv
        from bucket_manager import BucketManager

        mgr = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        monkeypatch.setattr(srv, "bucket_mgr", mgr)

        note_path = srv._fz_safe("干净笔记.md")
        os.makedirs(os.path.dirname(note_path), exist_ok=True)
        with open(note_path, "w", encoding="utf-8") as f:
            f.write("没有任何陈旧引用的笔记")

        text = await srv._fz_read("干净笔记.md", 0)

        assert "⚠️" not in text


# ============================================================
# tools/migrate_legacy_claim_index.py
# ============================================================
class TestMigrateLegacyClaimIndex:
    def _seed_bucket(self, root, subdir, bucket_id, extra_lines, content="正文"):
        d = os.path.join(root, subdir)
        os.makedirs(d, exist_ok=True)
        lines = [f"id: {bucket_id}", "importance: 5", "type: dynamic",
                 "created: 2026-01-01T00:00:00"] + extra_lines
        text = "---\n" + "\n".join(lines) + "\n---\n" + content
        with open(os.path.join(d, f"{bucket_id}.md"), "w", encoding="utf-8") as f:
            f.write(text)

    def _write_file(self, root, name, content):
        path = os.path.join(root, "files", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    @pytest.mark.asyncio
    async def test_scan_flags_marker_pointing_to_superseded_bucket(self, tmp_path):
        from migrate_legacy_claim_index import scan
        from bucket_manager import BucketManager

        self._seed_bucket(
            str(tmp_path), "dynamic", "aaaa11223344",
            ["superseded_by: bbbb99887766", "supersede_type: contradiction"],
        )
        self._seed_bucket(str(tmp_path), "dynamic", "bbbb99887766", [])
        self._write_file(str(tmp_path), "note.md", "提到过 [bucket_id:aaaa11223344] 这条旧结论")

        mgr = BucketManager({"buckets_dir": str(tmp_path)}, embedding_engine=None)
        stats = await scan(mgr, os.path.join(str(tmp_path), "files"))

        assert stats["marker_hits"] == 1
        assert len(stats["candidates"]) == 1
        assert stats["candidates"][0]["bucket_id"] == "aaaa11223344"
        assert stats["candidates"][0]["path"] == "file:note.md"
        assert stats["candidates"][0]["location"] == "line:1"

    @pytest.mark.asyncio
    async def test_scan_ignores_marker_when_bucket_not_superseded(self, tmp_path):
        from migrate_legacy_claim_index import scan
        from bucket_manager import BucketManager

        self._seed_bucket(str(tmp_path), "dynamic", "cccc55667788", [])
        self._write_file(str(tmp_path), "note.md", "提到过 [bucket_id:cccc55667788] 这条正常记忆")

        mgr = BucketManager({"buckets_dir": str(tmp_path)}, embedding_engine=None)
        stats = await scan(mgr, os.path.join(str(tmp_path), "files"))

        assert stats["marker_hits"] == 1
        assert stats["candidates"] == []

    @pytest.mark.asyncio
    async def test_scan_ignores_marker_for_nonexistent_bucket(self, tmp_path):
        from migrate_legacy_claim_index import scan
        from bucket_manager import BucketManager

        self._write_file(str(tmp_path), "note.md", "[bucket_id:deadbeef0000]")
        os.makedirs(os.path.join(str(tmp_path), "dynamic"), exist_ok=True)

        mgr = BucketManager({"buckets_dir": str(tmp_path)}, embedding_engine=None)
        stats = await scan(mgr, os.path.join(str(tmp_path), "files"))

        assert stats["candidates"] == []

    @pytest.mark.asyncio
    async def test_apply_writes_candidate_queue_not_derived_freshness(self, tmp_path):
        """候选队列(LegacyClaimCandidate) 跟 derived_freshness 是两回事——
        不确定匹配不能自动变成会在三入口渲染警示的"已确认陈旧"标记。"""
        from migrate_legacy_claim_index import scan
        from bucket_manager import BucketManager

        self._seed_bucket(
            str(tmp_path), "dynamic", "aaaa11223344",
            ["superseded_by: bbbb99887766", "supersede_type: contradiction"],
        )
        self._seed_bucket(str(tmp_path), "dynamic", "bbbb99887766", [])
        note_path = self._write_file(str(tmp_path), "note.md", "[bucket_id:aaaa11223344]")
        before_mtime = os.path.getmtime(note_path)

        mgr = BucketManager({"buckets_dir": str(tmp_path)}, embedding_engine=None)
        stats = await scan(mgr, os.path.join(str(tmp_path), "files"))
        for c in stats["candidates"]:
            await mgr.queue_legacy_claim_candidate(
                path=c["path"], bucket_id=c["bucket_id"], location=c["location"],
            )

        # 候选队列里能查到，但 derived_freshness（三入口读的那份 sidecar）
        # 应该仍然是空的——迁移脚本本身不产生"确认"，只产生"候选"。
        candidates = await mgr.list_legacy_claim_candidates()
        assert len(candidates) == 1
        assert await mgr.get_derived_stale("file:note.md") == []
        # 且没有碰过被扫描的文件本体。
        assert os.path.getmtime(note_path) == before_mtime

    @pytest.mark.asyncio
    async def test_dry_run_does_not_touch_ledger(self, tmp_path):
        from migrate_legacy_claim_index import scan
        from bucket_manager import BucketManager

        self._seed_bucket(
            str(tmp_path), "dynamic", "aaaa11223344",
            ["superseded_by: bbbb99887766", "supersede_type: contradiction"],
        )
        self._seed_bucket(str(tmp_path), "dynamic", "bbbb99887766", [])
        self._write_file(str(tmp_path), "note.md", "[bucket_id:aaaa11223344]")

        mgr = BucketManager({"buckets_dir": str(tmp_path)}, embedding_engine=None)
        await scan(mgr, os.path.join(str(tmp_path), "files"))  # 只扫描，不调用 queue_*

        assert await mgr.list_legacy_claim_candidates() == []

    @pytest.mark.asyncio
    async def test_same_bucket_id_reported_once_per_file_even_if_marker_repeats(self, tmp_path):
        from migrate_legacy_claim_index import scan
        from bucket_manager import BucketManager

        self._seed_bucket(
            str(tmp_path), "dynamic", "aaaa11223344",
            ["superseded_by: bbbb99887766", "supersede_type: contradiction"],
        )
        self._seed_bucket(str(tmp_path), "dynamic", "bbbb99887766", [])
        self._write_file(
            str(tmp_path), "note.md",
            "[bucket_id:aaaa11223344] 第一次\n又提了一次 [bucket_id:aaaa11223344]",
        )

        mgr = BucketManager({"buckets_dir": str(tmp_path)}, embedding_engine=None)
        stats = await scan(mgr, os.path.join(str(tmp_path), "files"))

        assert len(stats["candidates"]) == 1
