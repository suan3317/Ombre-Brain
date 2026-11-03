"""
========================================
album.py — 相册（A-1）：各存各的，手动存
========================================

每台 Ombre 自己一本相册，图片文件落在自己的卷里：

    <buckets_dir>/album/<yyyy>/<id>.<ext>     原图（jpg/png/webp/gif 原样存，不转码）
    <buckets_dir>/album/index.jsonl           索引，一行一条（随 GitHub 同步；图不同步）
    <buckets_dir>/album/.thumbs/<id>_<n>.jpg  缩略图缓存（可随时删，缺了重算）

一条索引记录（entry）的字段：
    id            12 位十六进制
    file          相对 album/ 的路径；只有 title/note、没有图的条目为 ""
    title         一行短标题（可空；空时前端用时间当标题）
    note          详情里才显示的长说明
    tags          list[str]
    linked_buckets list[str]   关联的桶 id（只存字符串，不校验桶是否还在）
    added_by      "user" | "ai"
    added_at      存入时间，ISO 8601 带洛杉矶时区偏移
    taken_at      EXIF DateTimeOriginal（有就记，"YYYY-MM-DDTHH:MM:SS"，无时区），否则 ""
    sha256 / bytes / width / height / mime

设计边界（工单 A-1 + 2026-11-03 裁定）：
- 全部手动：没有任何"收到图就存"的自动逻辑。
- 不做人脸/内容识别，不做跨人共享；heic 不做（不转码、也不收）。
- 同一张图（sha256 相同）不重复入册：add_entry 返回已有条目并标 duplicate=True。
- 本模块不依赖 bucket_mgr / FastMCP，只认 buckets_dir；REST 与 MCP 两侧共用。

对外暴露：
    album_root / index_path / add_entry / list_entries / get_entry /
    update_entry / delete_entry / thumb_bytes / view_bytes / entry_public
========================================
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import tempfile
from datetime import datetime
from typing import Iterable

MAX_IMAGE_BYTES = 10 * 1024 * 1024          # 单图上限 10MB
TITLE_MAX = 120
NOTE_MAX = 4000
TAG_MAX_ITEMS = 20
TAG_MAX_LEN = 40
LINKED_MAX_ITEMS = 20
THUMB_SIDE = 256                              # album_list with_thumbs / dashboard 网格
VIEW_SIDE = 1024                              # album_view 给模型看的长边
VIEW_QUALITY = 80

# 允许的格式：Pillow 识别出的 format 名 → (扩展名, mime)
_FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
}
_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class AlbumError(ValueError):
    """用户可见的错误（格式不对、太大、找不到……），带稳定的 code。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 路径

def album_root(buckets_dir: str) -> str:
    root = os.path.join(buckets_dir or "buckets", "album")
    os.makedirs(root, exist_ok=True)
    return root


def index_path(buckets_dir: str) -> str:
    return os.path.join(album_root(buckets_dir), "index.jsonl")


def _thumb_dir(buckets_dir: str) -> str:
    d = os.path.join(album_root(buckets_dir), ".thumbs")
    os.makedirs(d, exist_ok=True)
    return d


def file_path(buckets_dir: str, entry: dict) -> str | None:
    rel = str(entry.get("file") or "").replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel:
        return None
    return os.path.join(album_root(buckets_dir), *rel.split("/"))


# ---------------------------------------------------------------- 时间

def _now_iso() -> str:
    try:
        from tools.plan.lock import lock_timezone  # 与信件锁同一套洛杉矶时区
        return datetime.now(lock_timezone()).isoformat(timespec="seconds")
    except Exception:  # pragma: no cover
        return datetime.now().astimezone().isoformat(timespec="seconds")


def _exif_taken_at(img) -> str:
    """EXIF DateTimeOriginal(36867) / DateTime(306)：'2026:11:03 12:34:56' → ISO 无时区。"""
    try:
        exif = img.getexif()
    except Exception:
        return ""
    raw = ""
    try:
        raw = str(exif.get(36867) or "")
        if not raw:
            ifd = exif.get_ifd(0x8769)
            raw = str(ifd.get(36867) or "")
    except Exception:
        raw = ""
    if not raw:
        try:
            raw = str(exif.get(306) or "")
        except Exception:
            raw = ""
    m = re.match(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", raw.strip())
    if not m:
        return ""
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


# ---------------------------------------------------------------- 文本清洗

def _clean_line(value, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _clean_text(value, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    return text[:limit]


def _split_list(value, *, max_items: int, max_len: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，;；\n]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(p) for p in value]
    else:
        parts = [str(value)]
    out: list[str] = []
    for p in parts:
        p = p.strip()[:max_len]
        if p and p not in out:
            out.append(p)
        if len(out) >= max_items:
            break
    return out


def normalize_added_by(value) -> str:
    v = str(value or "").strip().lower()
    return "user" if v in ("user", "human", "silvia", "她") else "ai"


# ---------------------------------------------------------------- 索引读写

def _read_index(buckets_dir: str) -> list[dict]:
    path = index_path(buckets_dir)
    if not os.path.isfile(path):
        return []
    entries: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("id"):
                entries.append(obj)
    return entries


def _append_index(buckets_dir: str, entry: dict) -> None:
    with open(index_path(buckets_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _rewrite_index(buckets_dir: str, entries: Iterable[dict]) -> None:
    path = index_path(buckets_dir)
    fd, tmp = tempfile.mkstemp(prefix=".index.", suffix=".jsonl", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 图片解码

# A-1 追加 4（2026-11-03，Rio 查出来的）：iPhone 照片（HEIC 转出的 jpg，带 HDR 增益图
# 或人像景深图）是多帧 JPEG，Pillow 识别为 MPO，以前走到 _FORMATS 就被拒。处理：只取
# 第 0 帧，存成普通 jpg（量化表/采样跟原图一致，分辨率画质不变），EXIF 原样带走，只丢
# 增益层。gif/webp 的多帧是动画，不在这条路上，照旧原样存。
_MULTI_FRAME_JPEG = {"MPO"}


def _first_frame_jpeg(img) -> bytes:
    """多帧 JPEG（MPO）→ 第 0 帧的普通 jpg 字节。"""
    from PIL import JpegImagePlugin
    try:
        img.seek(0)
    except Exception:
        pass
    frame = img.copy()
    if frame.mode not in ("RGB", "L"):
        frame = frame.convert("RGB")
    kw: dict = {"format": "JPEG"}
    exif = img.info.get("exif")
    if exif:
        kw["exif"] = exif
    icc = img.info.get("icc_profile")
    if icc:
        kw["icc_profile"] = icc
    out = io.BytesIO()
    try:
        frame.save(
            out,
            qtables=getattr(img, "quantization", None),
            subsampling=JpegImagePlugin.get_sampling(img),
            **kw,
        )
    except Exception:
        out = io.BytesIO()
        frame.save(out, quality=95, **kw)
    return out.getvalue()


def _open_image(data: bytes):
    """→ (img, fmt, data)。MPO 会在这里被换成第 0 帧的 jpg，调用方要用返回的 data 落盘。"""
    from PIL import Image, UnidentifiedImageError
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise AlbumError("OB-AL02", f"不是能识别的图片（只收 jpg/png/webp/gif）：{e}") from None
    fmt = (img.format or "").upper()
    if fmt in _MULTI_FRAME_JPEG:
        data = _first_frame_jpeg(img)
        img = Image.open(io.BytesIO(data))
        img.load()
        fmt = "JPEG"
    if fmt not in _FORMATS:
        raise AlbumError("OB-AL02", f"不收 {fmt or '未知'} 格式，只收 jpg/png/webp/gif")
    return img, fmt, data


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 增

def add_entry(
    buckets_dir: str,
    data: bytes | None,
    *,
    title: str = "",
    note: str = "",
    tags=None,
    linked_buckets=None,
    added_by: str = "user",
    taken_at: str = "",
) -> dict:
    """存一条。data=None 时是"只有字、没有图"的条目（官端 AI 走这条）。

    返回 entry；若同一张图已经在册，返回已有条目并带 duplicate=True（不重复落盘）。
    """
    title = _clean_line(title, TITLE_MAX)
    note = _clean_text(note, NOTE_MAX)
    tags = _split_list(tags, max_items=TAG_MAX_ITEMS, max_len=TAG_MAX_LEN)
    linked = _split_list(linked_buckets, max_items=LINKED_MAX_ITEMS, max_len=64)
    added_by = normalize_added_by(added_by)
    if data is None and not title and not note:
        raise AlbumError("OB-AL01", "没有图也没有字，没什么可存的")

    entry: dict = {
        "id": secrets.token_hex(6),
        "file": "",
        "title": title,
        "note": note,
        "tags": tags,
        "linked_buckets": linked,
        "added_by": added_by,
        "added_at": _now_iso(),
        "taken_at": _clean_line(taken_at, 32),
        "sha256": "",
        "bytes": 0,
        "width": 0,
        "height": 0,
        "mime": "",
    }

    if data is not None:
        if len(data) > MAX_IMAGE_BYTES:
            raise AlbumError("OB-AL03", f"图片 {len(data) // 1024}KB 超过 10MB 上限")
        if not data:
            raise AlbumError("OB-AL02", "空文件")
        digest = _sha256(data)
        for old in _read_index(buckets_dir):
            if old.get("sha256") == digest:
                dup = dict(old)
                dup["duplicate"] = True
                return dup
        img, fmt, data = _open_image(data)   # MPO 在这里已换成第 0 帧 jpg
        ext, mime = _FORMATS[fmt]
        year = entry["added_at"][:4]
        rel = f"{year}/{entry['id']}.{ext}"
        abs_path = os.path.join(album_root(buckets_dir), year, f"{entry['id']}.{ext}")
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "wb") as f:
            f.write(data)
        entry.update(
            file=rel,
            sha256=digest,
            bytes=len(data),
            width=int(img.width),
            height=int(img.height),
            mime=mime,
        )
        if not entry["taken_at"]:
            entry["taken_at"] = _exif_taken_at(img)

    _append_index(buckets_dir, entry)
    return entry


# ---------------------------------------------------------------- 查

def _in_range(entry: dict, date_from: str, date_to: str) -> bool:
    day = str(entry.get("added_at") or "")[:10]
    if date_from and day < date_from[:10]:
        return False
    if date_to and day > date_to[:10]:
        return False
    return True


def _matches(entry: dict, query: str) -> bool:
    if not query:
        return True
    q = query.lower()
    hay = " ".join([
        str(entry.get("title") or ""),
        str(entry.get("note") or ""),
        " ".join(entry.get("tags") or []),
        " ".join(entry.get("linked_buckets") or []),
    ]).lower()
    return all(part in hay for part in q.split())


def list_entries(
    buckets_dir: str,
    *,
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    order: str = "desc",
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """按 added_at 排序后分页。返回 (本页条目, 过滤后的总数)。"""
    # 平手（同一秒存的）按入册顺序，不按随机 id
    indexed = [(i, e) for i, e in enumerate(_read_index(buckets_dir))
               if _in_range(e, date_from, date_to) and _matches(e, query)]
    indexed.sort(key=lambda t: (str(t[1].get("added_at") or ""), t[0]),
                 reverse=(order != "asc"))
    rows = [e for _, e in indexed]
    total = len(rows)
    offset = max(0, int(offset or 0))
    if limit is not None:
        rows = rows[offset: offset + max(0, int(limit))]
    elif offset:
        rows = rows[offset:]
    return rows, total


def get_entry(buckets_dir: str, entry_id: str) -> dict | None:
    entry_id = str(entry_id or "").strip().lower()
    if not _ID_RE.match(entry_id):
        return None
    for e in _read_index(buckets_dir):
        if e.get("id") == entry_id:
            return e
    return None


# ---------------------------------------------------------------- 改 / 删

def update_entry(buckets_dir: str, entry_id: str, **fields) -> dict:
    """只允许改 title / note / tags / linked_buckets / taken_at。"""
    entries = _read_index(buckets_dir)
    target = None
    for e in entries:
        if e.get("id") == str(entry_id or "").strip().lower():
            target = e
            break
    if target is None:
        raise AlbumError("OB-AL04", f"相册里没有 {entry_id}")
    if "title" in fields:
        target["title"] = _clean_line(fields["title"], TITLE_MAX)
    if "note" in fields:
        target["note"] = _clean_text(fields["note"], NOTE_MAX)
    if "tags" in fields:
        target["tags"] = _split_list(fields["tags"], max_items=TAG_MAX_ITEMS, max_len=TAG_MAX_LEN)
    if "linked_buckets" in fields:
        target["linked_buckets"] = _split_list(fields["linked_buckets"], max_items=LINKED_MAX_ITEMS, max_len=64)
    if "taken_at" in fields:
        target["taken_at"] = _clean_line(fields["taken_at"], 32)
    target["updated_at"] = _now_iso()
    _rewrite_index(buckets_dir, entries)
    return target


def delete_entry(buckets_dir: str, entry_id: str) -> dict:
    entries = _read_index(buckets_dir)
    kept, removed = [], None
    for e in entries:
        if removed is None and e.get("id") == str(entry_id or "").strip().lower():
            removed = e
        else:
            kept.append(e)
    if removed is None:
        raise AlbumError("OB-AL04", f"相册里没有 {entry_id}")
    _rewrite_index(buckets_dir, kept)
    path = file_path(buckets_dir, removed)
    if path and os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError:
            pass
    for side in (THUMB_SIDE, VIEW_SIDE):
        cached = os.path.join(_thumb_dir(buckets_dir), f"{removed['id']}_{side}.jpg")
        if os.path.isfile(cached):
            try:
                os.unlink(cached)
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------- 缩略 / 压缩

def _shrink_jpeg(data: bytes, max_side: int, quality: int) -> bytes:
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(data))
    try:
        img.seek(0)                      # gif 取第一帧
    except Exception:
        pass
    img = ImageOps.exif_transpose(img)   # 按 EXIF 方向摆正
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.getchannel("A"))
        img = bg
    elif img.mode == "L":
        img = img.convert("RGB")
    w, h = img.size
    scale = max(w, h) / float(max_side)
    if scale > 1:
        img = img.resize((max(1, round(w / scale)), max(1, round(h / scale))), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _cached_jpeg(buckets_dir: str, entry: dict, side: int, quality: int) -> bytes | None:
    src = file_path(buckets_dir, entry)
    if not src or not os.path.isfile(src):
        return None
    cache = os.path.join(_thumb_dir(buckets_dir), f"{entry['id']}_{side}.jpg")
    try:
        if os.path.isfile(cache) and os.path.getmtime(cache) >= os.path.getmtime(src):
            with open(cache, "rb") as f:
                return f.read()
    except OSError:
        pass
    with open(src, "rb") as f:
        data = _shrink_jpeg(f.read(), side, quality)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".t.", suffix=".jpg", dir=os.path.dirname(cache))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, cache)
    except OSError:
        pass
    return data


def thumb_bytes(buckets_dir: str, entry: dict) -> bytes | None:
    """256 长边 jpeg（网格封面 / album_list with_thumbs）。没图的条目返回 None。"""
    return _cached_jpeg(buckets_dir, entry, THUMB_SIDE, 75)


def view_bytes(buckets_dir: str, entry: dict) -> bytes | None:
    """1024 长边 jpeg q80（album_view 给模型看）。没图的条目返回 None。"""
    return _cached_jpeg(buckets_dir, entry, VIEW_SIDE, VIEW_QUALITY)


# ---------------------------------------------------------------- 对外视图

def entry_public(entry: dict) -> dict:
    """REST / MCP 文本层共用的条目视图：不含落盘路径，带 thumb/file url。"""
    has_file = bool(entry.get("file"))
    return {
        "id": entry.get("id"),
        "title": entry.get("title") or "",
        "note": entry.get("note") or "",
        "tags": list(entry.get("tags") or []),
        "linked_buckets": list(entry.get("linked_buckets") or []),
        "added_by": entry.get("added_by") or "user",
        "added_at": entry.get("added_at") or "",
        "taken_at": entry.get("taken_at") or "",
        "updated_at": entry.get("updated_at") or "",
        "has_image": has_file,
        "width": entry.get("width") or 0,
        "height": entry.get("height") or 0,
        "bytes": entry.get("bytes") or 0,
        "mime": entry.get("mime") or "",
        "thumb_url": f"/api/album/{entry.get('id')}/thumb" if has_file else "",
        "file_url": f"/api/album/{entry.get('id')}/file" if has_file else "",
        "duplicate": bool(entry.get("duplicate")),
    }
