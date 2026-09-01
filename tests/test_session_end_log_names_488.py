"""Tests for #488: two SessionEnd hooks in the same second must not share a
session-end flush log.

`scripts/session-end-hook.sh` used to name `$_END_LOG`

    session-end-$(_remember_date +%H%M%S).log

second granularity only, no PID or session id. Two SessionEnd hooks for the
same project ending inside the same wall-clock second resolved to the
identical path -- not contrived: scripts/doctor.sh's own SessionEnd-liveness
comments (#370) already treat two concurrently open windows on one project
as an ordinary case. PR #486 (#483) hardened the immediate consequence --
the header write and the subshell redirect both append rather than
truncate, so one hook can no longer clobber a sibling's already-written
output -- but that made the collision harmless, not absent: two flushes
still interleave into one file, and a reader cannot tell whose lines are
whose.

The fix suffixes the timestamp with `$$` (this hook process's own PID), so
two invocations that land in the same second still get distinct files.

The clock is frozen with a PATH shim rather than relied on to land two real
subprocess invocations in the same wall-clock second by luck -- REMEMBER_NO_PRINTF_T=1
(set by `_make_env`) keeps lib-clock.sh on the `date` process path a shim can
intercept (tests/test_ndc_day_boundary.py uses the identical technique for
the identical reason: a fake `date` on PATH is silently ignored once bash's
own spawn-free `printf '%(FMT)T'` builtin takes over on bash >= 4.2).
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


def _freeze_hhmmss(tmp_path: Path, env: dict, frozen: str = "123456") -> None:
    """Shim `date` on PATH so `_remember_date +%H%M%S` always answers
    `frozen`, forcing two hook invocations below to compute the identical
    timestamp component -- the exact same-second collision #488 is about,
    without depending on real wall-clock timing.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "date"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%H%M%S" ]; then\n'
        f'  echo {frozen}\n'
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    shim.chmod(0o755)
    env["PATH"] = f"{bindir}:{env['PATH']}"


class TestSessionEndLogNamesDoNotCollide:
    def test_must_fire_two_runs_with_the_same_computed_second_get_distinct_logs(self, tmp_path):
        """The defect this issue is filed about: same computed timestamp,
        two runs, must be two files -- not one shared, silently interleaved
        log.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        _freeze_hhmmss(tmp_path, env)
        autonomous = project / ".remember" / "logs" / "autonomous"

        result1 = _run_hook(plugin, env, session_id=sid)
        assert result1.returncode == 0, subprocess_failure_detail(result1, project / ".remember")

        result2 = _run_hook(plugin, env, session_id=sid)
        assert result2.returncode == 0, subprocess_failure_detail(result2, project / ".remember")

        session_logs = sorted(autonomous.glob("session-end-123456-*.log"))
        assert len(session_logs) == 2, (
            "two SessionEnd hooks that computed the identical HHMMSS "
            "timestamp did not get two distinct flush logs (#488) -- "
            f"found: {[p.name for p in session_logs]}, everything in the "
            f"directory: {[p.name for p in autonomous.iterdir()]}"
        )
        assert session_logs[0].name != session_logs[1].name, (
            "two distinct paths matched the glob but share a name, which "
            "cannot happen on one filesystem -- the glob itself is wrong"
        )

    def test_must_fire_the_pid_suffix_is_present_and_numeric(self, tmp_path):
        """Positive control, from the other direction: a single run's own
        log must carry a recognisable `-<PID>` suffix at all -- without
        this, the test above could pass for the wrong reason (two files
        that happen to differ for some reason OTHER than the PID this fix
        adds, e.g. a stray leftover from a previous test run).
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        _freeze_hhmmss(tmp_path, env)
        autonomous = project / ".remember" / "logs" / "autonomous"

        result = _run_hook(plugin, env, session_id=sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        session_logs = list(autonomous.glob("session-end-123456-*.log"))
        assert session_logs, (
            "no session-end log matched the frozen timestamp at all:\n"
            + str(list(autonomous.iterdir()))
        )
        suffix = session_logs[0].name[len("session-end-123456-"):-len(".log")]
        assert suffix.isdigit(), (
            f"the filename's own suffix ({suffix!r}) is not a bare PID -- "
            "session-end-hook.sh's naming changed shape unexpectedly"
        )
