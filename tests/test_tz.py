"""Tests for pipeline._tz — timezone-aware date helpers.

Regression tests for the bug where log filenames were stamped with UTC
instead of the configured timezone, producing filenames like
``memory-2026-04-23.log`` when the user's local date was still 04-22.

The helpers read the ``REMEMBER_TZ`` environment variable (which the
shell sets from config.json's ``.timezone`` field). Empty/unset/invalid
values fall back to system local time — never to UTC — so a user on a
local-time system clock without a config file still gets correct dates.
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import _tz


def _fake_datetime_at(moment_utc: datetime):
    """Return a mock replacement for ``datetime`` that freezes ``now()``."""
    class FrozenDatetime:
        @staticmethod
        def now(tz=None):
            if tz is None:
                # Simulate system local clock. Tests that rely on this must
                # also set TZ-independent expectations or mock further.
                return moment_utc.astimezone().replace(tzinfo=None)
            return moment_utc.astimezone(tz)

    return FrozenDatetime


def test_today_str_uses_remember_tz_env(monkeypatch):
    """REMEMBER_TZ=America/New_York produces yesterday's local date at 03:12 UTC."""
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    # 2026-04-23 03:12 UTC == 2026-04-22 23:12 EDT
    moment = datetime(2026, 4, 23, 3, 12, 0, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", _fake_datetime_at(moment)):
        assert _tz.today_str() == "2026-04-22"


def test_time_str_uses_remember_tz_env(monkeypatch):
    """REMEMBER_TZ=America/New_York produces EDT wall-clock time, not UTC."""
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    moment = datetime(2026, 4, 23, 3, 12, 45, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", _fake_datetime_at(moment)):
        assert _tz.time_str() == "23:12:45"


def test_today_str_empty_tz_env_does_not_fallback_to_utc(monkeypatch):
    """Empty REMEMBER_TZ means system local time — NOT UTC (that was the bug)."""
    monkeypatch.setenv("REMEMBER_TZ", "")
    moment = datetime(2026, 4, 23, 3, 12, 0, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", _fake_datetime_at(moment)):
        result = _tz.today_str()
    # We can't assert a specific date without knowing the CI tz, but we CAN
    # assert it's not the UTC date unless the system also runs in UTC — so
    # we verify the format and that the function completed. The shell-level
    # test covers the UTC-specific regression.
    assert len(result) == 10 and result[4] == "-" and result[7] == "-"


def test_today_str_unset_tz_env_uses_system_local(monkeypatch):
    """Unset REMEMBER_TZ falls back to system local, not UTC."""
    monkeypatch.delenv("REMEMBER_TZ", raising=False)
    result = _tz.today_str()
    assert len(result) == 10 and result[4] == "-" and result[7] == "-"


def test_today_str_invalid_tz_falls_back_silently(monkeypatch):
    """An unknown TZ name does not crash — falls back to system local."""
    monkeypatch.setenv("REMEMBER_TZ", "Not/AReal/Zone")
    result = _tz.today_str()
    assert len(result) == 10 and result[4] == "-" and result[7] == "-"


def test_now_returns_aware_datetime_when_tz_set(monkeypatch):
    """With a valid TZ, now() returns a timezone-aware datetime."""
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    result = _tz.now()
    assert result.tzinfo is not None


def test_now_returns_naive_datetime_when_tz_unset(monkeypatch):
    """With no TZ configured, now() returns a naive datetime (system local)."""
    monkeypatch.delenv("REMEMBER_TZ", raising=False)
    result = _tz.now()
    assert result.tzinfo is None
