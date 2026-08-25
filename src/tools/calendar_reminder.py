"""
========================================
tools/calendar_reminder.py — 日历事件的 wake/breath 提醒挂载
========================================

工单 S-4(2026-08-25):四机 wake/breath 时,临近事件提前 7/3/1 天 + 当天
各提醒一次。数据来源是既有 PINBOARD_URL/PINBOARD_TOKEN 通道(见
tools/pinboard.py 的 calendar_upcoming_impl),本模块只做:拉取 → 按
America/Los_Angeles 的"今天"算 7/3/1/0 档位 → 去重 → 拼接成文本区块。

关键行为：
- 去重记录存 <buckets_dir>/calendar_reminders/sent.json(扁平 JSON,
  key="{event_id}:{tier}"),wake 与 breath 共享同一份记录、同一次落盘时机——
  不像梦区块 wake 预览/breath 消费那样分两条路径,工单原话"同一事件同一
  档位每机至多提醒一次"是不分渠道的总数一。
- 拼接文本成功后才落盘去重记录,避免"标记了但没真正出现在响应里"。
- Pinboard 未配置/请求失败/响应解析失败:一律返回空字符串,不抛异常——
  调用方(breath dispatch / wake _wake_impl)在自己的 try/except 里再兜
  一层,双重保险,确保这条尾巴的任何故障都不阻塞 wake/breath 正文。

不做什么（边界）：
- 不做重复事件的规则展开(chatnest 侧已经把生日/年度节日实例化成具体
  日期,这里只读具体日期)
- 不做跨机器共享的去重(每台机器各自独立部署、各自的 buckets_dir,
  "每机至多提醒一次"天然靠这个隔离达成,不需要额外协调)

对外暴露: calendar_reminder_tail() -> str
========================================
"""

import json
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import _runtime as rt
from .pinboard import calendar_upcoming_impl

_DEFAULT_TIMEZONE = "America/Los_Angeles"
_TIERS = (7, 3, 1, 0)
_TIER_LABELS = {7: "7天后", 3: "3天后", 1: "明天", 0: "今天"}
_SENT_RECORD_DIRNAME = "calendar_reminders"
_SENT_RECORD_FILENAME = "sent.json"
_STALE_AFTER_DAYS = 30


def _log_warning(message: str) -> None:
    log = getattr(rt, "logger", None) or logging.getLogger(__name__)
    try:
        log.warning(message)
    except Exception:
        pass


def _today_la() -> date:
    try:
        return datetime.now(ZoneInfo(_DEFAULT_TIMEZONE)).date()
    except Exception as e:  # pragma: no cover - 环境缺 tzdata 时的兜底
        _log_warning(f"calendar_reminder: 时区 {_DEFAULT_TIMEZONE} 不可用（缺 tzdata？）: {e}")
        return datetime.utcnow().date()


def _sent_record_path() -> str:
    buckets_dir = "buckets"
    if getattr(rt, "config", None) is not None:
        buckets_dir = rt.config.get("buckets_dir", "buckets")
    directory = os.path.join(buckets_dir, _SENT_RECORD_DIRNAME)
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, _SENT_RECORD_FILENAME)


def _load_sent(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        _log_warning(f"calendar_reminder: 去重记录读取失败，按空记录处理: {e}")
        return {}


def _save_sent(path: str, record: dict, today: date) -> None:
    pruned = {}
    for key, value in record.items():
        event_date_str = (value or {}).get("event_date") if isinstance(value, dict) else None
        if event_date_str:
            try:
                event_date = date.fromisoformat(event_date_str)
                if (today - event_date).days > _STALE_AFTER_DAYS:
                    continue
            except ValueError:
                pass
        pruned[key] = value
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log_warning(f"calendar_reminder: 去重记录写入失败: {e}")


async def calendar_reminder_tail() -> str:
    raw = await calendar_upcoming_impl(max(_TIERS))
    try:
        payload = json.loads(raw)
        events = payload["events"]
    except Exception:
        # Pinboard 未配置/请求失败/响应不是预期形状——静默跳过，不阻塞
        # wake/breath 正文（pinboard._call_tool 失败时返回的是可读错误文本，
        # 不是合法 JSON，走的正是这一条）。
        return ""

    today = _today_la()
    due = []
    for event in events:
        try:
            event_date = date.fromisoformat(event["date"])
        except (KeyError, ValueError, TypeError):
            continue
        days_until = (event_date - today).days
        if days_until not in _TIERS:
            continue
        due.append((days_until, event))

    if not due:
        return ""

    path = _sent_record_path()
    sent = _load_sent(path)

    to_render = []
    newly_sent_keys = []
    for days_until, event in due:
        event_id = event.get("id")
        if event_id is None:
            continue
        key = f"{event_id}:{days_until}"
        if key in sent:
            continue
        to_render.append((days_until, event))
        newly_sent_keys.append((key, event["date"]))

    if not to_render:
        return ""

    to_render.sort(key=lambda pair: (-pair[0], pair[1]["date"]))
    lines = ["## 日历提醒"]
    for days_until, event in to_render:
        title = event.get("title", "(无标题)")
        category = event.get("category", "")
        lines.append(f"- [{_TIER_LABELS[days_until]}] {event['date']} {title}（{category}）")
    tail = "\n".join(lines)

    for key, event_date_str in newly_sent_keys:
        sent[key] = {"sent_at": datetime.now(ZoneInfo(_DEFAULT_TIMEZONE)).isoformat(), "event_date": event_date_str}
    _save_sent(path, sent, today)

    return tail
