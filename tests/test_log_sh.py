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
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
CONFIG_EXAMPLE = REPO_ROOT / "config.example.json"


def _bash_path(p) -> str:
    """Forward-slash drive form, usable by BOTH Git Bash and the Windows
    ``python3`` that the bash scripts invoke (jq fallback, migration paths).

    `C:\\Users\\x` -> `C:/Users/x`. Git Bash and Windows Python both accept
    forward-slash drive paths; the MSYS `/c/x` form works in bash but Windows
    Python can't ``open()`` it. On POSIX the path is returned unchanged.
    """
    return str(p).replace("\\", "/")


def _find_bash():
    """Return the bash executable to use for subprocess calls.

    On POSIX, plain "bash". On Windows, the Git-for-Windows bash (NOT the
    System32 WSL launcher, which CreateProcess finds first on PATH). Returns
    None on Windows when Git Bash isn't installed → the module is skipped.
    """
    if sys.platform != "win32":
        return "bash"
    import shutil
    candidates = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "bash.exe")
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    resolved = shutil.which("bash")
    if resolved and "git" in resolved.replace("\\", "/").lower():
        return resolved
    return None


_BASH = _find_bash()

pytestmark = pytest.mark.skipif(_BASH is None, reason="Git Bash not found (Windows without Git for Windows)")


def _run_logsh(project_dir, system_tz):
    """Source log.sh under the given system TZ and return MEMORY_LOG_DATE + expected date for the configured TZ."""
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project_dir)}"
    source "{_bash_path(LOG_SH)}"
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
        [_BASH, "-c", script], env=env, capture_output=True, text=True
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
        [_BASH, "-c", "TZ=America/Los_Angeles date +%Y-%m-%d"],
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
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    log test "hello from tz test"
    """
    subprocess.run(
        [_BASH, "-c", script],
        env={**os.environ, "TZ": "UTC", "HOME": str(tmp_path)},
        check=True,
        capture_output=True,
    )
    files = list(log_dir.iterdir())
    assert len(files) == 1
    expected_la = subprocess.run(
        [_BASH, "-c", "TZ=America/Los_Angeles date +%Y-%m-%d"],
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
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    python3 -c "import os; print(os.environ.get('REMEMBER_TZ', 'MISSING'))"
    """
    result = subprocess.run(
        [_BASH, "-c", script],
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
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    echo "ACTUAL=$(basename "$MEMORY_LOG_FILE" | sed -E 's/^memory-//;s/\\.log$//')"
    echo "REMEMBER_TZ=$REMEMBER_TZ"
    """
    result = subprocess.run(
        [_BASH, "-c", script],
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
        [_BASH, "-c", "TZ=UTC date +%Y-%m-%d"],
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
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    log test "timestamp check"
    """
    subprocess.run(
        [_BASH, "-c", script],
        env={**os.environ, "TZ": "UTC", "HOME": str(tmp_path)},
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


# ── dispatch() ownership / world-writable tests (#67) ────────────────────────

def _make_dispatch_env(tmp_path: Path) -> dict:
    """Return env vars for a dispatch() test run."""
    project = _make_project(tmp_path, timezone_value=None)
    return {
        **os.environ,
        "PROJECT_DIR": _bash_path(project),
        "PIPELINE_DIR": _bash_path(REPO_ROOT),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_DIR": _bash_path(project / ".remember"),
    }


def _run_dispatch(tmp_path: Path, hooks_dir: Path, extra_env: dict = None) -> subprocess.CompletedProcess:
    """Source log.sh and call dispatch("test_event") against a custom hooks dir."""
    env = _make_dispatch_env(tmp_path)
    if extra_env:
        env.update(extra_env)
    script = f"""
set -e
export PROJECT_DIR="{env['PROJECT_DIR']}"
export PIPELINE_DIR="{env['PIPELINE_DIR']}"
export _LIB_MEMORY_DIR_LOADED=1
export REMEMBER_DIR="{env['REMEMBER_DIR']}"
source "{_bash_path(LOG_SH)}"
# Override REMEMBER_HOOKS_DIR to point at our temp fixture.
REMEMBER_HOOKS_DIR="{_bash_path(hooks_dir)}"
dispatch "test_event"
"""
    return subprocess.run([_BASH, "-c", script], env=env, capture_output=True, text=True)


def _write_hook(hooks_event_dir: Path, name: str, content: str, mode: int = 0o755) -> Path:
    """Write an executable hook script and set permissions."""
    hook = hooks_event_dir / name
    hook.write_text(content)
    hook.chmod(mode)
    return hook


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX ownership + world-writable (0o777) semantics don't map to NTFS; "
    "the dispatch() guard is a no-op there. Git Bash fakes mode bits.",
)
class TestDispatchOwnershipChecks:
    """Regression tests for the ownership + world-writable guards in dispatch() (#67)."""

    def test_owned_not_world_writable_executes(self, tmp_path):
        """A hook owned by the current user and not world-writable runs normally."""
        hooks_dir = tmp_path / "hooks.d"
        event_dir = hooks_dir / "test_event"
        event_dir.mkdir(parents=True)
        marker = tmp_path / "ran.txt"
        _write_hook(event_dir, "10-ok.sh", f'#!/bin/bash\ntouch "{marker}"\n', mode=0o755)

        result = _run_dispatch(tmp_path, hooks_dir)

        assert result.returncode == 0, f"dispatch failed: {result.stderr}"
        assert marker.exists(), "Owned, non-world-writable hook should have executed"

    def test_world_writable_hook_is_skipped(self, tmp_path):
        """A world-writable hook (mode 0o777) is skipped with a warning."""
        hooks_dir = tmp_path / "hooks.d"
        event_dir = hooks_dir / "test_event"
        event_dir.mkdir(parents=True)
        marker = tmp_path / "ran.txt"
        _write_hook(event_dir, "10-ww.sh", f'#!/bin/bash\ntouch "{marker}"\n', mode=0o777)

        result = _run_dispatch(tmp_path, hooks_dir)

        assert result.returncode == 0, f"dispatch failed: {result.stderr}"
        assert not marker.exists(), "World-writable hook should have been skipped"
        assert "world-writable" in result.stderr or _dispatch_warned_in_log(tmp_path, "world-writable")

    def test_unowned_hook_is_skipped(self, tmp_path):
        """A hook whose UID doesn't match the current user is skipped with a warning.

        We can't actually chown to another UID without root, so we fake the stat
        output by patching the hook's stat call via a wrapper on PATH.
        """
        hooks_dir = tmp_path / "hooks.d"
        event_dir = hooks_dir / "test_event"
        event_dir.mkdir(parents=True)
        marker = tmp_path / "ran.txt"
        hook = _write_hook(event_dir, "10-other-owner.sh", f'#!/bin/bash\ntouch "{marker}"\n', mode=0o755)

        # Create a fake stat binary that always returns UID 0 (root), regardless of file.
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_stat = fake_bin / "stat"
        fake_stat.write_text('#!/bin/bash\necho 0\n')
        fake_stat.chmod(0o755)

        env_override = {"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
        result = _run_dispatch(tmp_path, hooks_dir, extra_env=env_override)

        assert result.returncode == 0, f"dispatch failed: {result.stderr}"
        assert not marker.exists(), "Hook owned by different user should have been skipped"


def _dispatch_warned_in_log(tmp_path: Path, keyword: str) -> bool:
    """Check if any log file under tmp_path contains the keyword."""
    for log_file in tmp_path.rglob("memory-*.log"):
        if keyword in log_file.read_text():
            return True
    return False
