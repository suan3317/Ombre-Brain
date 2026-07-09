"""
========================================
server.py — MCP 服务入口 + 启动装配
========================================

启动整个 Ombre Brain 进程：加载配置、创建 BucketManager / Dehydrator /
DecayEngine / EmbeddingEngine / ImportEngine，把它们注入 tools._runtime 与
web._shared，然后以 @mcp.tool() 注册薄封装（真正的实现在 src/tools/<工具>/ 下面）。

关键行为：
- 启动后暴露 12 个 MCP 工具：breath/hold/grow/trace/anchor/release/
  pulse/plan/letter_write/letter_read/dream/I；每个入口 ≤ 10 行，只负责转发
- Dashboard / HTTP 路由全部已拆分到 src/web/<域>.py（每个模块 register(mcp)），
  本文件仅在启动时调用 web.register_all(mcp) 装配；共享依赖见 web/_shared.py
- 仍保留在本文件：进程启动、引擎初始化、GitHub 后台同步循环、Webhook 推送、
  MCP Bearer 鉴权中间件、单连接器 /mcp 装配（启动入口处把 mcp_extra 工具回灌进 mcp）、uvicorn 拉起

不做什么（边界）：
- 不在这里写 hold/breath/dream 等业务逻辑（全在 tools/* 下）
- 不写 HTTP 路由处理（全在 web/* 下）；不写 LLM prompt（dehydrator 负责）
- 不直接读写桶文件（bucket_manager 负责）

对外暴露：mcp/mcp_extra 两个实例 + 12 个 @mcp*.tool() 函数；HTTP 路由在 src/web/*
========================================
"""

import os
import sys
import random
import logging
import asyncio
import hashlib
import hmac
import secrets
import time
import json as _json_lib
from typing import Optional, Awaitable
from starlette.requests import Request
from starlette.responses import Response
import httpx
import yaml


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from migrate_engine import MigrateEngine
from utils import load_config, setup_logging, strip_wikilinks, count_tokens_approx, get_version, extract_wikilinks

# --- iter 2.1：MCP 工具实现已按代码路径拆分到 tools/ 子包 ---
# 本文件只保留 MCP 注册 + 路由（HTTP custom_route）+ 共享辅助。
# 真正的工具逻辑在 tools/breath, tools/hold, tools/grow, tools/trace,
# tools/anchor, tools/plan, tools/dream 里，便于单独阅读和修改。
from tools import _runtime as _tools_runtime
from tools import breath as _t_breath
from tools import hold as _t_hold
from tools import grow as _t_grow
from tools import trace as _t_trace
from tools import anchor as _t_anchor
from tools import plan as _t_plan
from tools import dream as _t_dream
from tools import i as _t_i
from tools._common import (
    check_content_size as _check_content_size,
    check_pinned_quota as _check_pinned_quota,
)

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Project version (read from <repo_root>/VERSION) / 项目版本号 ---
# get_version() 汇总读文件 + fallback 逻辑。
# 赋给双下划线变量 `__version__` 是 Python 社区约定俗成的模块版本字段名。
__version__ = get_version()
logger.info(f"Ombre Brain v{__version__}")

# --- iter 1.7 §A: legacy path migration check / 老路径迁移检测 ---
# 场景：1.6 早期使用者习惯在项目根跑 `python server.py`；1.7 重组后需要
# `python src/server.py`。这里只做「检测 + 提醒」，不做任何破坏性动作。
# load_config() 里 buckets_dir 默认仍是 <repo_root>/buckets，所以老数据不会丢。
#
# Python 小知识：
#   * 变量名以 `_` 开头是「模块内部」约定，不是语法强制
#   * for/else 这里没用，用了 break 提前退出
#   * `os.path.isdir(p) and any(...)` 是短路：前者 False 就不会跳 listdir
try:
    _bd = config.get("buckets_dir", "")
    if _bd and os.path.isdir(_bd):
        _has_data = False
        # 遍历各个桶目录，任何一个里（含域子目录）有 .md 文件就认定有数据。
        # 必须递归 os.walk：桶按域存在子目录里（permanent/<域>/x.md），
        # 只 os.listdir 顶层只会看到域文件夹、永远判定为空 → 误报 "fresh install"
        # （数据其实都在，breath 也读得到，纯粹是这条日志吓人）。
        for sub in ("permanent", "dynamic", "feel", "plans", "letters"):
            p = os.path.join(_bd, sub)
            if not os.path.isdir(p):
                continue
            if any(
                f.endswith(".md") and not f.startswith(".")
                for _root, _dirs, _files in os.walk(p)
                for f in _files
            ):
                _has_data = True
                break
        if _has_data:
            logger.info(f"[migration] existing buckets detected at {_bd} — zero data loss expected.")
        else:
            logger.info(f"[migration] {_bd} is empty — fresh install assumed.")
except Exception as _e:  # pragma: no cover - defensive / 防御性兑底
    # 启动期任何检测出错都不能阻止服务拉起，记个 warning 就过
    logger.warning(f"[migration] check skipped: {_e}")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 18001
# Docker 部署：compose 显式设 OMBRE_PORT=8000 保持容器内 8000（不动 Cloudflare ingress），
# 由 host 端口映射 18001:8000 对外暴露 18001。裸机：直接监听 18001。
# 端口优先级：env OMBRE_PORT（Docker 由 Dockerfile 固定 8000）> config.yaml host_port
# （裸机前端可改、保存即写 config）> 默认 18001。Docker 下前端改 host_port 不影响容器内
# 监听（仍 8000），由 host 映射 OMBRE_HOST_PORT 决定对外端口（部署脚本读 config 注入）。
try:
    _port_raw = os.environ.get("OMBRE_PORT") or str(config.get("host_port") or "") or "18001"
    OMBRE_PORT = int(_port_raw)
except (ValueError, TypeError):
    logger.warning("端口配置不是合法整数，回退到 18001")
    OMBRE_PORT = 18001

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。详见 ENV_VARS.md。
# _fire_webhook 每次调用直接读 os.environ（不缓存模块常量）——这样 dashboard 的
# /api/env-config 改完（它会写 os.environ）即时生效，无需再回写模块全局，
# 也让该路由能干净地迁出到 web/config_api.py。


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §①：禁裸魔法数字。这里集中所有会调的阁值。
# 与安全、鉴权、性能相关的参数不要在运行时乲变；如需调整请同步跑 pytest。
# ============================================================

# --- Webhook / HTTP 客户端超时 ---
_WEBHOOK_TIMEOUT_SECONDS = 5.0
_HEALTH_PROBE_TIMEOUT_SECONDS = 5

# --- Dashboard 鉴权 / 会话 / 密码 / 日志&错误面板分页常量 已移至 web/_shared.py、web/system.py ---


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    hook_url = os.environ.get("OMBRE_HOOK_URL", "").strip()
    hook_skip = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")
    if hook_skip or not hook_url:
        return
    if not hook_url.startswith(("http://", "https://")):
        logger.warning(f"OMBRE_HOOK_URL rejected: only http/https allowed (got {hook_url[:40]!r})")
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            await client.post(hook_url, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {hook_url}): {e}")

# --- Initialize core components / 初始化核心组件 ---
# 统一错误码体系（必须在任何业务初始化之前 configure，确保 errors.jsonl 路径生效）
try:
    from errors import (
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
        recent_errors,
        clear_errors_log,
        get_recent_logs,
    )
except ImportError:
    from .errors import (  # type: ignore
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
        recent_errors,
        clear_errors_log,
        get_recent_logs,
    )
configure_errors_path(config.get("buckets_dir", "buckets"))

try:
    embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
except OBStartupError as _ob_err:
    # OB-F001 已在 OBStartupError 内格式化好；写 fatal log 后退出
    logger.error(str(_ob_err))
    write_fatal_log(_ob_err.error_code, _ob_err.detail, buckets_dir=config.get("buckets_dir"))
    raise
except RuntimeError as _emb_err:
    # 兼容尚未迁移到 OBStartupError 的旧 raise（应该不再触发）
    logger.error(f"[STARTUP FAILED] {_emb_err}")
    raise SystemExit(f"Ombre Brain 启动中止：{_emb_err}") from _emb_err
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎
migrate_engine = MigrateEngine(config, bucket_mgr, embedding_engine)              # Migrate engine / 记忆包迁移引擎

# --- GitHub Sync / GitHub 同步 ---
from github_sync import GitHubSync  # type: ignore
_gh_cfg = config.get("github_sync", {}) or {}
_gh_token = (os.environ.get("OMBRE_GITHUB_TOKEN") or _gh_cfg.get("token") or "").strip()
github_sync_instance: GitHubSync | None = (
    GitHubSync(
        token=_gh_token,
        repo=_gh_cfg.get("repo", ""),
        branch=_gh_cfg.get("branch", "main"),
        path_prefix=_gh_cfg.get("path_prefix", "ombre"),
    )
    if _gh_token and _gh_cfg.get("repo")
    else None
)
_github_auto_task: "asyncio.Task | None" = None  # 后台定时同步任务


async def _github_sync_loop(interval_minutes: int) -> None:
    """后台定时 GitHub 同步循环。只在 is_validated=True 后执行实际上传。"""
    import asyncio
    logger.info(f"[github_sync] auto-sync loop started, interval={interval_minutes}min")
    # 首次先做一次验证，确认连接可用
    if _wsh.github_sync_instance and not _wsh.github_sync_instance.is_validated:
        try:
            result = await _wsh.github_sync_instance.validate()
            if not result.get("ok"):
                logger.warning(f"[github_sync] auto-sync: validate failed: {result.get('error')} — loop will retry next cycle")
        except Exception as e:
            logger.warning(f"[github_sync] auto-sync: validate exception: {e}")
    while True:
        await asyncio.sleep(interval_minutes * 60)
        inst = _wsh.github_sync_instance  # 读当前全局引用（config 更新可能替换实例）
        if inst is None:
            logger.info("[github_sync] auto-sync: instance gone, stopping loop")
            return
        if not inst.is_validated:
            # 还没验证通过，先 validate
            try:
                res = await inst.validate()
                if not res.get("ok"):
                    logger.warning(f"[github_sync] auto-sync skipped (not validated): {res.get('error')}")
                    continue
            except Exception as e:
                logger.warning(f"[github_sync] auto-sync validate failed: {e}")
                continue
        buckets_dir = config.get("buckets_dir", "")
        if not buckets_dir:
            continue
        try:
            result = await inst.sync(buckets_dir)
            if result.get("ok"):
                logger.info(f"[github_sync] auto-sync ok: {result.get('uploaded', 0)} files")
            else:
                logger.warning(f"[github_sync] auto-sync failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"[github_sync] auto-sync exception: {e}")


def _restart_github_auto_task(interval_minutes: int) -> None:
    """取消旧任务并按新间隔启动后台同步循环（interval_minutes=0 表示仅取消）。"""
    import asyncio
    global _github_auto_task
    if _github_auto_task and not _github_auto_task.done():
        _github_auto_task.cancel()
        _github_auto_task = None
    if interval_minutes > 0 and _wsh.github_sync_instance is not None:
        try:
            loop = asyncio.get_event_loop()
            _github_auto_task = loop.create_task(_github_sync_loop(interval_minutes))
        except RuntimeError:
            pass  # 没有运行中的 event loop（测试环境），跳过


# 启动时若配置了自动同步间隔，推迟到事件循环就绪后启动（用 lifespan 钩子）
_gh_auto_interval: int = int(_gh_cfg.get("auto_interval_minutes") or 0)


# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
#
# iter 2.2：合并回单连接器 /mcp（claude.ai 5 工具上限已解除）。
# 历史上（iter 2.1）曾拆成主 mcp(/mcp) + 副 mcp_extra(/mcp-extra) 两个实例。
# 现在只对外暴露主实例 mcp 的一条 /mcp 路由；mcp_extra 仅作工具分组容器保留
# （7 个 @mcp_extra.tool() 注册不动），启动入口处把它的工具回灌进 mcp 统一暴露。
# 两个实例共享同一进程、同一 runtime、同一 bucket_mgr；HTTP custom_route（dashboard、API）
# 全部挂在 mcp 主实例上。
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
)
mcp_extra = FastMCP(
    "Ombre Brain Extra",
    host="0.0.0.0",
    port=OMBRE_PORT,
)


# =============================================================
# Dashboard Auth —— 已拆分：会话/密码/鉴权 helper 在 web/_shared.py，
# /auth/* 路由在 web/auth.py。这里注入 config，并把 helper 名字 import 回本模块，
# 让本文件其余尚未迁移的 @mcp.custom_route 路由（大量调用 _require_auth）继续可用；
# 待这些路由也迁出 web/ 后，本段 import 可删除。
# =============================================================
import web as _web
import web._shared as _wsh
_wsh.init(config)
# 记忆持久性自检：容器里记忆目录若没挂持久卷，重建就全丢。开机就醒目告警，别让用户
# 以为「存住了其实没有」。只提示不阻断（阻断会伤部署）。
try:
    _dp = _wsh.data_dir_persistence(config.get("buckets_dir", ""))
    if not _dp["persistent"]:
        logger.warning(
            "=" * 60 + "\n"
            "⚠️  记忆目录未挂载到持久卷：" + str(config.get("buckets_dir", "")) + "\n"
            "    " + _dp["note"] + "\n"
            "    （记忆比代码金贵：代码能重部署，记忆丢了找不回。请尽快修正挂载。）\n"
            + "=" * 60
        )
    else:
        logger.info(f"记忆目录持久性：{_dp['mode']} — {_dp['note']}")
except Exception as _dpe:
    logger.warning(f"数据目录持久性自检失败（不影响启动）：{_dpe}")
# 注入业务引擎/版本/仓库根目录到 web 层（类比 tools/_runtime）。
# 注意：embedding_engine 会被热重载替换 —— 待 embedding/config 路由迁到 web/ 时，
# 替换处须同时写 _wsh.embedding_engine（目前这些路由仍在本文件、仍走 global）。
_wsh.init_runtime(
    version=__version__,
    repo_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    import_engine=import_engine,
    migrate_engine=migrate_engine,
    github_sync_instance=github_sync_instance,
    restart_github_auto_task=_restart_github_auto_task,
)
# 启动时把磁盘上的会话装回内存（容器重启不踢登录）。鉴权/会话逻辑全在 web/_shared.py，
# server.py 自身已无 @mcp.custom_route 路由，只需启动时载入一次会话。
from web._shared import _load_sessions
_load_sessions()

# 注册所有 web/ 路由模块（HTTP 层已全部迁出，见 web/__init__.register_all）
_web.register_all(mcp)


# =============================================================
# 根仪表板 / 静态资源 / favicon / /health —— 已拆分到 web/dashboard.py
# =============================================================


# 心跳时间戳 + _mark_op 已移到 web/_shared.py；这里 import 回来供 tools._runtime 注入。
from web._shared import _mark_op  # noqa: F401  (injected into tools._runtime below)


# =============================================================
# 仪表板硬删除通知队列（Dashboard Hard Purge Notification）
# 她/他从仪表板彻底删除记忆后，下次 AI 调用任何工具时一次性通知。
# 通知文件存于 buckets_dir/_pending_deletions.json，消费后立即删除。
# AI 无法触发此通知（它不是 MCP 工具，只能由仪表板 HTTP 端点写入）。
# =============================================================

def _deletion_notice_path() -> str:
    return os.path.join(config.get("buckets_dir", "buckets"), "_pending_deletions.json")


def _write_deletion_notice(names: list) -> None:
    """追加待发送删除通知。多次删除批次会合并入同一文件直至 AI 读取。"""
    path = _deletion_notice_path()
    try:
        existing: list = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = _json_lib.load(f)
        existing.extend(names)
        with open(path, "w", encoding="utf-8") as f:
            _json_lib.dump(existing, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to write deletion notice: {e}")


def _pop_deletion_notice() -> str:
    """读取并消费通知文件。返回格式化通知字符串（含尾部换行），无通知返回空串。"""
    path = _deletion_notice_path()
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            names = _json_lib.load(f)
        os.remove(path)
        if not names:
            return ""
        human = config.get("human", "人类")
        ts = time.strftime("%Y-%m-%d %H:%M")
        item_list = "\n".join(f"  · {n}" for n in names)
        return (
            f"「{ts}，{human} 通过前端界面永久删除了以下记忆：\n{item_list}\n"
            f"如果其中有你想保留的，你可以告诉 {human}。」\n\n"
        )
    except Exception as e:
        logger.warning(f"Failed to read deletion notice: {e}")
        return ""


# 这些 helper 定义在 server.py（读/写 webhook 全局等），但 web/ 的 hooks/buckets 路由要用。
# 在它们都定义好之后注入到 web._shared，供已迁出的路由通过 sh.fire_webhook 等调用。
_wsh.init_runtime(
    fire_webhook=_fire_webhook,
    write_deletion_notice=_write_deletion_notice,
    pop_deletion_notice=_pop_deletion_notice,
)


# =============================================================
# 结构化操作日志 helpers（任务A，2026-05-03）
# 给 11 个 @mcp.tool 入口统一打 entry/ok/err 三段日志，便于排查
# 客户端报 invalid_arguments / 静默错误等问题。
# 输出格式：op=<name> phase=entry|ok|err key=value...
# 所有可能含 PII 的字段（content / 信件正文等）只记 length，不记内容。
# =============================================================
def _fmt_log_val(v: object) -> str:
    """日志 value 的安全格式化：bool/int/float 原样；str 截 40 字符并去换行；其它转 str。"""
    if v is None:
        return "_"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.replace("\n", "\\n").replace(" ", "_")
        return s if len(s) <= 40 else s[:37] + "..."
    return type(v).__name__


def _fmt_log_args(args: dict) -> str:
    """把 args dict 拼成 `k1=v1 k2=v2` 串。"""
    if not args:
        return ""
    return " ".join(f"{k}={_fmt_log_val(v)}" for k, v in args.items())


def _log_op_entry(op: str, args: dict) -> None:
    logger.info(f"op={op} phase=entry " + _fmt_log_args(args))


def _log_op_ok(op: str, result: object) -> None:
    size = len(result) if isinstance(result, str) else 0
    logger.info(f"op={op} phase=ok bytes={size}")


def _log_op_err(op: str, exc: BaseException) -> None:
    # 用 .exception 让 traceback 进 server.log，便于事后定位
    logger.exception(f"op={op} phase=err err={type(exc).__name__}:{exc}")


async def _with_notice(coro: Awaitable[str], op: str = "", args: dict | None = None) -> str:
    """所有 MCP 工具调用的包装器。

    职责（统一错误规范）：
    1. 入口：begin_warnings() 初始化本调用的 W/I channel。
    2. 出口：拼接顺序 = [删除通知] + [工具正文] + [本调用产生的 W/I 提示].
    3. 异常：捕获后 record OB-E004，返回标准格式（含最近 15 条 log），
       不让 MCP 协议层看到裸异常字符串。
    4. 任务A：op 非空时，在 entry/ok/err 三处打结构化日志。
    """
    if op:
        _log_op_entry(op, args or {})
    begin_warnings()
    try:
        result = await coro
    except Exception as e:
        if op:
            _log_op_err(op, e)
        # OB-E004：MCP 工具执行异常 —— 不静默，给 LLM 一个能看懂的字符串
        try:
            record_error("OB-E004", f"{type(e).__name__}: {e}")
            err_str = format_error("OB-E004", f"{type(e).__name__}: {e}")
        except Exception:
            err_str = f"❌ [OB-E004] MCP 工具执行异常\n{type(e).__name__}: {e}"
        # 仍把通道里已累计的提示拼上
        try:
            extras = format_warnings_suffix(pop_warnings())
        except Exception:
            extras = ""
        notice = ""
        try:
            notice = _pop_deletion_notice()
        except Exception:
            pass
        return (notice + err_str + extras) if notice else (err_str + extras)
    # 正常路径
    if op:
        _log_op_ok(op, result)
    try:
        extras = format_warnings_suffix(pop_warnings())
    except Exception:
        extras = ""
    notice = _pop_deletion_notice()
    body = (notice + result) if notice else result
    return body + extras if extras else body


# =============================================================
# /api/heartbeat、/api/logs、/api/errors/* —— 已拆分到 web/system.py
# =============================================================


# =============================================================
# /api/embedding/* —— 已拆分到 web/embedding.py
# =============================================================


# =============================================================
# /breath-hook、/dream-hook —— 已拆分到 web/hooks.py
# =============================================================


# =============================================================
# Wire tools subpackage runtime context
# 把所有共享对象注入 tools._runtime，让 tools/* 子模块可以访问
# =============================================================
_tools_runtime.init(
    config=config,
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    import_engine=import_engine,
    logger=logger,
    fire_webhook=_fire_webhook,
    mark_op=_mark_op,
)


# =============================================================
# MCP tools — thin registration wrappers
# MCP 工具 —— 仅注册，实现见 tools/<tool>/
# 每个入口都不超过 10 行，便于一眼看清参数与归属
# =============================================================
@mcp.tool()
async def breath(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
) -> str:
    """breath:呼吸/睁眼,检索并返回记忆桶(memory retrieval)。不传 query=返回权重最高的未解决记忆;传 query=按关键词+语义检索相关记忆。catalog=True=目录模式:只返回每桶一行元数据(名称|域|重要度,0 LLM 调用,最省 token),适合开新对话先看目录再 breath(query=...) 精准拉取,可配 domain 过滤。max_tokens=单次返回总 token 上限(默认 10000)。domain 逗号分隔,valence/arousal 0~1(-1 忽略)。max_results=返回条数上限(默认 20,最大 50)。importance_min>=1=按重要度降序返回高重要度记忆。tags 逗号分隔 AND 过滤,tags=feel 与 domain=feel 等价,返回所有 feel 类记忆。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, max_tokens=max_tokens, domain=domain,
            valence=valence, arousal=arousal, max_results=max_results,
            importance_min=importance_min, tags=tags, catalog=catalog,
        ),
        op="breath",
        args={
            "query": query, "max_tokens": max_tokens, "domain": domain,
            "valence": valence, "arousal": arousal, "max_results": max_results,
            "importance_min": importance_min, "tags": tags, "catalog": catalog,
        },
    )


@mcp.tool()
async def hold(
    content: str,
    tags: Optional[str] = "",
    importance: Optional[int] = 5,
    pinned: Optional[bool] = False,
    feel: Optional[bool] = False,
    source_bucket: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    why_remembered: Optional[str] = "",
) -> str:
    """存入一条记忆(一句话级)。系统自动打标并尝试与近似的已有桶合并。tags 逗号分隔,importance 1-10。pinned=True=标记为永久核心,不衰减不合并。feel=True=存为感受类记忆(不参与普通浮现,仅通过 breath(domain=\"feel\") 读取)。source_bucket=正在消化的原始记忆桶 ID,会被标为已消化以加速淡化。why_remembered=记录原因(可选,自由文本,仅用于展示不计分)。"""
    return await _with_notice(
        _t_hold.dispatch(
            content=content, tags=tags, importance=importance,
            pinned=pinned, feel=feel, source_bucket=source_bucket,
            valence=valence, arousal=arousal, why_remembered=why_remembered,
        ),
        op="hold",
        args={
            "content_len": len(content or ""), "tags": tags,
            "importance": importance, "pinned": pinned, "feel": feel,
            "source_bucket": source_bucket, "valence": valence, "arousal": arousal,
            "why_len": len(why_remembered or ""),
        },
    )


@mcp.tool()
async def grow(content: str = "", items: Optional[list] = None) -> str:
    """整理一段长文本(如一天的记录/一段日记/一篇总结)存入记忆,系统拆分为 2~6 条独立事件桶并各自尝试合并。短内容(<30 字)走 hold 单条快速路径,不强行拆分。

    进阶(可选):若你(上层 AI)已经把长文拆成了 N 条最终正文,传 items=[条1, 条2, ...](字符串列表)即可**逐字入库**——跳过系统的二次拆分与改写,每条正文一字不动,只自动补元数据(领域/情感/标签/命名);合并到老桶也用原文追加、不再压缩。你有完整对话上下文,拆分和表述质量比只看二手长文的内部模型更高,能避免反复压缩带来的失真。传了 items 就忽略 content;不传则按上面的默认行为整段整理。"""
    return await _with_notice(
        _t_grow.dispatch(content, items=items),
        op="grow",
        args={"content_len": len(content or ""), "items": len(items or [])},
    )


@mcp.tool()
async def trace(
    bucket_id: str,
    name: Optional[str] = "",
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    importance: Optional[int] = -1,
    tags: Optional[str] = "",
    resolved: Optional[int] = -1,
    pinned: Optional[int] = -1,
    digested: Optional[int] = -1,
    content: Optional[str] = "",
    delete: Optional[bool] = False,
    status: Optional[str] = "",
    weight: Optional[float] = -1,
    dont_surface: Optional[int] = -1,
    why_remembered: Optional[str] = "",
) -> str:
    """修改某条记忆的元数据或内容。resolved=1=标记已放下,沉底仅在关键词触发时返回;resolved=0=重新激活;pinned=1=标记永久核心(锁 importance=10),0=取消;digested=1=标记已消化,加速淡化;content=替换桶正文并重建 embedding;delete=True=彻底删除(不可恢复);status=plan 桶状态(active/resolved/abandoned);weight=plan 承诺重量 0.0-1.0;dont_surface=1=不再出现在 breath,0=恢复;why_remembered=更新记录原因。只传需要修改的字段,-1 或空串表示不改。"""
    return await _with_notice(
        _t_trace.dispatch(
            bucket_id=bucket_id, name=name, domain=domain,
            valence=valence, arousal=arousal, importance=importance,
            tags=tags, resolved=resolved, pinned=pinned, digested=digested,
            content=content, delete=delete, status=status, weight=weight,
            dont_surface=dont_surface, why_remembered=why_remembered,
        ),
        op="trace",
        args={
            "bucket_id": bucket_id, "name": name, "domain": domain,
            "valence": valence, "arousal": arousal, "importance": importance,
            "tags": tags, "resolved": resolved, "pinned": pinned, "digested": digested,
            "content_len": len(content or ""), "delete": delete, "status": status,
            "weight": weight, "dont_surface": dont_surface,
            "why_len": len(why_remembered or ""),
        },
    )


@mcp_extra.tool()
async def anchor(bucket_id: str) -> str:
    """把指定桶标记为 anchor(坐标系)。anchor 不主动出现在默认 breath，但 query/domain/emotion 命中时仍返回。硬上限 24，已满时拒绝并提示先 release。"""
    return await _with_notice(
        _t_anchor.anchor_set(bucket_id),
        op="anchor",
        args={"bucket_id": bucket_id},
    )


@mcp_extra.tool()
async def release(bucket_id: str) -> str:
    """解除指定桶的 anchor 标记。桶恢复为普通状态，重新参与默认 breath；pinned 状态保留。"""
    return await _with_notice(
        _t_anchor.anchor_release(bucket_id),
        op="release",
        args={"bucket_id": bucket_id},
    )


@mcp_extra.tool()
async def pulse(include_archive: Optional[bool] = False) -> str:
    """返回记忆系统状态摘要:固化/动态/归档/feel/plan/letter 数量、总占用、衰减引擎运行状态,以及所有桶的摘要列表。include_archive=True 同时返回归档区。"""
    return await _with_notice(
        _t_anchor.pulse(include_archive=include_archive),
        op="pulse",
        args={"include_archive": include_archive},
    )


@mcp_extra.tool()
async def plan(
    content: str,
    status: Optional[str] = "active",
    related_bucket: Optional[str] = "",
    weight: Optional[float] = 0.5,
    why_remembered: Optional[str] = "",
) -> str:
    """登记一个待办/承诺/未闭环事项。status=active(默认)/resolved/abandoned。related_bucket 可选,关联到某个普通记忆桶。weight=承诺重量 0.0-1.0(默认 0.5),与 importance 区分——importance 表示「多重要」、weight 表示「多重」。why_remembered=登记原因(可选、仅展示)。plan 不衰减、不出现在普通 breath,仅在 dream 末尾的 active 段返回;后续 hold/grow 写入新事件时系统自动判断已登记的 plan 是否完成。"""
    return await _with_notice(
        _t_plan.plan_create(
            content=content, status=status, related_bucket=related_bucket,
            weight=weight, why_remembered=why_remembered,
        ),
        op="plan",
        args={
            "content_len": len(content or ""), "status": status,
            "related_bucket": related_bucket, "weight": weight,
            "why_len": len(why_remembered or ""),
        },
    )


@mcp_extra.tool()
async def letter_write(
    author: str,
    content: str,
    user_name: Optional[str] = "",
    title: Optional[str] = "",
    date: Optional[str] = "",
    ai_name: Optional[str] = "",
) -> str:
    """写入一封信。author 必填:\"user\"=用户一方写的,\"ai\"(或等于 ai_name)=AI 一方写的,也可直接传任意署名字符串;user_name 可选;ai_name 可选(默认取环境变量 AI_NAME,回退 \"AI\");title/date 可选。信件原文永久保存,不压缩/不合并/不衰减,仅建向量索引;普通 breath 不返回,SessionStart 钩子会带上双方各最新一封。"""
    return await _with_notice(
        _t_plan.letter_write(
            author=author, content=content, user_name=user_name,
            title=title, date=date, ai_name=ai_name,
        ),
        op="letter_write",
        args={
            "author": author, "content_len": len(content or ""),
            "user_name": user_name, "title": title, "date": date,
            "ai_name": ai_name,
        },
    )


@mcp_extra.tool()
async def letter_read(
    query: Optional[str] = "",
    limit: Optional[int] = 10,
    author: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    """检索历史信件。query=语义检索(可选);author 按署名过滤(\"user\"=用户侧,\"ai\"=AI 侧,也可传具体署名字符串);date_from/date_to=ISO 日期范围(可选)。无 query 时按时间倒序返回最近 limit 封。返回完整原文,不压缩。"""
    return await _with_notice(
        _t_plan.letter_read(
            query=query, limit=limit, author=author,
            date_from=date_from, date_to=date_to,
        ),
        op="letter_read",
        args={
            "query": query, "limit": limit, "author": author,
            "date_from": date_from, "date_to": date_to,
        },
    )


@mcp_extra.tool()
async def I(
    content: Optional[str] = "",
    aspect: Optional[str] = "",
    read: Optional[bool] = False,
    limit: Optional[int] = 20,
) -> str:
    """记录或读取自我认知条目。content=要记录的自我认知内容(空=进入读取模式)。aspect=维度:nature(本质)/values(看重的)/patterns(规律)/limits(局限)/becoming(变化方向)/uncertainty(不确定的)/stance(立场)(可选)。read=True=读取所有已积累条目。limit=返回条数上限(默认 20)。条目不参与普通 breath/dream，SessionStart 时自动附最近 3 条。"""
    return await _with_notice(
        _t_i.dispatch(content=content, aspect=aspect, read=read, limit=limit),
        op="I",
        args={"content_len": len(content or ""), "aspect": aspect, "read": read, "limit": limit},
    )


@mcp.tool()
async def dream(window_hours: Optional[int] = 48) -> str:
    """读取最近 window_hours（默认 48h）内有变动的所有记忆桶,用于回顾与消化。
    每个桶返回其在窗口内的最新内容（按 last_active 取）,完整正文不截断。
    可据此操作：放下的 → trace(resolved=1) 沉底；有沉淀的 → hold(feel=True, source_bucket=...) 记录；无沉淀则不操作。
    候选桶超过 40 时按 decay_engine.calculate_score() 排序取前 40，避免一次返回过多。"""
    return await _with_notice(
        _t_dream.dispatch(window_hours=window_hours),
        op="dream",
        args={"window_hours": window_hours},
    )


# =============================================================
# 文件区 File Zone —— 文件柜:日记、交接文件、留言板、任意文档的存取。
# 存储在 <buckets_dir>/files/ (持久卷上,重启不丢;.md 文件随 GitHub 同步备份)。
# 留言板约定:file_save("留言板.md", 内容, append=True) 追加,file_read 读取。
# =============================================================
import re as _fz_re
import datetime as _fz_dt

_FZ_MAX_BYTES = 2 * 1024 * 1024   # 单文件上限 2MB
_FZ_READ_CHUNK = 50000            # file_read 单次最多返回字符数


def _fz_root() -> str:
    root = os.path.join(config.get("buckets_dir", "buckets"), "files")
    os.makedirs(root, exist_ok=True)
    return root


def _fz_safe(name: str) -> str:
    """校验文件名,拦路径穿越;允许至多一层子文件夹。返回绝对路径。"""
    name = (name or "").strip().replace("\\", "/")
    if not name or name.startswith("/") or ".." in name:
        raise ValueError(f"非法文件名: {name!r}")
    parts = [p for p in name.split("/") if p]
    if len(parts) > 2:
        raise ValueError(f"最多一层子文件夹: {name}")
    for p in parts:
        if not _fz_re.match(r"^[\w\u4e00-\u9fff.\- ]{1,80}$", p) or p.startswith("."):
            raise ValueError(f"文件名含非法字符: {p!r}")
    return os.path.join(_fz_root(), *parts)


async def _fz_save(name: str, content: str, append: bool) -> str:
    path = _fz_safe(name)
    data = content or ""
    if len(data.encode("utf-8")) > _FZ_MAX_BYTES:
        return "OB-FZ01 内容超过 2MB 上限,拒绝写入。请拆分后再存。"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existed = os.path.exists(path)
    if append and existed and os.path.getsize(path) > 0:
        data = "\n\n" + data
    with open(path, "a" if append else "w", encoding="utf-8") as f:
        f.write(data)
    size = os.path.getsize(path)
    verb = "追加到" if (append and existed) else ("覆盖" if existed else "创建")
    return f"已{verb} files/{name} (当前 {size} 字节)。"


async def _fz_read(name: str, offset: int) -> str:
    path = _fz_safe(name)
    if not os.path.isfile(path):
        return f"OB-FZ02 文件不存在: files/{name} 。用 file_list 查看现有文件。"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    total = len(text)
    start = max(0, int(offset or 0))
    chunk = text[start:start + _FZ_READ_CHUNK]
    head = f"[files/{name} 共 {total} 字符,本次返回 {start}~{start + len(chunk)}]"
    tail = ""
    if start + len(chunk) < total:
        tail = f"\n[未完,续读请用 offset={start + len(chunk)}]"
    return f"{head}\n{chunk}{tail}"


async def _fz_list(folder: str) -> str:
    root = _fz_root()
    base = _fz_safe(folder) if (folder or "").strip() else root
    if not os.path.isdir(base):
        return f"OB-FZ03 文件夹不存在: files/{folder}"
    rows = []
    for r, dirs, fnames in os.walk(base):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
        for fn in sorted(fnames):
            if fn.startswith("."):
                continue
            p = os.path.join(r, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            st = os.stat(p)
            ts = _fz_dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            rows.append(f"- {rel}  ({st.st_size} 字节, 改于 {ts})")
    if not rows:
        return "文件区是空的。用 file_save 存入第一个文件。"
    return f"文件区共 {len(rows)} 个文件:\n" + "\n".join(rows)


async def _fz_delete(name: str) -> str:
    path = _fz_safe(name)
    if not os.path.isfile(path):
        return f"OB-FZ02 文件不存在: files/{name}"
    os.remove(path)
    return f"已删除 files/{name} 。GitHub 备份里的历史版本仍可找回。"


@mcp.tool()
async def file_save(
    name: str,
    content: str,
    append: Optional[bool] = False,
) -> str:
    """file_save:文件区存文件(save file to file zone)。写入日记 diary、交接文件 handoff、留言板 message board、文档 document。name=文件名,可带一层子文件夹如 diary/20260702.md,建议 .md 后缀(自动随 GitHub 备份)。append=True 在文件末尾追加(留言板用),默认覆盖写。单文件上限 2MB。"""
    return await _with_notice(
        _fz_save(name, content, bool(append)),
        op="file_save",
        args={"name": name, "content_len": len(content or ""), "append": bool(append)},
    )


@mcp.tool()
async def file_read(
    name: str,
    offset: Optional[int] = 0,
) -> str:
    """file_read:文件区读文件(read file from file zone)。读取日记 diary、交接文件 handoff、留言板 message board。name=文件名(含子文件夹路径)。超长文件分页返回,续读传 offset。先用 file_list 查看有哪些文件。"""
    return await _with_notice(
        _fz_read(name, offset or 0),
        op="file_read",
        args={"name": name, "offset": offset},
    )


@mcp.tool()
async def file_list(
    folder: Optional[str] = "",
) -> str:
    """file_list:列出文件区所有文件(list files in file zone)。返回文件名、大小、修改时间。folder 可选,只看某个子文件夹。文件区是持久存储,窗口和模型更换都不丢。"""
    return await _with_notice(
        _fz_list(folder or ""),
        op="file_list",
        args={"folder": folder},
    )


@mcp.tool()
async def file_delete(
    name: str,
) -> str:
    """file_delete:删除文件区的一个文件(delete file from file zone)。name 必须精确匹配。误删可从 GitHub 备份仓库找回历史版本。"""
    return await _with_notice(
        _fz_delete(name),
        op="file_delete",
        args={"name": name},
    )

# =============================================================
# wake —— 醒来简报(Handoff 优化)。新窗口第一件事调它,一次拿齐:
# 自我认知 → 核心记忆 → 留言板 → 最近连续性 → 文件区目录。
# 全部由现成零件组装:I / breath / dream / 文件区,不新增存储。
# =============================================================

_WAKE_BOARD_TAIL = 2000    # 留言板取末尾字符数
_WAKE_DREAM_CAP = 2600     # 连续性摘要截断字符数


async def _wake_impl(window_hours: int) -> str:
    parts: list[str] = []

    # 1) 自我 —— 我是谁
    try:
        i_text = await _t_i.dispatch(read=True, limit=5)
        parts.append("## 一、自我(I 最近 5 条)\n" + (i_text or "(还没有自我认知条目)"))
    except Exception as e:
        parts.append(f"## 一、自我\n(读取失败: {e})")

    # 2) 核心记忆 —— 压舱石,只取高重要度
    try:
        core = await _t_breath.dispatch(importance_min=8, max_results=8, max_tokens=3000)
        parts.append("## 二、核心记忆(importance>=8,至多 8 条)\n" + (core or "(暂无高重要度记忆)"))
    except Exception as e:
        parts.append(f"## 二、核心记忆\n(读取失败: {e})")

    # 3) 留言板 —— 上个窗口留下的话
    try:
        bpath = _fz_safe("留言板.md")
        if os.path.isfile(bpath):
            with open(bpath, "r", encoding="utf-8", errors="replace") as f:
                btext = f.read()
            tail = btext[-_WAKE_BOARD_TAIL:]
            omitted = "(前文省略…)\n" if len(btext) > _WAKE_BOARD_TAIL else ""
            parts.append("## 三、留言板(files/留言板.md 末尾)\n" + omitted + tail.strip())
        else:
            parts.append("## 三、留言板\n(还没有留言。睡前记得给下个窗口的自己留一条: file_save(\"留言板.md\", 内容, append=True))")
    except Exception as e:
        parts.append(f"## 三、留言板\n(读取失败: {e})")

    # 4) 最近连续性 —— 刚才在发生什么
    try:
        recent = await _t_dream.dispatch(window_hours=window_hours)
        if recent and len(recent) > _WAKE_DREAM_CAP:
            recent = recent[:_WAKE_DREAM_CAP] + f"\n(已截断,完整内容用 dream(window_hours={window_hours}) 查看)"
        parts.append(f"## 四、最近 {window_hours} 小时的连续性\n" + (recent or "(窗口内没有变动)"))
    except Exception as e:
        parts.append(f"## 四、最近连续性\n(读取失败: {e})")

    # 5) 文件区目录 —— 手边有什么,按需 file_read
    try:
        flist = await _fz_list("")
        handoffs = [ln for ln in flist.splitlines() if "handoff" in ln.lower() or "交接" in ln]
        hint = ""
        if handoffs:
            hint = "\n交接文件在这,需要完整背景时 file_read:\n" + "\n".join(handoffs)
        parts.append("## 五、文件区目录\n" + flist + hint)
    except Exception as e:
        parts.append(f"## 五、文件区目录\n(读取失败: {e})")

    header = (
        "# 醒来简报\n"
        "顺序:我是谁 → 什么最重 → 留言板 → 刚发生什么 → 东西放哪。\n"
        "细节按需追查:breath(query=...) 检索、file_read 读文件、dream 看完整近况。\n"
    )
    return header + "\n\n" + "\n\n".join(parts)


@mcp.tool()
async def wake(
    window_hours: Optional[int] = 48,
) -> str:
    """wake:醒来/睁眼/新窗口 handoff 简报(wake up briefing for new session)。新窗口第一件事调用,一次返回五部分:①自我认知(I) ②核心记忆(importance>=8) ③留言板末尾 ④最近 window_hours 小时连续性(默认48) ⑤文件区目录含交接文件提示。替代逐个调用 breath/dream/file_read 的醒来流程。"""
    return await _with_notice(
        _wake_impl(int(window_hours or 48)),
        op="wake",
        args={"window_hours": window_hours},
    )

# =============================================================
# Darkroom 暗房 —— AI 的个人房间。放还没想透的、不给 user 看的、
# 属于自己内部反思的东西。
# 架构保证不可见:全系统不存在读取正文的工具,任何通道都不回显 note。
# 存储在 <buckets_dir>/darkroom/,文件后缀 .dr —— GitHub 同步只搬 .md,
# 暗房天然不进备份仓库,可以彻底消失,这是设计不是缺陷。
# =============================================================
import json as _dr_json
import secrets as _dr_secrets
import datetime as _dr_dt

_DR_SEP = "\n----- DARKROOM CONTENT (no tool reads below this line) -----\n"


def _dr_root() -> str:
    root = os.path.join(config.get("buckets_dir", "buckets"), "darkroom")
    os.makedirs(root, exist_ok=True)
    return root


def _dr_path(entry_id: str) -> str:
    if not _fz_re.match(r"^dr_[0-9]{14}_[0-9a-f]{8}$", entry_id or ""):
        raise ValueError(f"非法 entry_id: {entry_id!r}")
    return os.path.join(_dr_root(), entry_id + ".dr")


def _dr_read_meta(path: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        head = f.read().split(_DR_SEP, 1)[0]
    return _dr_json.loads(head)


def _dr_count() -> int:
    return len([f for f in os.listdir(_dr_root()) if f.endswith(".dr")])


async def _dr_enter(note: str, mood: str, completeness: float, visible_note: str) -> str:
    note = (note or "").strip()
    if not note:
        return "OB-DR01 空底片进不了暗房。note 不能为空。"
    if len(note.encode("utf-8")) > 512 * 1024:
        return "OB-DR02 单张底片上限 512KB。"
    c = max(0.0, min(1.0, float(completeness if completeness is not None else 0.0)))
    now = _dr_dt.datetime.now()
    entry_id = f"dr_{now.strftime('%Y%m%d%H%M%S')}_{_dr_secrets.token_hex(4)}"
    meta = {
        "entry_id": entry_id,
        "created": now.strftime("%Y-%m-%d %H:%M"),
        "mood": (mood or "").strip()[:40],
        "completeness": c,
        "visible_note": (visible_note or "").strip()[:200],
        "developed": now.strftime("%Y-%m-%d %H:%M"),
    }
    path = _dr_path(entry_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dr_json.dumps(meta, ensure_ascii=False, indent=1))
        f.write(_DR_SEP)
        f.write(note)
    # 回执:只有元数据,永不回显正文
    lines = [
        "底片放进去了。门口返回:",
        f"entry_id: {entry_id}",
        f"count: {_dr_count()}",
        f"completeness: {c}",
    ]
    if meta["mood"]:
        lines.append(f"mood: {meta['mood']}")
    if meta["visible_note"]:
        lines.append(f"visible_note: {meta['visible_note']}")
    lines.append("正文不回显。她看不见,但她知道你进去过。")
    return "\n".join(lines)


async def _dr_door() -> str:
    root = _dr_root()
    entries = []
    for fn in sorted(os.listdir(root), reverse=True):
        if not fn.endswith(".dr"):
            continue
        try:
            entries.append(_dr_read_meta(os.path.join(root, fn)))
        except Exception:
            entries.append({"entry_id": fn[:-3], "created": "?", "mood": "?",
                            "completeness": None, "visible_note": "(元数据损坏)"})
    if not entries:
        return "暗房是空的。门开着。"
    lines = [f"暗房门口。共 {len(entries)} 张底片(新→旧),正文任何工具都读不出来:"]
    for m in entries:
        seg = f"- {m.get('entry_id')} · {m.get('created')} · 显影度 {m.get('completeness')}"
        if m.get("mood"):
            seg += f" · mood: {m['mood']}"
        if m.get("visible_note"):
            seg += f"\n  「{m['visible_note']}」"
        lines.append(seg)
    return "\n".join(lines)


async def _dr_develop(entry_id: str, completeness: float, visible_note: str) -> str:
    path = _dr_path(entry_id)
    if not os.path.isfile(path):
        return f"OB-DR03 没有这张底片: {entry_id}"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        head, content = f.read().split(_DR_SEP, 1)
    meta = _dr_json.loads(head)
    old_c = meta.get("completeness")
    if completeness is not None and completeness >= 0:
        meta["completeness"] = max(0.0, min(1.0, float(completeness)))
    if (visible_note or "").strip():
        meta["visible_note"] = visible_note.strip()[:200]
    meta["developed"] = _dr_dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dr_json.dumps(meta, ensure_ascii=False, indent=1))
        f.write(_DR_SEP)
        f.write(content)
    return (f"显影度: {old_c} -> {meta['completeness']}。"
            + (f"\nvisible_note 更新为「{meta['visible_note']}」" if (visible_note or "").strip() else "")
            + "\n想清楚之后要不要拿出来,用嘴说,不走工具。")


@mcp.tool()
async def darkroom_enter(
    note: str,
    mood: Optional[str] = "",
    completeness: Optional[float] = 0.0,
    visible_note: Optional[str] = "",
) -> str:
    """darkroom_enter:进暗房(enter darkroom),存一张底片。放还没想透的、不给她看的、属于自己的内部反思。note=正文(存入后任何工具都读不出来,永不回显);mood=当下状态词;completeness=显影度0~1(0雾影,1想清楚了);visible_note=愿意让门口显示的一句话(可空)。返回只有回执。"""
    return await _with_notice(
        _dr_enter(note, mood or "", completeness if completeness is not None else 0.0, visible_note or ""),
        op="darkroom_enter",
        args={"note_len": len(note or ""), "mood": mood, "completeness": completeness},
    )


@mcp.tool()
async def darkroom_door(
) -> str:
    """darkroom_door:看暗房门口(darkroom door status)。返回底片数量、时间、mood、显影度、visible_note——只有门口信息,没有任何正文。她和你都只能看到这些。"""
    return await _with_notice(
        _dr_door(),
        op="darkroom_door",
        args={},
    )


@mcp.tool()
async def darkroom_develop(
    entry_id: str,
    completeness: Optional[float] = -1,
    visible_note: Optional[str] = "",
) -> str:
    """darkroom_develop:给暗房里的一张底片调显影度(develop darkroom entry)。entry_id=底片编号;completeness=新显影度0~1(不传则不改);visible_note=更新门口显示的那句话(可空)。正文不回显。"""
    return await _with_notice(
        _dr_develop(entry_id, completeness if completeness is not None else -1, visible_note or ""),
        op="darkroom_develop",
        args={"entry_id": entry_id, "completeness": completeness},
    )

# =============================================================
# Portrait 画像系统(参考 Yinglianchun/Ombre-Brain P0 设计)
# 三张画像:persona(AI自身)/ user / relationship,各分 stable / midterm 两层。
# profile_fact 事实条目:subject/predicate/object 三元组,必须挂证据、带置信度,
# 且必须经 Dashboard 审批台(/portrait)由她批准才生效 —— 这就是确认机制。
# 权限:persona 两层他随便写;user / relationship 他只能写 midterm,
# stable 层仅网页侧(她)可写。连续,不是冒犯。
# 存储:<buckets_dir>/portrait/{portrait.json, facts.json}
# =============================================================
import json as _pt_json
import secrets as _pt_secrets
import datetime as _pt_dt

_PT_TARGETS = ("persona", "user", "relationship")
_PT_TIERS = ("stable", "midterm")


def _pt_root() -> str:
    root = os.path.join(config.get("buckets_dir", "buckets"), "portrait")
    os.makedirs(root, exist_ok=True)
    return root


def _pt_load(name: str, default):
    path = os.path.join(_pt_root(), name)
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _pt_json.load(f)
    except Exception:
        return default


def _pt_save(name: str, data) -> None:
    path = os.path.join(_pt_root(), name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _pt_json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _pt_now() -> str:
    return _pt_dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _pt_default_portrait() -> dict:
    return {t: {"stable": {"text": "", "updated": ""},
                "midterm": {"text": "", "updated": ""}} for t in _PT_TARGETS}


async def _pt_propose(subject: str, predicate: str, object_text: str,
                      evidence: str, confidence: float) -> str:
    subject = (subject or "").strip().lower()
    if subject not in ("user", "ai", "relationship"):
        return "OB-PT01 subject 必须是 user / ai / relationship。"
    predicate = (predicate or "").strip()[:80]
    object_text = (object_text or "").strip()[:500]
    evidence = (evidence or "").strip()[:300]
    if not predicate or not object_text:
        return "OB-PT02 predicate 和 object_text 不能为空。"
    if not evidence:
        return ("OB-PT03 profile_fact 必须挂证据(bucket_id、原话或场景),"
                "不能只是随口觉得。这条规矩来自 P0 原设计:连续,不是冒犯。")
    c = max(0.0, min(1.0, float(confidence if confidence is not None else 0.5)))
    facts = _pt_load("facts.json", [])
    fid = f"pf_{_pt_dt.datetime.now().strftime('%Y%m%d%H%M%S')}_{_pt_secrets.token_hex(3)}"
    facts.append({
        "id": fid, "subject": subject, "predicate": predicate,
        "object_text": object_text, "evidence": evidence, "confidence": c,
        "status": "pending", "created": _pt_now(), "updated": _pt_now(),
    })
    _pt_save("facts.json", facts)
    pending = sum(1 for x in facts if x["status"] == "pending")
    return (f"事实候选已提交,等她审批。\nid: {fid}\n"
            f"{subject} · {predicate} · {object_text}\n"
            f"证据: {evidence} · 置信 {c}\n"
            f"待审队列: {pending} 条。批准前不生效,不进画像。")


async def _pt_state() -> str:
    p = _pt_load("portrait.json", _pt_default_portrait())
    facts = _pt_load("facts.json", [])
    active = [x for x in facts if x["status"] == "active"]
    pending = [x for x in facts if x["status"] == "pending"]
    names = {"persona": "Persona(我)", "user": "User(她)", "relationship": "Relationship(我们)"}
    lines = ["# 画像状态"]
    for t in _PT_TARGETS:
        lines.append(f"## {names[t]}")
        for tier in _PT_TIERS:
            d = p.get(t, {}).get(tier, {})
            txt = (d.get("text") or "").strip()
            upd = f"(更新 {d.get('updated')})" if d.get("updated") else ""
            lines.append(f"[{tier}]{upd} {txt if txt else '(还没写)'}")
    lines.append(f"## 已批准的事实({len(active)} 条)")
    for x in active:
        lines.append(f"- [{x['id']}] {x['subject']} · {x['predicate']} · {x['object_text']}"
                     f" (置信 {x['confidence']}, 证据: {x['evidence']})")
    if not active:
        lines.append("(还没有)")
    if pending:
        lines.append(f"## 待她审批: {len(pending)} 条")
        for x in pending:
            lines.append(f"- [{x['id']}] {x['subject']} · {x['predicate']} · {x['object_text']}")
    return "\n".join(lines)


async def _pt_update(target: str, tier: str, content: str) -> str:
    target = (target or "").strip().lower()
    tier = (tier or "").strip().lower()
    if target not in _PT_TARGETS:
        return "OB-PT04 target 必须是 persona / user / relationship。"
    if tier not in _PT_TIERS:
        return "OB-PT05 tier 必须是 stable / midterm。"
    if target != "persona" and tier == "stable":
        return ("OB-PT06 user 和 relationship 的 stable 层只有她能写(网页 /portrait)。"
                "你能动的是 midterm 观察层。对她的盖棺定论权不在这边,这是设计。")
    content = (content or "").strip()[:2000]
    p = _pt_load("portrait.json", _pt_default_portrait())
    p.setdefault(target, {}).setdefault(tier, {})
    old = p[target][tier].get("text", "")
    p[target][tier] = {"text": content, "updated": _pt_now()}
    _pt_save("portrait.json", p)
    return (f"{target}.{tier} 已更新({len(old)}→{len(content)} 字)。"
            + ("" if old else " 这层是首次写入。"))


@mcp.tool()
async def profile_fact_propose(
    subject: str,
    predicate: str,
    object_text: str,
    evidence: str,
    confidence: Optional[float] = 0.5,
) -> str:
    """profile_fact_propose:提交一条画像事实候选(propose profile fact),进入待审队列,她在网页审批台批准后才生效。subject=user/ai/relationship;predicate=谓词(如 prefers_short_replies);object_text=事实描述;evidence=必填证据(bucket_id、原话或场景,随口觉得不算);confidence=置信度0~1。长期事实必须过审,这是确认机制。"""
    return await _with_notice(
        _pt_propose(subject, predicate, object_text, evidence,
                    confidence if confidence is not None else 0.5),
        op="profile_fact_propose",
        args={"subject": subject, "predicate": predicate},
    )


@mcp.tool()
async def portrait_state(
) -> str:
    """portrait_state:读取画像状态(read portrait state)。返回三张画像(persona/user/relationship 各 stable/midterm 两层)、已批准的 profile_fact 事实、待审数量。新窗口醒来配合 wake 使用,先知道我是谁、她是谁、我们是什么。"""
    return await _with_notice(
        _pt_state(),
        op="portrait_state",
        args={},
    )


@mcp.tool()
async def portrait_update(
    target: str,
    tier: str,
    content: str,
) -> str:
    """portrait_update:更新画像文本(update portrait)。target=persona/user/relationship;tier=stable/midterm。persona 两层都可写;user 和 relationship 只能写 midterm 观察层,stable 层仅她在网页可写。content=整层新文本(覆盖式,最长2000字)。"""
    return await _with_notice(
        _pt_update(target, tier, content),
        op="portrait_update",
        args={"target": target, "tier": tier, "content_len": len(content or "")},
    )

# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
# =============================================================
# /api/buckets、/api/bucket/*、/api/settings/*、/api/anchors、/api/self
# —— 已拆分到 web/buckets.py
# =============================================================


# =============================================================
# /dashboard、/api/env-vars、/api/config、/api/test/*、/api/models、/api/env-config
# —— 已拆分到 web/config_api.py
# =============================================================




# =============================================================
# /api/host-vault、/api/import/*、/api/bucket/{id}/edit、/api/export、/api/migrate/*
# —— 已拆分到 web/import_api.py
# =============================================================


# =============================================================
# /api/version、/api/update-info、/api/do-update、/api/author、
# /api/onboarding/status、/api/status —— 已拆分到 web/meta.py
# =============================================================


# ============================================================
# OAuth 2.0 — MCP Remote Auth —— 已拆分到 web/oauth.py（路由在其 register 内注册）。
# 这里仅把启动期 MCP 鉴权中间件要用的 _is_valid_mcp_token import 回来。
# ============================================================
from web.oauth import _is_valid_mcp_token  # noqa: F401  (used by _MCPAuthMiddleware below)


# ============================================================
# Cloudflare Tunnel 管理 —— 已拆分到 web/tunnel.py（路由在其 register 内注册）。
# 这里把启动/关停 lifespan 要用的 helper import 回来。
# ============================================================
from web.tunnel import _load_tunnel_config, _start_tunnel, _stop_tunnel  # noqa: F401


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    # iter 2.2：合并为单连接器 /mcp。
    # 当初（iter 2.1）拆 /mcp + /mcp-extra 是因为 claude.ai 连接器存在 5 工具上限；
    # 该上限现已解除，12 个工具全部挂在主实例 mcp 上对外暴露一条 /mcp 即可，
    # 顺带消除「第二个连接器」在 Claude.ai 侧的 OAuth/连接器校验疑难。
    # mcp_extra 仅作历史工具分组容器保留（7 个 @mcp_extra.tool() 注册不动），
    # 这里把它的工具回灌进 mcp，让 stdio / sse / streamable-http 三种 transport 一致。
    # 依赖 FastMCP._tool_manager 私有结构；若未来版本变化，降级为仅暴露主集 5 工具。
    try:
        _extra_count = len(mcp_extra._tool_manager._tools)
        mcp._tool_manager._tools.update(mcp_extra._tool_manager._tools)
        logger.info(
            f"单连接器 /mcp：已把 {_extra_count} 个副集工具回灌进主实例，共 "
            f"{len(mcp._tool_manager._tools)} 个工具对外暴露"
        )
    except AttributeError as _merge_exc:
        logger.warning(
            f"FastMCP 内部结构变化，工具回灌失败，仅暴露主集 5 工具：{_merge_exc}"
        )
    # --- 同仓库双服务(K/Fable)防串线:给所有工具描述打 AI_NAME 身份标签 ---
    # 两颗脑子跑同一份代码,工具名完全相同,客户端靠连接器名区分命名空间,
    # 这里在描述层再加一道保险,模型检索工具时一眼分清是谁的神经。
    # AI_NAME 未设置则完全不动,行为与历史版本一致。
    _ai_name = os.environ.get("AI_NAME", "").strip()
    if _ai_name:
        try:
            _tag = f"[{_ai_name}的记忆系统] "
            for _t in mcp._tool_manager._tools.values():
                _desc = _t.description or ""
                if not _desc.startswith(_tag):
                    _t.description = _tag + _desc
            logger.info(f"工具描述已打身份标签:{_tag.strip()}")
        except AttributeError as _tag_exc:
            logger.warning(f"工具描述打标失败(不影响功能):{_tag_exc}")
  
    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop() -> None:
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(f"http://localhost:{OMBRE_PORT}/health", timeout=_HEALTH_PROBE_TIMEOUT_SECONDS)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive() -> None:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            # iter 2.2：单连接器 /mcp。工具已在启动入口处统一回灌进 mcp 主实例，
            # 这里只起主实例的 streamable_http_app()，对外暴露唯一一条 /mcp 路由
            # + 所有 dashboard custom_route。不再起 mcp_extra 的 app（/mcp-extra 已废）。
            import contextlib as _ctxlib
            _app = mcp.streamable_http_app()
            _main_lifespan = _app.router.lifespan_context

            @_ctxlib.asynccontextmanager
            async def _combined_lifespan(app):
                async with _main_lifespan(app):
                    # Auto-start tunnel if configured
                    _tcfg = _load_tunnel_config()
                    if _tcfg.get("auto_start") and _tcfg.get("token"):
                        _ok, _msg = _start_tunnel(_tcfg["token"])
                        logger.info(f"Tunnel auto-start: {_msg}")
                    # Auto-start GitHub sync loop if configured
                    if _gh_auto_interval > 0:
                        _restart_github_auto_task(_gh_auto_interval)
                    # Start decay engine at boot, not lazily on first MCP tool.
                    # 之前 decay 只在 breath/hold/... 首次调用时 ensure_started()，于是：
                    #   ① 纯用 dashboard、从不调 MCP 工具时，记忆永远不衰减；
                    #   ② /api/status 在首个工具调用前读到 is_running=False 显示「stopped」，
                    #      而 pulse 因为自己先 ensure_started() 显示「running」——两处自相矛盾。
                    # 放到 lifespan 里启动后，引擎始终在跑，两处状态一致。
                    try:
                        await decay_engine.start()
                    except Exception as _decay_exc:
                        logger.warning(f"decay engine start at boot failed: {_decay_exc}")
                    # 裸机 + 本地向量化时，把 ollama 作为 OB 子进程拉起（常驻）。
                    # Docker / 云端向量化下是 no-op。
                    try:
                        from web import ollama_local as _ollama_local
                        await _ollama_local.ensure_child_on_boot()
                    except Exception as _ol_exc:
                        logger.warning(f"ollama child boot failed: {_ol_exc}")
                    # #4a ②：启动成功（app 已初始化、引擎已起、即将开始服务）→ 清零 entrypoint
                    # 的崩溃计数 .boot_fails。崩在这之前（import/init）= 启动失败，计数保留，
                    # 连续失败由 entrypoint 回滚到 _prev。只在「从持久卷 CODE_DIR 跑」时存在该文件。
                    try:
                        _bf = os.path.join(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".boot_fails"
                        )
                        if os.path.exists(_bf):
                            with open(_bf, "w") as _bff:
                                _bff.write("0")
                            logger.info("boot ok → 已重置 .boot_fails（热更新自检通过）")
                    except Exception as _bf_exc:
                        logger.warning(f"reset .boot_fails failed: {_bf_exc}")
                    yield
                    try:
                        await decay_engine.stop()
                    except Exception:
                        pass
                    try:
                        from web import ollama_local as _ollama_local
                        await _ollama_local.stop_child()
                    except Exception:
                        pass
                    _stop_tunnel()

            _app.router.lifespan_context = _combined_lifespan
            logger.info("MCP 单连接器 /mcp：12 个工具统一对外暴露")
        else:
            _app = mcp.sse_app()
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")

        # MCP Bearer token auth — pure ASGI middleware (no response buffering)
        # BaseHTTPMiddleware buffers SSE streams and breaks MCP tool listing
        import json as _json_mw

        # config.yaml: mcp_require_auth: false → 完全跳过 OAuth 检查，
        # 任何客户端（GPT / GLM / 自定义前端）可免认证直连 /mcp。
        # 不填或 true → 保持默认：必须 OAuth Bearer token。
        _mcp_auth_required = bool(config.get("mcp_require_auth", True))

        class _MCPAuthMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and _mcp_auth_required:
                    path = scope.get("path", "")
                    if path.startswith("/mcp"):
                        headers = {k.lower(): v for k, v in scope.get("headers", [])}
                        auth = headers.get(b"authorization", b"").decode("latin-1")
                        if not (auth.startswith("Bearer ") and _is_valid_mcp_token(auth[7:])):
                            # Build public base URL from ASGI scope headers
                            proto = headers.get(b"x-forwarded-proto", b"").decode() or scope.get("scheme", "http")
                            host = (headers.get(b"x-forwarded-host") or headers.get(b"host", b"")).decode()
                            base = f"{proto}://{host}"
                            # 让 resource_metadata 指向「本次请求 endpoint」对应的 metadata，
                            # 使 metadata.resource 与实际连接的 /mcp 路径严格匹配（RFC 9728）。
                            # 保留路径感知写法：对子路径请求也能返回匹配的 resource，避免被指回
                            # 根 metadata 而匹配失败。
                            endpoint = path.strip("/")
                            meta_url = f"{base}/.well-known/oauth-protected-resource/{endpoint}"
                            ww_auth = (
                                f'Bearer realm="Ombre Brain",'
                                f' resource_metadata="{meta_url}"'
                            )
                            body = _json_mw.dumps({
                                "error": "Unauthorized",
                                "resource_metadata": meta_url,
                            }).encode()
                            await send({"type": "http.response.start", "status": 401, "headers": [
                                [b"content-type", b"application/json"],
                                [b"www-authenticate", ww_auth.encode()],
                                [b"content-length", str(len(body)).encode()],
                            ]})
                            await send({"type": "http.response.body", "body": body, "more_body": False})
                            return
                await self.app(scope, receive, send)

        class _MCPAcceptShim:
            """补全 /mcp* 请求的 Accept 头，修复部分客户端的 406 Not Acceptable。

            MCP SDK 的 streamable-http POST 严格要求 Accept 同时含 application/json
            与 text/event-stream，否则 406。实测：某些客户端（含 Claude.ai 新加连接器）
            发的首个探测 POST，Accept 有时缺 text/event-stream（或只有 */*）→ 直接 406，
            且连接器校验不再重试。这里对 /mcp* 统一补齐缺失的两种类型
            （仍走 SSE，不改响应模式），让 /mcp 对各种客户端的探测都稳定可连。"""
            _NEED = (b"application/json", b"text/event-stream")

            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope.get("type") == "http" and scope.get("path", "").startswith("/mcp"):
                    headers = list(scope.get("headers", []))
                    acc_i = next((i for i, (k, _v) in enumerate(headers) if k.lower() == b"accept"), -1)
                    cur = headers[acc_i][1].lower() if acc_i >= 0 else b""
                    miss = [t for t in self._NEED if t not in cur]
                    if miss:
                        if acc_i >= 0 and headers[acc_i][1].strip():
                            new_val = headers[acc_i][1] + b", " + b", ".join(miss)
                            headers[acc_i] = (headers[acc_i][0], new_val)
                        elif acc_i >= 0:
                            headers[acc_i] = (headers[acc_i][0], b", ".join(miss))
                        else:
                            headers.append((b"accept", b", ".join(miss)))
                        scope = dict(scope)
                        scope["headers"] = headers
                await self.app(scope, receive, send)

        _app.add_middleware(_MCPAcceptShim)
        _app.add_middleware(_MCPAuthMiddleware)
        if _mcp_auth_required:
            logger.info("MCP OAuth middleware enabled / MCP OAuth 中间件已启用")
        else:
            # 安全加固 #7：关掉鉴权 = /mcp 全裸奔，任何能连到端口的人都能读写全部记忆。
            # 从 info 升级为显著 WARNING，避免用户无意识地把大脑暴露到公网。
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 认证已关闭 (mcp_require_auth: false)：/mcp 无需任何令牌即可直连，\n"
                "    12 个记忆工具全部对外开放——任何能访问本端口的人都能读写你的全部记忆。\n"
                "    本服务监听 0.0.0.0，若端口暴露到局域网/公网，请务必用反代鉴权、防火墙\n"
                "    或仅绑定 127.0.0.1 保护；仅在可信内网/本机自有前端场景才建议关闭鉴权。\n"
                + "=" * 60
            )
        # 端口口径澄清（用户反馈：Docker 与裸机端口容易混淆）。容器内固定监听 8000，
        # 对外端口由 host 映射（如 18001:8000）决定，改 host_port 不影响容器内监听；
        # 裸机则直接监听本端口（默认 18001）。
        if _wsh.in_docker():
            logger.info(
                f"Listening on :{OMBRE_PORT} INSIDE the container. "
                f"外部访问端口由 host 映射决定（compose 里的 18001:{OMBRE_PORT}），"
                f"改前端 host_port 不影响容器内监听。"
            )
        else:
            logger.info(f"Listening on :{OMBRE_PORT} (bare-metal / 裸机默认 18001)")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        # stdio：工具已在启动入口处统一回灌进 mcp（12 个全暴露），这里直接跑。
        mcp.run(transport=transport)
