"""
========================================
web/dream_book_api.py — 梦境书（Dream Book）Dashboard API
========================================
施工单·工程二：梦脱离 files/ 文件区，独立存储在
<buckets_dir>/dream_book/。这里只是薄封装——真正的存储/生命周期逻辑
在 dream_engine.py 的模块级函数（list_dream_book_entries /
dream_book_keep / dream_book_delete），跟 MCP 的 dream_keep 工具共用
同一套函数，Dashboard 点按钮和 CC 调工具走的是完全一样的代码路径。

- /api/dream-book (GET)：全部条目，按日期倒序
- /api/dream-book/{date}/keep (POST)：标记 kept（幂等）
- /api/dream-book/{date} (DELETE)：物理删除（burned 的骨架不可删）

对外暴露：register(mcp)。
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh


def register(mcp) -> None:

    @mcp.custom_route("/api/dream-book", methods=["GET"])
    async def api_dream_book_list(request: Request) -> Response:
        """List all dream book entries, newest date first."""
        from starlette.responses import JSONResponse
        from dream_engine import list_dream_book_entries

        err = sh._require_auth(request)
        if err:
            return err
        buckets_dir = sh.config.get("buckets_dir", "buckets")
        try:
            entries = list_dream_book_entries(buckets_dir)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        expire_hours = float(sh.config.get("dream", {}).get("expire_hours", 48))
        last_run_at = getattr(sh.dream_engine, "last_run_at", None)
        return JSONResponse({
            "ok": True, "count": len(entries), "entries": entries,
            "expire_hours": expire_hours, "last_run_at": last_run_at,
        })

    @mcp.custom_route("/api/dream-book/{date}/keep", methods=["POST"])
    async def api_dream_book_keep(request: Request) -> Response:
        """Mark a dream book entry as kept (idempotent)."""
        from starlette.responses import JSONResponse
        from dream_engine import dream_book_keep

        err = sh._require_auth(request)
        if err:
            return err
        date = request.path_params["date"]
        buckets_dir = sh.config.get("buckets_dir", "buckets")
        result = dream_book_keep(buckets_dir, date)
        status_code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=status_code)

    @mcp.custom_route("/api/dream-book/{date}", methods=["DELETE"])
    async def api_dream_book_delete(request: Request) -> Response:
        """Hard-delete a dream book entry. Burned entries are protected."""
        from starlette.responses import JSONResponse
        from dream_engine import dream_book_delete

        err = sh._require_auth(request)
        if err:
            return err
        date = request.path_params["date"]
        buckets_dir = sh.config.get("buckets_dir", "buckets")
        result = dream_book_delete(buckets_dir, date)
        status_code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=status_code)
