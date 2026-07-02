"""文件区 Web 管理页 —— 浏览器这一侧的门。

- GET  /files                     文件管理页面(需 Dashboard 登录,未登录跳转登录页)
- GET  /api/files/list            JSON 文件列表
- PUT  /api/files/raw?name=xx     上传文件(请求体=文件原始内容,免 multipart 依赖)
- GET  /api/files/download?name=  下载/查看文件
- POST /api/files/delete?name=    删除文件

存储位置: <buckets_dir>/files/ ,与 MCP 端 file_save / file_read / file_list / file_delete
是同一个柜子:这边传的那边能读,那边存的这边能下载。
.md 文件随 GitHub 同步自动备份。
"""

import os
import re
import datetime as _dt

from starlette.requests import Request
from starlette.responses import Response, JSONResponse, HTMLResponse, RedirectResponse

from . import _shared as sh

_MAX_UPLOAD = 10 * 1024 * 1024  # 网页上传上限 10MB


def _root() -> str:
    root = os.path.join(sh.config.get("buckets_dir", "buckets"), "files")
    os.makedirs(root, exist_ok=True)
    return root


def _safe(name: str) -> str:
    name = (name or "").strip().replace("\\", "/")
    if not name or name.startswith("/") or ".." in name:
        raise ValueError(f"非法文件名: {name!r}")
    parts = [p for p in name.split("/") if p]
    if len(parts) > 2:
        raise ValueError(f"最多一层子文件夹: {name}")
    for p in parts:
        if not re.match(r"^[\w\u4e00-\u9fff.\- ]{1,80}$", p) or p.startswith("."):
            raise ValueError(f"文件名含非法字符: {p!r}")
    return os.path.join(_root(), *parts)


def _listing() -> list[dict]:
    root = _root()
    rows: list[dict] = []
    for r, dirs, fnames in os.walk(root):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
        for fn in sorted(fnames):
            if fn.startswith("."):
                continue
            p = os.path.join(r, fn)
            st = os.stat(p)
            rows.append({
                "name": os.path.relpath(p, root).replace("\\", "/"),
                "size": st.st_size,
                "mtime": _dt.datetime.utcfromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M UTC"),
            })
    return rows


_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件区 · Ombre Brain</title>
<style>
  body { font-family: ui-monospace, Menlo, Consolas, monospace; background:#f4f1ea;
         color:#3a3630; max-width:820px; margin:32px auto; padding:0 16px; }
  h1 { font-size:20px; } a { color:#8a6d3b; }
  .card { background:#faf8f3; border-radius:12px; padding:16px 20px; margin:14px 0;
          box-shadow:2px 2px 6px rgba(0,0,0,.08); }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  td,th { padding:7px 6px; border-bottom:1px solid #e4dfd4; text-align:left; }
  button { background:#efe9dd; border:none; border-radius:8px; padding:6px 12px;
           cursor:pointer; font-family:inherit; }
  button:hover { background:#e3dbc9; }
  .del { color:#a33; } input[type=text] { padding:6px 8px; border:1px solid #d8d2c4;
         border-radius:8px; background:#fff; font-family:inherit; }
  #msg { min-height:20px; font-size:13px; color:#6b6357; }
</style></head><body>
<h1>文件区</h1>
<p style="font-size:13px;color:#6b6357">这里和 MCP 端的 file_save / file_read 是同一个柜子。
你传的文件那边能读,那边存的日记这里能下载。.md 文件随 GitHub 同步自动备份。
<a href="/dashboard">← 返回 Dashboard</a></p>
<div class="card">
  <b>上传</b><br><br>
  <input type="file" id="f" multiple>
  子文件夹(可选): <input type="text" id="folder" placeholder="如 diary" size="10">
  <button onclick="up()">上传</button>
  <div id="msg"></div>
</div>
<div class="card"><b>文件列表</b>
  <table><thead><tr><th>文件</th><th>大小</th><th>修改时间(UTC)</th><th></th></tr></thead>
  <tbody id="rows"></tbody></table>
</div>
<script>
async function refresh() {
  const r = await fetch('/api/files/list');
  if (r.status === 401) { location.href = '/dashboard'; return; }
  const data = await r.json();
  const tb = document.getElementById('rows'); tb.innerHTML = '';
  for (const f of data.files) {
    const tr = document.createElement('tr');
    const a = document.createElement('a');
    a.href = '/api/files/download?name=' + encodeURIComponent(f.name);
    a.textContent = f.name; a.target = '_blank';
    const td0 = document.createElement('td'); td0.appendChild(a);
    const td1 = document.createElement('td'); td1.textContent = (f.size/1024).toFixed(1) + ' KB';
    const td2 = document.createElement('td'); td2.textContent = f.mtime;
    const td3 = document.createElement('td');
    const dl = document.createElement('a');
    dl.href = '/api/files/download?name=' + encodeURIComponent(f.name) + '&dl=1';
    dl.textContent = '下载'; td3.appendChild(dl);
    td3.appendChild(document.createTextNode(' '));
    const del = document.createElement('button'); del.textContent = '删除'; del.className = 'del';
    del.onclick = async () => {
      if (!confirm('删除 ' + f.name + ' ?')) return;
      await fetch('/api/files/delete?name=' + encodeURIComponent(f.name), {method:'POST'});
      refresh();
    };
    td3.appendChild(del);
    tr.append(td0, td1, td2, td3); tb.appendChild(tr);
  }
  if (!data.files.length) tb.innerHTML = '<tr><td colspan="4">柜子是空的。</td></tr>';
}
async function up() {
  const files = document.getElementById('f').files;
  const folder = document.getElementById('folder').value.trim();
  const msg = document.getElementById('msg');
  if (!files.length) { msg.textContent = '先选文件。'; return; }
  for (const file of files) {
    const name = (folder ? folder + '/' : '') + file.name;
    msg.textContent = '上传中: ' + name;
    const r = await fetch('/api/files/raw?name=' + encodeURIComponent(name),
                          {method:'PUT', body:file});
    const j = await r.json();
    if (!r.ok) { msg.textContent = '失败: ' + (j.error || r.status); return; }
  }
  msg.textContent = '全部上传完成。';
  document.getElementById('f').value = '';
  refresh();
}
refresh();
</script></body></html>"""


def register(mcp) -> None:

    @mcp.custom_route("/files", methods=["GET"])
    async def files_page(request: Request) -> Response:
        if sh._require_auth(request) is not None:
            return RedirectResponse("/dashboard")
        return HTMLResponse(_PAGE)

    @mcp.custom_route("/api/files/list", methods=["GET"])
    async def files_list(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({"files": _listing()})

    @mcp.custom_route("/api/files/raw", methods=["PUT"])
    async def files_upload(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        name = request.query_params.get("name", "")
        try:
            path = _safe(name)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        body = await request.body()
        if len(body) > _MAX_UPLOAD:
            return JSONResponse({"error": "文件超过 10MB 上限"}, status_code=413)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)
        return JSONResponse({"ok": True, "name": name, "size": len(body)})

    @mcp.custom_route("/api/files/download", methods=["GET"])
    async def files_download(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        name = request.query_params.get("name", "")
        try:
            path = _safe(name)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not os.path.isfile(path):
            return JSONResponse({"error": "文件不存在"}, status_code=404)
        with open(path, "rb") as f:
            data = f.read()
        fn = os.path.basename(path)
        inline = fn.lower().endswith((".md", ".txt", ".json", ".log"))
        if request.query_params.get("dl") == "1":
            inline = False
        disp = "inline" if inline else "attachment"
        media = "text/plain; charset=utf-8" if inline else "application/octet-stream"
        headers = {"Content-Disposition": f"{disp}; filename*=UTF-8''{__import__('urllib.parse', fromlist=['quote']).quote(fn)}"}
        return Response(content=data, media_type=media, headers=headers)

    @mcp.custom_route("/api/files/delete", methods=["POST"])
    async def files_delete(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        name = request.query_params.get("name", "")
        try:
            path = _safe(name)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not os.path.isfile(path):
            return JSONResponse({"error": "文件不存在"}, status_code=404)
        os.remove(path)
        return JSONResponse({"ok": True, "deleted": name})
