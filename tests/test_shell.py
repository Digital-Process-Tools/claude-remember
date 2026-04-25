"""Tests for shell integration helpers."""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from tz_helpers import frozen_datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.shell import (
    _shell_escape,
    cmd_build_ndc_prompt,
    cmd_build_prompt,
    cmd_consolidate,
    cmd_extract,
    cmd_parse_haiku,
    cmd_save_position,
    main,
)
from pipeline.types import ConsolidationResult, ExtractResult, HaikuResult, TokenUsage

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def test_shell_escape_simple():
    assert _shell_escape("hello") == "'hello'"


def test_shell_escape_with_quotes():
    assert _shell_escape("it's fine") == "'it'\\''s fine'"


def test_shell_escape_empty():
    assert _shell_escape("") == "''"


def test_cmd_parse_haiku_normal(capsys):
    haiku_json = json.dumps({
        "result": "## 10:30 | did stuff\ndetails here",
        "usage": {
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_read_input_tokens": 200,
        },
        "total_cost_usd": 0.005,
    })
    with patch("sys.stdin", StringIO(haiku_json)):
        cmd_parse_haiku()
    output = capsys.readouterr().out
    assert "IS_SKIP=false" in output
    assert "TK_IN=500" in output
    assert "TK_OUT=100" in output
    assert "TK_CACHE=200" in output
    assert "HAIKU_TEXT_FILE=" in output
    # Verify the text file was created with correct content
    for line in output.strip().split("\n"):
        if line.startswith("HAIKU_TEXT_FILE="):
            path = line.split("=", 1)[1].strip("'")
            content = open(path).read()
            assert "## 10:30 | did stuff" in content
            os.unlink(path)
            break


def test_cmd_parse_haiku_skip(capsys):
    haiku_json = json.dumps({
        "result": "SKIP — duplicate",
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_input_tokens": 0,
    })
    with patch("sys.stdin", StringIO(haiku_json)):
        cmd_parse_haiku()
    output = capsys.readouterr().out
    assert "IS_SKIP=true" in output
    # cleanup
    for line in output.strip().split("\n"):
        if line.startswith("HAIKU_TEXT_FILE="):
            os.unlink(line.split("=", 1)[1].strip("'"))


def test_cmd_save_position():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = f.name
    try:
        cmd_save_position(path, "test-session-123", 42)
        with open(path) as f:
            data = json.load(f)
        assert data["session"] == "test-session-123"
        assert data["line"] == 42
    finally:
        os.unlink(path)


def test_cmd_build_ndc_prompt(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        # Create a fake memory file
        mem = os.path.join(d, "now.md")
        with open(mem, "w") as f:
            f.write("## 10:30 | did stuff\ndetails")

        # Create a fake template
        import pipeline.prompts as prompts_mod
        templates_dir = os.path.join(d, "prompts")
        os.makedirs(templates_dir)
        with open(os.path.join(templates_dir, "compress-ndc.prompt.txt"), "w") as f:
            f.write("Compress:\n{{NOW_CONTENT}}")
        monkeypatch.setattr(prompts_mod, "PROMPTS_DIR", templates_dir)

        out = os.path.join(d, "prompt.txt")
        cmd_build_ndc_prompt(mem, out)

        content = open(out).read()
        assert "## 10:30 | did stuff" in content
        assert "details" in content


def test_cmd_parse_haiku_empty_stdin_raises():
    """Regression: empty stdin (broken pipe) must raise, not return zeros."""
    with patch("sys.stdin", StringIO("")):
        with pytest.raises(RuntimeError, match="invalid JSON"):
            cmd_parse_haiku()


# --- cmd_extract ---

def test_cmd_extract_prints_shell_vars(capsys):
    fake_result = ExtractResult(
        exchanges="[human] hello\n[assistant] hi",
        position=10,
        human_count=3,
        assistant_count=2,
    )
    with patch("pipeline.shell.extract_session", return_value=fake_result):
        cmd_extract(session_id="sess-abc", project_dir="/tmp/fake")

    output = capsys.readouterr().out
    assert "POSITION=10" in output
    assert "HUMAN_COUNT=3" in output
    assert "ASSISTANT_COUNT=2" in output
    assert "EXCHANGE_COUNT=5" in output
    assert "EXTRACT_FILE=" in output

    # Verify temp file was written with exchanges content
    for line in output.strip().split("\n"):
        if line.startswith("EXTRACT_FILE="):
            path = line.split("=", 1)[1].strip("'")
            content = open(path).read()
            assert "[human] hello" in content
            os.unlink(path)
            break


# --- cmd_build_prompt ---

def test_cmd_build_prompt_writes_output():
    fake_prompt = "You are summarizing: extract_text | last: last_text | time: 15m | branch: main"
    with tempfile.TemporaryDirectory() as d:
        extract_file = os.path.join(d, "extract.txt")
        last_entry_file = os.path.join(d, "last_entry.txt")
        output_file = os.path.join(d, "prompt.txt")

        with open(extract_file, "w") as f:
            f.write("extract_text")
        with open(last_entry_file, "w") as f:
            f.write("last_text")

        with patch("pipeline.shell.build_save_prompt", return_value=fake_prompt):
            cmd_build_prompt(
                extract_file=extract_file,
                last_entry_file=last_entry_file,
                time="15m",
                branch="main",
                output_file=output_file,
            )

        content = open(output_file).read()
        assert content == fake_prompt


def test_cmd_build_prompt_passes_correct_args():
    with tempfile.TemporaryDirectory() as d:
        extract_file = os.path.join(d, "extract.txt")
        last_entry_file = os.path.join(d, "last_entry.txt")
        output_file = os.path.join(d, "prompt.txt")

        with open(extract_file, "w") as f:
            f.write("  the extract  ")
        with open(last_entry_file, "w") as f:
            f.write("  the last entry  ")

        with patch("pipeline.shell.build_save_prompt", return_value="ok") as mock_bsp:
            cmd_build_prompt(
                extract_file=extract_file,
                last_entry_file=last_entry_file,
                time="30m",
                branch="feature/x",
                output_file=output_file,
            )

        mock_bsp.assert_called_once_with(
            time="30m",
            branch="feature/x",
            last_entry="the last entry",
            extract="the extract",
        )


# --- cmd_consolidate ---

def test_cmd_consolidate_no_staging_files_prints_zero(capsys):
    with tempfile.TemporaryDirectory() as d:
        cmd_consolidate(staging_dir=d, recent_file="/nonexistent", archive_file="/nonexistent")
    assert "STAGING_COUNT=0" in capsys.readouterr().out


def test_cmd_consolidate_with_staging_files(capsys):
    fake_tokens = TokenUsage(input=100, output=50, cache=10, cost_usd=0.001)
    fake_result = ConsolidationResult(recent="new recent", archive="new archive", tokens=fake_tokens)

    with tempfile.TemporaryDirectory() as d:
        # Create a staging file dated in the past (not today)
        past_file = os.path.join(d, "today-2020-01-01.md")
        with open(past_file, "w") as f:
            f.write("old entry")

        with patch("pipeline.consolidate.consolidate", return_value=fake_result) as mock_con:
            cmd_consolidate(staging_dir=d, recent_file="/nonexistent", archive_file="/nonexistent")

        output = capsys.readouterr().out
        assert "STAGING_COUNT=1" in output
        assert "RECENT_OUT=" in output
        assert "ARCHIVE_OUT=" in output
        assert "TK_IN=100" in output
        assert "TK_OUT=50" in output
        assert "TK_CACHE=10" in output

        # Verify consolidate was called with the staging content
        mock_con.assert_called_once()
        staging_arg = mock_con.call_args[0][0]
        assert "today-2020-01-01.md" in staging_arg

        # Cleanup temp files printed in output
        for line in output.strip().split("\n"):
            for prefix in ("RECENT_OUT=", "ARCHIVE_OUT="):
                if line.startswith(prefix):
                    path = line.split("=", 1)[1].strip("'")
                    if os.path.exists(path):
                        os.unlink(path)


# --- main ---

# --- Format detection (mirrors save-session.sh Step 5b regex) ---

import re

HEADER_RE = re.compile(r'^## \d{2}:\d{2} \|')


def test_format_regex_wellformed_header():
    """Well-formed '## HH:MM | branch' header passes the validation regex."""
    assert HEADER_RE.match("## 14:32 | infra/memory-prompts-extraction")


def test_format_regex_raw_echo_rejected():
    """Raw conversation echo does not match expected header format."""
    assert not HEADER_RE.match("[HUMAN] hello")


def test_format_regex_headerless_summary_rejected():
    """Summary without ## header does not match expected format."""
    assert not HEADER_RE.match("Fixed authentication bug in login flow")


def test_main_unknown_command_exits_1():
    with patch("sys.argv", ["shell.py", "nonexistent-cmd"]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1


def test_main_no_args_exits_1():
    with patch("sys.argv", ["shell.py"]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1


# --- cmd_parse_haiku output_file branch (lines 100-102) ---

def test_cmd_parse_haiku_with_output_file(capsys):
    """When output_file is provided, text is also written to that path."""
    haiku_json = json.dumps({
        "result": "## 11:00 | wrote tests\nall green",
        "usage": {
            "input_tokens": 300,
            "output_tokens": 60,
            "cache_read_input_tokens": 0,
        },
        "total_cost_usd": 0.003,
    })
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as out_f:
        out_path = out_f.name

    try:
        with patch("sys.stdin", StringIO(haiku_json)):
            cmd_parse_haiku(output_file=out_path)

        output = capsys.readouterr().out
        assert "IS_SKIP=false" in output

        content = open(out_path).read()
        assert "## 11:00 | wrote tests" in content
        assert "all green" in content

        # Cleanup the auto temp file too
        for line in output.strip().split("\n"):
            if line.startswith("HAIKU_TEXT_FILE="):
                p = line.split("=", 1)[1].strip("'")
                if os.path.exists(p):
                    os.unlink(p)
    finally:
        os.unlink(out_path)


# --- cmd_consolidate: today/done filtering (line 130) and existing recent/archive (lines 140-146) ---

def test_cmd_consolidate_skips_today_file(capsys):
    """A staging file named with today's date is excluded from consolidation."""
    from pipeline._tz import today_str
    today = today_str()

    with tempfile.TemporaryDirectory() as d:
        today_file = os.path.join(d, f"today-{today}.md")
        with open(today_file, "w") as f:
            f.write("today's entry — should be skipped")

        cmd_consolidate(staging_dir=d, recent_file="/nonexistent", archive_file="/nonexistent")

    assert "STAGING_COUNT=0" in capsys.readouterr().out


def test_cmd_consolidate_today_filter_respects_remember_tz(monkeypatch, capsys):
    """Regression: the 'today' filter in consolidate uses REMEMBER_TZ, not UTC.

    At 03:12 UTC on 04-23 with REMEMBER_TZ=America/New_York, 'today' is
    04-22 in the user's zone. A staging file named ``today-2026-04-22.md``
    must be SKIPPED (it's today locally), not consolidated as yesterday.
    If Python used UTC here, the file would be consolidated prematurely —
    a correctness bug, not just cosmetic.
    """
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    moment = datetime(2026, 4, 23, 3, 12, 0, tzinfo=timezone.utc)

    with tempfile.TemporaryDirectory() as d:
        edt_today = os.path.join(d, "today-2026-04-22.md")
        with open(edt_today, "w") as f:
            f.write("today in EDT — should be skipped")

        # Guard rail: patch consolidate so a bug here can never hit the real API.
        with patch("pipeline.consolidate.consolidate") as mock_con, \
                patch("pipeline._tz.datetime", frozen_datetime(moment)):
            cmd_consolidate(
                staging_dir=d,
                recent_file="/nonexistent",
                archive_file="/nonexistent",
            )

    assert "STAGING_COUNT=0" in capsys.readouterr().out
    mock_con.assert_not_called()


def test_cmd_consolidate_processes_yesterday_file_in_tz_context(monkeypatch, capsys):
    """The TZ filter must not over-skip: yesterday's file SHOULD be consolidated.

    At 03:12 UTC on 04-23 with REMEMBER_TZ=America/New_York, 'today' is
    04-22 in EDT. A staging file named ``today-2026-04-21.md`` is yesterday
    in both UTC and EDT — it must be picked up for consolidation.

    This is the inverse of test_cmd_consolidate_today_filter_respects_remember_tz:
    that test proves today is skipped, this test proves yesterday is NOT skipped.
    """
    monkeypatch.setenv("REMEMBER_TZ", "America/New_York")
    moment = datetime(2026, 4, 23, 3, 12, 0, tzinfo=timezone.utc)
    fake_tokens = TokenUsage(input=10, output=5, cache=0, cost_usd=0.0)
    fake_result = ConsolidationResult(recent="new", archive="new", tokens=fake_tokens)

    with tempfile.TemporaryDirectory() as d:
        yesterday = os.path.join(d, "today-2026-04-21.md")
        with open(yesterday, "w") as f:
            f.write("yesterday in EDT — should be consolidated")

        with patch("pipeline.consolidate.consolidate", return_value=fake_result) as mock_con, \
                patch("pipeline._tz.datetime", frozen_datetime(moment)):
            cmd_consolidate(
                staging_dir=d,
                recent_file=os.path.join(d, "recent.md"),
                archive_file=os.path.join(d, "archive.md"),
            )

    assert "STAGING_COUNT=1" in capsys.readouterr().out
    mock_con.assert_called_once()


def test_cmd_consolidate_skips_done_file(capsys):
    """A staging file ending with .done.md is excluded from consolidation."""
    with tempfile.TemporaryDirectory() as d:
        done_file = os.path.join(d, "today-2020-05-01.done.md")
        with open(done_file, "w") as f:
            f.write("already processed")

        cmd_consolidate(staging_dir=d, recent_file="/nonexistent", archive_file="/nonexistent")

    assert "STAGING_COUNT=0" in capsys.readouterr().out


def test_cmd_consolidate_reads_existing_recent_and_archive(capsys):
    """When recent_file and archive_file exist, their contents are passed to consolidate."""
    fake_tokens = TokenUsage(input=50, output=20, cache=0, cost_usd=0.0)
    fake_result = ConsolidationResult(recent="new recent", archive="new archive", tokens=fake_tokens)

    with tempfile.TemporaryDirectory() as d:
        staging_file = os.path.join(d, "today-2020-01-01.md")
        with open(staging_file, "w") as f:
            f.write("old entry")

        recent_file = os.path.join(d, "recent.md")
        archive_file = os.path.join(d, "archive.md")
        with open(recent_file, "w") as f:
            f.write("existing recent content")
        with open(archive_file, "w") as f:
            f.write("existing archive content")

        with patch("pipeline.consolidate.consolidate", return_value=fake_result) as mock_con:
            cmd_consolidate(staging_dir=d, recent_file=recent_file, archive_file=archive_file)

        mock_con.assert_called_once()
        _, recent_arg, archive_arg = mock_con.call_args[0]
        assert recent_arg == "existing recent content"
        assert archive_arg == "existing archive content"

        output = capsys.readouterr().out
        for line in output.strip().split("\n"):
            for prefix in ("RECENT_OUT=", "ARCHIVE_OUT="):
                if line.startswith(prefix):
                    p = line.split("=", 1)[1].strip("'")
                    if os.path.exists(p):
                        os.unlink(p)


# --- main() dispatcher branches (lines 215-255) ---

def test_main_dispatches_extract():
    with patch("pipeline.shell.cmd_extract") as mock_fn:
        with patch("sys.argv", ["shell.py", "extract", "sess-1", "/tmp/proj"]):
            main()
    mock_fn.assert_called_once_with(session_id="sess-1", project_dir="/tmp/proj")


def test_main_dispatches_build_prompt():
    with patch("pipeline.shell.cmd_build_prompt") as mock_fn:
        with patch("sys.argv", ["shell.py", "build-prompt", "ef", "lef", "15m", "main", "out"]):
            main()
    mock_fn.assert_called_once_with(
        extract_file="ef",
        last_entry_file="lef",
        time="15m",
        branch="main",
        output_file="out",
    )


def test_main_dispatches_build_ndc_prompt():
    with patch("pipeline.shell.cmd_build_ndc_prompt") as mock_fn:
        with patch("sys.argv", ["shell.py", "build-ndc-prompt", "mem.md", "out.txt"]):
            main()
    mock_fn.assert_called_once_with(memory_file="mem.md", output_file="out.txt")


def test_main_dispatches_parse_haiku_no_output_file(capsys):
    with patch("pipeline.shell.cmd_parse_haiku") as mock_fn:
        with patch("sys.argv", ["shell.py", "parse-haiku"]):
            main()
    mock_fn.assert_called_once_with(output_file="")


def test_main_dispatches_parse_haiku_with_output_file():
    with patch("pipeline.shell.cmd_parse_haiku") as mock_fn:
        with patch("sys.argv", ["shell.py", "parse-haiku", "/tmp/out.txt"]):
            main()
    mock_fn.assert_called_once_with(output_file="/tmp/out.txt")


def test_main_dispatches_save_position():
    with patch("pipeline.shell.cmd_save_position") as mock_fn:
        with patch("sys.argv", ["shell.py", "save-position", "last.json", "sess-2", "99"]):
            main()
    mock_fn.assert_called_once_with(last_save_file="last.json", session_id="sess-2", position=99)


def test_main_dispatches_consolidate():
    with patch("pipeline.shell.cmd_consolidate") as mock_fn:
        with patch("sys.argv", ["shell.py", "consolidate", "/staging", "recent.md", "archive.md"]):
            main()
    mock_fn.assert_called_once_with(
        staging_dir="/staging",
        recent_file="recent.md",
        archive_file="archive.md",
    )


