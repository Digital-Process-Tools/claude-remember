"""subprocess_failure_detail: the diagnostic these harnesses were missing (#210).

`scripts/bootstrap-dirs.sh:112` redirects a driven script's stderr into
`logs/hook-errors.log` once REMEMBER_DIR exists, so `result.stderr` for every
subprocess these tests drive is *always* empty by construction. The common
`assert result.returncode == 0, result.stderr` idiom therefore prints nothing
on failure -- exactly when the message is needed. This cost about an hour on
#208: a real error line (`exec: command: not found`) sat in hook-errors.log
the whole time while the blank stderr sent the investigation elsewhere.

These tests pin the post-condition that matters: the assertion message a
developer sees must contain whatever text landed in hook-errors.log, and must
say so explicitly when there is no such file to read (the `--dry` path never
reaches the redirect in bootstrap-dirs.sh at all, per the #210 follow-up).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .subprocess_helpers import subprocess_failure_detail


def _result(returncode: int = 1, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["stub"], returncode=returncode,
                                        stdout=stdout, stderr="")


def test_includes_hook_errors_log_contents_when_present(tmp_path):
    """The exact failure this issue is about: the real error is in the log,
    not in stderr, so the helper must surface it."""
    remember = tmp_path / ".remember"
    (remember / "logs").mkdir(parents=True)
    (remember / "logs" / "hook-errors.log").write_text(
        "some noise\nexec: command: not found\n"
    )

    detail = subprocess_failure_detail(_result(returncode=127), remember)

    assert "exec: command: not found" in detail, (
        "the real diagnostic sitting in hook-errors.log must appear in the "
        "assertion message -- a helper that omits it pins nothing over the "
        "status quo of an empty stderr"
    )


def test_says_no_log_file_explicitly_when_absent(tmp_path):
    """The --dry path: hook-errors.log never gets created because the
    redirect in bootstrap-dirs.sh is never reached. The absence is itself
    informative and must read as such, not as an empty string."""
    remember = tmp_path / ".remember"
    remember.mkdir()

    detail = subprocess_failure_detail(_result(), remember)

    assert "hook-errors.log" in detail and "no" in detail.lower(), (
        "a missing log must be reported as missing -- an empty section here "
        "reproduces the exact blank-message bug this helper exists to fix"
    )
    assert str(remember / "logs" / "hook-errors.log") in detail, (
        "the message should name the path that was checked, so the developer "
        "does not have to go rediscover REMEMBER_DIR themselves"
    )


def test_truncates_from_the_end_not_the_start(tmp_path):
    """The log accumulates across the whole test run; only the tail is
    relevant to the failure just observed."""
    remember = tmp_path / ".remember"
    (remember / "logs").mkdir(parents=True)
    log = remember / "logs" / "hook-errors.log"
    log.write_text(("old irrelevant line\n" * 2000) + "THE ACTUAL FAILURE\n")

    detail = subprocess_failure_detail(_result(), remember)

    assert "THE ACTUAL FAILURE" in detail
    assert detail.count("old irrelevant line") < 2000, (
        "truncation must drop from the front of the log, keeping the recent "
        "tail -- keeping the oldest bytes instead would bury the failure "
        "that just happened under noise from earlier in the run"
    )


def test_includes_returncode_and_stdout():
    detail = subprocess_failure_detail(_result(returncode=42, stdout="hello"), Path("/nonexistent"))

    assert "42" in detail
    assert "hello" in detail


def test_never_raises_on_unreadable_or_missing_directory():
    """A helper that throws while building an assertion message turns one
    clear failure into a confusing one -- this must degrade to a string no
    matter what is wrong with the filesystem underneath it."""
    detail = subprocess_failure_detail(_result(), Path("/nonexistent/deeply/nested/path"))

    assert isinstance(detail, str)
    assert detail != ""


def test_never_raises_when_remember_dir_is_a_file_not_a_directory(tmp_path):
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("surprise")

    detail = subprocess_failure_detail(_result(), not_a_dir)

    assert isinstance(detail, str)
    assert detail != ""
