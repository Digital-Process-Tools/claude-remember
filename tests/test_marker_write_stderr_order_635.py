"""Guarded marker writes' stderr suppression fires after the write, not
before (#635).

Two writes added by #625 are shaped like:

    date +%s > "$COOLDOWN_MARKER" 2>/dev/null \
        || report_error "cooldown" "WARNING: could not write ..."

at scripts/save-session.sh:405 (COOLDOWN_MARKER) and :999 (NDC_MARKER).
Redirections process left to right, so `>` is attempted -- and fails --
before `2>/dev/null` takes effect. The `2>/dev/null` is effectively a no-op:
it only redirects fd 2 for whatever happens AFTER the failed redirection
already reported to the real stderr.

Reproduced directly against a `chmod 500` directory in the issue; this test
uses a directory in place of the marker (the same technique
tests/test_save_session_marker_unreadable_625.py already uses, which does
not depend on this process's uid ever being denied a permission bit -- a
bit CI's usual root uid ignores outright): `date +%s > DIRECTORY` fails the
same way a permission-denied write does, and bash's own diagnostic for it
("... : Is a directory") is a raw shell line that must never reach this
script's own stderr, only the intended report_error() message should.
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

from .subprocess_helpers import subprocess_failure_detail
from .test_ndc_truncate_race import _ndc_env, _wait_for_background_ndc
from .test_save_session_gates import _make_env, _memory_log_text, _run

RAW_SHELL_DIAGNOSTIC = "Is a directory"


def _hook_errors_text(project: Path) -> str:
    log = project / ".remember" / "logs" / "hook-errors.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""


class TestCooldownMarkerWriteDoesNotDoubleEmit:

    def test_directory_marker_write_failure_reports_once_not_twice(self, tmp_path):
        env, project, plugin, _calls, session_id = _make_env(
            tmp_path, exchanges=40, humans=5)
        marker = project / ".remember" / "tmp" / "last-save-ts"
        marker.mkdir()  # `date +%s > $marker` always fails against a directory

        proc = _run(plugin, env, session_id)

        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")
        # Negative: bash's own raw diagnostic for the failed `>` redirection
        # must never leak anywhere -- not to this script's own stderr, and
        # not duplicated into hook-errors.log beside the intended
        # report_error() message. The real deployment's bootstrap-dirs.sh
        # already `exec 2>> hook-errors.log`s before sourcing this script
        # (that is reproduced here too, by the harness's own bootstrap), so
        # the raw line is observed landing in hook-errors.log rather than
        # in proc.stderr -- checking proc.stderr alone would miss it.
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert RAW_SHELL_DIAGNOSTIC not in seen and RAW_SHELL_DIAGNOSTIC not in proc.stderr, (
            f"the raw shell diagnostic for the failed redirection leaked -- "
            f"2>/dev/null was written AFTER the failing '>' redirection "
            f"rather than wrapping it, so it suppressed nothing. "
            f"stderr={proc.stderr!r}\nseen={seen!r}"
        )
        # Positive control: the write failure must still be reported via
        # report_error(), in both places it writes (#326) -- an assertion
        # that the raw line does NOT appear must be paired with one that
        # failure IS still reported, or a test that silenced report_error()
        # entirely would also pass this test.
        assert "could not write" in seen and str(marker) in seen, (
            f"report_error() must still fire for the write failure even "
            f"though the raw shell line must not leak.\n{seen}"
        )


class TestNdcMarkerWriteDoesNotDoubleEmit:

    def test_directory_marker_write_failure_reports_once_not_twice(self, tmp_path):
        env, project, plugin, _calls, sid = _ndc_env(tmp_path)
        env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- an entry\n"
        memory_file = project / ".remember" / "now.md"
        marker = project / ".remember" / "tmp" / "last-ndc.ts"
        marker.mkdir()  # `date +%s > $marker` always fails against a directory

        proc = _run(plugin, env, sid)

        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")
        _wait_for_background_ndc(memory_file)
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert RAW_SHELL_DIAGNOSTIC not in seen and RAW_SHELL_DIAGNOSTIC not in proc.stderr, (
            f"the raw shell diagnostic for the failed redirection leaked for "
            f"NDC_MARKER too. stderr={proc.stderr!r}\nseen={seen!r}"
        )
        assert "could not write" in seen and str(marker) in seen, (
            f"report_error() must still fire for the NDC marker write "
            f"failure even though the raw shell line must not leak.\n{seen}"
        )
