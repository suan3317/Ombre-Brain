"""
========================================
tools/pinboard.py — 家庭公告栏 Pinboard 代理工具
========================================

把两个 MCP 工具(broadcast_read/broadcast_post)转发到 chatnest 的
/broadcast-mcp 端点——一个独立的、按 token 署名的极简 JSON-RPC MCP 服务
(实现见 chatnest-home 仓库 app/broadcast_mcp.py)。本模块只做 HTTP 转发 +
结果透传,不落任何本地存储,不接触 bucket/记忆系统。

关键行为:
- POST env PINBOARD_URL,鉴权头 Authorization: Bearer <PINBOARD_TOKEN>
- body 是标准 JSON-RPC 2.0 tools/call 请求;署名完全由对端按 token 决定,
  这里既不传也不接受 author 参数
- 10 秒超时;任何失败(网络/超时/非 200/JSON 解析/JSON-RPC error)都返回
  可读错误文本,不抛出异常——跟 xhs_read 的防御风格保持一致
- 是否注册这两个工具由 server.py 在启动时按 env 是否齐全决定,本模块
  不做条件判断,只提供实现

不做什么(边界):
- 不做重试/退避(失败即返回错误文本,交给上层/用户决定是否重试)
- 不缓存结果
- 不在这里读取除 PINBOARD_URL/PINBOARD_TOKEN 之外的任何 env

对外暴露: broadcast_read_impl(limit) / broadcast_post_impl(content)
========================================
"""

import json
import os

import httpx

_TIMEOUT_SECONDS = 10.0


def _config() -> tuple[str, str]:
    url = os.environ.get("PINBOARD_URL", "").strip()
    token = os.environ.get("PINBOARD_TOKEN", "").strip()
    return url, token


async def _call_tool(name: str, arguments: dict) -> str:
    url, token = _config()
    if not url or not token:
        return "OB-PB01 Pinboard 未配置:缺少 PINBOARD_URL 或 PINBOARD_TOKEN。"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as e:
        return f"OB-PB02 请求失败: {e}"
    if resp.status_code != 200:
        return f"OB-PB03 Pinboard 返回状态码 {resp.status_code}: {resp.text[:300]}"
    try:
        data = resp.json()
    except Exception as e:
        return f"OB-PB04 响应不是合法 JSON: {e}"
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        message = err.get("message", err) if isinstance(err, dict) else err
        return f"OB-PB05 Pinboard 返回 JSON-RPC 错误: {message}"
    result = (data or {}).get("result") or {}
    content = result.get("content") or []
    if content and isinstance(content, list):
        text = content[0].get("text", "")
        if result.get("isError"):
            return f"OB-PB06 Pinboard 工具执行出错: {text}"
        return text
    return json.dumps(data, ensure_ascii=False)


async def broadcast_read_impl(limit: int) -> str:
    return await _call_tool("broadcast_read", {"limit": limit})


async def broadcast_post_impl(content: str) -> str:
    return await _call_tool("broadcast_post", {"content": content})
