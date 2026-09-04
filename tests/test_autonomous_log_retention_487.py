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


def _dump_dir(d: Path) -> str:
    """TEMP DEBUG (#487 CI iteration): filenames AND contents, so the real
    windows-latest job log actually shows what the housekeeping block saw
    -- the DEBUG printf lines added to scripts/save-session.sh land inside
    the run's own session-end-*.log file, not in this subprocess's own
    captured stdout/stderr, since the flush is backgrounded. Remove once
    the mechanism is understood.

    Second CI round found the fresh session-end-*.log itself carries NO
    output at all past its own seeded header line -- not even the stderr
    DEBUG lines added at the housekeeping block -- which means
    save-session.sh is not reaching that block on real Windows. This now
    also dumps `logs/` as a whole (memory-YYYY-MM-DD.log, hook-errors.log)
    since save-session.sh's own `log()` writes there, not to stdout/stderr,
    and its `trap ... ERR` handler logs an early failure the same way.
    """
    out = []
    for p in sorted(d.iterdir()):
        try:
            out.append(f"--- {p.name} ---\n{p.read_text(errors='replace')}")
        except OSError as exc:
            out.append(f"--- {p.name} (unreadable: {exc}) ---")
    logs_dir = d.parent
    for p in sorted(logs_dir.iterdir()):
        if p.is_file() and p.parent == logs_dir:
            try:
                out.append(f"--- logs/{p.name} ---\n{p.read_text(errors='replace')}")
            except OSError as exc:
                out.append(f"--- logs/{p.name} (unreadable: {exc}) ---")
    return "\n".join(out) if out else "(empty)"


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
            "never empty\n" + _dump_dir(autonomous)
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
            "keyed to the configured age at all\n" + _dump_dir(autonomous)
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
            + _dump_dir(autonomous)
        )


class TestHousekeepingRunsIndependentlyOfNdcCompression:
    def test_must_fire_reclaim_survives_ndc_compression_disabled(self, tmp_path):
        """#498: the retention sweep above lived inside
        `if [ "$RUN_NDC" = true ]; then ... fi`, so setting
        features.ndc_compression=false silently disabled ALL
        logs/autonomous/ housekeeping, not just NDC compression -- an
        operator who turns that flag off gets an inert
        autonomous_log_retention_days with no signal it stopped doing
        anything. An old, non-empty log must still be reclaimed with NDC
        compression turned off.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        cfg_layer = plugin / "config.json"
        cfg = json.loads(cfg_layer.read_text())
        cfg.setdefault("features", {})["ndc_compression"] = False
        cfg_layer.write_text(json.dumps(cfg))
        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        stale = autonomous / "session-end-000000-44444.log"
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
            "flush's own housekeeping with features.ndc_compression=false -- "
            "#498's coupling: the sweep lived inside the RUN_NDC block and "
            "turning NDC compression off silently turned housekeeping off "
            "too\n" + _dump_dir(autonomous)
        )


class TestSeedWriteFailureIsReported:
    """#503: the header write that seeds $_END_LOG (and the mkdir -p just
    above it) were both unchecked -- `printf ... >> "$_END_LOG" 2>/dev/null`
    and `mkdir -p ... 2>/dev/null`. A failed seed write leaves the file
    absent or empty exactly as if it had never been opened, so the very
    next flush's own -empty housekeeping reclaims it and #483's original
    bug (no on-disk trace that SessionEnd ever fired) is silently back --
    with scripts/doctor.sh's own SessionEnd-liveness check then misdirecting
    an operator toward a hook-registration problem that does not exist.

    A regular FILE at .remember/logs/autonomous (not a directory) is the
    portable way to make both the mkdir and the printf genuinely fail --
    unlike a read-only-bits fixture, this does not need a root/euid(0) skip,
    and a file occupying that path fails the same way on every platform
    this suite runs on.

    Placed in THIS file rather than in test_session_end_hook_345.py (#503's
    naming would have put it there): that file's own module-level
    pytestmark is still the blanket `sys.platform == "win32"` skip #497
    narrowed everywhere else in this suite, plus a hardcoded literal
    `["bash", ...]` argv and native (unslashed) paths -- the two things
    _run_hook/_posix_path above exist to work around (see #432/#497's own
    comment a few lines up in this file). A #503 test filed there would
    have inherited both and never actually run on the one platform its own
    root-cause narrative is about, rendering as green coverage that is not
    there -- reusing this file's already Windows-safe harness instead.
    """

    def test_must_fire_seed_write_failure_is_reported(self, tmp_path):
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=1, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        autonomous_path = project / ".remember" / "logs" / "autonomous"
        autonomous_path.write_text("blocking file, not a directory (#503 fixture)\n")

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert autonomous_path.is_file() and not autonomous_path.is_dir(), (
            "the fixture must actually block the directory, or this test "
            "proves nothing about the degraded path"
        )
        hook_errors = project / ".remember" / "logs" / "hook-errors.log"
        reported = (hook_errors.read_text() if hook_errors.exists() else "") + result.stderr
        assert "WARNING" in reported, (
            "a seed write that could never land must be reported -- "
            "otherwise this session's flush silently leaves no on-disk "
            "trace, and /remember:doctor cannot tell that apart from a "
            "hook that never fired at all\n" + reported
        )

    def test_must_not_fire_control_an_ordinary_flush_reports_nothing_here(self, tmp_path):
        """Positive control, same fixture shape, autonomous/ left as an
        ordinary writable directory: nothing about this specific failure
        mode should be reported when nothing failed."""
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=1, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        hook_errors = project / ".remember" / "logs" / "hook-errors.log"
        reported = hook_errors.read_text() if hook_errors.exists() else ""
        assert "could not create" not in reported and "could not seed" not in reported, (
            "autonomous/ was left writable -- nothing should be reported "
            "about it\n" + reported
        )


class TestHousekeepingGlobIsPortableAcrossSeparators:
    """CI (PR #499, windows-latest 3.9/3.10/3.11/3.12 -- job 100831279309 and
    its three siblings): every one of those legs left BOTH the backdated
    file and this run's own fresh log in place, for every one of the three
    tests above -- default retention, configured retention, NDC disabled.
    No deletion at any age. That is the exact shape of a housekeeping loop
    whose glob matches nothing at all, for any file, every time.

    Root cause: resolve-paths.sh's `_remember_normalize_win_path` rewrites
    CLAUDE_PROJECT_DIR to a fully backslash-separated Windows-native form on
    msys/cygwin (Claude Code hands it over as `/c/Users/...`; #263/#448
    convert that to `C:\\Users\\...` so the three shell slug sites and
    Python's `_session_dir` agree with Claude Code's own slugging), and
    REMEMBER_DIR is lib-memory-dir.sh's legacy `"${proj}/${data_dir}"` --
    backslash-separated end to end on that platform, same as PROJECT_DIR.

    Every ordinary file op downstream (mkdir -p, >>, stat, rm -f) still
    works with that string on Windows, because the MSYS runtime that
    implements those syscalls translates it -- which is exactly why the
    earlier mkdir, the header write and the mtime read in this same flush
    all succeed on that leg (job log shows both files present, exit 0).
    bash's own glob does not get that translation: it recognises only '/'
    as a path-component boundary on every platform, including Windows Git
    Bash, because that is POSIX glob(3)'s own definition of a pathname, not
    a filesystem property -- a directory ARGUMENT to a glob with no real
    '/' anywhere in it can never match a real subtree, on any bash,
    anywhere. That divergence -- syscalls translate backslash, bash's own
    glob does not -- is the actual mechanism, and it is exactly as true on
    this machine's bash as it is on Windows Git Bash's.

    A full end-to-end run of save-session.sh with a genuinely
    Windows-native REMEMBER_DIR cannot be built on POSIX: POSIX mkdir/open
    treat a backslash as an ordinary filename character rather than a
    separator, so a literal backslash-named directory WOULD satisfy a
    literal-string glob component on POSIX, the two platforms would stop
    disagreeing by accident, and this bug would not reproduce. Extracting
    the real housekeeping block verbatim from the script under test (never
    retyped -- a hand-copied duplicate asserts what the copy happens to do,
    not what the file ships) and feeding it a SYNTHETIC backslash-laden
    REMEMBER_DIR string sidesteps that: only the directory argument to the
    glob is backslash-laden, so the fix's own normalization is exercised
    for real, while the glob's own expansion, and everything after it in
    the loop, land back on the real, forward-slash files this fixture
    created -- no windows-only filesystem behaviour needed to prove it.
    """

    _MARKER_START = "# --- Housekeeping: reclaim aged autonomous logs"
    _MARKER_END = "unset _remember_auto_dir _remember_auto_log"

    @classmethod
    def _extract_housekeeping_block(cls) -> str:
        source = (Path(__file__).parent.parent / "scripts" / "save-session.sh").read_text()
        lines = source.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith(cls._MARKER_START))
        end = next(i for i, line in enumerate(lines) if line.startswith(cls._MARKER_END))
        assert end > start, (
            "the housekeeping block's own start/end markers moved or were "
            "renamed in scripts/save-session.sh -- update _MARKER_START/"
            "_MARKER_END in this test to match, or this extraction silently "
            "grabs the wrong span\n"
            f"start={start} end={end}"
        )
        return "\n".join(lines[start : end + 1])

    def _run_extracted_block(self, autonomous: Path, remember_dir: str, *,
                              retention_days: int = 7, ostype: str = ""):
        """Runs the REAL housekeeping block (extracted verbatim above) in a
        standalone bash process, stood up with just enough of its own
        dependencies (`config`, `log`) stubbed to let it execute in
        isolation from the rest of save-session.sh.

        `remember_dir` is embedded via `shlex.quote`, NOT an f-string
        `!r}` -- `repr()` of a string containing backslashes ESCAPES each
        one (Python-literal syntax: one input backslash becomes two
        characters, `\\\\`), and bash's own single-quoted strings do no
        backslash processing at all, so those doubled characters would
        survive into REMEMBER_DIR's runtime value verbatim -- a
        double-backslash-separated path, not the genuine single-backslash
        Windows-native shape #448 actually produces (self-review finding).
        `shlex.quote` wraps the value in single quotes without escaping
        backslashes, since backslash is not special inside them either --
        exactly what bash itself does with the string, byte for byte.

        `ostype`, empty by default, sets $OSTYPE for the subprocess. The
        fix in scripts/save-session.sh is itself gated on
        `case "$OSTYPE" in msys|cygwin)`, matching
        `_remember_normalize_win_path`'s own gate in resolve-paths.sh (the
        thing that puts backslashes into REMEMBER_DIR in the first place)
        -- so a backslash-laden REMEMBER_DIR only gets normalized when
        $OSTYPE says this is actually Windows Git Bash, never on whatever
        $OSTYPE this test happens to run under.
        """
        block = self._extract_housekeeping_block()
        script = f"""
set -u
config() {{ printf '%s\\n' '{retention_days}'; }}
log() {{ :; }}
_remember_date() {{ date "$@"; }}
REMEMBER_DIR={shlex.quote(remember_dir)}
{block}
"""
        env = dict(os.environ)
        if ostype:
            env["OSTYPE"] = ostype
        result = subprocess.run(
            [BASH, "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, (
            f"the extracted housekeeping block itself failed to run "
            f"(REMEMBER_DIR={remember_dir!r}, OSTYPE={ostype!r})\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

    def test_must_fire_backslash_separated_remember_dir_is_still_swept(self, tmp_path):
        """The fix: even when REMEMBER_DIR arrives fully backslash-separated
        (the real Windows-native shape #448 produces), the extracted block
        must still reclaim an old, non-empty log -- proving the glob's own
        directory argument gets normalized before it is used.
        """
        autonomous = tmp_path / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True)
        stale = autonomous / "session-end-000000-11111.log"
        stale.write_text("12:00:00 [session-end] flush started\n")
        eight_days_ago = time.time() - (8 * 24 * 3600)
        os.utime(stale, (eight_days_ago, eight_days_ago))

        windows_style = str(tmp_path / ".remember").replace("/", "\\")
        self._run_extracted_block(autonomous, windows_style, ostype="msys")

        assert not stale.exists(), (
            "a backslash-separated REMEMBER_DIR (the real Windows-native "
            "form #448 produces), with $OSTYPE=msys (Windows Git Bash, the "
            "one platform the fix is gated on), must not defeat the "
            "retention sweep's own glob -- this is CI job 100831279309's "
            "own failure, reproduced locally by feeding the REAL "
            "housekeeping block a synthetic Windows-shaped REMEMBER_DIR\n"
            + _dump_dir(autonomous)
        )

    def test_must_not_fire_control_a_posix_backslash_named_dir_is_left_untouched(self, tmp_path):
        """Self-review finding: the fix is gated on `$OSTYPE` (msys/cygwin
        only), not applied unconditionally -- a backslash is a perfectly
        ordinary, legal filename character on POSIX, and
        `_remember_normalize_win_path` (resolve-paths.sh) that actually
        puts backslashes into REMEMBER_DIR is itself gated the identical
        way. Without this gate, a POSIX project directory whose real name
        happens to contain a literal `\\` would get silently rewritten to a
        different, generally nonexistent path by an unconditional
        normalization -- turning a working retention sweep into a broken
        one for exactly the directory this fix has no business touching.

        This does not construct a real backslash-NAMED directory (bash's
        `[ -f ]`/`stat`/`rm -f` calls inside the extracted block would
        happily follow such a literal name on POSIX, same as any other
        filename -- proving nothing about the glob's own directory
        argument specifically). It asserts the narrower, decisive claim
        instead: with $OSTYPE left at whatever this test's own platform
        reports (never msys/cygwin), the block must NOT even attempt to
        normalize a backslash-laden REMEMBER_DIR -- checked by feeding a
        REMEMBER_DIR that is backslash-laden AND does not correspond to
        any real directory at all, so if the gate were ever removed and
        the block normalized it anyway, the now-real (forward-slash)
        target it would produce is deliberately made to be this fixture's
        actual, empty autonomous/ directory -- exposing the removal as a
        false "swept" rather than as an unrelated no-op.
        """
        autonomous = tmp_path / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True)
        stale = autonomous / "session-end-000000-33333.log"
        stale.write_text("12:00:00 [session-end] flush started\n")
        eight_days_ago = time.time() - (8 * 24 * 3600)
        os.utime(stale, (eight_days_ago, eight_days_ago))

        # A REMEMBER_DIR that is backslash-laden but resolves, once
        # normalized, to the REAL .remember dir above -- so an ungated
        # normalization would still sweep `stale`, and this control would
        # then wrongly look identical to the fixed behaviour.
        backslash_but_real_if_normalized = str(tmp_path / ".remember").replace("/", "\\")
        self._run_extracted_block(autonomous, backslash_but_real_if_normalized)

        assert stale.exists(), (
            "a backslash-laden REMEMBER_DIR must be left untouched (and "
            "therefore match nothing) when $OSTYPE does not say Windows "
            "Git Bash -- normalizing it anyway would silently mangle a "
            "real POSIX path containing a literal backslash\n"
            + _dump_dir(autonomous)
        )

    def test_must_fire_forward_slash_remember_dir_is_swept_too(self, tmp_path):
        """Positive control: an ordinary POSIX REMEMBER_DIR (what every
        non-Windows leg has always had) must still work after the fix --
        the normalization is a no-op there, not a new requirement.
        """
        autonomous = tmp_path / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True)
        stale = autonomous / "session-end-000000-22222.log"
        stale.write_text("12:00:00 [session-end] flush started\n")
        eight_days_ago = time.time() - (8 * 24 * 3600)
        os.utime(stale, (eight_days_ago, eight_days_ago))

        self._run_extracted_block(autonomous, str(tmp_path / ".remember"))

        assert not stale.exists(), (
            "an ordinary forward-slash REMEMBER_DIR must still be swept "
            "after the fix -- the normalization must be a no-op here, not "
            "a regression\n" + _dump_dir(autonomous)
        )
