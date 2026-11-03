"""Small ASGI request-size guard for the public MCP endpoint."""

from __future__ import annotations

import json
from typing import Awaitable, Callable


_Receive = Callable[[], Awaitable[dict]]
_Send = Callable[[dict], Awaitable[None]]
_REJECTION_DRAIN_MULTIPLIER = 2

_MCP_ENDPOINT_ROOTS = ("/mcp", "/sse", "/messages")

# These routes belong to the Dashboard, its session-auth flow, or the OAuth
# bootstrap protocol. They must reach their own route-level access controls;
# every other HTTP path is treated as an MCP transport path by the outer auth
# middleware so a newly introduced SDK endpoint cannot silently become public.
_MCP_AUTH_EXEMPT_EXACT_PATHS = {
    "/",
    "/breath-hook",
    "/dashboard",
    "/favicon.ico",
    "/files",
    "/health",
    "/letters",
    "/onboarding",
    "/portrait",
}
_MCP_AUTH_EXEMPT_PREFIXES = (
    "/.well-known/",
    "/api/",
    "/auth/",
    "/oauth/",
    "/static/",
)


def is_mcp_endpoint_path(path: object) -> bool:
    """Match every SDK MCP transport endpoint, including nested forms."""
    normalized = str(path or "").rstrip("/") or "/"
    return any(
        normalized == root or normalized.startswith(f"{root}/")
        for root in _MCP_ENDPOINT_ROOTS
    )


def is_mcp_auth_exempt_path(path: object) -> bool:
    """Return whether a path may bypass the outer MCP bearer-token gate."""
    normalized = str(path or "").rstrip("/") or "/"
    if normalized in _MCP_AUTH_EXEMPT_EXACT_PATHS:
        return True
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in _MCP_AUTH_EXEMPT_PREFIXES
    )


class MCPRequestBodyLimitMiddleware:
    """Reject oversized MCP requests before JSON-RPC parsing or tool dispatch."""

    def __init__(
        self,
        app,
        *,
        max_bytes: int,
        path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
    ) -> None:
        self.app = app
        self.max_bytes = max(0, int(max_bytes))
        self.path_matcher = path_matcher

    def limit_for_path(self, path: object) -> int:
        """Per-request byte ceiling; subclasses may widen it for specific routes."""
        return self.max_bytes

    async def __call__(self, scope: dict, receive: _Receive, send: _Send) -> None:
        if (
            scope.get("type") != "http"
            or not self.path_matcher(scope.get("path"))
            or str(scope.get("method", "GET")).upper() not in {"POST", "PUT", "PATCH"}
        ):
            await self.app(scope, receive, send)
            return
        max_bytes = self.limit_for_path(scope.get("path"))
        if max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length", b"").decode("latin-1").strip()
        if raw_length:
            try:
                declared_length = int(raw_length)
            except ValueError:
                await self._send_json(send, 400, "invalid Content-Length")
                return
            if declared_length < 0:
                await self._send_json(send, 400, "invalid Content-Length")
                return
            if declared_length > max_bytes:
                # Docker Desktop on Windows may reset the TCP connection when
                # an ASGI app returns before a modest in-flight request body is
                # consumed. Drain only a bounded amount, without parsing or
                # retaining it, so normal oversized clients reliably see 413
                # while very large/slow attacks are still rejected promptly.
                if declared_length <= max_bytes * _REJECTION_DRAIN_MULTIPLIER:
                    await self._drain_request(receive, max_bytes=declared_length)
                await self._send_too_large(send, max_bytes)
                return

        received = 0
        buffered: list[dict] = []
        while True:
            message = await receive()
            if not isinstance(message, dict):
                return
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    if message.get("more_body", False):
                        await self._drain_request(receive, max_bytes=max_bytes)
                    await self._send_too_large(send, max_bytes)
                    return
                buffered.append(message)
                if not message.get("more_body", False):
                    break

        buffered_iter = iter(buffered)

        async def replay_receive() -> dict:
            try:
                return next(buffered_iter)
            except StopIteration:
                # Long-lived MCP transports keep reading after the request body
                # to observe the real disconnect. Replaying synthetic empty
                # requests forever creates a tight CPU loop.
                return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _drain_request(receive: _Receive, *, max_bytes: int) -> bool:
        """Discard at most ``max_bytes`` and report whether the request ended."""
        drained = 0
        while drained <= max(0, max_bytes):
            message = await receive()
            if not isinstance(message, dict):
                return False
            if message.get("type") == "http.disconnect":
                return True
            if message.get("type") != "http.request":
                continue
            drained += len(message.get("body", b""))
            if not message.get("more_body", False):
                return True
        return False

    async def _send_too_large(self, send: _Send, max_bytes: int) -> None:
        await self._send_json(
            send,
            413,
            f"MCP request body exceeds {max_bytes} bytes",
        )

    @staticmethod
    async def _send_json(send: _Send, status: int, error: str) -> None:
        body = json.dumps({"error": error}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


_LARGE_UPLOAD_PATHS = {
    "/api/import/preflight",
    "/api/import/upload",
    "/api/migrate/upload",
}


# A-1 追加 3(2026-11-03):相册的两条图片上传路径(dashboard 上传 + chatnest ingest)
# 单独放宽到 12MB(含 multipart 开销),其它管理接口仍是 4MB。相册单图上限 10MB 在
# web/album.py 另行校验;这里只是让 10MB 的图加上表单字段能进门,不再被 413 挡在外面。
_ALBUM_UPLOAD_PATHS = {
    "/api/album",
    "/api/album/ingest",
}
DEFAULT_MAX_ALBUM_UPLOAD_BYTES = 12 * 1024 * 1024


class ManagementRequestBodyLimitMiddleware(MCPRequestBodyLimitMiddleware):
    """Bound normal Dashboard/OAuth mutations while preserving large upload APIs."""

    def __init__(
        self,
        app,
        *,
        max_bytes: int,
        max_album_upload_bytes: int = DEFAULT_MAX_ALBUM_UPLOAD_BYTES,
    ) -> None:
        def should_limit(path: object) -> bool:
            normalized = str(path or "").rstrip("/") or "/"
            return (
                not is_mcp_endpoint_path(normalized)
                and normalized not in _LARGE_UPLOAD_PATHS
            )

        super().__init__(app, max_bytes=max_bytes, path_matcher=should_limit)
        self.max_album_upload_bytes = max(0, int(max_album_upload_bytes))

    def limit_for_path(self, path: object) -> int:
        normalized = str(path or "").rstrip("/") or "/"
        if normalized in _ALBUM_UPLOAD_PATHS:
            # 永远不比普通管理上限更紧:配置把相册值调小也只是回落到 4MB 那条线;
            # 管理上限配成 0(不限)时相册也不限。
            if self.max_bytes <= 0:
                return 0
            return max(self.max_bytes, self.max_album_upload_bytes)
        return self.max_bytes

    async def _send_too_large(self, send: _Send, max_bytes: int) -> None:
        await self._send_json(
            send,
            413,
            f"management request body exceeds {max_bytes} bytes",
        )
