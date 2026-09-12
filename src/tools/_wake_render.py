"""
========================================
tools/_wake_render.py — wake 目录化渲染（任务书阶段3）
========================================

wake 的"核心记忆"段与"最近连续性"段过去是把候选桶的存储正文整段拼接后
一次性截断——低权重条目和高权重条目挤在同一个字符预算里，谁先渲染谁占位，
砍的时候连正在渲染到一半的条目都可能被腰斩。

这里改成"每条要么完整渲染要么整条不进"的目录段渲染器：桶数据来自调用方
（bucket_mgr / collect_candidates 等，不在本模块做 I/O），逐条转成
catalog_line() 目录行，按 token 预算累加；预算不够时不再继续渲染下一条，
而是显式追加一行"还有 N 条未展示"的提示——不静默丢弃、不做半截截断。

不做什么（边界）：
- 不判定桶是否该被浮现（候选集由调用方决定）
- 不做任何 LLM 生成式摘要（catalog_line 只用已有 meaning/标题）
- 不依赖 bucket_mgr/rt，纯函数，脱离 server.py 独立单测

对外暴露：
  render_catalog_segment(buckets, budget_tokens, overflow_hint, exclude_ids) -> list[str]
  render_file_zone_summary(entries, top_n, archive_prefix) -> str
========================================
"""

from datetime import datetime

from tools.breath._verbatim import catalog_line
from utils import count_tokens_approx, t as _t


def render_catalog_segment(
    buckets: list,
    budget_tokens: int,
    overflow_hint: str,
    exclude_ids: set | None = None,
) -> list:
    """把桶列表渲染成目录行列表，遵守"死配额"规则：预算不够就整条不进，
    绝不切半；被跳过的数量显式留痕在最后一行，附 overflow_hint 指路怎么补看。
    """
    exclude_ids = exclude_ids or set()
    pool = [b for b in buckets if b.get("id") not in exclude_ids]
    lines = []
    used = 0
    for b in pool:
        line = catalog_line(b)
        cost = count_tokens_approx(line)
        if used + cost > budget_tokens:
            break
        lines.append(line)
        used += cost
    remaining = len(pool) - len(lines)
    if remaining > 0:
        lines.append(_t(
            f"...还有 {remaining} 条未展示(预算不足)。{overflow_hint}",
            f"...{remaining} more not shown (budget insufficient). {overflow_hint}",
        ))
    return lines


def render_file_zone_summary(entries: list, top_n: int = 10, archive_prefix: str = "搬家前/") -> str:
    """entries: list[(rel_path, size_bytes, mtime)]。

    默认只列最近修改的 top_n 个文件 + 所有 handoff/交接文件（哪怕不在最近
    修改之列）；archive_prefix 开头的历史存档文件夹整体折叠成一行摘要，
    完整展开用 file_list(folder=archive_prefix)。
    """
    if not entries:
        return _t(
            "文件区是空的。用 file_save 存入第一个文件。",
            "The file zone is empty. Use file_save to store the first file.",
        )

    archived = [e for e in entries if e[0].startswith(archive_prefix)]
    others = [e for e in entries if not e[0].startswith(archive_prefix)]
    handoff = [e for e in others if "handoff" in e[0].lower() or "交接" in e[0]]

    others_by_mtime = sorted(others, key=lambda e: e[2], reverse=True)
    top = others_by_mtime[:top_n]
    shown_rel = {e[0] for e in top}
    extra_handoff = [e for e in handoff if e[0] not in shown_rel]

    def _row(e):
        rel, size, mt = e
        when = datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
        return _t(f"- {rel}  ({size} 字节, 改于 {when})", f"- {rel}  ({size} bytes, modified {when})")

    rows = [_row(e) for e in (top + extra_handoff)]
    if archived:
        rows.append(_t(
            f"{archive_prefix} ({len(archived)} 个历史存档,file_list 可展开)",
            f"{archive_prefix} ({len(archived)} archived, expand with file_list)",
        ))

    total = len(entries)
    shown_count = len(top) + len(extra_handoff)
    # 死配额规则(阶段3步骤5,适用于所有段):被折叠进"历史存档"的文件已经算
    # 有显式留痕(那一行摘要本身就是留痕);但 top_n 窗口之外、又不是
    # handoff/交接的文件如果就这样不出现，等于无标记删减——必须显式点出数量。
    unlisted = total - shown_count - len(archived)
    if unlisted > 0:
        rows.append(_t(
            f"...另有 {unlisted} 个文件未展示(不在最近 {top_n} 个之列)。完整列表用 file_list 查看。",
            f"...{unlisted} more files not shown (outside the most recent {top_n}). "
            "See the full list with file_list.",
        ))

    header = _t(f"文件区共 {total} 个文件(默认显示最近改动的 {len(top)} 个",
                f"The file zone has {total} files (showing the {len(top)} most recently changed by default")
    if extra_handoff:
        header += _t(f" + {len(extra_handoff)} 个交接文件", f" + {len(extra_handoff)} handoff files")
    if archived:
        header += _t("，历史存档已折叠", ", archived history folded")
    header += _t("；完整列表用 file_list 查看):", "; see the full list with file_list):")
    return header + "\n" + "\n".join(rows)
