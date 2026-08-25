"""S-4 回归测试:tools/calendar_reminder.py(wake/breath 日历提醒挂载)。

覆盖:7/3/1/0 天档位判定(冻结时钟,按 America/Los_Angeles 算"今天",手法
同 chatnest-home P1-9a 的 _freeze_store_clock)、去重(同一事件同一档位
第二次不再出现)、Pinboard 未配置/响应异常时静默返回空字符串(不阻塞
wake/breath 正文)。
"""
import datetime as dt
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import tools._runtime as rt
import tools.calendar_reminder as calendar_reminder


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
class TestTierMatching:
    async def test_events_at_each_tier_are_included(self, tmp_path, monkeypatch):
        install_runtime(tmp_path)
        # 冻结在 2026-08-25T18:00:00Z = LA 11:00 PDT(夏令时 UTC-7)。
        _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))
        events = [
            {"id": 1, "date": "2026-09-01", "title": "开学", "category": "学业"},  # 7天后
            {"id": 2, "date": "2026-08-28", "title": "体检", "category": "健康"},  # 3天后
            {"id": 3, "date": "2026-08-26", "title": "面试", "category": "工作"},  # 明天
            {"id": 4, "date": "2026-08-25", "title": "生日", "category": "生日"},  # 今天
            {"id": 5, "date": "2026-08-27", "title": "不该出现", "category": "休闲"},  # 2天后,不在档位
        ]
        monkeypatch.setattr(
            calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
        )

        tail = await calendar_reminder.calendar_reminder_tail()

        assert "开学" in tail
        assert "体检" in tail
        assert "面试" in tail
        assert "生日" in tail
        assert "不该出现" not in tail
        assert "[7天后]" in tail and "[3天后]" in tail and "[明天]" in tail and "[今天]" in tail

    async def test_no_due_events_returns_empty_string(self, tmp_path, monkeypatch):
        install_runtime(tmp_path)
        _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))
        events = [{"id": 1, "date": "2026-09-10", "title": "太远", "category": "休闲"}]
        monkeypatch.setattr(
            calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
        )

        assert await calendar_reminder.calendar_reminder_tail() == ""


@pytest.mark.asyncio
class TestDedup:
    async def test_same_event_same_tier_not_repeated(self, tmp_path, monkeypatch):
        install_runtime(tmp_path)
        _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))
        events = [{"id": 1, "date": "2026-08-25", "title": "生日", "category": "生日"}]
        monkeypatch.setattr(
            calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
        )

        first = await calendar_reminder.calendar_reminder_tail()
        assert "生日" in first

        second = await calendar_reminder.calendar_reminder_tail()
        assert second == ""

    async def test_wake_and_breath_share_dedup_record(self, tmp_path, monkeypatch):
        """工单原话"同一事件同一档位每机至多提醒一次"不分渠道——两次调用
        模拟 wake 和 breath 各自独立调用 calendar_reminder_tail(),同一份
        buckets_dir 下的去重记录是共享的,不像梦区块分 consume=True/False
        两条路径。"""
        install_runtime(tmp_path)
        _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))
        events = [{"id": 7, "date": "2026-08-25", "title": "纪念日", "category": "纪念日"}]
        monkeypatch.setattr(
            calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
        )

        wake_tail = await calendar_reminder.calendar_reminder_tail()
        breath_tail = await calendar_reminder.calendar_reminder_tail()

        assert "纪念日" in wake_tail
        assert breath_tail == ""

    async def test_different_tier_of_same_event_still_announced(self, tmp_path, monkeypatch):
        install_runtime(tmp_path)
        events = [{"id": 1, "date": "2026-09-01", "title": "开学", "category": "学业"}]
        monkeypatch.setattr(
            calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
        )

        _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))  # 7天后
        first = await calendar_reminder.calendar_reminder_tail()
        assert "[7天后]" in first

        _freeze_now(monkeypatch, dt.datetime(2026, 8, 29, 18, 0, 0, tzinfo=dt.timezone.utc))  # 3天后
        second = await calendar_reminder.calendar_reminder_tail()
        assert "[3天后]" in second


@pytest.mark.asyncio
class TestNonBlocking:
    async def test_pinboard_not_configured_returns_empty_string(self, tmp_path, monkeypatch):
        install_runtime(tmp_path)
        monkeypatch.setattr(
            calendar_reminder,
            "calendar_upcoming_impl",
            AsyncMock(return_value="OB-PB01 Pinboard 未配置:缺少 PINBOARD_URL 或 PINBOARD_TOKEN。"),
        )

        assert await calendar_reminder.calendar_reminder_tail() == ""

    async def test_pinboard_request_failure_returns_empty_string(self, tmp_path, monkeypatch):
        install_runtime(tmp_path)
        monkeypatch.setattr(
            calendar_reminder,
            "calendar_upcoming_impl",
            AsyncMock(return_value="OB-PB02 请求失败: connection refused"),
        )

        assert await calendar_reminder.calendar_reminder_tail() == ""

    async def test_malformed_events_are_skipped_not_fatal(self, tmp_path, monkeypatch):
        install_runtime(tmp_path)
        _freeze_now(monkeypatch, dt.datetime(2026, 8, 25, 18, 0, 0, tzinfo=dt.timezone.utc))
        events = [
            {"id": 1, "date": "not-a-date", "title": "坏数据", "category": "休闲"},
            {"id": 2, "title": "缺日期字段", "category": "休闲"},
            {"id": 3, "date": "2026-08-25", "title": "正常事件", "category": "生日"},
        ]
        monkeypatch.setattr(
            calendar_reminder, "calendar_upcoming_impl", AsyncMock(return_value=_events_payload(events))
        )

        tail = await calendar_reminder.calendar_reminder_tail()
        assert "正常事件" in tail
        assert "坏数据" not in tail


@pytest.mark.asyncio
class TestBreathDispatchIntegration:
    async def test_breath_dispatch_appends_calendar_tail_without_dream_engine(self, tmp_path, monkeypatch):
        """breath.dispatch() 的日历尾部拼接是独立 try/except,不依赖
        dream_engine 是否配置——用真实 dispatch() 走一遍,只 mock 掉日历
        这一层,验证正文不受影响、日历区块确实追加在尾部。"""
        install_runtime(tmp_path)
        rt.dream_engine = None
        rt.decay_engine = MagicMock(is_running=True, ensure_started=AsyncMock())
        rt.dehydrator = MagicMock()
        rt.embedding_engine = MagicMock(enabled=False)
        rt.bucket_mgr = MagicMock()
        rt.bucket_mgr.list_all = AsyncMock(return_value=[])

        import tools.breath as breath_module

        monkeypatch.setattr(
            breath_module, "calendar_reminder_tail", AsyncMock(return_value="## 日历提醒\n- [今天] 测试")
        )

        result = await breath_module.dispatch(catalog=True)

        assert "## 日历提醒" in result

    async def test_breath_dispatch_survives_calendar_tail_exception(self, tmp_path, monkeypatch):
        install_runtime(tmp_path)
        rt.dream_engine = None
        rt.decay_engine = MagicMock(is_running=True, ensure_started=AsyncMock())
        rt.dehydrator = MagicMock()
        rt.embedding_engine = MagicMock(enabled=False)
        rt.bucket_mgr = MagicMock()
        rt.bucket_mgr.list_all = AsyncMock(return_value=[])

        import tools.breath as breath_module

        monkeypatch.setattr(
            breath_module, "calendar_reminder_tail", AsyncMock(side_effect=RuntimeError("boom"))
        )

        result = await breath_module.dispatch(catalog=True)

        assert isinstance(result, str)
        assert "boom" not in result
