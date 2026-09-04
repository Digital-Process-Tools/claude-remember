"""Regression tests for #524/#525/#526: three more of doctor.sh's and
run-consolidation.sh's own REMEMBER_DIR-derived globs (the same class
#517 fixed at 4 other sites in doctor.sh, plus the snapshot sweep in
run-consolidation.sh) left un-normalized -- found by the gate-3 release
audit ahead of v0.26.0.

#524: doctor.sh's `_SESSION_END_FIRED` glob (line ~469) silently stays 0
under a backslash-separated REMEMBER_DIR even when a real
session-end-*.log exists, so /remember:doctor falls through to its
transcript heuristic and misreports a hook-registration problem that does
not exist.

#525: doctor.sh's operator-facing memory-file count/byte total (line
~617) silently undercounts to 0 under the same condition -- the third
counter of this shape in the file; #517's own fragment named only the
other two ("both printed directly to the operator").

#526: run-consolidation.sh's `.tail-*`/`.prefix-*` stray-sibling sweep
(line ~242) globs a `staging_path` that inherits REMEMBER_DIR's
backslashes on msys, so the cleanup never fires and one small inert file
accumulates per failed consolidation split.

Same extracted-block technique as tests/test_windows_glob_backslash_517.py
(see tests/_glob_backslash_517.py's own module docstring for why the full
end-to-end hook cannot reproduce a backslash-laden REMEMBER_DIR on a POSIX
CI runner at all): real code pulled verbatim out of its owning script,
run standalone in a bash subprocess with the relevant variable handed a
SYNTHETIC backslash-laden string, $OSTYPE shadowed with a `local` inside a
wrapping function.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash
from _glob_backslash_517 import extract_function, extract_lines, run_block

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

_FORWARD_SLASH_FN = extract_function(
    "scripts/resolve-paths.sh", "_remember_forward_slash"
)


class TestDoctorSessionEndFired:
    """#524: doctor.sh's _SESSION_END_FIRED glob -- misreports SessionEnd
    as never having fired under a backslash-separated REMEMBER_DIR even
    when a real session-end-*.log exists."""

    _BLOCK = extract_lines(
        "scripts/doctor.sh",
        '_SESSION_END_LOG_DIR="$REMEMBER_DIR/logs/autonomous"',
        "done",
    )

    def _run(self, remember_dir, ostype):
        import shlex
        setup = _FORWARD_SLASH_FN + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
        return run_block(
            self._BLOCK, ostype=ostype, setup=setup,
            after='printf "%s" "$_SESSION_END_FIRED"',
        )

    def test_must_fire_session_end_log_seen_under_backslash_remember_dir(self, tmp_path):
        remember_dir = tmp_path / ".remember"
        logs = remember_dir / "logs" / "autonomous"
        logs.mkdir(parents=True)
        (logs / "session-end-2020-01-01T00-00-00.log").write_text("x\n")

        windows_style = str(remember_dir).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "1", (
            "a backslash-separated REMEMBER_DIR must not hide a real "
            f"session-end-*.log -- stdout={result.stdout!r} stderr={result.stderr}"
        )

    def test_must_not_fire_control_no_log_reports_unfired(self, tmp_path):
        """Positive control for the assertion's own honesty: with no log
        on disk at all, _SESSION_END_FIRED must genuinely still read 0 --
        not confused with the bug (which also reads 0, for the wrong
        reason) being reintroduced."""
        remember_dir = tmp_path / ".remember"
        logs = remember_dir / "logs" / "autonomous"
        logs.mkdir(parents=True)

        windows_style = str(remember_dir).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "0"

    def test_must_fire_forward_slash_remember_dir_seen_too(self, tmp_path):
        """Positive control: an ordinary POSIX REMEMBER_DIR still works."""
        remember_dir = tmp_path / ".remember"
        logs = remember_dir / "logs" / "autonomous"
        logs.mkdir(parents=True)
        (logs / "session-end-2020-01-01T00-00-00.log").write_text("x\n")

        result = self._run(str(remember_dir), "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "1"


class TestDoctorMemoryFileCount:
    """#525: doctor.sh's operator-facing memory-file count/byte total --
    the third counter of this shape in the file, undercounts to 0 under a
    backslash-separated REMEMBER_DIR.

    Extracted as two pieces stitched together (the assign+nested-loop body
    through its own inner `done`, then the outer `done` found via
    `after_marker` so the substring search does not stop at the inner
    `done` first -- both lines are literally "done", one nested inside the
    other's indentation) rather than one straight extract_lines call
    through the enclosing `if [ -d "$REMEMBER_DIR" ]; then ... fi`: that
    wrapper tests $REMEMBER_DIR itself with `-d`, a real syscall that
    resolves a backslash path fine on actual Windows but that this
    synthetic POSIX harness cannot fake (no leading "/", so bash treats
    the whole backslash string as one relative filename, never matching
    tmp_path). The wrapper is not part of #525's own fix -- the loop body
    below is -- so _MEMORY_FILE_COUNT/_MEMORY_BYTES are seeded via setup
    exactly as $_DOCTOR_TODAY is for the sibling _STAGING_BYTES test."""

    _BLOCK = (
        extract_lines(
            "scripts/doctor.sh",
            "_remember_memory_glob_dir=$(_remember_forward_slash",
            "        done",
        )
        + "\n"
        + extract_lines(
            "scripts/doctor.sh", "    done", "    done", after_marker="        done"
        )
    )

    def _run(self, remember_dir, ostype):
        import shlex
        setup = (
            _FORWARD_SLASH_FN
            + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
            + "\n_MEMORY_FILE_COUNT=0"
            + "\n_MEMORY_BYTES=0"
        )
        return run_block(
            self._BLOCK, ostype=ostype, setup=setup,
            after='printf "%s:%s" "$_MEMORY_FILE_COUNT" "$_MEMORY_BYTES"',
        )

    def test_must_fire_memory_files_counted_under_backslash_remember_dir(self, tmp_path):
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        (remember_dir / "today-2020-01-01.md").write_text("12345")  # 5 bytes
        (remember_dir / "now.md").write_text("123")  # 3 bytes
        (remember_dir / "recent.md").write_text("12")  # 2 bytes
        (remember_dir / "archive-2020.md").write_text("1")  # 1 byte

        windows_style = str(remember_dir).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "4:11", (
            "a backslash-separated REMEMBER_DIR must not undercount the "
            f"operator-facing memory-file total -- stdout={result.stdout!r} "
            f"stderr={result.stderr}"
        )

    def test_must_not_fire_control_empty_store_reports_zero(self, tmp_path):
        """Positive control: an empty (but existing) store genuinely has
        nothing to count -- must not be confused with the bug (also 0, for
        the wrong reason) being reintroduced."""
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()

        windows_style = str(remember_dir).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "0:0"

    def test_must_fire_forward_slash_remember_dir_counted_too(self, tmp_path):
        """Positive control: an ordinary POSIX REMEMBER_DIR still counts."""
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        (remember_dir / "now.md").write_text("12345")

        result = self._run(str(remember_dir), "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "1:5"


class TestRunConsolidationStagingTempCleanup:
    """#526: run-consolidation.sh's .tail-*/.prefix-* stray-sibling sweep
    -- same file, same _remember_forward_slash helper, one call site below
    the snapshot sweep #517 already normalized (_remember_consolidate_glob_dir)."""

    _BLOCK = extract_lines(
        "scripts/run-consolidation.sh",
        "_remember_staging_rm_glob=$(_remember_forward_slash",
        'rm -f "${_remember_staging_rm_glob}".tail-* "${_remember_staging_rm_glob}".prefix-* 2>/dev/null',
    )

    def _run(self, staging_path, ostype):
        import shlex
        setup = _FORWARD_SLASH_FN + f"\nstaging_path={shlex.quote(staging_path)}"
        return run_block(self._BLOCK, ostype=ostype, setup=setup)

    def test_must_fire_stray_siblings_removed_under_backslash_staging_path(self, tmp_path):
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        staging = remember_dir / "today-2020-01-01.md"
        staging.write_text("x\n")
        stray_tail = remember_dir / "today-2020-01-01.md.tail-abc123"
        stray_tail.write_text("stray\n")
        stray_prefix = remember_dir / "today-2020-01-01.md.prefix-def456"
        stray_prefix.write_text("stray\n")

        windows_style = str(staging).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert not stray_tail.exists(), (
            "a backslash-separated staging_path must not defeat the "
            f".tail-* sweep\nstderr={result.stderr}"
        )
        assert not stray_prefix.exists(), (
            "a backslash-separated staging_path must not defeat the "
            f".prefix-* sweep\nstderr={result.stderr}"
        )
        assert staging.exists(), "the staging file itself must survive the sweep"

    def test_must_not_fire_control_no_stray_siblings_nothing_removed(self, tmp_path):
        """Positive control: with no stray siblings on disk, the sweep
        genuinely has nothing to remove -- the staging file itself must
        survive either way."""
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        staging = remember_dir / "today-2020-01-01.md"
        staging.write_text("x\n")

        windows_style = str(staging).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert staging.exists()

    def test_must_fire_forward_slash_staging_path_swept_too(self, tmp_path):
        """Positive control: an ordinary POSIX staging_path still works."""
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        staging = remember_dir / "today-2020-01-01.md"
        staging.write_text("x\n")
        stray_tail = remember_dir / "today-2020-01-01.md.tail-abc123"
        stray_tail.write_text("stray\n")

        result = self._run(str(staging), "")

        assert result.returncode == 0, result.stderr
        assert not stray_tail.exists()
