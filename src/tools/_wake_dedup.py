"""
========================================
tools/_wake_dedup.py — wake 组装层的单次调用内去重（任务书阶段2）
========================================

wake() 把"核心记忆"段（importance_min=8）与"最近连续性"段（dream dispatch，
内含它自己的核心准则参考子段）分别独立取数拼起来；同一个桶完全可能在两段
里都全文出现一遍。这里只在 wake 的组装层做去重——不改 tools/breath、
tools/dream 自身的输出格式，单独调用 breath()/dream() 时它们的行为不变。

纯字符串处理，不依赖 bucket_mgr/rt，便于独立单测。

对外暴露：collapse_dupe_buckets(text, seen_ids) → str
========================================
"""

import re

ID_TAG_RE = re.compile(r"\[bucket_id:([^\]]+)\]")

# 匹配 tools/dream/output.py 里"最近 N 小时"段的单桶块：[名字]...\nID: xxx\n正文...
_RECENT_BLOCK_RE = re.compile(
    r"\[(?P<name>[^\]]*)\][^\n]*\nID: (?P<bid>\S+)\n(?P<body>.*?)(?=\n---\n|\n\n===|\Z)",
    re.DOTALL,
)
# 匹配同一段落里内嵌的"核心准则参考"子段：📌 [id] 名字 主题:... 重要:...\n正文...
_CORE_BLOCK_RE = re.compile(
    r"📌 \[(?P<bid>[^\]]+)\] (?P<name>[^\n]*?) 主题:[^\n]*\n(?P<body>.*?)(?=\n---\n|\n\n===|\Z)",
    re.DOTALL,
)
_MEANING_RE = re.compile(r"💭 meaning: (.+)")


def meaning_or_first_line(body: str) -> str:
    """红线2：目录行只用已有 meaning 字段与标题；meaning 为空则截正文首行，不做生成式摘要。"""
    m = _MEANING_RE.search(body)
    if m:
        return m.group(1).strip()[:50]
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("🖼️ media:"):
            continue
        return line[:50]
    return ""


def collapse_dupe_buckets(text: str, seen_ids: set) -> str:
    """已经在 seen_ids 里全文出现过的 bucket_id，第二次出现时只留一行目录。"""
    if not text or not seen_ids:
        return text

    def _collapse(m):
        bid = m.group("bid")
        if bid not in seen_ids:
            return m.group(0)
        name = m.group("name").strip()
        tail = meaning_or_first_line(m.group("body"))
        return f"[{bid}] {name}" + (f" · {tail}" if tail else "")

    text = _RECENT_BLOCK_RE.sub(_collapse, text)
    text = _CORE_BLOCK_RE.sub(_collapse, text)
    return text
