"""
========================================
web/album.py — 相册 REST（A-1）
========================================

Dashboard 鉴权（cookie 会话）：
- GET    /api/album?page=1&page_size=12&order=asc&q=&date_from=&date_to=   分页列表
- POST   /api/album            multipart：file(可选) + title/note/tags/linked_buckets；added_by=user
- GET    /api/album/{id}       单条详情
- PATCH  /api/album/{id}       JSON：title/note/tags/linked_buckets/taken_at
- DELETE /api/album/{id}       她删不需要 AI 批
- GET    /api/album/{id}/file  原图
- GET    /api/album/{id}/thumb 256 缩略 jpeg

Bearer MCP token（OAuth 签发的或 mcp_token 静态密钥，与 /mcp 同一套）：
- POST   /api/album/ingest     multipart：file(必填) + title/note/tags/linked_buckets；added_by 默认 ai
  给 chatnest 的 save_to_album 用——chatnest 本来就拿这把 token 连 Ombre，不新增 secret。
  Ombre 不会去拉任何 URL；图只能被推进来。

对外暴露：register(mcp)。
========================================
"""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh

try:
    import album as _album  # type: ignore
except ImportError:  # pragma: no cover
    from .. import album as _album  # type: ignore

DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE = 100
_FORM_TEXT_MAX = 8000


def _buckets_dir() -> str:
    return str((sh.config or {}).get("buckets_dir", "buckets"))


def _err(e: Exception, status: int = 400) -> Response:
    code = getattr(e, "code", "")
    return JSONResponse({"error": str(e), "code": code}, status_code=status)


def _ingest_authorized(request: Request) -> bool:
    auth = str(request.headers.get("authorization", "") or "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:].strip()
    if not token:
        return False
    from . import oauth as _oauth
    try:
        if _oauth._is_valid_mcp_token(token):
            return True
    except Exception:
        pass
    try:
        return bool(_oauth._is_valid_static_mcp_token(token))
    except Exception:
        return False


async def _read_form(request: Request) -> tuple[bytes | None, dict]:
    """multipart → (图片字节或 None, 文本字段)。文件超过上限直接抛 AlbumError。"""
    content_type = str(request.headers.get("content-type", "") or "")
    fields: dict = {}
    data: bytes | None = None
    if "multipart/form-data" in content_type:
        form = await request.form()
        for key in ("title", "note", "tags", "linked_buckets", "added_by", "taken_at"):
            v = form.get(key)
            if isinstance(v, str):
                fields[key] = v[:_FORM_TEXT_MAX]
        file_field = form.get("file")
        if file_field is not None and not isinstance(file_field, str):
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = await file_field.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > _album.MAX_IMAGE_BYTES:
                    raise _album.AlbumError("OB-AL03", "图片超过 10MB 上限")
                chunks.append(chunk)
            data = b"".join(chunks)
            fields.setdefault("filename", getattr(file_field, "filename", "") or "")
    else:
        body = await sh._read_json_object(request)
        for key in ("title", "note", "tags", "linked_buckets", "added_by", "taken_at"):
            if key in body:
                v = body.get(key)
                fields[key] = v if isinstance(v, list) else str(v or "")[:_FORM_TEXT_MAX]
    return data, fields


def _page_payload(page: int, page_size: int, total: int, rows: list[dict], order: str) -> dict:
    pages = max(1, (total + page_size - 1) // page_size) if page_size else 1
    return {
        "items": [_album.entry_public(e) for e in rows],
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "order": order,
    }


def register(mcp) -> None:

    @mcp.custom_route("/api/album", methods=["GET"])
    async def album_list(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        qp = request.query_params
        try:
            page = max(1, int(qp.get("page", "1") or 1))
            page_size = min(MAX_PAGE_SIZE, max(1, int(qp.get("page_size", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE)))
        except ValueError:
            return JSONResponse({"error": "page / page_size 必须是整数"}, status_code=400)
        order = "asc" if str(qp.get("order", "asc")).lower() == "asc" else "desc"
        rows, total = _album.list_entries(
            _buckets_dir(),
            query=str(qp.get("q", "") or "")[:200],
            date_from=str(qp.get("date_from", "") or "")[:10],
            date_to=str(qp.get("date_to", "") or "")[:10],
            order=order,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return JSONResponse(_page_payload(page, page_size, total, rows, order))

    @mcp.custom_route("/api/album", methods=["POST"])
    async def album_upload(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            data, fields = await _read_form(request)
            entry = _album.add_entry(
                _buckets_dir(), data,
                title=fields.get("title", ""), note=fields.get("note", ""),
                tags=fields.get("tags"), linked_buckets=fields.get("linked_buckets"),
                added_by="user", taken_at=fields.get("taken_at", ""),
            )
        except _album.AlbumError as e:
            return _err(e, 413 if e.code == "OB-AL03" else 400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        status = 200 if entry.get("duplicate") else 201
        return JSONResponse({"ok": True, "entry": _album.entry_public(entry)}, status_code=status)

    @mcp.custom_route("/api/album/ingest", methods=["POST"])
    async def album_ingest(request: Request) -> Response:
        if not _ingest_authorized(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            data, fields = await _read_form(request)
            if data is None:
                raise _album.AlbumError("OB-AL01", "ingest 必须带 file；只记字请用 album_add")
            entry = _album.add_entry(
                _buckets_dir(), data,
                title=fields.get("title", ""), note=fields.get("note", ""),
                tags=fields.get("tags"), linked_buckets=fields.get("linked_buckets"),
                added_by=fields.get("added_by") or "ai", taken_at=fields.get("taken_at", ""),
            )
        except _album.AlbumError as e:
            return _err(e, 413 if e.code == "OB-AL03" else 400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        status = 200 if entry.get("duplicate") else 201
        return JSONResponse({"ok": True, "entry": _album.entry_public(entry)}, status_code=status)

    @mcp.custom_route("/api/album/{album_id}", methods=["GET"])
    async def album_detail(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        entry = _album.get_entry(_buckets_dir(), request.path_params.get("album_id", ""))
        if not entry:
            return JSONResponse({"error": "相册里没有这条"}, status_code=404)
        return JSONResponse({"entry": _album.entry_public(entry)})

    @mcp.custom_route("/api/album/{album_id}", methods=["PATCH"])
    async def album_patch(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception as e:
            return JSONResponse({"error": f"请求体不是 JSON 对象：{e}"}, status_code=400)
        fields = {k: body[k] for k in ("title", "note", "tags", "linked_buckets", "taken_at") if k in body}
        if not fields:
            return JSONResponse({"error": "没有可改的字段（title/note/tags/linked_buckets/taken_at）"}, status_code=400)
        try:
            entry = _album.update_entry(_buckets_dir(), request.path_params.get("album_id", ""), **fields)
        except _album.AlbumError as e:
            return _err(e, 404)
        return JSONResponse({"ok": True, "entry": _album.entry_public(entry)})

    @mcp.custom_route("/api/album/{album_id}", methods=["DELETE"])
    async def album_delete(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            removed = _album.delete_entry(_buckets_dir(), request.path_params.get("album_id", ""))
        except _album.AlbumError as e:
            return _err(e, 404)
        return JSONResponse({"ok": True, "deleted": removed.get("id")})

    @mcp.custom_route("/api/album/{album_id}/file", methods=["GET"])
    async def album_file(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        entry = _album.get_entry(_buckets_dir(), request.path_params.get("album_id", ""))
        path = _album.file_path(_buckets_dir(), entry) if entry else None
        if not entry or not path or not os.path.isfile(path):
            return JSONResponse({"error": "这条没有图"}, status_code=404)
        with open(path, "rb") as f:
            data = f.read()
        return Response(
            content=data,
            media_type=entry.get("mime") or "application/octet-stream",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @mcp.custom_route("/api/album/{album_id}/thumb", methods=["GET"])
    async def album_thumb(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        entry = _album.get_entry(_buckets_dir(), request.path_params.get("album_id", ""))
        data = _album.thumb_bytes(_buckets_dir(), entry) if entry else None
        if data is None:
            return JSONResponse({"error": "这条没有图"}, status_code=404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})
