"""wake 目录化渲染测试（任务书阶段3）：段级预算 + 死配额（预算不足显式
留痕，绝不半截截断）、文件区摘要（top10 + handoff + 历史存档折叠）。"""
from tools._wake_render import render_catalog_segment, render_file_zone_summary


def _bucket(bid, name, importance=8, meaning=None, content="正文"):
    return {
        "id": bid,
        "content": content,
        "metadata": {"name": name, "importance": importance, "meaning": meaning or []},
    }


def test_render_catalog_segment_all_fit_within_budget():
    buckets = [_bucket("a", "桶A"), _bucket("b", "桶B"), _bucket("c", "桶C")]
    lines = render_catalog_segment(buckets, budget_tokens=10000, overflow_hint="hint")
    assert len(lines) == 3
    assert all("未展示" not in ln for ln in lines)
    assert lines[0].startswith("[a] 桶A")


def test_render_catalog_segment_dead_quota_never_cuts_a_line_in_half():
    # 每条目录行本身很短，但预算故意设得只够第一条，逼出死配额分支。
    buckets = [_bucket("a", "桶A"), _bucket("b", "桶B"), _bucket("c", "桶C")]
    line_cost = None
    from utils import count_tokens_approx
    from tools.breath._verbatim import catalog_line
    line_cost = count_tokens_approx(catalog_line(buckets[0]))

    lines = render_catalog_segment(buckets, budget_tokens=line_cost, overflow_hint="用 X 查看")

    # 第一条完整渲染，不是被截断的半条
    assert lines[0] == catalog_line(buckets[0])
    # 剩下两条不进,但显式留痕数量+指路
    assert len(lines) == 2
    assert "还有 2 条未展示" in lines[-1]
    assert "用 X 查看" in lines[-1]


def test_render_catalog_segment_excludes_already_seen_ids():
    buckets = [_bucket("a", "桶A"), _bucket("b", "桶B")]
    lines = render_catalog_segment(buckets, budget_tokens=10000, overflow_hint="", exclude_ids={"a"})
    assert len(lines) == 1
    assert lines[0].startswith("[b]")


def test_render_catalog_segment_empty_budget_reports_all_as_unshown():
    buckets = [_bucket("a", "桶A"), _bucket("b", "桶B")]
    lines = render_catalog_segment(buckets, budget_tokens=0, overflow_hint="查看全部")
    assert len(lines) == 1
    assert "还有 2 条未展示" in lines[0]


def test_render_file_zone_summary_empty():
    assert "文件区是空的" in render_file_zone_summary([])


def test_render_file_zone_summary_top_n_and_handoff_and_archive_collapse():
    entries = []
    # 10 个最近文件（mtime 从大到小），加 1 个更早但是 handoff 的文件
    for i in range(10):
        entries.append((f"note{i}.md", 100, 1000 - i))
    entries.append(("旧 handoff 交接.md", 50, 1))  # mtime 很旧，不在 top10 里，但是交接文件
    entries.append(("另一个不重要的老文件.md", 50, 2))  # 旧且非交接，不该出现
    for i in range(37):
        entries.append((f"搬家前/archive{i}.md", 10, 500))

    out = render_file_zone_summary(entries, top_n=10, archive_prefix="搬家前/")

    # top10 最近文件都在
    for i in range(10):
        assert f"note{i}.md" in out
    # handoff 文件即使不在 top10 时间窗内也要出现
    assert "旧 handoff 交接.md" in out
    # 不重要的旧文件不应该出现
    assert "另一个不重要的老文件.md" not in out
    # 历史存档折叠成一行，不逐个列出
    assert "搬家前/ (37 个历史存档,file_list 可展开)" in out
    assert "archive0.md" not in out
    assert "archive36.md" not in out
    # 总数统计准确（10 + 1 handoff + 1 旧文件 + 37 archive = 49）
    assert "共 49 个文件" in out
    # 死配额:top_n 窗口外、非 handoff 的"另一个不重要的老文件.md"没有被静默丢弃，
    # 数量必须显式留痕
    assert "另有 1 个文件未展示" in out


def test_render_file_zone_summary_no_archive_folder_no_collapse_line():
    entries = [("a.md", 10, 1), ("b.md", 10, 2)]
    out = render_file_zone_summary(entries, top_n=10, archive_prefix="搬家前/")
    assert "历史存档" not in out
