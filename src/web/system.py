"""
========================================
web/system.py — 心跳 / 日志 / 错误码面板
========================================

- /api/heartbeat：前端心跳灯轮询（alive/uptime/last_op/decay 状态）
- /api/logs：读 server.log 末尾若干行（按级别过滤）
- /api/errors/recent、/api/errors/clear：统一错误码体系（errors.jsonl）读取/清空

对外暴露：register(mcp)。
========================================
"""

import os
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

try:
    from errors import recent_errors, format_error, clear_errors_log, get_recent_logs  # type: ignore
except ImportError:  # pragma: no cover
    from ..errors import recent_errors, format_error, clear_errors_log, get_recent_logs  # type: ignore

_LOGS_DEFAULT_LIMIT = 200
_LOGS_MAX_LIMIT = 2000
_ERRORS_DEFAULT_LIMIT = 50
_ERRORS_MAX_LIMIT = 500


def _check(
    check_id: str,
    label: str,
    status: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    action: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "status": status,
        "message": message,
        "details": details or {},
    }
    if action:
        item["action"] = action
    return item


def _secret_is_set(config_value: Any, env_name: str) -> bool:
    raw = str(config_value or "").strip()
    if raw:
        return True
    if os.environ.get(env_name, "").strip():
        return True
    try:
        return bool(sh._read_env_var(env_name).strip())
    except Exception:
        return False


def _probe_writable_dir(path: str) -> tuple[bool, str]:
    if not path:
        return False, "buckets_dir 未配置"
    if not os.path.isdir(path):
        return False, "目录不存在"
    probe = os.path.join(path, ".ombre_diagnostics_probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True, ""
    except Exception as e:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except Exception:
            pass
        return False, str(e)


async def build_system_diagnostics() -> dict[str, Any]:
    """Build a read-only Dashboard diagnostics report.

    This intentionally avoids network calls; explicit connectivity probes remain
    behind the existing "test" buttons so opening Settings never blocks on API
    latency.
    """
    cfg = sh.config or {}
    checks: list[dict[str, Any]] = []

    buckets_dir = str(cfg.get("buckets_dir") or "").strip()
    writable, storage_error = _probe_writable_dir(buckets_dir)
    persistence = sh.data_dir_persistence(buckets_dir)
    if not writable:
        storage_status = "error"
        storage_msg = f"数据目录不可用：{storage_error}"
        storage_action = "检查 buckets_dir / OMBRE_VAULT_DIR 挂载与写权限"
    elif not persistence["persistent"]:
        # 可写但不持久：容器重建即全丢，比不可写更隐蔽也更致命 → 高亮为 error。
        storage_status = "error"
        storage_msg = "数据目录可写，但没挂到持久卷——容器重建会丢记忆！"
        storage_action = "在 docker-compose 里把数据目录挂到命名卷或宿主机目录"
    else:
        storage_status = "ok"
        storage_msg = "数据目录存在、可写、且在持久位置"
        storage_action = ""
    checks.append(_check(
        "storage",
        "数据目录",
        storage_status,
        storage_msg,
        details={
            "buckets_dir": buckets_dir,
            "in_docker": sh.in_docker(),
            "persistent": persistence["persistent"],
            "persistence_mode": persistence["mode"],
            "persistence_note": persistence["note"],
        },
        action=storage_action,
    ))

    try:
        stats = await sh.bucket_mgr.get_stats() if sh.bucket_mgr else {}
        permanent = int(stats.get("permanent_count", 0) or 0)
        dynamic = int(stats.get("dynamic_count", 0) or 0)
        archive = int(stats.get("archive_count", 0) or 0)
        checks.append(_check(
            "buckets",
            "记忆桶",
            "ok",
            f"共 {permanent + dynamic} 条活跃记忆，归档 {archive} 条",
            details={
                "permanent": permanent,
                "dynamic": dynamic,
                "archive": archive,
                "total": permanent + dynamic,
            },
        ))
    except Exception as e:
        checks.append(_check(
            "buckets",
            "记忆桶",
            "warning",
            f"记忆桶统计读取失败：{e}",
            action="查看日志页或检查 bucket markdown frontmatter",
        ))

    dehy = cfg.get("dehydration", {}) or {}
    llm_key_set = _secret_is_set(dehy.get("api_key", ""), "OMBRE_COMPRESS_API_KEY")
    llm_model = str(dehy.get("model") or "").strip()
    llm_base = str(dehy.get("base_url") or "").strip()
    if not llm_key_set:
        llm_status = "error"
        llm_message = "压缩/打标 LLM API Key 未配置"
        llm_action = "到 设置 -> 引擎 填写压缩 API Key，或设置 OMBRE_COMPRESS_API_KEY"
    elif not llm_model or not llm_base:
        llm_status = "warning"
        llm_message = "压缩/打标 LLM 已有 Key，但模型或 Base URL 不完整"
        llm_action = "补齐 model 与 base_url 后点击测试"
    else:
        llm_status = "ok"
        llm_message = "压缩/打标 LLM 配置已就绪"
        llm_action = ""
    checks.append(_check(
        "llm",
        "脱水 / 打标 LLM",
        llm_status,
        llm_message,
        details={
            "api_key_set": llm_key_set,
            "model": llm_model,
            "base_url": llm_base,
            "api_format": str(dehy.get("api_format") or "openai_compat"),
            "timeout_seconds": dehy.get("timeout_seconds", 60),
        },
        action=llm_action,
    ))

    emb_cfg = cfg.get("embedding", {}) or {}
    emb_enabled_cfg = bool(emb_cfg.get("enabled", True))
    emb_key_set = _secret_is_set(emb_cfg.get("api_key", ""), "OMBRE_EMBED_API_KEY")
    emb_engine = sh.embedding_engine
    emb_runtime_enabled = bool(getattr(emb_engine, "enabled", False))
    emb_backend = getattr(emb_engine, "_backend", None)
    emb_db_path = str(getattr(emb_engine, "db_path", "") or "")
    if not emb_enabled_cfg:
        emb_status = "error"
        emb_message = "向量化已关闭，语义检索不可用"
        emb_action = "开启 embedding 并配置云端 Key 或本地 Ollama"
    elif not emb_runtime_enabled or emb_backend is None:
        emb_status = "error"
        emb_message = "向量化已开启但运行时仍在待机，通常是 Embedding API Key 或本地模型未就绪"
        emb_action = "填写 Embedding API Key 后保存，或完成本地 bge-m3 安装"
    else:
        emb_status = "ok"
        emb_message = "向量化运行时已就绪"
        emb_action = ""
    checks.append(_check(
        "embedding",
        "向量化",
        emb_status,
        emb_message,
        details={
            "config_enabled": emb_enabled_cfg,
            "runtime_enabled": emb_runtime_enabled,
            "api_key_set": emb_key_set,
            "model": str(getattr(emb_engine, "model", "") or emb_cfg.get("model") or ""),
            "backend": type(emb_backend).__name__ if emb_backend is not None else "",
            "db_path": emb_db_path,
            "db_exists": bool(emb_db_path and os.path.exists(emb_db_path)),
            "timeout_seconds": emb_cfg.get("timeout_seconds", 30),
        },
        action=emb_action,
    ))

    gh_cfg = cfg.get("github_sync", {}) or {}
    gh_inst = sh.github_sync_instance
    if gh_inst is None:
        gh_repo = str(gh_cfg.get("repo") or "").strip()
        # 没配异地备份时，风险高低取决于本地这份是否持久：
        # 记忆目录不持久（Docker 未挂卷）+ 没备份 = 随时全丢 → error 级强提醒；
        # 本地持久但只有一份 = 仍建议开备份（盘坏/换机找不回）→ warning。
        only_copy_at_risk = not persistence["persistent"]
        checks.append(_check(
            "github",
            "GitHub 备份",
            "error" if only_copy_at_risk else "warning",
            (
                "还没配云端备份，而且本地这份也不持久——记忆随时可能全部丢失，请尽快开启备份"
                if only_copy_at_risk else
                "记忆目前只有本地一份，没有云端备份。建议开启 GitHub 备份，换电脑或磁盘损坏时也能找回"
            ) if not gh_repo else "GitHub 配置存在但运行时实例未创建",
            details={
                "configured": False,
                "repo": gh_repo,
                "branch": gh_cfg.get("branch", "main"),
                "path_prefix": gh_cfg.get("path_prefix", "ombre"),
                "token_set": _secret_is_set(gh_cfg.get("token", ""), "OMBRE_GITHUB_TOKEN"),
                "auto_interval_minutes": int(gh_cfg.get("auto_interval_minutes") or 0),
            },
            action="在 设置 → GitHub 同步 里填仓库和 Token，开启云端备份" if not gh_repo else "在 设置 → GitHub 同步 中保存并验证",
        ))
    else:
        gh_status = gh_inst.status()
        last_status = gh_status.get("last_status", "idle")
        validated = bool(gh_status.get("is_validated"))
        consecutive = int(gh_status.get("consecutive_failures") or 0)
        last_sync = gh_status.get("last_sync")
        if last_status == "error":
            # 连挂多次 = 用户很可能以为有备份其实没有 → 升级为醒目 error 并说清多久没成功。
            if consecutive >= 3:
                status = "error"
                message = f"云端备份已连续失败 {consecutive} 次，可能一直没备份成功——请尽快检查"
            else:
                status = "warning"
                message = "最近一次 GitHub 备份失败"
            message += f"（上次成功：{last_sync}）" if last_sync else "（还没有过一次成功备份）"
            action = "查看 GitHub 同步状态、点「验证」确认 Token/仓库是否还有效"
        elif not validated:
            status = "warning"
            message = "GitHub 同步已配置，但尚未验证权限"
            action = "点击 GitHub 同步里的“验证”"
        else:
            status = "ok"
            message = "GitHub 同步配置已就绪"
            action = ""
        checks.append(_check(
            "github",
            "GitHub 备份",
            status,
            message,
            details={"configured": True, **gh_status},
            action=action,
        ))

    try:
        setup_needed = bool(sh._is_setup_needed())
    except Exception:
        setup_needed = False
    mcp_oauth_required = bool(cfg.get("mcp_require_auth", True))
    checks.append(_check(
        "auth",
        "访问控制",
        "error" if setup_needed else "ok",
        "Dashboard 密码未设置" if setup_needed else (
            "Dashboard 密码已设置，MCP OAuth 已开启" if mcp_oauth_required else "Dashboard 密码已设置，MCP OAuth 已关闭"
        ),
        details={
            "dashboard_password_set": not setup_needed,
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            "mcp_oauth_required": mcp_oauth_required,
        },
        action="先设置 Dashboard 密码" if setup_needed else "",
    ))

    decay_engine = sh.decay_engine
    decay_running = bool(getattr(decay_engine, "is_running", False))
    checks.append(_check(
        "runtime",
        "运行时",
        "ok" if decay_running else "warning",
        "服务运行中，衰减引擎已启动" if decay_running else "服务运行中，但衰减引擎未运行",
        details={
            "version": sh.version,
            "uptime_s": int(time.time() - sh._SERVER_START_TS),
            "repo_root": sh.repo_root,
            "in_docker": sh.in_docker(),
            "decay_engine": "running" if decay_running else "stopped",
        },
        action="如长期停止，请重启服务并查看日志" if not decay_running else "",
    ))

    summary = {"ok": 0, "warning": 0, "error": 0}
    for item in checks:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    return {
        "ok": summary["error"] == 0,
        "summary": summary,
        "checks": checks,
    }


def register(mcp) -> None:

    @mcp.custom_route("/api/heartbeat", methods=["GET"])
    async def api_heartbeat(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({
            "alive": True,
            "ts": time.time(),
            "uptime_s": int(time.time() - sh._SERVER_START_TS),
            "last_op_ts": sh._LAST_OP_TS,
            "decay_engine": "running" if sh.decay_engine.is_running else "stopped",
        })

    @mcp.custom_route("/api/system/diagnostics", methods=["GET"])
    async def api_system_diagnostics(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse(await build_system_diagnostics())

    @mcp.custom_route("/api/logs", methods=["GET"])
    async def api_logs(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        log_file = os.environ.get("OMBRE_LOG_FILE", "")
        if not log_file or not os.path.isfile(log_file):
            return JSONResponse({
                "lines": [],
                "log_file": log_file or "",
                "note": "日志文件尚未创建（可能未启用文件日志或刚启动）",
            })
        try:
            limit = max(1, min(int(request.query_params.get("limit", str(_LOGS_DEFAULT_LIMIT))), _LOGS_MAX_LIMIT))
        except ValueError:
            limit = _LOGS_DEFAULT_LIMIT
        level = request.query_params.get("level", "WARNING").upper()
        allow = {"ERROR": ("ERROR",),
                 "WARNING": ("WARNING", "ERROR"),
                 "INFO": ("INFO", "WARNING", "ERROR"),
                 "ALL": None}
        keep = allow.get(level, ("WARNING", "ERROR"))
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if keep is not None:
                lines = [ln for ln in lines if any(f" {lv}: " in ln for lv in keep)]
            lines = lines[-limit:]
            return JSONResponse({
                "lines": [ln.rstrip("\n") for ln in lines],
                "log_file": log_file,
                "level": level,
                "count": len(lines),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/errors/recent", methods=["GET"])
    async def api_errors_recent(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            limit = max(1, min(int(request.query_params.get("limit", str(_ERRORS_DEFAULT_LIMIT))), _ERRORS_MAX_LIMIT))
        except ValueError:
            limit = _ERRORS_DEFAULT_LIMIT
        min_level = request.query_params.get("min_level", "W").upper()
        items = recent_errors(limit=limit, min_level=min_level)
        tail = get_recent_logs(15)
        for it in items:
            it["formatted"] = format_error(
                it.get("code", ""), it.get("detail", ""),
                extra=it.get("extra"), include_logs=True,
            )
        return JSONResponse({
            "ok": True,
            "count": len(items),
            "min_level": min_level,
            "log_tail": tail,
            "errors": items,
        })

    @mcp.custom_route("/api/errors/clear", methods=["POST"])
    async def api_errors_clear(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        n = clear_errors_log()
        return JSONResponse({"ok": True, "cleared": n})
