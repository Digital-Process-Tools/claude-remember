"""COOLDOWN_MARKER and NDC_MARKER collapse absent-vs-unreadable, same as #619 (#625).

#619 fixed this exact shape at NDC_GEN_FILE: `cat FILE 2>/dev/null || echo 0`
cannot tell "never created" (legitimately 0) from "exists but a read of it
failed" (permission or I/O error, or a directory in its place) -- both
produce the identical fallback 0. save-session.sh's two OTHER timestamp
markers, tmp/last-save-ts (COOLDOWN_MARKER) and tmp/last-ndc.ts (NDC_MARKER),
read with the same bare `cat ... || echo 0` and have the identical collapse.

Lower stakes than the NDC_GEN_FILE case -- an unreadable marker here reads as
"very old", so the cooldown it guards is bypassed (an extra Haiku call), not a
silently lost entry -- but the same defect class: a durably unreadable marker
now silently defeats its cooldown on EVERY invocation, forever, with no trace
in the log. The fix must make that state visible (a WARNING naming the marker
and "could not be read"), the same way #619 made NDC_GEN_FILE's failure mode
visible via the new "SKIPPED commit" log line.

A directory in place of the marker reproduces "exists but every read fails"
without depending on this process's uid ever being denied a permission bit --
a bit CI's usual root uid ignores outright. Same technique #619's own test
suite used for NDC_GEN_FILE (tests/test_ndc_commit_lock.py).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

from .subprocess_helpers import subprocess_failure_detail
from .test_ndc_truncate_race import _ndc_env, _wait_for_background_ndc
from .test_save_session_gates import _make_env, _memory_log_text, _run

UNREADABLE_PHRASE = "could not be used"


def _hook_errors_text(project: Path) -> str:
    log = project / ".remember" / "logs" / "hook-errors.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""


def _extract_ts_marker_read_source() -> str:
    """Pull `ts_marker_read()`'s definition out of save-session.sh in Python,
    not via a `sed`-into-`source <(...)` pipeline.

    That pipeline (`source <(sed -n '/^ts_marker_read()/,/^}/p' FILE)`) works
    on every macOS bash this was authored and locally re-tested against
    (3.2.57 and Homebrew 5.x) but was OBSERVED failing -- consistently, on
    all four macos-latest legs, never on ubuntu-latest or windows-latest --
    on GitHub Actions CI with `bash: ts_marker_read: command not found`: the
    function silently ended up undefined despite `source` reporting no
    error, the same shape of macOS-CI-only process-substitution flakiness
    this session had already hit once this session (#621/#627) with a
    different fragile shell-substitution trick. Unreproducible locally under
    any bash version tried, so the actual OS/runner-image mechanism was not
    chased further -- the fix is to stop depending on process substitution
    at all, per the maintainer's explicit steer after the CI failure was
    reported. A plain multiline regex extraction (identical bytes to what
    the old `sed` range selected) removes both the subprocess and the
    `<(...)` construct from this path entirely.
    """
    script = REPO_ROOT / "scripts" / "save-session.sh"
    text = script.read_text(encoding="utf-8")
    match = re.search(r"^ts_marker_read\(\).*?^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "ts_marker_read() not found in scripts/save-session.sh -- extraction regex is stale"
    return match.group(0) + "\n"


def _call_ts_marker_read(marker_path: Path, timeout: float = 5) -> subprocess.CompletedProcess:
    """Call `ts_marker_read()` in isolation, via a REAL temp file.

    Sourcing the WHOLE script would run its main flow; sourcing just this
    one function's definition lets a test call it directly and cheaply,
    without also standing up a full save-session.sh environment. A real
    file on disk (not a process substitution) so `source` has an ordinary
    seekable path to read, whatever the underlying mechanism was that made
    macOS CI misbehave with `<(...)` here. Wrapped in a short timeout
    because the entire point of the test that uses this is proving the call
    does NOT block indefinitely -- a bare `_run()` through the full script
    would otherwise hang the test suite itself for up to the harness's own
    60s subprocess timeout.
    """
    func_src = _extract_ts_marker_read_source()
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8"
    ) as f:
        f.write(func_src)
        func_file = f.name
    try:
        cmd = ["bash", "-c", f'source "{func_file}"; ts_marker_read "{marker_path}"']
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    finally:
        os.unlink(func_file)


class TestCooldownMarkerUnreadable:

    def test_save_proceeds_and_warns_when_cooldown_marker_is_unreadable(self, tmp_path):
        """A directory in place of last-save-ts: every `cat` of it fails.

        Before the fix this silently proceeds with no trace at all -- the
        exact same silence #619 closed at NDC_GEN_FILE. After the fix it must
        still proceed (fail-open is the correct call here, see module
        docstring) but must say so.
        """
        env, project, plugin, calls, session_id = _make_env(
            tmp_path, exchanges=40, humans=5)
        cfg_path = Path(env["REMEMBER_CONFIG"])
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["save_seconds"] = 3600
        for target in (cfg_path, plugin / "config.json"):
            target.write_text(json.dumps(cfg))
        marker = project / ".remember" / "tmp" / "last-save-ts"
        marker.mkdir()  # exists, but `cat` on a directory always fails

        proc = _run(plugin, env, session_id)

        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")
        ran = calls.read_text() if calls.is_file() else ""
        assert "extract" in ran, (
            "an unreadable cooldown marker must fail OPEN (proceed with the "
            f"save), not stay silently throttled forever. calls: {ran!r}"
        )
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert str(marker) in seen and UNREADABLE_PHRASE in seen, (
            "a cooldown marker that exists but cannot be read must be logged "
            "as such -- it is not the same fact as a marker that was never "
            f"created, and the silence must not repeat #619's failure mode.\n{seen}"
        )
        # A directory in place of the marker breaks the LATER unconditional
        # `date +%s > "$COOLDOWN_MARKER"` write too, not just this read --
        # and that write is unguarded upstream of this fix, so under `set -e`
        # it crashes the whole script rather than merely leaving the
        # cooldown to re-trigger. `proc.returncode == 0` above is already the
        # regression check for that (reverting the write guard turns this
        # red), but pin the guard's own log line too, distinct from the
        # read-side WARNING asserted above, so a change that keeps the exit
        # code green while dropping the write-side message is still caught.
        assert "could not write" in seen and str(marker) in seen, (
            "the later unconditional marker write also fails for a directory "
            f"marker and must say so, separately from the read-side WARNING.\n{seen}"
        )

    def test_save_proceeds_quietly_when_cooldown_marker_is_absent(self, tmp_path):
        """Positive control: no marker has ever been written -- 0 is legitimate.

        Paired with the unreadable-marker test above -- both must let the save
        proceed, but only the unreadable one is a fact worth a WARNING.
        """
        env, project, plugin, calls, session_id = _make_env(
            tmp_path, exchanges=40, humans=5)
        cfg_path = Path(env["REMEMBER_CONFIG"])
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["save_seconds"] = 3600
        for target in (cfg_path, plugin / "config.json"):
            target.write_text(json.dumps(cfg))
        marker = project / ".remember" / "tmp" / "last-save-ts"
        assert not marker.exists(), "setup must start from a marker that was never created"

        proc = _run(plugin, env, session_id)

        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")
        ran = calls.read_text() if calls.is_file() else ""
        assert "extract" in ran, f"a never-created marker must not throttle the save. calls: {ran!r}"
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert UNREADABLE_PHRASE not in seen, (
            "an absent marker is not a read failure; it must not be reported as one"
        )


class TestNdcMarkerUnreadable:

    def test_ndc_proceeds_and_warns_when_marker_is_unreadable(self, tmp_path):
        """Same collapse, at tmp/last-ndc.ts (NDC_MARKER)."""
        env, project, plugin, _calls, sid = _ndc_env(tmp_path)
        env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- an entry\n"
        cfg_path = Path(env["REMEMBER_CONFIG"])
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["ndc_seconds"] = 3600
        for target in (cfg_path, plugin / "config.json"):
            target.write_text(json.dumps(cfg))
        memory_file = project / ".remember" / "now.md"
        marker = project / ".remember" / "tmp" / "last-ndc.ts"
        marker.mkdir()  # exists, but `cat` on a directory always fails

        proc = _run(plugin, env, sid)
        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")

        _wait_for_background_ndc(memory_file)
        today_text = "".join(
            f.read_text() for f in (project / ".remember").glob("today-*.md")
        )
        assert "compressed summary" in today_text, (
            "an unreadable NDC marker must fail OPEN (compress now), not stay "
            "silently throttled forever"
        )
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert str(marker) in seen and UNREADABLE_PHRASE in seen, (
            "an NDC marker that exists but cannot be read must be logged as "
            f"such, distinct from one that was never created.\n{seen}"
        )
        # Same write-guard pin as the cooldown marker test above: a directory
        # marker also breaks the later unconditional `date +%s > "$NDC_MARKER"`
        # write, which is unguarded upstream of this fix and crashes the
        # whole script under `set -e`. `proc.returncode == 0` above is the
        # regression check; this pins the guard's own distinct log line.
        assert "could not write" in seen and str(marker) in seen, (
            "the later unconditional marker write also fails for a directory "
            f"marker and must say so, separately from the read-side WARNING.\n{seen}"
        )

    def test_ndc_proceeds_quietly_when_marker_is_absent(self, tmp_path):
        """Positive control for the NDC marker."""
        env, project, plugin, _calls, sid = _ndc_env(tmp_path)
        env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- an entry\n"
        cfg_path = Path(env["REMEMBER_CONFIG"])
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["ndc_seconds"] = 3600
        for target in (cfg_path, plugin / "config.json"):
            target.write_text(json.dumps(cfg))
        memory_file = project / ".remember" / "now.md"
        marker = project / ".remember" / "tmp" / "last-ndc.ts"
        assert not marker.exists(), "setup must start from a marker that was never created"

        proc = _run(plugin, env, sid)
        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")

        _wait_for_background_ndc(memory_file)
        today_text = "".join(
            f.read_text() for f in (project / ".remember").glob("today-*.md")
        )
        assert "compressed summary" in today_text, "a never-created marker must not throttle NDC"
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert UNREADABLE_PHRASE not in seen, (
            "an absent marker is not a read failure; it must not be reported as one"
        )


class TestTsMarkerReadDoesNotHangOnNonRegularFiles:
    """Widening the existence test from `-f` to `-e || -L` (#625) means a
    marker that is a FIFO now reaches ts_marker_read's body -- and `cat` on a
    FIFO with no writer present blocks forever, with no timeout anywhere in
    this script. Found in self-review, not by the TDD cycle above: pinned
    here as its own class because it needs its OWN red/green cycle (a hang
    is not a value a plain assertion sees; it needs a timeout around the
    call) rather than fitting the read/warn shape the tests above pin.
    """

    def test_a_fifo_marker_returns_unreadable_without_blocking(self, tmp_path):
        marker = tmp_path / "fifo-marker"
        os.mkfifo(marker)
        # No writer is ever opened on this FIFO. `cat` on it would block
        # until one appears -- i.e. forever, in this test. The timeout below
        # is the assertion: a hang fails the test by raising TimeoutExpired,
        # exactly the shape a `pytest.raises` cannot express for "did not
        # hang", so it is asserted by NOT catching the exception.
        proc = _call_ts_marker_read(marker, timeout=5)
        assert proc.stdout.strip() == "unreadable", (
            f"a FIFO marker must read as unreadable (it can never legitimately "
            f"hold a timestamp), got: {proc.stdout!r} / {proc.stderr!r}"
        )

    def test_a_regular_file_marker_is_unaffected(self, tmp_path):
        """Positive control: the -f narrowing must not break the ordinary case."""
        marker = tmp_path / "plain-marker"
        marker.write_text("1700000000")
        proc = _call_ts_marker_read(marker, timeout=5)
        assert proc.stdout.strip() == "1700000000", (
            f"a regular file holding a valid timestamp must still read as "
            f"that timestamp, got: {proc.stdout!r} / {proc.stderr!r}"
        )

    def test_an_absent_marker_still_reads_as_zero(self, tmp_path):
        """Positive control: the absence path (never created) is untouched."""
        marker = tmp_path / "never-created"
        proc = _call_ts_marker_read(marker, timeout=5)
        assert proc.stdout.strip() == "0", (
            f"a marker that was never created must still read as 0, got: "
            f"{proc.stdout!r} / {proc.stderr!r}"
        )

    def test_a_dangling_symlink_reads_as_unreadable_not_zero(self, tmp_path):
        """A dangling symlink is something that WAS created (-L is true for
        it), not the same fact as a marker that was never created -- the
        same distinction ndc_read_gen's own comment already draws. Pinned
        after a re-audit round found this module's docstring-level comment
        (just above ts_marker_read) mis-describing this case as reading 0.
        """
        marker = tmp_path / "dangling-symlink-marker"
        marker.symlink_to(tmp_path / "nonexistent-target")
        proc = _call_ts_marker_read(marker, timeout=5)
        assert proc.stdout.strip() == "unreadable", (
            f"a dangling symlink was created here even though its target is "
            f"gone -- it must read as unreadable, not as the fresh-0 that a "
            f"marker never created at all would, got: {proc.stdout!r} / "
            f"{proc.stderr!r}"
        )
