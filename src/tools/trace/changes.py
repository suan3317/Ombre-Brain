"""
========================================
tools/trace/changes.py — trace 参数收口（N-1 #4，2026-11-10）
========================================

trace 有 1 个必填 + 27 个可选参数，每个 Optional 在 MCP schema 里展开成
anyOf + default + title，整份 schema 946 token（tiktoken），每轮都带。

收口办法（F 裁定）：
- 对外公布的 schema 只留 bucket_id + 6 个最常用的显式参数 + changes: dict；
  其余 21 个字段收进 changes={键: 值}，键名就是原来的参数名。
- server.trace 的 Python 签名一个不删。FastMCP 校验入参用的是签名生成的模型，
  所以旧写法 trace(bucket_id, domain="x") 照样进得来；只是 tools/list 不再列它们。
  （反过来把签名也砍掉是不行的：FastMCP 对签名里没有的参数静默丢弃，旧写法会
  "成功"但什么都没改。）

本模块只做两件纯函数的事，不碰桶：
- merge_changes：把 changes 摊平并进显式参数，冲突/未知键拒绝。
- slim_schema：从完整 inputSchema 里裁出对外公布的那一份。

对外暴露：PUBLIC_EXPLICIT_PARAMS, CHANGES_KEYS, merge_changes, slim_schema
========================================
"""

import json
from typing import Any, Optional

# 对外公布的显式参数（bucket_id 之外）。顺序即 schema 里的顺序。
PUBLIC_EXPLICIT_PARAMS = (
    "content",
    "importance",
    "pinned",
    "resolved",
    "delete",
    "meaning_append",
)

# 收进 changes 的字段。键名 = server.trace 的参数名，值的写法也不变。
CHANGES_KEYS = (
    "name",
    "domain",
    "tags",
    "valence",
    "arousal",
    "digested",
    "status",
    "weight",
    "dont_surface",
    "why_remembered",
    "meaning_replace",
    "media_append",
    "media_replace",
    "hard_delete",
    "delete_reason",
    "seed",
    "cited",
    "event_at",
    "superseded_by",
    "supersede_type",
    "supersede_effective_at",
)

# changes 里也允许写这六个（模型把所有改动都塞进 changes 是合理写法），照常生效。
_ALLOWED_IN_CHANGES = frozenset(CHANGES_KEYS) | frozenset(PUBLIC_EXPLICIT_PARAMS)

# "没传"的哨兵值：与 server.trace 签名里的默认值一致。
_UNSET = ("", -1, False, None)


def _is_unset(value: Any) -> bool:
    # bool 是 int 的子类：False == 0 但 0 是合法值（pinned=0 取消钉选），所以按类型分开比。
    if value is None or value is False:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (int, float)):
        return value == -1
    return False


def merge_changes(explicit: dict[str, Any], changes: Any) -> tuple[dict[str, Any], Optional[str]]:
    """把 changes 并进显式参数。返回 (合并后的参数 dict, 错误短句或 None)。

    规则：
    - changes 为 None / 空 dict → 原样返回 explicit。
    - changes 是 JSON 字符串（有的客户端会把 dict 序列化成字符串传）→ 解析后照常处理。
    - 不是 dict → 拒绝。
    - 出现不认识的键（含 bucket_id）→ 整次调用拒绝并列出合法键。不静默忽略：
      键名靠描述、不靠 schema 约束，写错了必须让调用方知道没生效。
    - 同一字段显式参数和 changes 都给了且值不同 → 拒绝；值相同放行。
    - 值不做类型转换，交给 trace_core（它有 _safe_int / _finite_float / parse_bool 兜底）。
    """
    merged = dict(explicit)
    if changes is None or changes == {} or changes == "":
        return merged, None
    if isinstance(changes, str):
        try:
            changes = json.loads(changes)
        except (ValueError, TypeError):
            return merged, "changes 要传 {键: 值} 形式的对象，这次传的是解析不了的字符串，什么都没改。"
    if not isinstance(changes, dict):
        return merged, "changes 要传 {键: 值} 形式的对象，什么都没改。"

    unknown = sorted(str(k) for k in changes if k not in _ALLOWED_IN_CHANGES)
    if unknown:
        hint = "bucket_id 不能放进 changes。" if "bucket_id" in unknown else ""
        return merged, (
            f"changes 里有不认识的键：{'、'.join(unknown)}，什么都没改。{hint}"
            f"可用的键：{'、'.join(CHANGES_KEYS)}。"
        )

    conflicts = []
    for key, value in changes.items():
        current = merged.get(key)
        if _is_unset(current):
            merged[key] = value
        elif current != value:
            conflicts.append(key)
    if conflicts:
        return dict(explicit), (
            f"{'、'.join(sorted(conflicts))} 在显式参数和 changes 里给了不同的值，什么都没改。只在一处传。"
        )
    return merged, None


def slim_schema(full_schema: dict[str, Any]) -> dict[str, Any]:
    """从 FastMCP 按完整签名生成的 inputSchema 里裁出对外公布的那一份：
    bucket_id + PUBLIC_EXPLICIT_PARAMS + changes。属性定义原样沿用（类型、默认值不变）。"""
    props = full_schema.get("properties", {})
    keep = ("bucket_id", *PUBLIC_EXPLICIT_PARAMS, "changes")
    missing = [k for k in keep if k not in props]
    if missing:
        raise KeyError(f"trace schema 缺少字段: {missing}")
    slim = {k: v for k, v in full_schema.items() if k != "properties"}
    slim["properties"] = {k: props[k] for k in keep}
    slim["required"] = [k for k in full_schema.get("required", []) if k in keep]
    return slim
