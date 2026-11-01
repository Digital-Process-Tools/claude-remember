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

Not conditioned on recognizing MSYS's own wording: that text is not a
stable contract across bash/MSYS versions, and retrying any nonzero exit
once is safe here regardless of cause -- save-session.sh's own cooldown/
position bookkeeping only advances on an actual successful save, so a
retry after a non-fork failure costs one harmless extra invocation, not a
duplicate save.

TDD shape: a must-fire case (first attempt fails, second succeeds -> no
WARNING, exactly two invocations) paired with a must-not-fire control
(first attempt already succeeds -> exactly one invocation, retry logic
never engaged) and a bound check (persistent failure still gives up after
exactly one retry, not an unbounded loop).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_save_session_gates import _make_env  # shared harness
from test_session_end_hook_345 import _run_hook, _wire_hook

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _stub_save_session_that_fails_then_succeeds(plugin: Path, counter_file: Path,
                                                 *, always_fail: bool = False):
    """Replace the fake plugin's save-session.sh with a stub that fails on
    its first invocation (simulating a transient fork failure) and succeeds
    on every later one -- or, with always_fail=True, never succeeds, to
    exercise the retry's own bound."""
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
            "exit 1",
        ]
    else:
        lines += [
            'if [ "$n" -eq 1 ]; then',
            '    echo "stub: simulated fork: retry: Resource temporarily unavailable" >&2',
            "    exit 1",
            "fi",
            "exit 0",
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
