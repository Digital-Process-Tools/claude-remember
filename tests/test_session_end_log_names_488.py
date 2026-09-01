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
import shlex
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


def _posix_path(p) -> str:
    """Forward-slash a path before handing it to bash (#432/#497 follow-up,
    PR #499 CI: job 100051322749).

    session-end-hook.sh (and save-session.sh, which it invokes one level
    down) derive their OWN script directory from a bash parameter
    expansion (${BASH_SOURCE[0]%/*}) and from `dirname "$0"` -- both of
    which only recognise the ASCII forward slash as a separator. A native
    Windows path handed to bash as its own script argument is
    backslash-separated end to end, so that expansion strips nothing:
    session-end-hook.sh's own comment on that exact line documents this as
    the SAME fallback `dirname` takes on a bare filename with no slash in
    it at all, and sets its hook-directory variable to the current
    directory. Every subsequent `source` of a sibling script then resolves
    against the bash process's own working directory (pytest's, not the
    scripts directory), fails to find resolve-paths.sh, and the hook's own
    soft-fail guard on that source line exits the ENTIRE hook, silently,
    before mkdir, before the flush, before anything -- which is what the CI
    failure actually was: not a broken retention sweep and not a hook that
    failed, but a hook that never ran, one cause behind both reported
    symptoms. tests/test_hooks_json.py already works around this for
    session-start-hook.sh with the identical forward-slashing; this mirrors
    it for every path that becomes part of the invoked script's own path or
    a downstream source line built from it.
    """
    return str(p).replace(chr(92), "/")


def _run_hook(plugin: Path, env: dict, *, session_id, reason: str = "other"):
    """Same shape as test_session_end_hook_345.py's own `_run_hook`, but
    invoking the resolved `BASH` (Git Bash on Windows, not whatever `bash`
    happens to resolve to on PATH) with a forward-slashed script path and
    env (`_posix_path`, above), and waiting via `_pid_alive` instead of
    `_reap` -- see that function's own docstring for why.
    """
    hook = _posix_path(plugin / "scripts" / HOOK_NAME)
    run_env = dict(env)
    for key in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR", "HOME"):
        if key in run_env:
            run_env[key] = _posix_path(run_env[key])
    body = {"reason": reason}
    if session_id is not None:
        body["session_id"] = session_id
    result = subprocess.run(
        [BASH, hook], env=run_env, capture_output=True, text=True, timeout=60,
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
    # PR #499 CI (jobs 100051322749, 100054224050): the hook runs for real
    # under Git Bash on windows-latest but writes REAL wall-clock
    # timestamps, meaning this shim did not intercept `date`. Python's own
    # os.chmod on native Windows only ever toggles the read-only attribute
    # -- there is no POSIX execute bit on that platform for it to set, so a
    # shim `Path.chmod(0o755)` creates from a native Windows process may
    # carry none of whatever MSYS2's own exec()/PATH-search checks for.
    # Best-effort second attempt: ask bash itself (already resolved as
    # `BASH`, real Git Bash on Windows) to chmod the file, which goes
    # through MSYS2's own permission layer instead of Windows Python's.
    # Deliberately best-effort (`check=False`, no assertion on the
    # result) -- if bash's own chmod ALSO does not make MSYS treat this
    # file as executable, `_assert_shim_took_or_skip` below is the second
    # line of defence: it detects the shim not taking and skips the
    # specific test with a stated reason rather than asserting a false
    # failure against #488's own fix, which is unaffected either way.
    if os.name == "nt" and BASH is not None:
        subprocess.run(
            [BASH, "-c", f"chmod +x {shlex.quote(_posix_path(shim))}"],
            capture_output=True, timeout=10, check=False,
        )
    # os.pathsep, not a hardcoded ":" -- this env dict becomes the WINDOWS
    # process environment CreateProcess hands to bash.exe (Git Bash), which
    # performs its own MSYS conversion from a native, semicolon-separated
    # Windows PATH at startup. A colon-joined bindir (itself a
    # backslash-separated Windows path on that platform) ahead of an
    # already-semicolon-joined PATH is neither format -- os.pathsep is ";"
    # there and ":" everywhere else, which is the one join every platform
    # reads back correctly.
    env["PATH"] = os.pathsep.join([str(bindir), env["PATH"]])


def _assert_shim_took_or_skip(autonomous: Path, frozen: str) -> None:
    """Detect whether `_freeze_hhmmss`'s date shim actually intercepted
    `_remember_date`'s `date +%H%M%S` call, and skip THIS test with a
    specific, stated reason if it did not -- rather than asserting a false
    failure against #488's own fix.

    PR #499 CI (jobs 100051322749, 100054224050) showed the hook running
    for real on windows-latest and writing a correctly PID-suffixed
    `session-end-<HHMMSS>-<PID>.log` at the REAL wall-clock HHMMSS: #488's
    naming fix works there, and specifically the SHIM did not take, not the
    fix under test. Reported for filing as a follow-up rather than guessed
    at further here: no Windows machine is available to confirm why an
    extensionless PATH shim built by native Windows Python is, or is not,
    treated as executable by Git Bash / MSYS2's own PATH search once the
    bash-mediated `chmod +x` attempt above has already been tried.
    """
    logs = list(autonomous.glob("session-end-*.log")) if autonomous.is_dir() else []
    if any(frozen in p.name for p in logs):
        return
    pytest.skip(
        f"the date PATH shim (_freeze_hhmmss) did not intercept "
        f"_remember_date's `date +%H%M%S` call on this platform -- observed "
        f"real wall-clock timestamps instead of the frozen {frozen!r} value "
        f"({[p.name for p in logs]}). #488's own PID-suffix fix is not what "
        f"this pins and is unaffected (produced names ARE correctly "
        f"PID-suffixed) -- only the same-second COLLISION case this specific "
        f"test forces via the frozen clock cannot be exercised here."
    )


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

        _assert_shim_took_or_skip(autonomous, "123456")
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

        _assert_shim_took_or_skip(autonomous, "123456")
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
