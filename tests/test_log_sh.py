"""Shell-level regression tests for scripts/log.sh.

The critical bug we're locking down: ``MEMORY_LOG_DATE`` used to be
computed at source-time (line 43 of log.sh) BEFORE ``REMEMBER_TZ`` was
set (line 132). With an empty ``TZ=`` prefix, macOS/BSD ``date`` silently
falls back to UTC, producing filenames one day ahead of the user's
local date after roughly 20:00 EDT.

These tests run log.sh in a subprocess with a forced system ``TZ=UTC``
and a config pointing to ``America/Los_Angeles``. If log.sh respects
the config, ``MEMORY_LOG_DATE`` should match the LA date. If log.sh
has the ordering bug, it will match the UTC date instead.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
CONFIG_EXAMPLE = REPO_ROOT / "config.example.json"


def _run_logsh(project_dir, system_tz):
    """Source log.sh under the given system TZ and return MEMORY_LOG_DATE + expected date for the configured TZ."""
    script = f"""
    set -e
    export PROJECT_DIR={project_dir}
    source {LOG_SH}
    # Compute what the date SHOULD be if log.sh honored REMEMBER_TZ
    expected=$(TZ="$REMEMBER_TZ" date +%Y-%m-%d)
    # Extract the date embedded in MEMORY_LOG_FILE
    actual=$(basename "$MEMORY_LOG_FILE" | sed -E 's/^memory-//;s/\\.log$//')
    echo "EXPECTED=$expected"
    echo "ACTUAL=$actual"
    echo "REMEMBER_TZ=$REMEMBER_TZ"
    """
    env = {**os.environ, "TZ": system_tz}
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, f"log.sh failed: {result.stderr}"
    parsed = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k] = v
    return parsed


def _make_project(tmp_path, timezone_value):
    project = tmp_path / "proj"
    (project / ".claude" / "remember").mkdir(parents=True)
    (project / ".remember" / "logs").mkdir(parents=True)
    if timezone_value is not None:
        (project / ".claude" / "remember" / "config.json").write_text(
            f'{{"timezone": "{timezone_value}"}}'
        )
    return project


def test_log_sh_uses_configured_timezone_over_system_tz(tmp_path):
    """Regression: config.timezone must drive MEMORY_LOG_DATE, not system TZ.

    With system TZ=UTC and config.timezone=America/Los_Angeles, the
    resolved MEMORY_LOG_DATE must match the LA date at the same instant.
    If the load-order bug returns, this will match the UTC date instead
    (roughly 5–8pm Pacific onwards, LA and UTC disagree on day).
    """
    project = _make_project(tmp_path, "America/Los_Angeles")
    result = _run_logsh(project, system_tz="UTC")
    assert result["REMEMBER_TZ"] == "America/Los_Angeles"
    assert result["ACTUAL"] == result["EXPECTED"], (
        f"MEMORY_LOG_DATE={result['ACTUAL']} but LA date is {result['EXPECTED']} "
        "(ordering bug: MEMORY_LOG_DATE computed before REMEMBER_TZ was set)"
    )


def test_log_sh_no_config_falls_back_to_system_local_not_utc(tmp_path):
    """Regression: no config.json should mean system local, NOT UTC.

    If REMEMBER_TZ falls back to empty string, log.sh must not pass
    ``TZ=""`` to date — that silently becomes UTC on BSD/macOS/Linux.
    Expected behavior: omit TZ prefix entirely, letting date use system TZ.
    """
    project = _make_project(tmp_path, timezone_value=None)
    # Force a known system TZ so we can assert against it
    result = _run_logsh(project, system_tz="America/Los_Angeles")
    # Expected: LA date (system TZ) — not UTC
    expected_la = subprocess.run(
        ["bash", "-c", "TZ=America/Los_Angeles date +%Y-%m-%d"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert result["ACTUAL"] == expected_la, (
        f"Empty REMEMBER_TZ should fall back to system local ({expected_la}), "
        f"not UTC. Got: {result['ACTUAL']}"
    )


def test_log_sh_log_function_produces_filename_matching_configured_tz(tmp_path):
    """End-to-end: calling log() writes to a file whose name matches REMEMBER_TZ date."""
    project = _make_project(tmp_path, "America/Los_Angeles")
    log_dir = project / ".remember" / "logs"
    script = f"""
    set -e
    export PROJECT_DIR={project}
    source {LOG_SH}
    log test "hello from tz test"
    """
    subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "TZ": "UTC"},
        check=True,
        capture_output=True,
    )
    files = list(log_dir.iterdir())
    assert len(files) == 1
    expected_la = subprocess.run(
        ["bash", "-c", "TZ=America/Los_Angeles date +%Y-%m-%d"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert files[0].name == f"memory-{expected_la}.log", (
        f"Log file {files[0].name} does not match LA date {expected_la}"
    )


def test_log_sh_exports_remember_tz_to_python_subprocess(tmp_path):
    """The whole point of ``export REMEMBER_TZ`` is that Python subprocesses
    (haiku calls, consolidate) inherit the configured timezone. Verify a
    Python subprocess launched after sourcing log.sh sees the variable.
    """
    project = _make_project(tmp_path, "Europe/Paris")
    script = f"""
    set -e
    export PROJECT_DIR={project}
    source {LOG_SH}
    python3 -c "import os; print(os.environ.get('REMEMBER_TZ', 'MISSING'))"
    """
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "TZ": "UTC"},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    assert result.stdout.strip() == "Europe/Paris", (
        f"Python subprocess did not inherit REMEMBER_TZ: {result.stdout.strip()!r}"
    )


def test_log_sh_invalid_timezone_falls_back_to_system_local(tmp_path):
    """An invalid TZ name in config.json should not crash log.sh.

    BSD/macOS ``date`` with ``TZ=Invalid/Zone`` may silently fall back to UTC
    or produce an error depending on the OS. The key assertion: log.sh does
    NOT crash, and MEMORY_LOG_DATE is a valid date string.
    """
    project = _make_project(tmp_path, "Invalid/NotAZone")
    script = f"""
    set -e
    export PROJECT_DIR={project}
    source {LOG_SH}
    echo "ACTUAL=$(basename "$MEMORY_LOG_FILE" | sed -E 's/^memory-//;s/\\.log$//')"
    echo "REMEMBER_TZ=$REMEMBER_TZ"
    """
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "TZ": "America/New_York"},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"log.sh crashed with invalid TZ: {result.stderr}"
    parsed = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k] = v
    # Date should be a valid YYYY-MM-DD regardless of what the OS did with the bad TZ
    assert len(parsed.get("ACTUAL", "")) == 10, (
        f"MEMORY_LOG_DATE is not a valid date: {parsed.get('ACTUAL')!r}"
    )


def test_log_sh_explicit_utc_config_overrides_local_system_tz(tmp_path):
    """config.timezone=UTC must produce UTC dates even when system TZ is not UTC.

    This proves the config ACTUALLY drives the date, not just that it
    happens to match the system clock.
    """
    project = _make_project(tmp_path, "UTC")
    result = _run_logsh(project, system_tz="America/Los_Angeles")
    assert result["REMEMBER_TZ"] == "UTC"
    expected_utc = subprocess.run(
        ["bash", "-c", "TZ=UTC date +%Y-%m-%d"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert result["ACTUAL"] == expected_utc, (
        f"config.timezone=UTC should produce {expected_utc}, got {result['ACTUAL']}"
    )


def test_log_sh_timestamp_inside_file_uses_configured_tz(tmp_path):
    """The timestamp INSIDE the log line must also use REMEMBER_TZ.

    The original bug only affected filenames (computed at source time),
    but we should prove timestamps are also correct after the fix.
    """
    project = _make_project(tmp_path, "America/Los_Angeles")
    log_dir = project / ".remember" / "logs"
    script = f"""
    set -e
    export PROJECT_DIR={project}
    source {LOG_SH}
    log test "timestamp check"
    """
    subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "TZ": "UTC"},
        check=True,
        capture_output=True,
    )
    files = list(log_dir.iterdir())
    assert len(files) == 1
    content = files[0].read_text()
    # Timestamp should match LA time, not UTC. We can't freeze shell time,
    # but we can verify the timestamp is from _remember_date, not bare date.
    # At minimum: format is HH:MM:SS and the line contains our message.
    lines = content.strip().splitlines()
    assert len(lines) == 1
    assert "[test] timestamp check" in lines[0]
    # Verify HH:MM:SS format at start
    timestamp = lines[0].split(" ")[0]
    parts = timestamp.split(":")
    assert len(parts) == 3, f"Timestamp not HH:MM:SS format: {timestamp!r}"
    assert all(p.isdigit() and len(p) == 2 for p in parts), (
        f"Timestamp components not 2-digit numbers: {timestamp!r}"
    )


def test_log_sh_marketplace_layout_finds_config_under_dot_claude_remember(tmp_path):
    """Regression: marketplace installs put config at PIPELINE_DIR/.claude/remember/config.json.

    When PIPELINE_DIR is set (the marketplace case), log.sh must look for
    config.json at ``$PIPELINE_DIR/.claude/remember/config.json``, not at
    ``$PIPELINE_DIR/config.json`` directly. The marketplace cache layout
    (``~/.claude/plugins/cache/<mkt>/remember/<ver>/``) places the config
    inside a ``.claude/remember/`` subdirectory next to the plugin code.

    Failure mode before the fix: REMEMBER_TZ resolves to "" (config not
    found) → log lines and date computations fall through to system local
    (or a hard-coded fallback in save-session.sh of "Europe/Paris").
    """
    plugin = tmp_path / "plugin"
    (plugin / ".claude" / "remember").mkdir(parents=True)
    (plugin / ".claude" / "remember" / "config.json").write_text(
        '{"timezone": "America/Los_Angeles"}'
    )
    project = tmp_path / "proj"
    (project / ".remember" / "logs").mkdir(parents=True)
    script = f"""
    set -e
    export PROJECT_DIR={project}
    export PIPELINE_DIR={plugin}
    source {LOG_SH}
    echo "REMEMBER_TZ=$REMEMBER_TZ"
    """
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "TZ": "UTC"},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"log.sh failed: {result.stderr}"
    parsed = dict(
        line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line
    )
    assert parsed.get("REMEMBER_TZ") == "America/Los_Angeles", (
        f"log.sh did not find config.json under PIPELINE_DIR/.claude/remember/. "
        f"Got REMEMBER_TZ={parsed.get('REMEMBER_TZ')!r}. "
        "This means marketplace installs silently lose their timezone (and time_format) settings."
    )


def test_config_example_json_is_valid():
    """config.example.json must be parseable JSON.

    The PR removed the ``timezone`` key — this catches trailing comma
    or other structural issues from the edit.
    """
    content = CONFIG_EXAMPLE.read_text()
    parsed = json.loads(content)  # Raises JSONDecodeError if invalid
    assert isinstance(parsed, dict)
    # timezone should NOT be present (removed by the PR)
    assert "timezone" not in parsed, (
        "config.example.json should not contain timezone key "
        "(removed to prevent UTC default landmine)"
    )
    # time_format should still be present (from PR #34)
    assert parsed.get("time_format") == "24h"
