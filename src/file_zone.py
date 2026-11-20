"""文件区(files/)的文件名规整、改名索引、撞名另存、草稿兜底。

工单 FZ-1(2026-11-19):MCP 的 file_save/file_read/file_list/file_delete
(src/server.py)和 Dashboard 的 /api/files/*(src/web/files_api.py)原来各有
一份一模一样的白名单校验,任何中文标点都会被拒,而拒收时整篇内容已经发出去了。
现在两边都走这里,原则:内容永远不因为文件名被丢——名字不合规就改名字,不拒收。

规整规则(normalize_rel):
- 反斜杠当路径分隔符;整个名字去掉首尾的半角空白(沿用原来的行为)。
- 开头的 "/" 去掉;空段丢掉;超过一层子文件夹时,多出来的 "/" 换成 "_"。
- 每段:控制字符(\\x00-\\x1f、\\x7f)和 Windows 不认的 : * ? " < > | 换成 "_";
  结尾的半角空格和 "." 去掉;开头的 "." 换成 "_"(".." "..." 这种段因此变成 "_");
  每段按 UTF-8 不超过 200 字节,超了截主名、保留后缀,截在字符边界上。
- 中文标点、全角符号、全角空格(U+3000)、不换行空格(U+00A0)、emoji 一律原样保留。
- normalize_rel 是幂等的:规整过的名字再规整一次不变。

改名索引 files/.renamed.json:{实际名: 原名},只记被改过名(含截断)的文件。
- 同一个原名再存 → 索引里找到它的实际名,照常覆盖/追加。
- 改名后的目标已存在且索引里记的不是这个原名(或根本没记)→ 撞名,另存 <主名>_2<后缀>、_3…
- 没改过名的名字照旧同名覆盖(原来的约定)。
- 读/删拿原名时先查索引。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re

try:
    from utils import atomic_write_text, safe_path  # type: ignore
except ImportError:  # pragma: no cover
    from .utils import atomic_write_text, safe_path  # type: ignore

INDEX_NAME = ".renamed.json"
DRAFT_DIR = "草稿"
SEGMENT_MAX_BYTES = 200
_SUFFIX_MAX_BYTES = 40  # 比这还长的"后缀"不当后缀看(比如 "a.一大段话")

_BAD_CHARS_RE = re.compile(r'[\x00-\x1f\x7f:*?"<>|]')
_EDGE_WS = " \t\r\n\f\v"  # 只去首尾半角空白;全角空格、不换行空格算名字的一部分


class FileNameError(ValueError):
    """名字规整后什么都不剩,或者修完仍不在 files/ 里。调用方据此走草稿兜底。"""


# ------------------------------------------------------------------ 规整

def _canon(name: str) -> str:
    return (name or "").strip(_EDGE_WS).replace("\\", "/")


def _truncate_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def _fit_segment(seg: str, extra: str = "") -> str:
    """主名 + extra + 后缀 截到 SEGMENT_MAX_BYTES 以内,后缀保留。"""
    stem, suffix = os.path.splitext(seg)
    if not stem or len(suffix.encode("utf-8")) > _SUFFIX_MAX_BYTES:
        stem, suffix = seg, ""
    if len((stem + extra + suffix).encode("utf-8")) > SEGMENT_MAX_BYTES:
        budget = SEGMENT_MAX_BYTES - len((extra + suffix).encode("utf-8"))
        stem = _truncate_bytes(stem, max(budget, 1)).rstrip(" .")
    return stem + extra + suffix


def _clean_segment(seg: str) -> str:
    seg = _BAD_CHARS_RE.sub("_", seg)
    if "." in seg and not seg.strip(" ."):
        return "_"  # ".." "..." 这种只有点的段换成 "_"
    seg = seg.rstrip(" .")
    if seg.startswith("."):
        seg = "_" + seg[1:]
    seg = _fit_segment(seg)
    return seg.rstrip(" .")


def normalize_rel(name: str) -> str:
    """原名 → 规整后的相对路径(用 "/" 分隔,最多一层子文件夹)。规整完为空则抛 FileNameError。"""
    cleaned = [c for c in (_clean_segment(p) for p in _canon(name).split("/")) if c]
    if len(cleaned) > 2:
        cleaned = [cleaned[0], _clean_segment("_".join(cleaned[1:]))]
    if not cleaned:
        raise FileNameError(name)
    return "/".join(cleaned)


def abs_path(root: str, rel: str) -> str:
    """相对路径 → files/ 下的绝对路径;resolve 后不在 files/ 里就抛 FileNameError。"""
    try:
        safe_path(root, rel)
    except ValueError as e:
        raise FileNameError(str(e)) from e
    return os.path.join(root, *rel.split("/"))


# ------------------------------------------------------------------ 索引

def _index_path(root: str) -> str:
    return os.path.join(root, INDEX_NAME)


def load_index(root: str) -> dict:
    try:
        with open(_index_path(root), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(root: str, index: dict) -> None:
    atomic_write_text(_index_path(root), json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True))


def _lookup_original(index: dict, original: str) -> str | None:
    for actual, orig in index.items():
        if orig == original:
            return actual
    return None


def _variant(rel: str, n: int) -> str:
    folder, _, seg = rel.rpartition("/")
    seg = _fit_segment(seg, f"_{n}")
    return f"{folder}/{seg}" if folder else seg


# ------------------------------------------------------------------ 对外

class WriteTarget:
    """一次写入的落点。rel=实际相对路径;renamed=名字被改过;collided_with=撞上的那个名字。"""

    def __init__(self, root: str, original: str, rel: str, renamed: bool, collided_with: str | None):
        self.root = root
        self.original = original
        self.rel = rel
        self.path = abs_path(root, rel)
        self.renamed = renamed
        self.collided_with = collided_with

    def commit(self) -> None:
        """写入成功后更新索引:改过名的记上;没改过名的若占了索引里的位置,把那条撤掉。"""
        index = load_index(self.root)
        if self.renamed:
            if index.get(self.rel) == self.original:
                return
            index[self.rel] = self.original
        elif self.rel in index:
            index.pop(self.rel)
        else:
            return
        _save_index(self.root, index)


def resolve_for_write(root: str, name: str) -> WriteTarget:
    original = _canon(name)
    rel = normalize_rel(original)
    if rel == original:
        return WriteTarget(root, original, rel, False, None)
    index = load_index(root)
    known = _lookup_original(index, original)
    if known is not None:
        return WriteTarget(root, original, known, True, None)
    candidate, n = rel, 2
    while os.path.lexists(abs_path(root, candidate)) and index.get(candidate) != original:
        candidate = _variant(rel, n)
        n += 1
    return WriteTarget(root, original, candidate, True, rel if candidate != rel else None)


def resolve_for_read(root: str, name: str) -> str:
    """原名或实际名 → 实际相对路径。先查索引,查不到就按规整规则算。"""
    original = _canon(name)
    known = _lookup_original(load_index(root), original)
    if known is not None:
        return known
    return normalize_rel(original)


def forget(root: str, rel: str) -> None:
    """文件删掉后把它在索引里的那条撤掉。"""
    index = load_index(root)
    if rel in index:
        index.pop(rel)
        _save_index(root, index)


def _la_stamp() -> str:
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:  # pragma: no cover - 没有时区库时退回本机时间
        now = _dt.datetime.now()
    return now.strftime("%Y%m%d-%H%M%S")


def write_draft(root: str, content: str) -> str:
    """把内容原样写进 files/草稿/<LA 时间>.md,返回实际相对路径。同一秒撞了就 _2、_3。"""
    stamp = _la_stamp()
    rel = f"{DRAFT_DIR}/{stamp}.md"
    n = 2
    while os.path.lexists(abs_path(root, rel)):
        rel = f"{DRAFT_DIR}/{stamp}_{n}.md"
        n += 1
    path = abs_path(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(content)
    return rel


def _rename_bits(original: str) -> tuple[str, bool]:
    canon = _canon(original)
    bad = sorted(set(_BAD_CHARS_RE.findall(canon)))
    shown = " ".join(c if c.isprintable() else repr(c) for c in bad)
    too_long = any(len(p.encode("utf-8")) > SEGMENT_MAX_BYTES for p in canon.split("/"))
    return shown, too_long


def rename_note(original: str, lang_en: bool) -> str:
    """回执里说明这次改名改了什么。"""
    shown, too_long = _rename_bits(original)
    bits = []
    if lang_en:
        if shown:
            bits.append(f"{shown} in the original name replaced with _")
        if too_long:
            bits.append(f"truncated to {SEGMENT_MAX_BYTES} bytes")
        return "; ".join(bits) or "path / leading-trailing dots and spaces tidied"
    if shown:
        bits.append(f"原名里的 {shown} 换成了 _")
    if too_long:
        bits.append(f"超过 {SEGMENT_MAX_BYTES} 字节的部分截掉了")
    return ",".join(bits) or "路径或开头结尾的点、空格做了修整"
