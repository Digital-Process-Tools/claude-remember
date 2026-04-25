"""Tests for pipeline logging."""

import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from pipeline.log import log, log_tokens, format_duration
from pipeline.types import TokenUsage


from tz_helpers import frozen_datetime


def test_log_creates_file_and_writes():
    with tempfile.TemporaryDirectory() as d:
        log("test", "hello world", d)
        files = os.listdir(d)
        assert len(files) == 1
        assert files[0].startswith("memory-")
        content = open(os.path.join(d, files[0])).read()
        assert "[test] hello world" in content


def test_log_appends():
    with tempfile.TemporaryDirectory() as d:
        log("a", "first", d)
        log("b", "second", d)
        files = os.listdir(d)
        assert len(files) == 1
        content = open(os.path.join(d, files[0])).read()
        assert "[a] first" in content
        assert "[b] second" in content


def test_log_tokens_format():
    with tempfile.TemporaryDirectory() as d:
        usage = TokenUsage(input=1000, output=200, cache=500, cost_usd=0.0012)
        log_tokens("save", usage, d)
        files = os.listdir(d)
        content = open(os.path.join(d, files[0])).read()
        assert "[save] tokens:" in content
        assert "1000+500cache" in content


def test_format_duration_seconds():
    assert format_duration(0) == "0s"
    assert format_duration(42) == "42s"
    assert format_duration(59) == "59s"


def test_format_duration_minutes():
    assert format_duration(60) == "1m"
    assert format_duration(114) == "1m54s"
    assert format_duration(600) == "10m"


def test_format_duration_hours():
    assert format_duration(3600) == "1h"
    assert format_duration(11460) == "3h11m"
    assert format_duration(7200) == "2h"


def test_log_fallback_to_stderr_on_oserror(capsys):
    """When the log file can't be written, log() prints to stderr instead of raising."""
    with tempfile.TemporaryDirectory() as d:
        with patch("pipeline.log.open", side_effect=OSError("disk full")):
            log("test", "fallback message", d)

    captured = capsys.readouterr()
    assert "[test] fallback message" in captured.err


def test_log_tokens_with_nonzero_cost():
    """log_tokens() includes the $ cost in the output when cost_usd is non-zero."""
    with tempfile.TemporaryDirectory() as d:
        usage = TokenUsage(input=500, output=100, cache=0, cost_usd=0.0042)
        log_tokens("consolidate", usage, d)
        files = os.listdir(d)
        content = open(os.path.join(d, files[0])).read()
        assert "[consolidate] tokens:" in content
        assert "$0.0042" in content


def test_log_tokens_with_zero_cost():
    """log_tokens() still formats correctly when cost_usd is 0.0 (the default)."""
    with tempfile.TemporaryDirectory() as d:
        usage = TokenUsage(input=300, output=80, cache=50)
        log_tokens("save", usage, d)
        files = os.listdir(d)
        content = open(os.path.join(d, files[0])).read()
        assert "[save] tokens:" in content
        assert "$0.0000" in content


def test_log_filename_uses_remember_tz(monkeypatch):
    """Regression: at 03:12 UTC on 04-23, with REMEMBER_TZ=America/New_York,
    the filename must be memory-2026-04-22.log (local EDT), not 04-23 (UTC).

    This is the exact bug: the plugin was stamping filenames with UTC,
    producing next-day filenames after 20:00 local on EDT.
    """
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    moment = datetime(2026, 4, 23, 3, 12, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        with patch("pipeline._tz.datetime", frozen_datetime(moment)):
            log("test", "boundary check", d)
        files = os.listdir(d)
    assert files == ["memory-2026-04-22.log"], (
        f"Expected memory-2026-04-22.log (EDT), got {files}"
    )


def test_log_timestamp_uses_remember_tz(monkeypatch):
    """Timestamp inside the log line must be in REMEMBER_TZ, not UTC."""
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    moment = datetime(2026, 4, 23, 3, 12, 45, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        with patch("pipeline._tz.datetime", frozen_datetime(moment)):
            log("test", "stamp check", d)
        content = open(os.path.join(d, os.listdir(d)[0])).read()
    assert content.startswith("23:12:45 [test]"), f"unexpected content: {content!r}"


def test_log_tokens_filename_uses_remember_tz(monkeypatch):
    """log_tokens() writes to the same TZ-aware filename as log().

    Both go through _log_path() → today_str(), but log_tokens() is a
    separate code path worth proving independently — a 20k-download
    plugin can't assume code paths share behavior without testing both.
    """
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    moment = datetime(2026, 4, 23, 3, 12, 0, tzinfo=timezone.utc)
    usage = TokenUsage(input=100, output=50, cache=0)
    with tempfile.TemporaryDirectory() as d:
        with patch("pipeline._tz.datetime", frozen_datetime(moment)):
            log_tokens("save", usage, d)
        files = os.listdir(d)
    assert files == ["memory-2026-04-22.log"], (
        f"log_tokens should use EDT date, got {files}"
    )


def test_log_tokens_timestamp_uses_remember_tz(monkeypatch):
    """Timestamp inside the log_tokens line must be in REMEMBER_TZ."""
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    moment = datetime(2026, 4, 23, 3, 12, 45, tzinfo=timezone.utc)
    usage = TokenUsage(input=100, output=50, cache=0)
    with tempfile.TemporaryDirectory() as d:
        with patch("pipeline._tz.datetime", frozen_datetime(moment)):
            log_tokens("save", usage, d)
        content = open(os.path.join(d, os.listdir(d)[0])).read()
    assert content.startswith("23:12:45 [save] tokens:"), (
        f"unexpected content: {content!r}"
    )
