"""
========================================
tools/plan/lock.py — 信件时间锁（U-1 第 3 条，上游 79227b3 + 4033f15 手工移植）
========================================

一封信可以带锁：timed（到期才能打开）或 permanent（永久封存）。锁属于写信
的那一方（locked_by = "ai" / "human"），自己永远能读；另一方在到期前只能
看到"有一封锁到 X 日的信"，标题、正文、向量检索一律不可见。

关键行为：
- unlock_date 一律按 America/Los_Angeles 理解：只给日期（2027-01-01）按
  当天 00:00 洛杉矶时间；完整时刻不带时区同样按洛杉矶；带时区照原样。存储
  时写成带显式偏移的 ISO 字符串。
- 判过期时不要求 unlock_date 带时区（上游 4033f15）：手改 Markdown / 导入
  / 旧备份里可能是裸时间，裸值同样按洛杉矶解释——最坏早/晚几小时，远好过
  一封 2020 年就该解锁的信永远锁着。
- 过期的 timed 锁由读取方惰性归一化为 lock_type=none（见 core.py）。

对外暴露：normalize_lock_type / normalize_unlock_date / letter_lock_state /
author_side / resolve_writer_name / safe_letter_metadata / locked_summary
========================================
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from utils import get_ai_name, get_owner_name, strip_wikilinks, t as _t

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

LETTER_LOCK_TYPES = {"none", "timed", "permanent"}
PERMANENT_UNLOCK_DATE = "9999-12-31"
LOCK_TZ_NAME = "America/Los_Angeles"
_HUMAN_AUTHOR_ALIASES = {"user", "human", "human-side"}
_AI_AUTHOR_ALIASES = {"ai", "ai-side", "claude"}
_GENERIC_RELATION_NAMES = {
    "ai", "a.i.", "assistant", "claude", "bot", "model",
    "user", "human", "human-side", "ai-side", "you", "me",
}


def lock_timezone():
    """洛杉矶时区；tzdata 缺席时退回固定 -08:00（只差夏令时一小时）。"""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(LOCK_TZ_NAME)
        except Exception:
            pass
    return timezone(timedelta(hours=-8))


def normalize_lock_type(value: object) -> str:
    lock_type = str(value or "none").strip().lower()
    if lock_type not in LETTER_LOCK_TYPES:
        raise ValueError(
            _t("lock_type 只能是 none / timed / permanent",
               "lock_type must be one of: none, timed, permanent")
        )
    return lock_type


def _parse_unlock(raw: str) -> datetime:
    text = str(raw or "").strip().replace("Z", "+00:00")
    if not text:
        raise ValueError("empty")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        parsed = datetime.strptime(text, "%Y-%m-%d")
    else:
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=lock_timezone())
    return parsed


def normalize_unlock_date(lock_type: str, value: object, *, now: datetime | None = None) -> str | None:
    if lock_type == "none":
        return None
    if lock_type == "permanent":
        return PERMANENT_UNLOCK_DATE
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(
            _t("timed 锁必须同时给 unlock_date（未来的日期或时刻）",
               "timed lock requires unlock_date as a future date or datetime")
        )
    try:
        parsed = _parse_unlock(raw)
    except ValueError as exc:
        raise ValueError(
            _t("unlock_date 要写成 2027-01-01 或 2027-01-01T09:00:00（按洛杉矶时间）",
               "unlock_date must look like 2027-01-01 or 2027-01-01T09:00:00 (Los Angeles time)")
        ) from exc
    current = now or datetime.now(timezone.utc)
    if parsed.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
        raise ValueError(_t("unlock_date 必须是未来的时间", "unlock_date must be in the future"))
    return parsed.isoformat()


def _is_actual_relation_name(value: object) -> bool:
    name = str(value or "").strip()
    return bool(name) and name.casefold() not in _GENERIC_RELATION_NAMES


def author_side(author: object, *, ai_name: str | None = None) -> str | None:
    """署名属于哪一侧："human" / "ai" / None（自定义署名，看不出来）。"""
    raw = str(author or "").strip()
    low = raw.casefold()
    if low in _HUMAN_AUTHOR_ALIASES:
        return "human"
    configured_ai = str(ai_name or get_ai_name() or "").strip()
    if low in _AI_AUTHOR_ALIASES or (configured_ai and raw == configured_ai):
        return "ai"
    return None


def resolve_writer_name(
    caller_side: str,
    *,
    author: object,
    user_name: object = "",
    ai_name: object = "",
) -> str | None:
    """写信人的实际名字（锁信另一方只看得到这个名字）。找不到实际名字返回 None，
    调用方自己决定退回什么标签——我们这版不因为没名字而拒绝写锁信。"""
    raw_author = str(author or "").strip()
    if caller_side == "human":
        candidates = (user_name, get_owner_name(), raw_author)
    else:
        candidates = (ai_name, get_ai_name(), raw_author)
    return next((str(v).strip() for v in candidates if _is_actual_relation_name(v)), None)


def letter_lock_state(bucket: dict, caller_side: str | None, *, now: datetime | None = None) -> dict:
    meta = bucket.get("metadata") or {}
    try:
        lock_type = normalize_lock_type(meta.get("lock_type", "none"))
    except ValueError:
        lock_type = "none"
    unlock_date = meta.get("unlock_date") or None
    locked_by = str(meta.get("locked_by") or "").strip() or None
    expired = False
    if lock_type == "timed" and unlock_date:
        try:
            parsed = _parse_unlock(str(unlock_date))
            reference = now or datetime.now(timezone.utc)
            expired = reference.astimezone(timezone.utc) >= parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            expired = False
    effective_type = "none" if expired else lock_type
    owner = bool(locked_by and caller_side == locked_by)
    locked = effective_type != "none" and not owner
    return {
        "lock_type": effective_type,
        "stored_lock_type": lock_type,
        "unlock_date": None if expired else unlock_date,
        "locked_by": locked_by,
        "owner": owner,
        "locked": locked,
        "expired": expired,
    }


def unlock_date_label(unlock_date: object) -> str:
    """给人看的解锁时刻：按洛杉矶时间，精确到分钟。"""
    try:
        parsed = _parse_unlock(str(unlock_date or ""))
    except ValueError:
        return str(unlock_date or "")
    local = parsed.astimezone(lock_timezone())
    if local.hour == 0 and local.minute == 0:
        return local.strftime("%Y-%m-%d")
    return local.strftime("%Y-%m-%d %H:%M")


def locked_summary(bucket: dict, state: dict) -> str:
    """锁信在另一方眼里的全部信息：谁、锁到几号。没有标题，没有正文。"""
    meta = bucket.get("metadata") or {}
    who = str(meta.get("writer_name") or meta.get("author") or "?").strip()
    if state["lock_type"] == "permanent":
        return _t(
            f"[{bucket.get('id', '')}] 🔒 {who} 留了一封永久封存的信，正文不可见。",
            f"[{bucket.get('id', '')}] 🔒 {who} left a permanently sealed letter; the body is hidden.",
        )
    when = unlock_date_label(state.get("unlock_date"))
    return _t(
        f"[{bucket.get('id', '')}] 🔒 {who} 留了一封锁到 {when} 的信，到期前正文不可见。",
        f"[{bucket.get('id', '')}] 🔒 {who} left a letter locked until {when}; the body stays hidden until then.",
    )


def safe_letter_metadata(bucket: dict, caller_side: str | None) -> dict:
    """Dashboard 列表用：锁着的信只给元数据，不给标题和正文。"""
    meta = bucket.get("metadata") or {}
    state = letter_lock_state(bucket, caller_side)
    payload = {
        "id": bucket.get("id", ""),
        "author": meta.get("author", ""),
        "user_name": meta.get("user_name", ""),
        "writer_name": str(meta.get("writer_name") or "").strip(),
        "date": meta.get("letter_date") or str(meta.get("created", ""))[:10],
        "created": meta.get("created", ""),
        "lock_type": state["lock_type"],
        "unlock_date": state["unlock_date"],
        "unlock_label": unlock_date_label(state["unlock_date"]) if state["unlock_date"] else "",
        "locked": state["locked"],
        "lock_owner": state["owner"],
    }
    if not state["locked"]:
        payload["title"] = meta.get("title", "") or meta.get("name", "")
        payload["content"] = strip_wikilinks(bucket.get("content", ""))
    return payload
