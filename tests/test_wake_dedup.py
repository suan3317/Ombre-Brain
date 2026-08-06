"""wake 组装层单次调用内去重测试（任务书阶段2）。

不导入 server.py（模块级会拉起整个引擎栈），只测 tools/_wake_dedup.py 这个
纯字符串处理模块——它就是 _wake_impl 用来把"核心记忆"段与"最近连续性"段
之间重复出现的桶折叠成一行目录的逻辑。
"""
from tools._wake_dedup import collapse_dupe_buckets, meaning_or_first_line, ID_TAG_RE


def _recent_block(name, bid, meaning_lines=(), content="正文内容"):
    meta_line = f"[{name}] [未解决] 主题:工作 V0.5/A0.3 创建:2026-08-01 最近活跃:2026-08-05"
    body_lines = [f"ID: {bid}"]
    for m in meaning_lines:
        body_lines.append(f"💭 meaning: {m}")
    body_lines.append(content)
    return meta_line + "\n" + "\n".join(body_lines)


def _core_block(bid, name, content="核心正文"):
    return f"📌 [{bid}] {name} 主题:rules 重要:10\n{content}"


def test_id_tag_re_extracts_bucket_ids_from_importance_header_format():
    core_text = "[importance:9] [bucket_id:abc123] " + "[content_role:x] blah\n正文"
    assert ID_TAG_RE.findall(core_text) == ["abc123"]


def test_meaning_or_first_line_prefers_meaning_field():
    body = "💭 meaning: 这是重量所在\n正文第一行\n正文第二行"
    assert meaning_or_first_line(body) == "这是重量所在"


def test_meaning_or_first_line_falls_back_to_first_content_line():
    body = "正文第一行\n正文第二行"
    assert meaning_or_first_line(body) == "正文第一行"


def test_meaning_or_first_line_skips_media_line():
    body = "🖼️ media: foo.png\n真正的第一行"
    assert meaning_or_first_line(body) == "真正的第一行"


def test_collapse_no_seen_ids_is_noop():
    text = "=== Dreaming ===\n" + _recent_block("桶A", "id1")
    assert collapse_dupe_buckets(text, set()) == text


def test_collapse_replaces_duplicate_recent_block_with_one_line():
    header = "=== Dreaming · 过去 48 小时全量记忆（2 个桶）===\n引导语...\n"
    block_dup = _recent_block("已在核心记忆出现过的桶", "dup1", meaning_lines=["这条我记得"])
    block_keep = _recent_block("没重复的桶", "keep1")
    text = header + block_dup + "\n---\n" + block_keep

    out = collapse_dupe_buckets(text, {"dup1"})

    # 重复的桶只剩一行目录，且不再带完整的 meta 头（[未解决] 主题:...）
    assert "[dup1] 已在核心记忆出现过的桶 · 这条我记得" in out
    assert "[未解决]" not in out.split("\n---\n")[0]
    # 没重复的桶原样保留全文（完整 meta 头 + 正文都在）
    keep_part = out.split("\n---\n", 1)[1]
    assert "ID: keep1" in keep_part
    assert "正文内容" in keep_part
    # header 不受影响
    assert out.startswith(header)


def test_collapse_replaces_duplicate_core_context_block():
    long_content = "这条核心准则的原文远远超过五十个字符的目录行截断长度，用来确认折叠后的目录行不会把完整正文原样带出来，只应该截断展示一小段而不是整段塞回结果里，这句话本身也足够长了。"
    text = (
        "=== Dreaming ===\n"
        + _recent_block("桶A", "id1")
        + "\n\n=== 核心准则参考 ===\n"
        + _core_block("dup1", "重复的核心准则", content=long_content)
    )
    out = collapse_dupe_buckets(text, {"dup1"})
    assert "[dup1] 重复的核心准则" in out
    # 折叠后的目录行只截前 50 字，不应该把完整长正文原样带出来
    assert long_content not in out
    # header 格式（主题:/重要:）随着折叠一起消失，不再是原来的多行块
    assert "主题:rules 重要:10" not in out
    # 未被折叠的部分（recent block）保持原样
    assert "正文内容" in out


def test_collapse_leaves_unrelated_sections_untouched():
    text = (
        "=== Dreaming ===\n"
        + _recent_block("桶A", "id1")
        + "\n\n=== 你的 active plans ===\n[plan1] 2026-08-01 一个计划正文"
    )
    out = collapse_dupe_buckets(text, {"id1"})
    # active plans 段完全不受影响
    assert "[plan1] 2026-08-01 一个计划正文" in out
