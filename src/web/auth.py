"""
========================================
web/auth.py — Dashboard 鉴权相关 HTTP 路由
========================================

承载 /auth/* 这一组 cookie 会话鉴权接口（状态/首启设密/登录/登出/改密/安全问题急救）。
真正的会话/密码逻辑在 web/_shared.py，本文件只做 HTTP 入口与参数校验。

对外暴露：register(mcp) —— server.py 启动装配时调用，把下列路由挂到主 mcp 实例。
========================================
"""

import asyncio
import hmac
import os
import secrets

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from . import oauth

_MAX_PASSWORD_CHARS = 1024
_MAX_SECURITY_QUESTION_CHARS = 500
_MAX_SECURITY_ANSWER_CHARS = 1024


def _json_object(body) -> dict | None:
    return body if isinstance(body, dict) else None


# --- P0-5 5-A: 首次初始化加固 ---------------------------------------------
# /auth/setup 之前无 bootstrap secret、无回环限制,且"是否已初始化"检查发生
# 在 await request.json() 之前、之后无锁无二次检查——首次启动窗口内可被
# 远程抢注,并发请求还能产生两个"都成功"的会话。
#
# 加固:
# 1. 非回环地址调用必须带上一次性 bootstrap secret(启动时生成,打日志 +
#    落盘到 <buckets_dir>/.bootstrap_secret,setup 成功后立即失效并删除)。
# 2. 整个 setup 处理过程(含解析请求体、二次校验 is_setup_needed、写密码)
#    都在同一把进程内 asyncio.Lock 里执行,消除并发 TOCTOU 窗口。
_setup_lock = asyncio.Lock()
_BOOTSTRAP_SECRET_BYTES = 24
_bootstrap_secret: str | None = None


def _bootstrap_secret_file() -> str | None:
    """None when buckets_dir isn't configured yet (e.g. tests that register()
    routes without a full config) — the file is a convenience, not required
    for the secret itself to work (it's also printed to the log)."""
    buckets_dir = sh.config.get("buckets_dir") if isinstance(sh.config, dict) else None
    if not buckets_dir:
        return None
    return os.path.join(buckets_dir, ".bootstrap_secret")


def _ensure_bootstrap_secret() -> None:
    """启动时(register() 调用一次)生成一次性初始化密钥。已经设过密码就不生成
    ——bootstrap secret 只为"还没设密"这个窗口存在。任何一步失败(包括
    buckets_dir 还没配置好)都不能把 register() 炸掉——这只是给非回环访问
    多加的一道门，回环访问完全不依赖它，日志里的那份 secret 才是权威来源，
    落盘只是方便。"""
    global _bootstrap_secret
    if _bootstrap_secret is not None:
        return
    try:
        if not sh._is_setup_needed():
            return
    except Exception:
        return
    secret = secrets.token_urlsafe(_BOOTSTRAP_SECRET_BYTES)
    _bootstrap_secret = secret
    sh.logger.warning(
        "[auth] Dashboard 尚未初始化。非回环地址访问 /auth/setup 需要 "
        "bootstrap_secret(随 JSON body 一起传),回环地址(127.0.0.1/::1)"
        f"直接免验证。本次生成的 bootstrap_secret: {secret}"
    )
    try:
        path = _bootstrap_secret_file()
        if path is None:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(secret)
        os.chmod(path, 0o600)
    except Exception as e:
        sh.logger.warning(f"[auth] failed to write bootstrap secret file: {e}")


def _consume_bootstrap_secret() -> None:
    """setup 成功后一次性密钥立即作废,文件删掉,内存值清空。"""
    global _bootstrap_secret
    _bootstrap_secret = None
    path = _bootstrap_secret_file()
    if path is None:
        return
    try:
        os.remove(path)
    except OSError:
        pass


# --- P0-5 5-B: 改密/账户恢复撤销全部 OAuth 授权 -----------------------------
# 抢注者能签发长期 OAuth token（65.7年 TTL），改密与账户恢复此前都不会让
# 这些 token 失效——接管在改密之后依然存活。这两条路径成功后都必须撤销
# 当前全部已签发的 access/refresh token。
#
# 副作用（必须告知，不许静默上线）：执行后 claude.ai 侧的 Ombre-Fable /
# Ombre-Kieran 等所有已连接的 MCP 连接器会断连，需要重新走一次 OAuth 授权
# 页（不需要删除重建连接器本身，client_id 注册没有被清，只是 token 全部
# 作废，重新点一次授权即可）。
def _revoke_all_mcp_tokens_safe() -> tuple[int, bool]:
    """返回 (撤销的 access token 数量, 是否成功落盘)。密码已经改成功了，
    撤销 token 这一步万一落盘失败也不能让整个改密请求失败——但绝不能装作
    没发生，失败与否都如实带进响应体里。"""
    try:
        count = oauth.revoke_all_mcp_tokens()
        return count, True
    except Exception as e:
        sh.logger.error(f"[auth] failed to persist mcp token revocation: {e}")
        return 0, False


def register(mcp) -> None:
    """把 /auth/* 路由注册到传入的 FastMCP 实例。"""

    _ensure_bootstrap_secret()

    @mcp.custom_route("/auth/status", methods=["GET"])
    async def auth_status(request: Request) -> Response:
        """Return auth state (authenticated, setup_needed)."""
        from starlette.responses import JSONResponse
        return JSONResponse({
            "authenticated": sh._is_authenticated(request),
            "setup_needed": sh._is_setup_needed(),
        })

    @mcp.custom_route("/auth/setup", methods=["POST"])
    async def auth_setup_endpoint(request: Request) -> Response:
        """Initial password setup (only when no password is configured)."""
        from starlette.responses import JSONResponse
        if not sh._is_setup_needed():
            return JSONResponse({"error": "Already configured"}, status_code=400)
        # P0-5 5-A: 整个临界区(解析请求体、二次校验、写密码)都在这把锁里
        # 顺序执行——并发的第二个请求会在这里排队,而不是各自跑到
        # _save_password_hash 互相踩踏,产生两个"都成功"的会话。
        async with _setup_lock:
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON"}, status_code=400)
            body = _json_object(body)
            if body is None:
                return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
            # 解析请求体之后再查一次——锁本身已经把并发请求串行化了,这道
            # 检查是防守性的第二层,和铁律要求的"解析之后再校验"字面对齐。
            if not sh._is_setup_needed():
                return JSONResponse({"error": "Already configured"}, status_code=400)
            if not sh._is_loopback_request(request):
                provided = body.get("bootstrap_secret", "")
                if (
                    not isinstance(provided, str)
                    or _bootstrap_secret is None
                    or not hmac.compare_digest(provided, _bootstrap_secret)
                ):
                    return JSONResponse(
                        {
                            "error": "首次初始化仅允许从本机(回环地址)访问，"
                            "或在请求体里提供正确的 bootstrap_secret（见启动日志）"
                        },
                        status_code=403,
                    )
            password = body.get("password", "")
            if not isinstance(password, str):
                return JSONResponse({"error": "password must be a string"}, status_code=400)
            password = password.strip()
            if not 6 <= len(password) <= _MAX_PASSWORD_CHARS:
                return JSONResponse({"error": "密码长度必须在 6-1024 位之间"}, status_code=400)
            sh._save_password_hash(password)
            _consume_bootstrap_secret()
            token = sh._create_session()
        resp = JSONResponse({"ok": True})
        sh._set_session_cookie(resp, token, request)
        return resp

    @mcp.custom_route("/auth/login", methods=["POST"])
    async def auth_login(request: Request) -> Response:
        """Login with password."""
        from starlette.responses import JSONResponse
        retry = sh._login_retry_after(request)
        if retry:
            return JSONResponse(
                {"error": f"尝试过于频繁，请 {retry} 秒后再试"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        body = _json_object(body)
        if body is None:
            sh._record_login_failure(request)
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        password = body.get("password", "")
        if not isinstance(password, str) or len(password) > _MAX_PASSWORD_CHARS:
            sh._record_login_failure(request)
            return JSONResponse({"error": "密码格式无效"}, status_code=400)
        if sh._verify_any_password(password):
            sh._record_login_success(request)
            token = sh._create_session()
            resp = JSONResponse({"ok": True})
            sh._set_session_cookie(resp, token, request)
            return resp
        sh._record_login_failure(request)
        return JSONResponse({"error": "密码错误"}, status_code=401)

    @mcp.custom_route("/auth/logout", methods=["POST"])
    async def auth_logout(request: Request) -> Response:
        """Invalidate session."""
        from starlette.responses import JSONResponse
        token = request.cookies.get("ombre_session")
        if token:
            sh._sessions.pop(token, None)
            sh._save_sessions()
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("ombre_session")
        return resp

    @mcp.custom_route("/auth/change-password", methods=["POST"])
    async def auth_change_password(request: Request) -> Response:
        """Change dashboard password (requires current password)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
            return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        body = _json_object(body)
        if body is None:
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        current = body.get("current", "")
        new_pwd = body.get("new", "")
        if not isinstance(current, str) or not isinstance(new_pwd, str):
            return JSONResponse({"error": "密码格式无效"}, status_code=400)
        new_pwd = new_pwd.strip()
        if len(current) > _MAX_PASSWORD_CHARS:
            return JSONResponse({"error": "当前密码格式无效"}, status_code=400)
        if not sh._verify_any_password(current):
            return JSONResponse({"error": "当前密码错误"}, status_code=401)
        if not 6 <= len(new_pwd) <= _MAX_PASSWORD_CHARS:
            return JSONResponse({"error": "新密码长度必须在 6-1024 位之间"}, status_code=400)
        sh._save_password_hash(new_pwd)
        sh._sessions.clear()
        sh._save_sessions()
        revoked, persisted = _revoke_all_mcp_tokens_safe()
        token = sh._create_session()
        resp = JSONResponse({
            "ok": True,
            "revoked_mcp_tokens": revoked,
            "mcp_revocation_persisted": persisted,
        })
        sh._set_session_cookie(resp, token, request)
        return resp

    @mcp.custom_route("/auth/recovery-question", methods=["GET"])
    async def auth_recovery_question(request: Request) -> Response:
        """Return the configured security question (public, no auth needed)."""
        from starlette.responses import JSONResponse
        q = sh._load_auth_data().get("security_question", "")
        return JSONResponse({"question": q or None})

    @mcp.custom_route("/auth/recover", methods=["POST"])
    async def auth_recover(request: Request) -> Response:
        """Reset password via security question answer."""
        from starlette.responses import JSONResponse
        if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
            return JSONResponse({"error": "当前使用环境变量密码，无法通过安全问题重置"}, status_code=400)
        if not sh._load_auth_data().get("security_answer_hash"):
            return JSONResponse({"error": "未设置安全问题，无法使用急救模式"}, status_code=400)
        retry = sh._login_retry_after(request)
        if retry:
            return JSONResponse(
                {"error": f"尝试过于频繁，请 {retry} 秒后再试"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        body = _json_object(body)
        if body is None:
            sh._record_login_failure(request)
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        answer = body.get("answer", "")
        new_pwd = body.get("new_password", "")
        if not isinstance(answer, str) or not isinstance(new_pwd, str):
            sh._record_login_failure(request)
            return JSONResponse({"error": "恢复参数格式无效"}, status_code=400)
        new_pwd = new_pwd.strip()
        if len(answer) > _MAX_SECURITY_ANSWER_CHARS:
            sh._record_login_failure(request)
            return JSONResponse({"error": "答案格式无效"}, status_code=400)
        if not sh._verify_security_answer(answer):
            sh._record_login_failure(request)
            return JSONResponse({"error": "答案不正确"}, status_code=401)
        if not 6 <= len(new_pwd) <= _MAX_PASSWORD_CHARS:
            return JSONResponse({"error": "新密码长度必须在 6-1024 位之间"}, status_code=400)
        sh._record_login_success(request)
        sh._save_password_hash(new_pwd, keep_qa=True)
        sh._sessions.clear()
        sh._save_sessions()
        revoked, persisted = _revoke_all_mcp_tokens_safe()
        token = sh._create_session()
        resp = JSONResponse({
            "ok": True,
            "revoked_mcp_tokens": revoked,
            "mcp_revocation_persisted": persisted,
        })
        sh._set_session_cookie(resp, token, request)
        return resp

    @mcp.custom_route("/auth/security-question", methods=["POST"])
    async def auth_set_security_question(request: Request) -> Response:
        """Set or update the security question (requires login)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        body = _json_object(body)
        if body is None:
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        question = body.get("question", "")
        answer = body.get("answer", "")
        if not isinstance(question, str) or not isinstance(answer, str):
            return JSONResponse({"error": "问题和答案必须是字符串"}, status_code=400)
        question = question.strip()
        answer = answer.strip()
        if not question or not answer:
            return JSONResponse({"error": "问题和答案不能为空"}, status_code=400)
        if (
            len(question) > _MAX_SECURITY_QUESTION_CHARS
            or len(answer) > _MAX_SECURITY_ANSWER_CHARS
        ):
            return JSONResponse({"error": "问题或答案过长"}, status_code=400)
        sh._save_security_qa(question, answer)
        return JSONResponse({"ok": True})
