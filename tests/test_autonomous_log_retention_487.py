"""Tests for #487: logs/autonomous/session-end-*.log is never reclaimed.

#483 seeded `$_END_LOG` with a header line before the flush subshell ever
opens it, so save-session.sh's own housekeeping

    find "${REMEMBER_DIR}/logs/autonomous" -name "*.log" -empty -delete

no longer matches the log its own parent shell is writing into -- the
correct fix for the bug #483 was filed about. But that `-empty -delete` was
this directory's ONLY retention mechanism: there is no mtime sweep, no
count cap, no rotation. Before #483's fix, every ordinary session-end-*.log
was reclaimed on the next flush because it stayed empty; after it, every
one of them is non-empty by construction and nothing ever removed it. One
file per session, forever.

The fix adds a second, age-keyed sweep over the same "*.log" glob (so it
covers save-*.log and session-end-*.log alike -- both file classes this
directory ever holds), independent of emptiness. Emptiness was always a
proxy for staleness, and it is the proxy that produced #483 in the first
place.

Positive control lives in the same fixture, per this repo's own testing
rule: a run's OWN freshly-written log (mtime "now") must survive its own
housekeeping, exactly as test_session_end_log_swept_483.py already pins for
the emptiness sweep -- an assertion that only checked "the old file is
gone" would also pass if the housekeeping deleted the whole directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash
from subprocess_helpers import subprocess_failure_detail
from test_session_end_hook_345 import HOOK_NAME, _make_env, _wire_hook

# #432/#497: a blanket skipif(sys.platform == "win32") makes the
# windows-latest CI leg collect these tests, skip every one of them, and
# report the leg green -- a check that never ran rendering exactly like a
# check that found nothing. tests/test_hooks_json.py already proves a real
# bash is reachable under Git Bash on that same leg, so the platform is not
# the limitation; narrow the skip to the one thing that actually is: no
# usable bash on PATH at all.
BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)


def _pid_alive(pid: int) -> bool:
    """Portable liveness probe for `_run_hook`'s own wait below.

    NOT `os.kill(pid, 0)` -- the idiom
    tests/test_session_end_hook_345.py's own `_reap` uses (that file still
    carries the blanket win32 skip this fix is retiring here, so `_reap`
    itself has never actually run on Windows). On Windows, CPython maps
    signal 0 to `signal.CTRL_C_EVENT` and calls
    `GenerateConsoleCtrlEvent(0, pid)` -- a console control event sent to a
    PROCESS GROUP, not a liveness probe of one PID -- which is a different
    operation from the POSIX no-op `kill(pid, 0)` performs, and can raise or
    signal the wrong thing when `pid` does not itself name a process group.
    `tasklist` is queried instead: it ships with every supported Windows
    version and answers the same question (does a process with this PID
    exist) without touching signal delivery at all.
    """
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except OSError:
        return False
    return str(pid) in out


def _run_hook(plugin: Path, env: dict, *, session_id, reason: str = "other"):
    """Same shape as test_session_end_hook_345.py's own `_run_hook`, but
    invoking the resolved `BASH` (Git Bash on Windows, not whatever `bash`
    happens to resolve to on PATH) and waiting via `_pid_alive` instead of
    `_reap` -- see that function's own docstring for why.
    """
    hook = plugin / "scripts" / HOOK_NAME
    body = {"reason": reason}
    if session_id is not None:
        body["session_id"] = session_id
    result = subprocess.run(
        [BASH, str(hook)], env=env, capture_output=True, text=True, timeout=60,
        check=False, input=json.dumps(body),
    )
    pid_file = Path(env["CLAUDE_PROJECT_DIR"]) / ".remember" / "tmp" / "save-session.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid = None
        if pid is not None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and _pid_alive(pid):
                time.sleep(0.05)
    return result


class TestAgedAutonomousLogsAreReclaimed:
    def test_must_fire_an_old_nonempty_session_end_log_is_swept(self, tmp_path):
        """The defect: a non-empty session-end-*.log, backdated well past
        the default retention window, must be reclaimed by an ordinary
        flush's own housekeeping -- not just the still-empty ones.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        stale = autonomous / "session-end-000000-11111.log"
        stale.write_text(
            "12:00:00 [session-end] flush started\n"
            "12:00:01 save-session.sh output from a run long finished\n"
        )
        eight_days_ago = time.time() - (8 * 24 * 3600)
        os.utime(stale, (eight_days_ago, eight_days_ago))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert not stale.exists(), (
            "a non-empty session-end log, 8 days old, survived an ordinary "
            "flush's own housekeeping -- #487's retention gap: emptiness "
            "was the only thing ever reclaimed here, and this file was "
            "never empty\n" + str(list(autonomous.iterdir()))
        )

    def test_must_fire_a_fresh_nonempty_log_survives_the_same_sweep(self, tmp_path):
        """Positive control, same fixture shape as the test above but with
        the stale log backdated only 1 day (inside the default 7-day
        window) -- must survive. Without this, a housekeeping change that
        deleted every "*.log" regardless of age would also pass the test
        above.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        recent = autonomous / "session-end-000000-22222.log"
        recent.write_text("12:00:00 [session-end] flush started\n")
        one_day_ago = time.time() - (1 * 24 * 3600)
        os.utime(recent, (one_day_ago, one_day_ago))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert recent.exists(), (
            "a 1-day-old, non-empty session-end log was reclaimed well "
            "inside the default 7-day retention window -- the sweep is not "
            "keyed to the configured age at all\n" + str(list(autonomous.iterdir()))
        )

    def test_must_fire_retention_window_is_configurable(self, tmp_path):
        """`thresholds.autonomous_log_retention_days` must actually gate the
        sweep -- without this, the config read could be dead code that
        always falls through to the hardcoded default.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        cfg_layer = plugin / "config.json"
        import json as _json
        cfg = _json.loads(cfg_layer.read_text())
        cfg.setdefault("thresholds", {})["autonomous_log_retention_days"] = 1
        cfg_layer.write_text(_json.dumps(cfg))
        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        two_days_old = autonomous / "session-end-000000-33333.log"
        two_days_old.write_text("12:00:00 [session-end] flush started\n")
        two_days_ago = time.time() - (2 * 24 * 3600)
        os.utime(two_days_old, (two_days_ago, two_days_ago))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert not two_days_old.exists(), (
            "thresholds.autonomous_log_retention_days=1 did not shrink the "
            "retention window -- a 2-day-old log survived a sweep "
            "configured to reclaim anything over 1 day old\n"
            + str(list(autonomous.iterdir()))
        )
