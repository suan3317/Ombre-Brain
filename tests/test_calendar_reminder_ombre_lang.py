"""工单 D-4 三期：tools/calendar_reminder.py 区块标题 + 7/3/1/0 天档位提醒句
英文化。事件标题/category 来自 Pinboard 数据，原样不翻（任务书第3条）。
en 下抽 3 条断言无中文字符，zh 回归 1 条。
"""
import datetime as dt
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import tools._runtime as rt
import tools.calendar_reminder as calendar_reminder

_CJK_RE = __import__("re").compile(r"[一-鿿]")


def install_runtime(buckets_dir):
    rt.config = {"buckets_dir": str(buckets_dir)}
    rt.logger = MagicMock()


def _events_payload(events):
    return json.dumps({"events": events}, ensure_ascii=False)


def _freeze_now(monkeypatch, frozen: dt.datetime):
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr(calendar_reminder, "datetime", FixedDateTime)


@pytest.mark.asyncio
async def test_en_header_and_tier_labels_have_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(tmp_path)
    _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))
    events = [
        {"id": 1, "date": "2026-09-01", "title": "Orientation Day", "category": "school"},  # 7 days
        {"id": 2, "date": "2026-08-25", "title": "Birthday", "category": "birthday"},  # today
    ]
    monkeypatch.setattr(
        calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
    )

    tail = await calendar_reminder.calendar_reminder_tail()

    assert not _CJK_RE.search(tail)
    assert "## Calendar Reminders" in tail
    assert "[in 7 days]" in tail
    assert "[today]" in tail
    # Pinboard 数据本身（英文标题/分类）原样透传，不是本条断言的重点，
    # 但顺带确认没有被误处理。
    assert "Orientation Day" in tail and "Birthday" in tail


@pytest.mark.asyncio
async def test_en_untitled_fallback_has_no_chinese_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(tmp_path)
    _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))
    events = [{"id": 1, "date": "2026-08-25", "category": "misc"}]  # 缺 title
    monkeypatch.setattr(
        calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
    )

    tail = await calendar_reminder.calendar_reminder_tail()

    assert not _CJK_RE.search(tail)
    assert "(untitled)" in tail


@pytest.mark.asyncio
async def test_en_category_uses_ascii_parens(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_LANG", "en")
    install_runtime(tmp_path)
    _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))
    events = [{"id": 1, "date": "2026-08-25", "title": "Checkup", "category": "health"}]
    monkeypatch.setattr(
        calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
    )

    tail = await calendar_reminder.calendar_reminder_tail()

    assert not _CJK_RE.search(tail)
    assert "(health)" in tail
    assert "（health）" not in tail


# ============================================================
# zh 回归：默认（不设 OMBRE_LANG）路径必须逐字保持原有中文
# ============================================================

@pytest.mark.asyncio
async def test_zh_default_header_and_tier_labels_unaffected(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    install_runtime(tmp_path)
    _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))
    events = [{"id": 1, "date": "2026-08-25", "title": "生日", "category": "生日"}]
    monkeypatch.setattr(
        calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
    )

    tail = await calendar_reminder.calendar_reminder_tail()

    assert "## 日历提醒" in tail
    assert "[今天]" in tail
    assert "生日（生日）" in tail
