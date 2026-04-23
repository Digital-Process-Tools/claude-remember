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

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"


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
