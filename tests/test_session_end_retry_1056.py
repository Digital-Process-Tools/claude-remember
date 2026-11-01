"""SessionEnd's background flush gets exactly one bounded retry on failure
(#1056).

The issue's own scenario: an app restart reconnects 10-20 sessions in one
project at once, each one's SessionEnd hook backgrounding a
`save-session.sh --force` call, all competing for the same MSYS fork table
-- "fork: retry: Resource temporarily unavailable" is the reporter's own
error string. When THIS session's flush loses that race, there is no next
cooldown tick to catch the tail later (#345's whole point): the unsaved
content is gone. A short, bounded retry gives the transient pressure one
chance to clear before this hook gives up and reports the WARNING.

Round 2 self-review finding: retrying on ANY nonzero exit (the original
shape) is NOT safe regardless of cause -- a summarizer failure already
counts itself against thresholds.max_summary_failures, and a failure that
strikes AFTER now.md's own append but during the position write is not a
"harmless extra invocation", it is a genuine duplicate save, because the
retried run re-extracts and re-summarizes the same unadvanced span. The
hook's retry is now gated on save-session.sh's own
EXIT_RETRYABLE_BEFORE_WRITE (75) instead -- see session-end-hook.sh's own
comment at the retry site, and tests/test_save_session_exit_codes_1056.py
for save-session.sh's half of this contract.

TDD shape: a must-fire case on exit 75 (first attempt fails, second
succeeds -> no WARNING, exactly two invocations), paired with a must-not-
fire CONTROL (first attempt already succeeds -> exactly one invocation,
retry logic never engaged), a bound check (persistent 75 still gives up
after exactly one retry, not an unbounded loop), and two must-NOT-retry
cases for the exit codes the old "any nonzero" shape used to retry
incorrectly: a plain exit 1 (summarizer failure / lock timeout / any other
deliberate failure -- indistinguishable from here, which is the point: this
hook only ever sees the number) and exit 0 (a deliberate refusal --
cooldown, nothing-to-save -- already a no-op retry candidate before #1056
existed, confirmed here rather than assumed).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash
from test_save_session_gates import _make_env  # shared harness
from test_session_end_hook_345 import _run_hook, _wire_hook

# #1056: _run_hook() resolves a real Git-Bash binary rather than a hardcoded
# "bash" literal (see test_session_end_hook_345.py), so this module narrows
# to "no usable bash found" instead of a blanket win32 skip -- the #432
# route, which needs no docs/windows-skip-triage.md row because it is not a
# blanket skip at all.
pytestmark = pytest.mark.skipif(
    resolve_bash() is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

EXIT_RETRYABLE_BEFORE_WRITE = 75


def _stub_save_session_that_fails_then_succeeds(plugin: Path, counter_file: Path,
                                                 *, always_fail: bool = False):
    """Replace the fake plugin's save-session.sh with a stub that fails on
    its first invocation (simulating a transient fork failure caught by
    save-session.sh's own ERR trap -- EXIT_RETRYABLE_BEFORE_WRITE, 75) and
    succeeds on every later one -- or, with always_fail=True, never
    succeeds, to exercise the retry's own bound."""
    stub = plugin / "scripts" / "save-session.sh"
    lines = [
        "#!/bin/bash",
        "n=0",
        f'[ -f "{counter_file}" ] && n=$(cat "{counter_file}")',
        "n=$((n + 1))",
        f'echo "$n" > "{counter_file}"',
    ]
    if always_fail:
        lines += [
            'echo "stub: simulated persistent fork: retry: Resource temporarily unavailable" >&2',
            f"exit {EXIT_RETRYABLE_BEFORE_WRITE}",
        ]
    else:
        lines += [
            'if [ "$n" -eq 1 ]; then',
            '    echo "stub: simulated fork: retry: Resource temporarily unavailable" >&2',
            f"    exit {EXIT_RETRYABLE_BEFORE_WRITE}",
            "fi",
            "exit 0",
        ]
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(0o755)


def _stub_save_session_always_exits(plugin: Path, counter_file: Path, code: int):
    """A stub that always exits `code` -- for the must-NOT-retry controls,
    where any invocation count beyond 1 is already the failure."""
    stub = plugin / "scripts" / "save-session.sh"
    lines = [
        "#!/bin/bash",
        "n=0",
        f'[ -f "{counter_file}" ] && n=$(cat "{counter_file}")',
        "n=$((n + 1))",
        f'echo "$n" > "{counter_file}"',
        'echo "stub: simulated deliberate failure (not the pre-write class)" >&2',
        f"exit {code}",
    ]
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(0o755)


def _hook_errors_text(project: Path) -> str:
    """report_error() (scripts/log.sh) writes WARNING lines here, not into
    $_END_LOG -- $_END_LOG only ever sees whatever log()/report_error()
    write to the subshell's own captured stdout+stderr, and log.sh's real
    report_error additionally (and separately) appends to this file."""
    f = project / ".remember" / "logs" / "hook-errors.log"
    return f.read_text() if f.is_file() else ""


class TestRetryRecoversFromATransientFailure:

    def test_must_fire_first_attempt_fails_second_succeeds_no_warning(self, tmp_path):
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        _wire_hook(plugin)
        counter = tmp_path / "save-invocations"
        _stub_save_session_that_fails_then_succeeds(plugin, counter)

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0
        assert counter.exists(), "the stub save-session.sh was never invoked at all"
        assert counter.read_text().strip() == "2", (
            "the hook must retry the failed flush exactly once -- "
            f"invocation count was {counter.read_text().strip()!r}"
        )
        errors = _hook_errors_text(project)
        assert "WARNING" not in errors, (
            "the retry succeeded -- no WARNING belongs in hook-errors.log"
            f"\n{errors}"
        )

    def test_must_not_fire_control_a_clean_first_attempt_never_retries(self, tmp_path):
        """Positive control: when the flush succeeds immediately, the retry
        path must not engage at all -- proves the counter-file fixture
        itself is live, not a harness that always reports 2."""
        env, _project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        _wire_hook(plugin)
        counter = tmp_path / "save-invocations"
        _stub_save_session_that_fails_then_succeeds(plugin, counter, always_fail=False)
        # Pre-seed the counter past 1 so a clean run's own "n==1 fails" branch
        # is already behind it -- isolates "does a SUCCESSFUL run retry" from
        # the fail-then-succeed stub's own first-call behaviour.
        counter.write_text("1")

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0
        assert counter.read_text().strip() == "2", (
            "expected exactly one invocation on top of the pre-seeded count "
            f"(a clean success must not retry): {counter.read_text().strip()!r}"
        )

    def test_persistent_failure_still_gives_up_after_exactly_one_retry(self, tmp_path):
        """The bound: a save-session.sh that never recovers must not loop
        forever -- exactly two invocations (the original attempt plus one
        retry), then report the WARNING same as before #1056."""
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        _wire_hook(plugin)
        counter = tmp_path / "save-invocations"
        _stub_save_session_that_fails_then_succeeds(plugin, counter, always_fail=True)

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0
        assert counter.read_text().strip() == "2", (
            "a persistently failing flush must be retried exactly once, not "
            f"zero times and not in an unbounded loop: "
            f"{counter.read_text().strip()!r}"
        )
        errors = _hook_errors_text(project)
        assert "WARNING" in errors, (
            "a flush that never recovers must still be reported, exactly as "
            f"it was before the retry existed\n{errors}"
        )


class TestDeliberateFailuresAreNotRetried:
    """#1056 round 2: the old shape retried on ANY nonzero exit, which
    doubled a summarizer failure's own count against
    thresholds.max_summary_failures and could turn a position-write failure
    into a duplicate save. From this hook's own vantage point a summarizer
    failure, a lock timeout, and any other deliberate exit 1 are
    indistinguishable -- it only ever sees the number -- so one exit-1 case
    stands in for all of them; save-session.sh's own exit-code contract
    (tests/test_save_session_exit_codes_1056.py) is what tells them apart
    from the retryable 75."""

    def test_a_plain_exit_1_failure_is_reported_but_not_retried(self, tmp_path):
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        _wire_hook(plugin)
        counter = tmp_path / "save-invocations"
        _stub_save_session_always_exits(plugin, counter, 1)

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0
        assert counter.read_text().strip() == "1", (
            "a plain exit 1 (summarizer failure / lock timeout / any other "
            "deliberate failure) must NOT be retried -- exactly one "
            f"invocation: {counter.read_text().strip()!r}"
        )
        errors = _hook_errors_text(project)
        assert "WARNING" in errors, (
            f"the failure must still be reported even though it was not retried\n{errors}"
        )

    def test_a_deliberate_exit_0_refusal_is_not_retried(self, tmp_path):
        """Positive control for the exit-1 case above: exit 0 (cooldown,
        nothing-to-save) was already a no-op for the retry before #1056 --
        confirmed here, not merely assumed, so a harness that retried on
        0 too would be caught rather than passing silently."""
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        _wire_hook(plugin)
        counter = tmp_path / "save-invocations"
        _stub_save_session_always_exits(plugin, counter, 0)

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0
        assert counter.read_text().strip() == "1", (
            "a successful/declined flush (exit 0) must not be retried: "
            f"{counter.read_text().strip()!r}"
        )
        errors = _hook_errors_text(project)
        assert "WARNING" not in errors, f"exit 0 is success -- no WARNING belongs here\n{errors}"
