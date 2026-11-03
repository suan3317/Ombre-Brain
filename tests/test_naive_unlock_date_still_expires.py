"""上游 4033f15：unlock_date 不带时区时锁不能永远不过期。我们按洛杉矶时间解释裸值。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tools.plan.lock import letter_lock_state

PAST = "2020-01-01T00:00:00"
FUTURE = "2099-01-01T00:00:00"


def _state(unlock_date, caller="ai", locked_by="human"):
    return letter_lock_state(
        {
            "id": "x",
            "metadata": {
                "lock_type": "timed",
                "unlock_date": unlock_date,
                "locked_by": locked_by,
            },
        },
        caller,
    )


@pytest.mark.parametrize(
    "unlock_date", [PAST, PAST + "Z", PAST + "+00:00", PAST + "+08:00", PAST + "-05:00", "2020-01-01"]
)
def test_a_past_unlock_date_expires_with_or_without_a_timezone(unlock_date):
    state = _state(unlock_date)
    assert state["expired"] is True
    assert state["locked"] is False


@pytest.mark.parametrize(
    "unlock_date",
    [FUTURE, FUTURE + "Z", FUTURE + "+00:00", FUTURE + "+08:00", FUTURE + "-05:00", "2099-01-01"],
)
def test_a_future_unlock_date_stays_locked_with_or_without_a_timezone(unlock_date):
    state = _state(unlock_date)
    assert state["expired"] is False
    assert state["locked"] is True


def test_the_owner_reads_their_own_locked_letter():
    assert _state(FUTURE, caller="human", locked_by="human")["locked"] is False


def test_a_permanent_lock_is_unaffected():
    state = letter_lock_state(
        {"id": "x", "metadata": {"lock_type": "permanent", "locked_by": "human"}}, "ai"
    )
    assert state["locked"] is True
    assert state["expired"] is False


def test_a_malformed_unlock_date_keeps_the_letter_locked():
    state = _state("不是时间")
    assert state["expired"] is False
    assert state["locked"] is True


def test_a_pre_lock_letter_without_owner_is_never_locked():
    state = letter_lock_state({"id": "x", "metadata": {"type": "letter"}}, "ai")
    assert state["locked"] is False
    assert state["locked_by"] is None


def test_an_explicit_now_is_honoured():
    boundary = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    meta = {"lock_type": "timed", "unlock_date": "2026-06-01T12:00:00+00:00", "locked_by": "human"}
    before = letter_lock_state({"id": "x", "metadata": meta}, "ai", now=boundary - timedelta(seconds=1))
    after = letter_lock_state({"id": "x", "metadata": meta}, "ai", now=boundary)
    assert before["locked"] is True
    assert after["locked"] is False


def test_naive_value_is_read_as_los_angeles_time():
    # 洛杉矶 2026-06-01 09:00 = UTC 16:00（夏令时 -07:00）
    meta = {"lock_type": "timed", "unlock_date": "2026-06-01T09:00:00", "locked_by": "human"}
    just_before = datetime(2026, 6, 1, 15, 59, 0, tzinfo=timezone.utc)
    just_after = datetime(2026, 6, 1, 16, 1, 0, tzinfo=timezone.utc)
    assert letter_lock_state({"id": "x", "metadata": meta}, "ai", now=just_before)["locked"] is True
    assert letter_lock_state({"id": "x", "metadata": meta}, "ai", now=just_after)["locked"] is False


@pytest.mark.asyncio
async def test_letter_read_returns_a_letter_whose_naive_lock_has_passed(bucket_mgr):
    from tests.test_letter_read_regression import install_letter_runtime
    from tools.plan.core import letter_read

    letter_id = await bucket_mgr.create(content="一封早就该解锁的信", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(
        letter_id, author="user", writer_name="Silvia",
        lock_type="timed", unlock_date=PAST, locked_by="human",
    )
    install_letter_runtime(bucket_mgr)

    output = await letter_read()
    assert letter_id in output
    assert "一封早就该解锁的信" in output
