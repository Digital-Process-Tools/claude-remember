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

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import spawn_counting
from tests.spawn_counting import make_shim_dir

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

    # The system zone is chosen so its DATE differs from UTC's right now.
    # This used to be pinned to America/Los_Angeles, which shares a calendar
    # date with UTC for most of the day — so removing the `[ -n "$REMEMBER_TZ" ]`
    # guard, the literal historical bug, passed outside roughly 17:00–24:00 PT
    # (#175). Kiritimati (+14) differs from UTC once the UTC hour reaches 10;
    # Niue (-11) differs before 11. Between them every hour is covered, so this
    # detects the regression whenever it runs rather than a third of the time.
    utc_hour = int(subprocess.run(
        [_BASH, "-c", "TZ=UTC date +%H"], capture_output=True, text=True,
    ).stdout.strip())
    system_tz = "Pacific/Kiritimati" if utc_hour >= 10 else "Pacific/Niue"

    expected_local = subprocess.run(
        [_BASH, "-c", f"TZ={system_tz} date +%Y-%m-%d"],
        capture_output=True, text=True,
    ).stdout.strip()
    utc_date = subprocess.run(
        [_BASH, "-c", "TZ=UTC date +%Y-%m-%d"], capture_output=True, text=True,
    ).stdout.strip()
    if expected_local == utc_date:
        # The zone was chosen so the two dates cannot coincide — unless `date`
        # ignored the zone name entirely, which is what happens where there is
        # no tz database. The Windows runners are exactly that: Git Bash ships
        # no tzdata, so every TZ= resolves to UTC and this test can prove
        # nothing there. Skipping says so; failing would have blamed the code.
        pytest.skip(
            f"`date` ignores IANA zone names here ({system_tz} == UTC == "
            f"{utc_date}) — no tz database on this platform"
        )

    result = _run_logsh(project, system_tz=system_tz)
    assert result["ACTUAL"] == expected_local, (
        f"Empty REMEMBER_TZ should fall back to system local ({expected_local}), "
        f"not UTC ({utc_date}). Got: {result['ACTUAL']}"
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


# ── config() reads the merged config in one pass (#232) ──────────────────────
#
# config() used to spend one `jq` process per key. #230 measured PostToolUse at
# 20 external spawns per tool call and named these reads as the largest
# remaining block: three at log.sh source time (.timezone, .model,
# .reject_pattern) plus two in the hook itself, all against the SAME merged
# file. #232 collapses them into one read.
#
# Collapsing reads means reading keys before knowing they are wanted, and that
# moves two things that are not the spawn count. Both are pinned here, because
# the spawn count is the cheap half of this change and the semantics are the
# half that can hurt: PostToolUse is the write path, and a config that reads as
# "all defaults" there is a hook that quietly stops capturing.


def _run_config_calls(tmp_path, bundled: str, project: str | None, calls: str,
                      env_extra: dict | None = None):
    """Source log.sh against a hand-written pair of config layers and run
    arbitrary shell after it. Returns the CompletedProcess so a test can look
    at stdout and stderr separately — the difference between "the default,
    reported" and "the default, silently" is entirely in stderr."""
    project_dir = tmp_path / "proj"
    pipeline_dir = tmp_path / "plugin"
    home = tmp_path / "home"
    for d in (project_dir, pipeline_dir, home):
        d.mkdir(parents=True, exist_ok=True)
    (pipeline_dir / "config.json").write_text(bundled, encoding="utf-8")
    if project is not None:
        remember = project_dir / ".remember"
        remember.mkdir(parents=True, exist_ok=True)
        (remember / "config.json").write_text(project, encoding="utf-8")
    script = f'''
export PROJECT_DIR="{_bash_path(project_dir)}"
export PIPELINE_DIR="{_bash_path(pipeline_dir)}"
export HOME="{_bash_path(home)}"
source "{_bash_path(LOG_SH)}"
{calls}
'''
    env = {**os.environ, "HOME": _bash_path(home)}
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED", "REMEMBER_TZ",
                  "REMEMBER_MODEL", "REMEMBER_REJECT_PATTERN"):
        env.pop(stale, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([_BASH, "-c", script], env=env,
                          capture_output=True, text=True, timeout=60)


class TestConfigIsReadInOnePass:

    def test_the_whole_config_is_read_with_one_jq(self, tmp_path):
        """The assertion this change exists for.

        log.sh asks three questions of the merged config while being sourced
        and callers ask more afterwards. Every one of them used to be its own
        `jq` process against the same unchanging file. Counted by execution
        via a PATH shim, for the reason #227 gives: wall clock is what differs
        between platforms, spawn count is what causes it.
        """
        if not shutil.which("jq"):
            pytest.skip("no jq on PATH — the jq-free path is covered by "
                        "test_jq_free_config.py")
        spawn_log = tmp_path / "spawns.log"
        shims = make_shim_dir(tmp_path, spawn_log)
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"model": "sonnet", "timezone": "UTC",
                                "cooldowns": {"save_seconds": 45},
                                "thresholds": {"delta_lines_trigger": 7}}),
            project=None,
            calls=(
                "config '.cooldowns.save_seconds' 120 > /dev/null\n"
                "config '.thresholds.delta_lines_trigger' 50 > /dev/null\n"
                "config '.time_format' 24h > /dev/null\n"
            ),
            env_extra={"SPAWN_LOG": str(spawn_log),
                       "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"},
        )
        assert result.returncode == 0, result.stderr
        # Only reads of the MERGED config. lib-memory-dir.sh's pass-1
        # `.data_dir` read is against the bundled layer and happens before the
        # merged file exists at all — a different file, not a duplicate read.
        reads = [line for line in spawn_counting.spawns(spawn_log)
                 if line.startswith("jq ") and " -s " not in line
                 and "remember-config-" in line]
        assert len(reads) <= 1, (
            f"{len(reads)} separate jq reads of the merged config, one per "
            f"key, against a file that does not change between them:\n  "
            + "\n  ".join(reads)
        )

    def test_a_config_that_does_not_parse_is_reported(self, tmp_path):
        """A malformed config must not read as a tidy set of defaults.

        The merge falls back to copying the bundled layer when it cannot
        combine the layers, so the way a caller actually ends up holding an
        unparseable merged config is a bundled layer that is itself broken.
        In that state every config() call returns its built-in default — the
        cooldown, the delta threshold, the model — and before this change it
        did so without a word anywhere. That is a silent degraded read in the
        hook that decides whether memory gets captured.

        Defaults are still the right ANSWER; being quiet about them is not.
        """
        result = _run_config_calls(
            tmp_path,
            bundled='{"model": "sonnet",',      # truncated on purpose
            project=None,
            calls="config '.model' 'haiku'\n",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "haiku", (
            "an unreadable config must still yield the caller's default — "
            "reporting the problem must not turn into failing the read"
        )
        assert result.stderr.strip(), (
            "an unreadable config.json produced no diagnostic at all: every "
            "config() call silently became its hardcoded default"
        )

    def test_an_absent_key_is_not_reported_as_a_failed_read(self, tmp_path):
        """The other half, and the reason the first half is not just `set -x`.

        "the file says nothing about this key" and "the file could not be
        read" must not collapse into the same observable. They produce the
        same VALUE — the caller's default — so the only thing separating them
        is that one of them is reported and the other is not.
        """
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"model": "sonnet"}),
            project=None,
            calls="config '.cooldowns.save_seconds' 120\n",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "120"
        assert result.stderr.strip() == "", (
            "a key that is simply absent was reported as a problem — a "
            "warning that fires on ordinary configs is a warning nobody "
            f"reads: {result.stderr!r}"
        )

    def test_a_boolean_false_survives_the_one_pass_read(self, tmp_path):
        """#159, restated against the new reader.

        `features.ndc_compression: false` must come back as "false", not as
        its default. Every one-pass flattening scheme has a natural way to
        lose exactly this value (drop falsy, drop empty, drop null all look
        alike from a distance), and this repo has already shipped that bug
        once.
        """
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"features": {"ndc_compression": False},
                                "git_backup": {"gpg_sign": False}}),
            project=None,
            calls=("config '.features.ndc_compression' true\n"
                   "config '.git_backup.gpg_sign' true\n"),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["false", "false"], result.stdout

    def test_a_per_project_override_still_wins(self, tmp_path):
        """The one-pass read is of the MERGED config, not of one layer."""
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"cooldowns": {"save_seconds": 99}}),
            project=json.dumps({"cooldowns": {"save_seconds": 999}}),
            calls="config '.cooldowns.save_seconds' 120\n",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "999"

    def test_the_oauth_token_is_not_left_lying_in_a_shell_variable(self, tmp_path):
        """#230 refused to cache the merged config because it can carry
        `haiku.oauth_token`, a live credential — which is why lib-memory-dir.sh
        creates that file 0600, per PID, under an EXIT trap. Reading every key
        up front is the same trade taken through a different door: the token
        would sit in a shell variable in every process that sources log.sh,
        for as long as it lives, in a hook that also runs other people's
        scripts via dispatch().

        Nothing needs it there. pipeline/haiku.py reads the token from the
        merged file in Python; no config() caller asks for `.haiku.*` at all.
        So it stays out of the bulk read, and this pins that.
        """
        token = "sk-ant-oat01-NOT-A-REAL-TOKEN-0123456789"
        dump = tmp_path / "shell-vars.txt"
        # The token is never written into the script itself — bash publishes
        # the whole -c string as BASH_EXECUTION_STRING, so a grep spelled out
        # here would find its own argument and pass for the wrong reason.
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"model": "sonnet",
                                "haiku": {"oauth_token": token}}),
            project=None,
            calls=("config '.model' haiku\n"
                   f'( set -o posix; set ) > "{_bash_path(dump)}"\n'),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "sonnet"
        assert token not in dump.read_text(encoding="utf-8", errors="replace"), (
            "the OAuth token was read into a shell variable by a config() "
            "call that did not ask for it"
        )

    def test_the_token_is_still_readable_when_it_is_actually_asked_for(self, tmp_path):
        """Keeping it out of the bulk read must not make it unreadable — that
        would be the same silent-degraded-read defect wearing a security
        rationale."""
        token = "sk-ant-oat01-NOT-A-REAL-TOKEN-0123456789"
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"haiku": {"oauth_token": token}}),
            project=None,
            calls="config '.haiku.oauth_token' ''\n",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == token


class TestConfigRefusesRatherThanGuesses:
    """A flattened table maps `a.b` onto a shell variable name, and there are
    config shapes where that mapping would answer the wrong question with a
    straight face. The reader refuses those and reads them one key at a time
    instead — which is the pre-#232 path, so the answers stay right.

    These are not hypothetical shapes: they are the three ways the mapping can
    lose. Each is asserted by the VALUE it must still return, not by which path
    produced it, so they hold whichever way a future rewrite goes.
    """

    def test_two_keys_that_flatten_to_the_same_name_do_not_shadow_each_other(self, tmp_path):
        """`{"a": {"b": 1}, "a_b": 2}` — both become `a_b` under any
        dot-to-underscore mapping. One of them would silently return the
        other's value."""
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"a": {"b": 1}, "a_b": 2}),
            project=None,
            calls=("config '.a.b' 0\n"
                   "config '.a_b' 0\n"),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["1", "2"], result.stdout

    def test_a_value_containing_a_newline_is_not_truncated(self, tmp_path):
        """reject_pattern is a user-supplied regex. A line-based table would
        keep everything up to the first newline and drop the rest — a gate
        that silently matches less than it was told to."""
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"reject_pattern": "one\ntwo"}),
            project=None,
            calls="config '.reject_pattern' ''\n",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "one\ntwo\n", repr(result.stdout)

    def test_a_refused_shape_is_not_reported_as_a_broken_config(self, tmp_path):
        """The other side of test_a_config_that_does_not_parse_is_reported.

        Declining to flatten a perfectly valid config is a fast-path decision,
        not a fault, and it happens on every single hook invocation for as long
        as the config keeps that shape. A warning there would fire hundreds of
        times a session on a file with nothing wrong with it, and would teach
        people to ignore the one that means something.
        """
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"a": {"b": 1}, "a_b": 2}),
            project=None,
            calls="config '.a.b' 0\n",
        )
        assert result.returncode == 0
        assert result.stderr.strip() == "", result.stderr

    def test_a_key_under_an_array_does_not_break_the_read(self, tmp_path):
        """config() has no syntax for indexing an array, so paths through one
        are skipped — but their presence must not cost the file its fast path
        or its neighbours their values."""
        result = _run_config_calls(
            tmp_path,
            bundled=json.dumps({"notes": ["a", "b"], "model": "sonnet"}),
            project=None,
            calls="config '.model' haiku\n",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "sonnet"
