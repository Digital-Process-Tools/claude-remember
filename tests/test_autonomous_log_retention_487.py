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
import shutil
import subprocess
import sys
import tempfile
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


def _write_shell_script(path: Path, text: str) -> None:
    r"""Writes a `#!/bin/sh` (or bash) script via `open(..., newline="\n")`,
    never `Path.write_text()` (CI, PR #960, windows-latest jobs
    112746585138/.../152 then 112755717371: `Path.write_text()`'s default
    `newline=None` applies universal-newline translation on write, turning
    every `\n` in the content into `os.linesep` -- `\r\n` on Windows. A
    CRLF-corrupted shebang line is a classic "bad interpreter" exec
    failure: the loader reads `#!/bin/sh\r`, fails to resolve that as a
    path, and the script never starts at all -- indistinguishable from
    the shim simply not existing, which is exactly `mktemp_shim_log`
    reading back empty on every statement, not just a later one, that
    was observed). `Path.write_text()` DOES take a `newline=` keyword,
    but only since Python 3.10 -- this repo's own CI matrix still runs
    3.9, so that keyword is not available here; `open(..., newline="\n")`
    is, on every version this suite runs on, and is the same mechanism
    `_run_bash_script` below already uses for the identical reason."""
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        f.write(text)


def _bash_path_prepend(dirs) -> str:
    r"""Builds a bash `:`-joined PATH-prefix string from one or more native
    directories, meant to be spliced into the SCRIPT TEXT a generated
    script exports its own `$PATH` from (`export PATH="<this>:$PATH"`) --
    never into `env["PATH"]`, the dict `_run_bash_script` hands to
    `subprocess.run` (#951).

    `env["PATH"]` stays Windows-native (`;`-joined, drive-letter paths)
    right up until CreateProcess hands it to `bash.exe`; whether MSYS's
    own startup conversion into bash's internal, `:`-joined $PATH
    succeeds for an entry THIS PROCESS prepended is outside Python's
    control and not reproducible on a non-Windows runner. The drive
    letter's own colon is PATH's own separator once that string is
    POSIX-split, so merely forward-slashing a prepended directory
    (`_grc_posix`, used elsewhere in this file for the shim's OWN
    embedded paths -- each a single `open()`/exec target, where a drive
    letter's colon is harmless) does not help here: locally confirmed
    (`/tmp` simulation, trap.d/951) that bash's native colon-splitting
    shreds a `C:\...;C:\...` PATH value and a `C:/...;C:/...` one
    IDENTICALLY, into components none of which resolve, regardless of
    slash direction -- which is also why PR #960's two prior attempts
    (forward-slashing shim content, then fixing shebang newlines) left
    the symptom byte-for-byte unchanged. The one form with no embedded
    colon at all is bash's own MSYS convention (`/c/Users/...`), built
    here in Python and hand-written into the script as literal bash
    source, so it needs no env-block conversion to already be correct --
    on every other platform this repo tests on, a path already has no
    drive letter and this is a no-op forward-slash normalisation.
    """

    def _to_posix(d) -> str:
        s = str(d)
        if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
            tail = s[2:].replace("\\", "/")
            if not tail.startswith("/"):
                tail = "/" + tail
            return f"/{s[0].lower()}{tail}"
        return s.replace("\\", "/")

    return ":".join(_to_posix(d) for d in dirs)


def test_bash_path_prepend_strips_the_drive_letters_own_colon():
    """#951: a test that would have caught both prior, reasoned-but-wrong
    fix attempts in this PR (forward-slashing the shim's own content,
    then fixing the shim's shebang newline) -- NEITHER touches how PATH
    is built, which is where the reporter's CI symptom (the shim never
    invoked at all, not even its own first `echo`) actually originates.

    Platform-independent and runs on every leg -- it exercises
    `_bash_path_prepend`'s string transform directly, not bash's own
    MSYS startup conversion, which cannot be reproduced outside a real
    Windows runner (not something this fix claims to verify; see the
    PR body for what remains reasoned rather than observed).
    """
    # A bare forward-slash of a Windows-native path (the fix ALREADY
    # shipped, twice, in this same file, for the shim's own embedded
    # paths and log target) still carries the drive letter's own colon
    # -- the exact character PATH's own POSIX `:`-splitting treats as a
    # separator, shredding the value regardless of slash direction.
    forward_slashed_only = r"C:\Users\runneradmin\AppData\Local\Temp\x".replace("\\", "/")
    assert ":" in forward_slashed_only, (
        "fixture assumption broken -- a forward-slashed Windows path is "
        "expected to still carry the drive letter's colon"
    )

    posix = _bash_path_prepend([r"C:\Users\runneradmin\AppData\Local\Temp\remember-mktemp-marker-abc"])
    assert posix == "/c/Users/runneradmin/AppData/Local/Temp/remember-mktemp-marker-abc", (
        f"expected the MSYS-style posix form with no embedded colon, got {posix!r}"
    )
    assert ":" not in posix, (
        "#951: a colon survived the conversion -- PATH's own separator "
        f"would still shred this value under bash's native splitting: {posix!r}"
    )

    # Must fire: a path that already has no drive letter (every path on
    # macOS/Linux, where this repo's own CI legs run this same helper)
    # is a no-op forward-slash normalisation, not mangled by the
    # drive-letter branch -- a positive control pairing the negative
    # assertions above, per this repo's own testing rule.
    already_posix = _bash_path_prepend(["/tmp/already/posix/dir"])
    assert already_posix == "/tmp/already/posix/dir", (
        f"a path with no drive letter must pass through unchanged, got {already_posix!r}"
    )

    multi = _bash_path_prepend([r"C:\a", r"C:\b"])
    assert multi == "/c/a:/c/b", (
        f"multiple directories must join with ':' once each is already "
        f"colon-free, got {multi!r}"
    )


def _run_bash_script(script: str, env: dict, *, timeout: int = 30):
    """Runs `script` under BASH by writing it to a real file and invoking
    `bash <path>`, never `bash -c "<script>"` (#914 self-review, PR #927
    CI job 112366941574): on windows-latest, the extracted housekeeping
    block's own generated script -- several times longer than the block
    it replaced -- was silently TRUNCATED when passed as a single `-c`
    argument (confirmed by the reported error itself: an `if` opened on
    line 131 hit EOF at line 133, two lines later, nowhere near the
    block's real closing `fi`/`done`/`unset` thirty-odd lines further
    down) -- a known class of bug when a non-MSYS parent process (Python)
    launches MSYS/Git-Bash's bash.exe directly via CreateProcess, which
    is not MSYS-aware and does not go through MSYS's own argv marshaling.
    A real script FILE sidesteps the whole command-line-argument path;
    `bash -c` is not used again anywhere else in this file.
    """
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", newline="\n") as f:
            f.write(script)
        return subprocess.run(
            [BASH, path], env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    finally:
        os.unlink(path)


def _dump_dir(d: Path) -> str:
    """Filenames AND contents of the autonomous dir plus its parent logs/
    dir, for a decisive assertion-failure message.

    save-session.sh's own `log()` writes to `logs/memory-YYYY-MM-DD.log`,
    not to stdout/stderr, and its `trap ... ERR` handler reports an early
    failure the same way -- neither would be visible from a bare filename
    listing, which is what made this file's own CI iteration on #487 (PR
    #499) slow: several rounds were needed to see that the real windows
    runner's flush was not failing at all, just not finished yet by the
    time the assertion ran (see `_run_hook`'s own
    REMEMBER_TEST_COMPLETION_MARKER wait, below).
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
    # Self-review finding (Explore, PR #499): this helper's own docstring
    # names REMEMBER_TEST_COMPLETION_MARKER's wait as the reason it was
    # written, but the marker file itself (.remember/tmp/completion-marker.log)
    # lived outside logs_dir and was never dumped -- exactly the file that
    # would show whether _run_hook's wait timed out or genuinely observed
    # completion. Included so a future failure here is diagnosable from the
    # assertion message alone, the way this whole helper exists to be.
    marker = logs_dir.parent / "tmp" / "completion-marker.log"
    if marker.exists():
        out.append(f"--- tmp/completion-marker.log ---\n{marker.read_text(errors='replace')}")
    else:
        out.append("--- tmp/completion-marker.log --- (absent)")
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
    env (`_posix_path`, above), and waiting for the real flush to finish
    via REMEMBER_TEST_COMPLETION_MARKER (scripts/session-end-hook.sh)
    rather than `_pid_alive`/`_reap`.

    CI iteration on #487 (PR #499): a real windows-latest runner reports
    $OSTYPE=cygwin, and `_pid_alive`'s own `tasklist` lookup below never
    finds the backgrounded flush there -- returns "not alive" on its very
    first check, long before the real flush (which does complete; it was
    never the retention sweep itself that was broken) is actually done.
    `_pid_alive` is kept as a cheap first pass (it is correct and fast on
    every platform this suite runs on aside from that one real-Windows
    case), and REMEMBER_TEST_COMPLETION_MARKER is the authoritative
    fallback: an explicit, unambiguous "flush exited" line the hook
    itself appends once `bash "$SAVE_SCRIPT"` actually returns, which does
    not depend on any PID or process-table lookup at all.
    """
    hook = _posix_path(plugin / "scripts" / HOOK_NAME)
    run_env = dict(env)
    for key in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR", "HOME"):
        if key in run_env:
            run_env[key] = _posix_path(run_env[key])
    marker = Path(env["CLAUDE_PROJECT_DIR"]) / ".remember" / "tmp" / "completion-marker.log"
    run_env["REMEMBER_TEST_COMPLETION_MARKER"] = _posix_path(marker)
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
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _pid_alive(pid):
                time.sleep(0.05)
    # 30s: TestSeedWriteFailureIsReported's own positive fixture
    # (autonomous/ blocked by a FILE, not a directory) makes $_END_LOG's own
    # path unopenable, which appears to abort the whole backgrounded
    # compound command before its body -- including this marker write --
    # ever runs, so that test waits out the full deadline every time, in
    # exchange for evidence the flush degraded exactly as designed. Not
    # measured against a real flush's own exact completion time -- CI
    # round 4 used a 90s ceiling and the 3 "must fire" tests it fixed did
    # not report how much of that they actually used -- so this is a
    # judgment call: generous enough that a real flush is very unlikely to
    # be cut off, without paying the full 90s on every run of the fixture
    # above. If a future CI round shows a real flush still exceeding this,
    # raise it back up rather than re-guessing.
    deadline = time.monotonic() + 30
    _marker_seen = False
    while time.monotonic() < deadline:
        if marker.exists() and "exited status=" in marker.read_text(errors="replace"):
            _marker_seen = True
            break
        time.sleep(0.1)
    if not _marker_seen:
        # Self-review finding (Explore, PR #499): without this, the loop
        # above degrades silently into an unmonitored 30s sleep on a
        # future regression -- no distinction between "the marker showed
        # up" and "the deadline simply expired". TestSeedWriteFailureIsReported's
        # own positive fixture (autonomous/ blocked by a FILE) is the one
        # EXPECTED case where the marker never appears (see the comment
        # above this loop), so this is a note for pytest's captured
        # stderr, not an assertion -- a hard failure here would break that
        # test's own designed degraded path.
        print(
            f"_run_hook: REMEMBER_TEST_COMPLETION_MARKER not observed within "
            f"30s (marker={marker}) -- either the flush is still running, "
            f"or (expected for TestSeedWriteFailureIsReported's own "
            f"blocking-file fixture) it could never start",
            file=sys.stderr,
        )
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
        # #933: 9 days, not 8 -- the fast path's own cutoff now requires a
        # FULL N+1 days (matching the fallback's floored-days comparison),
        # so an exactly-8-day-old file (N+1 with the default N=7) sits
        # right ON that boundary: its real mtime carries sub-second
        # precision (a real filesystem write, or `os.utime` here), while
        # the fast path's reference file can only represent whole seconds
        # (`touch -d`/`touch -t`), so whether this exact instant lands on
        # one side of that boundary or the other is a coin flip on
        # whichever side of a wall-clock second the test happened to land.
        # 9 days restores the comfortable margin this test always meant to
        # have (same margin #487 originally measured with 8 days against
        # the old cutoff).
        nine_days_ago = time.time() - (9 * 24 * 3600)
        os.utime(stale, (nine_days_ago, nine_days_ago))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert not stale.exists(), (
            "a non-empty session-end log, 9 days old, survived an ordinary "
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
        # #933: 3 days, not 2 -- with retention=1, the fast path's cutoff
        # is now exactly 2 days (N+1, N=1); a 2-day-old file sits right on
        # that boundary (the same sub-second race documented above), so
        # bump to 3 days for the same comfortable margin this test always
        # meant to exercise.
        three_days_old = autonomous / "session-end-000000-33333.log"
        three_days_old.write_text("12:00:00 [session-end] flush started\n")
        three_days_ago = time.time() - (3 * 24 * 3600)
        os.utime(three_days_old, (three_days_ago, three_days_ago))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert not three_days_old.exists(), (
            "thresholds.autonomous_log_retention_days=1 did not shrink the "
            "retention window -- a 3-day-old log survived a sweep "
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
        # #933: 9 days, not 8 -- see the identical comment in
        # test_must_fire_an_old_nonempty_session_end_log_is_swept above;
        # the fast path's cutoff now sits at exactly 8 days (N+1, N=7),
        # so this fixture needs the same margin bump to stay clear of
        # that boundary's own sub-second race.
        nine_days_ago = time.time() - (9 * 24 * 3600)
        os.utime(stale, (nine_days_ago, nine_days_ago))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert not stale.exists(), (
            "a non-empty session-end log, 9 days old, survived an ordinary "
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

        `ostype`, empty by default, shadows $OSTYPE for the block via a
        `local` inside a wrapping function -- NOT an environment variable
        override (self-review-round-2 finding, CI round 5, job
        100892436094): a real windows-latest runner's own Git Bash resets
        $OSTYPE to its own compiled default ("cygwin" there) regardless of
        what the parent process's environment carries, so
        `env["OSTYPE"] = ostype` silently has no effect at all on that one
        platform -- the negative control that relied on it (forcing
        "linux-gnu") passed everywhere else and failed specifically there,
        for a reason that had nothing to do with the fix's own gate.
        `local OSTYPE=...` inside a function is a shell-scoping
        mechanism, not an inherited-environment one: it is not subject to
        whatever makes Git Bash re-assert its own OSTYPE from the
        environment, and `readonly -p` confirms OSTYPE is not a readonly
        bash special (a real one, like BASH_VERSION, could not be
        shadowed this way either). The fix in scripts/save-session.sh is
        itself gated on `case "$OSTYPE" in msys|cygwin)`, matching
        `_remember_normalize_win_path`'s own gate in resolve-paths.sh (the
        thing that puts backslashes into REMEMBER_DIR in the first place)
        -- so a backslash-laden REMEMBER_DIR only gets normalized when
        $OSTYPE says this is actually Windows Git Bash, never on whatever
        $OSTYPE this test happens to run under.
        """
        block = self._extract_housekeeping_block()
        _ostype_shadow = f"local OSTYPE={shlex.quote(ostype)}" if ostype else ":"
        script = f"""
set -u
config() {{ printf '%s\\n' '{retention_days}'; }}
log() {{ :; }}
_remember_date() {{ date "$@"; }}
_run_housekeeping_block() {{
    {_ostype_shadow}
    REMEMBER_DIR={shlex.quote(remember_dir)}
{block}
}}
_run_housekeeping_block
"""
        result = _run_bash_script(script, dict(os.environ))
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
        # #933: 9 days, not 8 -- same margin bump as the full-hook tests
        # above, same reason: 8 days is now exactly the fast path's own
        # cutoff (N+1, N=7), a sub-second race rather than a safe margin.
        nine_days_ago = time.time() - (9 * 24 * 3600)
        os.utime(stale, (nine_days_ago, nine_days_ago))

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

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="$OSTYPE cannot be made to say anything other than this "
               "runner's own real value under real Windows Git Bash -- "
               "confirmed empirically two separate ways on PR #499's own "
               "windows-latest CI (jobs 100892436094 and 100895310415): "
               "an env var override (`env['OSTYPE']=...`) and a `local "
               "OSTYPE=...` shadow inside a wrapping function both left "
               "the extracted block reading the runner's real OSTYPE "
               "(cygwin there) instead of the forced value, so this "
               "negative control's own premise -- exercise the block "
               "with $OSTYPE genuinely NOT saying Windows Git Bash -- "
               "cannot be constructed while actually running under "
               "Windows Git Bash. Skipped loudly rather than left to "
               "pass vacuously against a value it never actually forced; "
               "the fix's OSTYPE gate is still exercised for real by the "
               "msys/forward-slash cases above, which do not depend on "
               "overriding OSTYPE away from its real value.",
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
        instead: with $OSTYPE forced to a definitely-not-Windows value,
        the block must NOT even attempt to normalize a backslash-laden
        REMEMBER_DIR -- checked by feeding a REMEMBER_DIR that is
        backslash-laden AND does not correspond to any real directory at
        all, so if the gate were ever removed and the block normalized it
        anyway, the now-real (forward-slash) target it would produce is
        deliberately made to be this fixture's actual, empty autonomous/
        directory -- exposing the removal as a false "swept" rather than
        as an unrelated no-op.

        Skipped on win32 -- see the class-level skipif above this method.
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
        self._run_extracted_block(autonomous, backslash_but_real_if_normalized, ostype="linux-gnu")

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
        # #933: 9 days, not 8 -- same margin bump, same reason as the
        # other fast-path fixtures in this file.
        nine_days_ago = time.time() - (9 * 24 * 3600)
        os.utime(stale, (nine_days_ago, nine_days_ago))

        self._run_extracted_block(autonomous, str(tmp_path / ".remember"))

        assert not stale.exists(), (
            "an ordinary forward-slash REMEMBER_DIR must still be swept "
            "after the fix -- the normalization must be a no-op here, not "
            "a regression\n" + _dump_dir(autonomous)
        )


class TestHousekeepingSweepIsForkFree:
    """#914: the age-keyed sweep forked a `stat` subshell AND a
    `_remember_date` (date) subshell for EVERY surviving file, on every
    flush that reached this point -- on Windows Git Bash, where a single
    fork costs 50-300ms+ (#511's own measurement), a directory of ~1,200
    files turned one sweep into ~4,000 process starts and ~140s of wall
    time, recurring about every 15 minutes in the reporter's own session.

    The fix reads the clock once, before the loop, and builds a single
    reference file whose mtime is the retention cutoff; `[ "$f" -ot
    "$ref" ]` is a bash builtin and forks nothing. This asserts the
    actual claim #914 measured -- PROCESS STARTS -- not just that the
    sweep still reclaims/retains correctly: a behavior-only test would
    pass unchanged whether the loop forks once per sweep or twice per
    file, and #914 is entirely about which of those two it does.
    """

    def _run_extracted_block_counting_forks(
        self, autonomous: Path, remember_dir: str, *,
        retention_days: int = 7, bin_dir: Path, counter: Path,
    ):
        block = TestHousekeepingGlobIsPortableAcrossSeparators._extract_housekeeping_block()
        # #951: PATH is prepended inside the SCRIPT ITSELF via
        # `_bash_path_prepend`, not through `env["PATH"]` -- see that
        # helper's own docstring for why an env-level prepend cannot be
        # trusted to survive MSYS's win32->posix conversion on Windows.
        script = f"""
set -u
export PATH="{_bash_path_prepend([bin_dir])}:$PATH"
config() {{ printf '%s\n' '{retention_days}'; }}
log() {{ :; }}
_remember_date() {{ date "$@"; }}
REMEMBER_DIR={shlex.quote(remember_dir)}
{block}
"""
        env = dict(os.environ)
        env["REMEMBER_FORK_COUNTER"] = str(counter)
        result = _run_bash_script(script, env)
        assert result.returncode == 0, (
            f"the extracted housekeeping block itself failed to run\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

    def test_must_fire_stat_and_date_fork_count_does_not_scale_with_file_count(self, tmp_path):
        autonomous = tmp_path / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True)
        # #933: 9 days, not 8 -- same margin bump, same reason as the
        # other fast-path fixtures in this file (8 days is now exactly
        # the fast path's own N+1 cutoff with the default N=7).
        nine_days_ago = time.time() - (9 * 24 * 3600)
        stale_files = []
        for i in range(8):
            stale = autonomous / f"session-end-00000{i}-99999.log"
            stale.write_text("12:00:00 [session-end] flush started\n")
            os.utime(stale, (nine_days_ago, nine_days_ago))
            stale_files.append(stale)
        # Positive control in the SAME run: one fresh file must survive,
        # exactly as test_must_fire_a_fresh_nonempty_log_survives_the_same_sweep
        # pins for the end-to-end path -- without this, a sweep that just
        # deleted the whole directory would also report a fork count of 0.
        fresh = autonomous / "session-end-000009-99999.log"
        fresh.write_text("12:00:00 [session-end] flush started\n")

        bin_dir = tmp_path / "fork-counter-bin"
        bin_dir.mkdir()
        counter = tmp_path / "fork-count.txt"
        counter.write_text("")
        real_stat = shutil.which("stat")
        real_date = shutil.which("date")
        assert real_stat and real_date, "test environment needs real stat/date on PATH"
        for name, real in (("stat", real_stat), ("date", real_date)):
            wrapper = bin_dir / name
            _write_shell_script(
                wrapper,
                "#!/bin/sh\n"
                f'echo {name} >> "$REMEMBER_FORK_COUNTER"\n'
                f'exec {shlex.quote(real)} "$@"\n',
            )
            wrapper.chmod(0o755)

        self._run_extracted_block_counting_forks(
            autonomous, str(tmp_path / ".remember"), bin_dir=bin_dir, counter=counter,
        )

        for stale in stale_files:
            assert not stale.exists(), (
                f"{stale.name} (9 days old) survived the sweep\n" + _dump_dir(autonomous)
            )
        assert fresh.exists(), (
            "the fresh (just-written) log was reclaimed by the sweep -- "
            "positive control failed\n" + _dump_dir(autonomous)
        )

        fork_count = len([l for l in counter.read_text().splitlines() if l])
        # Before the fix: 9 files x (1 stat + 1 date) = 18 forks, growing
        # linearly with file count. After the fix: one `date` read before
        # the loop, independent of file count. A ceiling well under the
        # file count (9) still clearly distinguishes O(1) from O(n)
        # without pinning an exact count that would break on the next
        # portable tweak to the fast path.
        assert fork_count < 9, (
            f"stat/date were invoked {fork_count} times reclaiming 8 of 9 "
            f"files -- the sweep is still forking per file instead of "
            f"reading the clock once and comparing with a builtin "
            f"(#914)\ncounter contents: {counter.read_text()!r}"
        )


class TestHousekeepingSweepFallbackAndEdgeCases:
    """#914 self-review (Explore round 1): two things the fast-path tests
    above cannot exercise because they only ever run on a machine where
    the fast path succeeds.
    """

    def _run_extracted_block_with_path_override(
        self, autonomous: Path, remember_dir: str, *,
        retention_days, extra_path_dir: Path | None = None,
    ):
        """Same shape as TestHousekeepingGlobIsPortableAcrossSeparators's
        own `_run_extracted_block`, but with the retention-days value
        passed through as given (no implicit str()/int() coercion that
        would hide a config value shaped like "08") and an optional
        directory prepended to PATH, ahead of the real `mktemp`/`touch` --
        used below to force the fast path's own reference-file build to
        fail without touching the block itself.
        """
        block = TestHousekeepingGlobIsPortableAcrossSeparators._extract_housekeeping_block()
        # #951: a test naming itself as exercising the fast path (or its
        # fallback) asserted only the SWEEP'S OUTCOME, never whether
        # `_remember_auto_ref` was actually built by `mktemp` on the
        # runner it executes on. Two dead ends on the way to this fix,
        # kept here so neither gets tried again:
        #   1. A marker read AFTER the block runs cannot tell the two
        #      paths apart -- the block's own last line (`unset
        #      _remember_auto_ref ...`, scripts/save-session.sh, carried
        #      verbatim into this extraction) clears the variable
        #      unconditionally on EVERY run, before any code placed
        #      after `{block}` can ever see it.
        #   2. A shim that writes its marker to ITS OWN stderr is also
        #      invisible: the block's own mktemp call is written as
        #      `$(mktemp ... 2>/dev/null)` -- that redirect is the
        #      PRODUCTION code's own suppression of mktemp's real error
        #      output, and it swallows a stderr-based marker exactly as
        #      thoroughly as a real error message (confirmed by a real
        #      reproduction: `type mktemp` resolved to this very shim,
        #      yet nothing it wrote to stderr ever appeared).
        # A marker file the shim appends to, named by an env var neither
        # the block nor its `2>/dev/null` ever touches, survives both.
        real_mktemp = shutil.which("mktemp")
        assert real_mktemp, "no mktemp on PATH -- fixture assumption broken"
        marker_bin = Path(tempfile.mkdtemp(prefix="remember-mktemp-marker-"))
        marker_log = marker_bin / "invocations.log"
        marker_shim = marker_bin / "mktemp"
        # CI (windows-latest, job 112746585138/...152, PR #960): both paths
        # embedded into the shim's own bash content below arrive from
        # Python as Windows-native, backslash-separated strings on that
        # platform. `>> "$MKTEMP_SHIM_LOG"` (an append REDIRECT, not a
        # glob) silently failed to open that target under git-bash there
        # -- the shim's very first statement -- so EVERY run on that leg
        # read back an empty log regardless of which path the extracted
        # block actually took, exactly as #448/#263's own backslash-vs-
        # forward-slash split already caught for REMEMBER_DIR in this
        # same file (that fix forward-slashes only when gated on $OSTYPE;
        # this one is unconditional, since a forward-slash absolute path
        # is accepted by git-bash on every platform this runs on, POSIX
        # included, where it is simply a no-op). `.exists()`/`.read_text()`
        # below still use the ORIGINAL, OS-native `marker_log` Path --
        # Windows' own file APIs accept '/' as a separator too, so Python
        # and the shim agree on the same file either way.
        _grc_posix = lambda p: str(p).replace("\\", "/")
        _write_shell_script(
            marker_shim,
            "#!/bin/sh\n"
            'echo "INVOKED" >> "$MKTEMP_SHIM_LOG"\n'
            f'"{_grc_posix(real_mktemp)}" "$@"\n'
            "rc=$?\n"
            'if [ "$rc" -eq 0 ]; then echo "SUCCEEDED" >> "$MKTEMP_SHIM_LOG"; fi\n'
            'exit "$rc"\n',
        )
        marker_shim.chmod(0o755)

        path_dirs = [marker_bin]
        if extra_path_dir is not None:
            # Ahead of the marker shim: a caller forcing the fallback via
            # its own broken mktemp must still win over this shim, the
            # same way it already wins over the real binary.
            path_dirs.insert(0, extra_path_dir)
        # #951: PATH is prepended inside the SCRIPT ITSELF via
        # `_bash_path_prepend`, not through `env["PATH"]` -- an
        # env-level prepend of a native, drive-lettered directory is not
        # trustworthy on Windows: see that helper's own docstring for why
        # (bash's native colon-splitting of the resulting PATH value
        # shreds it on the drive letter's own colon, independent of
        # slash direction, which the two PREVIOUS fix attempts on this
        # PR -- forward-slashing the shim content, then fixing the
        # shebang's newline -- could never have touched, since neither
        # changed how PATH itself was built).
        script = f"""
set -u
export PATH="{_bash_path_prepend(path_dirs)}:$PATH"
config() {{ printf '%s\n' {shlex.quote(str(retention_days))}; }}
log() {{ :; }}
_remember_date() {{ date "$@"; }}
CLEANUP_FILES=()
REMEMBER_DIR={shlex.quote(remember_dir)}
{block}
"""
        env = dict(os.environ)
        env["MKTEMP_SHIM_LOG"] = _grc_posix(marker_log)
        result = _run_bash_script(script, env)
        result.mktemp_shim_log = (
            marker_log.read_text(encoding="utf-8") if marker_log.exists() else ""
        )
        return result

    def test_must_fire_zero_padded_retention_days_does_not_misfire_the_fast_path(self, tmp_path):
        """Self-review finding: the digits-only sanitizer in
        save-session.sh accepts a config value like "08" unchanged (it
        rejects non-digit characters, not leading zeros), and bash
        arithmetic treats an unprefixed leading-zero literal as octal --
        where 8/9 are not valid octal digits. Without `10#` on
        `_AUTONOMOUS_LOG_RETENTION_DAYS` too (only the clock read had it),
        `_remember_auto_cutoff=$(( 10#$_remember_auto_now -
        _AUTONOMOUS_LOG_RETENTION_DAYS * 86400 ))` aborts with "value too
        great for base" for this exact, human-plausible config value --
        permanently defeating the fast path (and spamming stderr on every
        flush) for anyone who writes "08" rather than "8".
        """
        autonomous = tmp_path / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True)
        # Clearly past an 8-day window (10 days, not exactly 8) -- at the
        # boundary itself the sweep's own semantics (age STRICTLY greater
        # than the retention window, matching the pre-existing `-gt`
        # comparison this fast path replaces) keep the file, so a file
        # exactly as old as the window would pass vacuously here too.
        ten_days_ago = time.time() - (10 * 24 * 3600)
        stale = autonomous / "session-end-000000-11111.log"
        stale.write_text("12:00:00 [session-end] flush started\n")
        os.utime(stale, (ten_days_ago, ten_days_ago))
        one_day_ago = time.time() - (1 * 24 * 3600)
        fresh = autonomous / "session-end-000000-22222.log"
        fresh.write_text("12:00:00 [session-end] flush started\n")
        os.utime(fresh, (one_day_ago, one_day_ago))

        result = self._run_extracted_block_with_path_override(
            autonomous, str(tmp_path / ".remember"), retention_days="08",
        )

        assert result.returncode == 0, (
            f"the extracted housekeeping block itself failed to run with "
            f"retention_days='08'\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "value too great for base" not in result.stderr, (
            "retention_days='08' crashed the cutoff arithmetic with a "
            "bash 'value too great for base' error (octal "
            "misinterpretation of a leading zero) -- #914's own fast "
            f"path can never engage for this config value\nstderr={result.stderr!r}"
        )
        assert not stale.exists(), (
            "a 10-day-old log survived with retention_days='08' -- the "
            "sweep must still reclaim past-window files even when the "
            "fast path's own arithmetic degrades\n" + _dump_dir(autonomous)
        )
        assert fresh.exists(), (
            "a 1-day-old log was reclaimed with retention_days='08' -- "
            "positive control failed\n" + _dump_dir(autonomous)
        )
        assert "SUCCEEDED" in result.mktemp_shim_log, (
            "#951: this test names itself as exercising the fast path, "
            "but the real mktemp binary was never seen to succeed -- the "
            "fallback ran instead and this test exercised nothing new"
            f"\nmktemp_shim_log={result.mktemp_shim_log!r}"
        )

    def test_must_fire_fallback_loop_still_reclaims_when_reference_file_cannot_be_built(self, tmp_path):
        """Self-review finding: every test above only ever runs the FAST
        path (the real `mktemp`/`touch -d` succeed on every CI platform
        this suite runs on), so nothing exercises the reintroduced
        per-file stat()+date() fallback loop -- including the rename of
        its own loop variable (`_remember_auto_now` ->
        `_remember_auto_file_now`) made in the same diff that added the
        fast path. A `mktemp` that always fails forces `_remember_auto_ref`
        to stay empty, the documented trigger for the fallback.
        """
        autonomous = tmp_path / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True)
        # #933 CI (windows-latest, all four interpreters, PR #937): this
        # fixture used to sit at exactly 8 days (N+1, N=7) and relied
        # entirely on `broken_bin`'s `mktemp` override actually winning
        # over the real `mktemp` on PATH to force the fallback. On a real
        # windows-latest runner that assumption did not hold -- the real
        # `mktemp` apparently still resolved, so this test exercised the
        # FAST path after all, landing on the exact N+1-day boundary the
        # two tests below this one exist to pin, and losing the same
        # whole-second-vs-sub-second race documented there. Bumping to 9
        # days removes the dependency on which path actually runs: either
        # one reliably reclaims a file this far past the window, so the
        # test now proves what its own name claims regardless of whether
        # the mktemp override takes effect on a given platform.
        nine_days_ago = time.time() - (9 * 24 * 3600)
        stale = autonomous / "session-end-000000-33333.log"
        stale.write_text("12:00:00 [session-end] flush started\n")
        os.utime(stale, (nine_days_ago, nine_days_ago))
        one_day_ago = time.time() - (1 * 24 * 3600)
        fresh = autonomous / "session-end-000000-44444.log"
        fresh.write_text("12:00:00 [session-end] flush started\n")
        os.utime(fresh, (one_day_ago, one_day_ago))

        broken_bin = tmp_path / "broken-mktemp-bin"
        broken_bin.mkdir()
        fake_mktemp = broken_bin / "mktemp"
        _write_shell_script(fake_mktemp, "#!/bin/sh\nexit 1\n")
        fake_mktemp.chmod(0o755)

        result = self._run_extracted_block_with_path_override(
            autonomous, str(tmp_path / ".remember"), retention_days=7,
            extra_path_dir=broken_bin,
        )

        assert result.returncode == 0, (
            f"the extracted housekeeping block failed to run with mktemp "
            f"forced to fail\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert not stale.exists(), (
            "a 9-day-old log survived the sweep (intended to force the "
            "FALLBACK loop via a `mktemp` override, though on some "
            "platforms the fast path may run instead -- either one must "
            "reclaim a file this far past the window) -- the per-file "
            "stat()+date() comparison this diff kept as a fallback, or "
            "the fast path itself, is broken\n" + _dump_dir(autonomous)
        )
        assert fresh.exists(), (
            "a 1-day-old log was reclaimed by the fallback loop -- "
            "positive control failed\n" + _dump_dir(autonomous)
        )
        assert "SUCCEEDED" not in result.mktemp_shim_log, (
            "#951 (sibling check): this test's own `broken_bin` mktemp "
            "override did not actually win over the real mktemp on "
            "PATH -- the real binary was still reached and succeeded, so "
            "the FAST path ran instead of the fallback this test names "
            f"itself after\nmktemp_shim_log={result.mktemp_shim_log!r}"
        )

    def test_must_not_fire_fast_path_keeps_a_log_between_n_and_n_plus_one_days(self, tmp_path):
        """#933: the fast path's own cutoff (`now - N*86400`, then `-ot`)
        deletes anything strictly older than exactly N days -- a REAL-valued
        boundary. The fallback loop floors `(now - mtime) / 86400` before its
        `-gt N` check, so it only deletes once a file is a FULL N+1 days old.
        A log aged 7.5 days with N=7 sits in the gap between those two
        thresholds: the fast path (pre-fix) reclaims it, the fallback does
        not -- so whether a 7.5-day-old log survives a flush depends on
        which path happened to run, which is itself platform-dependent
        (mktemp/touch -d success). Paired with an 8.5-day-old log (clearly
        past BOTH thresholds) as the "must fire" positive control in the
        same fixture, per this repo's own testing rule: an assertion that
        checks only "the boundary file survived" would also pass if the
        sweep reclaimed nothing at all.
        """
        autonomous = tmp_path / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True)
        seven_and_a_half_days_ago = time.time() - (7 * 24 * 3600 + 12 * 3600)
        boundary = autonomous / "session-end-000000-55555.log"
        boundary.write_text("12:00:00 [session-end] flush started\n")
        os.utime(boundary, (seven_and_a_half_days_ago, seven_and_a_half_days_ago))
        eight_and_a_half_days_ago = time.time() - (8 * 24 * 3600 + 12 * 3600)
        past_both = autonomous / "session-end-000000-66666.log"
        past_both.write_text("12:00:00 [session-end] flush started\n")
        os.utime(past_both, (eight_and_a_half_days_ago, eight_and_a_half_days_ago))

        result = self._run_extracted_block_with_path_override(
            autonomous, str(tmp_path / ".remember"), retention_days=7,
        )

        assert result.returncode == 0, (
            f"the extracted housekeeping block itself failed to run\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert boundary.exists(), (
            "a log aged 7.5 days (N=7) was reclaimed by the FAST path -- "
            "the fallback loop's own floored-days comparison would have "
            "kept this same file (floor(7.5) == 7, not > 7), so which path "
            "ran determines whether this file survives a flush\n"
            + _dump_dir(autonomous)
        )
        assert not past_both.exists(), (
            "an 8.5-day-old log (past both the fast path's and the "
            "fallback's own thresholds) survived the fast path -- "
            "positive control failed\n" + _dump_dir(autonomous)
        )
        assert "SUCCEEDED" in result.mktemp_shim_log, (
            "#951: this test's own name and docstring claim it exercises "
            "the FAST path at the N/N+1 boundary, but the real mktemp "
            "binary was never seen to succeed -- the fallback silently "
            "ran instead, making this test a duplicate of its fallback "
            "sibling rather than a check on the fast path's own boundary "
            f"arithmetic\nmktemp_shim_log={result.mktemp_shim_log!r}"
        )

    def test_must_not_fire_fallback_keeps_a_log_between_n_and_n_plus_one_days(self, tmp_path):
        """#933: same fixture as the fast-path boundary test above, run
        through the FALLBACK loop instead (mktemp forced to fail), to
        confirm the two paths now agree at the N/N+1 boundary instead of
        disagreeing depending on which one happened to run.
        """
        autonomous = tmp_path / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True)
        seven_and_a_half_days_ago = time.time() - (7 * 24 * 3600 + 12 * 3600)
        boundary = autonomous / "session-end-000000-77777.log"
        boundary.write_text("12:00:00 [session-end] flush started\n")
        os.utime(boundary, (seven_and_a_half_days_ago, seven_and_a_half_days_ago))
        eight_and_a_half_days_ago = time.time() - (8 * 24 * 3600 + 12 * 3600)
        past_both = autonomous / "session-end-000000-88888.log"
        past_both.write_text("12:00:00 [session-end] flush started\n")
        os.utime(past_both, (eight_and_a_half_days_ago, eight_and_a_half_days_ago))

        broken_bin = tmp_path / "broken-mktemp-bin-933"
        broken_bin.mkdir()
        fake_mktemp = broken_bin / "mktemp"
        _write_shell_script(fake_mktemp, "#!/bin/sh\nexit 1\n")
        fake_mktemp.chmod(0o755)

        result = self._run_extracted_block_with_path_override(
            autonomous, str(tmp_path / ".remember"), retention_days=7,
            extra_path_dir=broken_bin,
        )

        assert result.returncode == 0, (
            f"the extracted housekeeping block failed to run with mktemp "
            f"forced to fail\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert boundary.exists(), (
            "a log aged 7.5 days (N=7) was reclaimed by the FALLBACK "
            "loop -- floor(7.5) == 7, not > 7, so this file must survive\n"
            + _dump_dir(autonomous)
        )
        assert not past_both.exists(), (
            "an 8.5-day-old log survived the fallback loop -- positive "
            "control failed\n" + _dump_dir(autonomous)
        )
        assert "SUCCEEDED" not in result.mktemp_shim_log, (
            "#951 (sibling check): this test's own `broken_bin` mktemp "
            "override did not actually win over the real mktemp on "
            "PATH -- the real binary was still reached and succeeded, so "
            "the FAST path ran instead of the fallback this test names "
            f"itself after\nmktemp_shim_log={result.mktemp_shim_log!r}"
        )
