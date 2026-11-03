"""
========================================
tools/album.py — 相册的三个 MCP 工具（A-1）
========================================

- album_add  ：只记 title/note/tags/关联桶，**不带图**。官端（claude.ai）传不了图像数据，
               Ombre 也不去拉任何 URL；Everlight 房间里要存她发的图走 chatnest 的
               save_to_album → POST /api/album/ingest（见 web/album.py）。
- album_list ：按时间倒序列条目（id、时间、标题、note 摘要、tags、缩略 url）。默认不带图；
               with_thumbs=True 时每条附一张 256 缩略的 ImageContent。
- album_view ：看一张——返回 1024 长边 jpeg q80 的 ImageContent + 文字元数据。
               一张约 1500 token，要看再调。

返回形态：纯文本工具返回 str；带图的返回 list[ContentBlock]（文本块 + 图片块），
FastMCP 原样下发。server.py 负责 _with_notice 包装文本部分。

对外暴露：album_add / album_list / album_view / image_block
========================================
"""

from __future__ import annotations

import base64
from typing import Optional

from . import _runtime as rt

try:
    import album as _album  # type: ignore
except ImportError:  # pragma: no cover
    from .. import album as _album  # type: ignore


def _buckets_dir() -> str:
    cfg = rt.config or {}
    try:
        return str(cfg.get("buckets_dir", "buckets"))
    except Exception:
        return "buckets"


def image_block(data: bytes, mime: str = "image/jpeg"):
    from mcp.types import ImageContent
    return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType=mime)


def text_block(text: str):
    from mcp.types import TextContent
    return TextContent(type="text", text=text)


def _when(entry: dict) -> str:
    added = str(entry.get("added_at") or "")
    out = added[:16].replace("T", " ") if added else "?"
    taken = str(entry.get("taken_at") or "")
    if taken and taken[:10] != added[:10]:
        out += f"（拍摄 {taken[:10]}）"
    return out


def _who(entry: dict) -> str:
    return "她存的" if entry.get("added_by") == "user" else "我存的"


def _line(entry: dict) -> str:
    title = entry.get("title") or "（无题）"
    bits = [f"[{entry['id']}] {_when(entry)} · {title} · {_who(entry)}"]
    if not entry.get("file"):
        bits.append("（只有字，没有图）")
    note = str(entry.get("note") or "").strip().replace("\n", " ")
    if note:
        bits.append(f"\n    note: {note[:140]}{'…' if len(note) > 140 else ''}")
    tags = entry.get("tags") or []
    if tags:
        bits.append(f"\n    tags: {', '.join(tags)}")
    linked = entry.get("linked_buckets") or []
    if linked:
        bits.append(f"\n    关联桶: {', '.join(linked)}")
    return "".join(bits)


def _detail(entry: dict) -> str:
    lines = [
        f"[{entry['id']}] {entry.get('title') or '（无题）'}",
        f"存入: {entry.get('added_at') or '?'} · {_who(entry)}",
    ]
    if entry.get("taken_at"):
        lines.append(f"拍摄: {entry['taken_at']}")
    if entry.get("file"):
        lines.append(f"原图: {entry.get('width')}×{entry.get('height')} {entry.get('mime')} {int(entry.get('bytes') or 0) // 1024}KB")
    else:
        lines.append("（只有字，没有图）")
    if entry.get("note"):
        lines.append(f"note: {entry['note']}")
    if entry.get("tags"):
        lines.append(f"tags: {', '.join(entry['tags'])}")
    if entry.get("linked_buckets"):
        lines.append(f"关联桶: {', '.join(entry['linked_buckets'])}")
    return "\n".join(lines)


async def album_add(
    title: str = "",
    note: str = "",
    tags: Optional[str] = "",
    linked_buckets: Optional[str] = "",
) -> str:
    try:
        entry = _album.add_entry(
            _buckets_dir(), None,
            title=title, note=note, tags=tags, linked_buckets=linked_buckets, added_by="ai",
        )
    except _album.AlbumError as e:
        return f"❌ [{e.code}] {e}"
    return f"📷 album→{entry['id']} · {entry.get('title') or '（无题）'} · 只记了字（图要在 Everlight 里用 save_to_album 存）"


async def album_list(
    query: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
    limit: Optional[int] = 20,
    with_thumbs: Optional[bool] = False,
):
    try:
        n = max(1, min(int(limit or 20), 100))
    except (TypeError, ValueError):
        n = 20
    rows, total = _album.list_entries(
        _buckets_dir(),
        query=str(query or "")[:200],
        date_from=str(date_from or "")[:10],
        date_to=str(date_to or "")[:10],
        order="desc",
        limit=n,
    )
    if not rows:
        text = "相册里还没有东西。" if not (query or date_from or date_to) else "没有符合条件的照片。"
        return text
    head = f"相册 共 {total} 条，显示最近 {len(rows)} 条（要看图：album_view(id)）"
    if not with_thumbs:
        return head + "\n" + "\n".join(_line(e) for e in rows)
    blocks = [text_block(head)]
    for e in rows:
        blocks.append(text_block(_line(e)))
        data = _album.thumb_bytes(_buckets_dir(), e) if e.get("file") else None
        if data:
            blocks.append(image_block(data))
    return blocks


async def album_view(album_id: str):
    entry = _album.get_entry(_buckets_dir(), album_id)
    if not entry:
        return f"❌ [OB-AL04] 相册里没有 {album_id}"
    text = _detail(entry)
    if not entry.get("file"):
        return text
    data = _album.view_bytes(_buckets_dir(), entry)
    if not data:
        return text + "\n（原图文件丢了，只剩索引）"
    return [image_block(data), text_block(text)]
