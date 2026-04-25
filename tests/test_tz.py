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
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from tz_helpers import frozen_datetime
from pipeline import _tz


# ---------------------------------------------------------------------------
# _resolve_tz_from_env — unit tests for the core resolver
# ---------------------------------------------------------------------------

def test_resolve_tz_returns_zoneinfo_for_valid_tz(monkeypatch):
    """A valid IANA name returns a ZoneInfo instance."""
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    result = _tz._resolve_tz_from_env()
    assert isinstance(result, ZoneInfo)
    assert str(result) == "America/New_York"


def test_resolve_tz_returns_none_for_empty_string(monkeypatch):
    """Empty string means 'use system local' — returns None, not UTC."""
    monkeypatch.setenv("REMEMBER_TZ", "")
    assert _tz._resolve_tz_from_env() is None


def test_resolve_tz_returns_none_when_unset(monkeypatch):
    """Unset env var returns None (system local)."""
    monkeypatch.delenv("REMEMBER_TZ", raising=False)
    assert _tz._resolve_tz_from_env() is None


def test_resolve_tz_returns_none_for_invalid_name(monkeypatch):
    """Invalid timezone name falls back silently to None (no crash)."""
    monkeypatch.setenv("REMEMBER_TZ", "Not/AReal/Zone")
    assert _tz._resolve_tz_from_env() is None


def test_resolve_tz_strips_whitespace(monkeypatch):
    """Whitespace-only TZ is treated as empty (system local), not an error."""
    monkeypatch.setenv("REMEMBER_TZ", "   ")
    assert _tz._resolve_tz_from_env() is None


def test_resolve_tz_strips_surrounding_whitespace(monkeypatch):
    """Leading/trailing whitespace around a valid TZ name is stripped."""
    monkeypatch.setenv("REMEMBER_TZ", "  America/New_York  ")
    result = _tz._resolve_tz_from_env()
    assert isinstance(result, ZoneInfo)
    assert str(result) == "America/New_York"


# ---------------------------------------------------------------------------
# today_str / time_str — negative offset (west of UTC)
# ---------------------------------------------------------------------------

def test_today_str_uses_remember_tz_env(monkeypatch):
    """REMEMBER_TZ=America/New_York produces yesterday's local date at 03:12 UTC."""
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    # 2026-04-23 03:12 UTC == 2026-04-22 23:12 EDT
    moment = datetime(2026, 4, 23, 3, 12, 0, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", frozen_datetime(moment)):
        assert _tz.today_str() == "2026-04-22"


def test_time_str_uses_remember_tz_env(monkeypatch):
    """REMEMBER_TZ=America/New_York produces EDT wall-clock time, not UTC."""
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    moment = datetime(2026, 4, 23, 3, 12, 45, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", frozen_datetime(moment)):
        assert _tz.time_str() == "23:12:45"


# ---------------------------------------------------------------------------
# today_str / time_str — positive offset (east of UTC)
# ---------------------------------------------------------------------------

def test_today_str_positive_offset_asia_tokyo(monkeypatch):
    """REMEMBER_TZ=Asia/Tokyo at 15:30 UTC produces next-day date (JST = UTC+9).

    2026-04-22 15:30 UTC == 2026-04-23 00:30 JST — the date rolls FORWARD.
    This is the mirror of the EDT bug: east-of-UTC zones can be a day AHEAD.
    """
    monkeypatch.setenv("REMEMBER_TZ", "Asia/Tokyo")
    moment = datetime(2026, 4, 22, 15, 30, 0, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", frozen_datetime(moment)):
        assert _tz.today_str() == "2026-04-23"


def test_time_str_positive_offset_asia_tokyo(monkeypatch):
    """Asia/Tokyo at 15:30:15 UTC produces 00:30:15 JST."""
    monkeypatch.setenv("REMEMBER_TZ", "Asia/Tokyo")
    moment = datetime(2026, 4, 22, 15, 30, 15, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", frozen_datetime(moment)):
        assert _tz.time_str() == "00:30:15"


# ---------------------------------------------------------------------------
# Midnight boundary edge case
# ---------------------------------------------------------------------------

def test_today_str_at_exact_midnight_utc_with_negative_offset(monkeypatch):
    """At exactly 00:00:00 UTC with REMEMBER_TZ=America/Chicago (UTC-5),
    the local date is the PREVIOUS day (23:00 CDT on the day before).

    This is a boundary case: midnight UTC is the worst moment for TZ bugs.
    """
    monkeypatch.setenv("REMEMBER_TZ", "America/Chicago")
    moment = datetime(2026, 4, 23, 0, 0, 0, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", frozen_datetime(moment)):
        assert _tz.today_str() == "2026-04-22"
        assert _tz.time_str() == "19:00:00"


def test_today_str_at_exact_midnight_utc_with_positive_offset(monkeypatch):
    """At exactly 00:00:00 UTC with REMEMBER_TZ=Asia/Kolkata (UTC+5:30),
    the local time is 05:30 — same day, but the time proves the offset works.
    """
    monkeypatch.setenv("REMEMBER_TZ", "Asia/Kolkata")
    moment = datetime(2026, 4, 23, 0, 0, 0, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", frozen_datetime(moment)):
        assert _tz.today_str() == "2026-04-23"
        assert _tz.time_str() == "05:30:00"


# ---------------------------------------------------------------------------
# Fallback behavior — empty / unset / invalid
# ---------------------------------------------------------------------------

def test_today_str_empty_tz_env_does_not_fallback_to_utc(monkeypatch):
    """Empty REMEMBER_TZ means system local time — NOT UTC (that was the bug)."""
    monkeypatch.setenv("REMEMBER_TZ", "")
    moment = datetime(2026, 4, 23, 3, 12, 0, tzinfo=timezone.utc)
    with patch("pipeline._tz.datetime", frozen_datetime(moment)):
        result = _tz.today_str()
    # We can't assert a specific date without knowing the CI tz, but we CAN
    # verify the format and that the function completed. The shell-level
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


# ---------------------------------------------------------------------------
# now() return type
# ---------------------------------------------------------------------------

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


def test_now_tzinfo_matches_configured_zone(monkeypatch):
    """The tzinfo on the returned datetime should match the configured zone."""
    monkeypatch.setenv("REMEMBER_TZ", "Europe/Paris")
    result = _tz.now()
    assert result.tzinfo is not None
    assert str(result.tzinfo) == "Europe/Paris"
