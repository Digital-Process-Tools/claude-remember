"""save-session.sh's own exit-code contract for #1056 (round 2 self-review).

session-end-hook.sh's bounded retry used to fire on ANY nonzero exit from
save-session.sh --force. Two concrete problems with that:

* a failure that strikes AFTER now.md's own append succeeds -- during the
  position write itself -- was retried into a SECOND append, because the
  retried run re-extracts and re-summarizes the same unadvanced span.
* a summarizer failure already counts itself against
  thresholds.max_summary_failures (see record_summary_failure); retrying it
  blindly doubled that count and could drop a span for good in one
  SessionEnd.

The fix: save-session.sh now reserves ONE exit code,
EXIT_RETRYABLE_BEFORE_WRITE (75), for an unexpected failure (its own ERR
trap, not one of its explicit `exit` lines) that struck before now.md was
touched -- the only thing a blind retry is safe to repeat. Every deliberate
outcome (lock timeout, invalid session id, summarizer failure, write error,
cooldown, nothing-to-save) keeps its ordinary 0/1.

Four cases below: the must-fire 75 case, two must-not-fire controls proving
75 is not just "any failure", and the half-success scenario itself -- the
PENDING_POSITION_MARKER recovery that catches a retry exit codes alone
cannot gate (a human re-running --force by hand, a later cooldown tick).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from subprocess_helpers import subprocess_failure_detail
from test_save_session_gates import STUB_SHELL, _make_env, _run, _saved_position

# This file's own fixtures are entirely a creature of test_save_session_gates's
# _make_env/_run (bash subprocess + POSIX layout), which already skips the
# whole platform (see that module's own pytestmark) rather than narrowing to
# "no bash found" the way #432 converted four other modules -- retrofitting
# that fixture chain for Windows is tracked separately and is not part of
# #1056's own scope. docs/windows-skip-triage.md carries this file's row.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="inherits test_save_session_gates's bash-subprocess + POSIX-layout fixtures (#79); not independently portable",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

EXIT_RETRYABLE_BEFORE_WRITE = 75

_ORIGINAL_SAVE_POSITION_BLOCK = (
    'elif cmd == "save-position":\n'
    '    last_save_file, session_id, position = sys.argv[2], sys.argv[3], sys.argv[4]\n'
    '    import json\n'
    '    with open(last_save_file, "w") as f:\n'
    '        json.dump({"session": session_id, "line": int(position)}, f)'
)
_FAILING_SAVE_POSITION_BLOCK = (
    'elif cmd == "save-position":\n'
    '    fail_once = os.environ.get("STUB_SAVE_POSITION_FAIL_ONCE")\n'
    '    if fail_once and not os.path.exists(fail_once):\n'
    '        open(fail_once, "w").close()\n'
    '        sys.stderr.write("stub: simulated save-position failure (fork/resource pressure)\\n")\n'
    '        sys.exit(1)\n'
    '    last_save_file, session_id, position = sys.argv[2], sys.argv[3], sys.argv[4]\n'
    '    import json\n'
    '    with open(last_save_file, "w") as f:\n'
    '        json.dump({"session": session_id, "line": int(position)}, f)'
)
FAILING_SAVE_POSITION_STUB = STUB_SHELL.replace(
    _ORIGINAL_SAVE_POSITION_BLOCK, _FAILING_SAVE_POSITION_BLOCK
)
assert FAILING_SAVE_POSITION_STUB != STUB_SHELL, (
    "the shared STUB_SHELL's save-position block moved -- update the text this "
    "file's replace() targets, in test_save_session_gates.py"
)


def _memory_text(project: Path) -> str:
    f = project / ".remember" / "now.md"
    return f.read_text() if f.is_file() else ""


def _pending_marker(project: Path) -> Path:
    return project / ".remember" / "tmp" / "pending-position"


class TestUnexpectedEarlyFailureGetsTheRetryableCode:

    def test_a_broken_dependency_before_any_write_exits_75(self, tmp_path):
        """An unexpected failure that strikes before Step 1 even starts --
        standing in for the fork/resource exhaustion #1056's retry exists to
        rescue -- must exit EXIT_RETRYABLE_BEFORE_WRITE, not whatever raw
        code the broken command happened to return.

        lib-staging-lock.sh is sourced before the save lock is even
        acquired; replacing it with a file whose last statement fails
        reproduces "a dependency failed to load" without a real fork-table
        exhaustion.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        (plugin / "scripts" / "lib-staging-lock.sh").write_text("false\n")

        result = _run(plugin, env, sid)

        assert result.returncode == EXIT_RETRYABLE_BEFORE_WRITE, (
            f"expected the retryable code {EXIT_RETRYABLE_BEFORE_WRITE}, got "
            f"{result.returncode}\n{subprocess_failure_detail(result, project / '.remember')}"
        )
        assert _memory_text(project) == "", "nothing should have been written to now.md"


class TestDeliberateFailuresNeverGetTheRetryableCode:
    """The positive controls: a failure save-session.sh already handles
    explicitly must keep its ordinary code, so session-end-hook.sh's retry
    -- gated on EXIT_RETRYABLE_BEFORE_WRITE -- never fires for it. Without
    this pair, a harness that always returns 75 would pass the test above
    for the wrong reason."""

    def test_summarizer_failure_is_plain_1_not_75(self, tmp_path):
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        env["STUB_HAIKU_FAIL"] = "1"

        result = _run(plugin, env, sid)

        assert result.returncode == 1, (
            f"expected the summarizer failure's own plain code 1, got "
            f"{result.returncode}\n{subprocess_failure_detail(result, project / '.remember')}"
        )
        assert result.returncode != EXIT_RETRYABLE_BEFORE_WRITE

    def test_lock_timeout_is_plain_1_not_75(self, tmp_path):
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["REMEMBER_FORCE_LOCK_TIMEOUT"] = "0"
        lock_dir = project / ".remember" / "tmp" / "save.lock"
        lock_dir.mkdir(parents=True)
        # A live pid (this test process itself): lock_acquire can neither
        # steal nor adopt it, so --force genuinely times out waiting.
        (lock_dir / "pid").write_text(str(os.getpid()))

        result = _run(plugin, env, sid, "--force")

        assert result.returncode == 1, (
            f"expected the lock-timeout's own plain code 1, got "
            f"{result.returncode}\n{subprocess_failure_detail(result, project / '.remember')}"
        )
        assert result.returncode != EXIT_RETRYABLE_BEFORE_WRITE


class TestHalfSuccessIsRecoveredNotDuplicated:
    """#1056 gap 1's core scenario: now.md's append succeeds, then the
    position write itself fails. The NEXT invocation must finish the
    position write using the pending marker -- NOT re-extract and
    re-summarize the same span into a second now.md entry."""

    def test_pending_marker_recovers_without_duplicating_the_append(self, tmp_path):
        env, project, plugin, _calls, sid = _make_env(
            tmp_path, exchanges=6, humans=5, position=900,
        )
        (plugin / "pipeline" / "shell.py").write_text(FAILING_SAVE_POSITION_STUB)
        env["STUB_HAIKU_TEXT"] = "## 10:00 | main\n\n- did some real work\n"
        fail_once_marker = tmp_path / "save-position-failed-once"
        env["STUB_SAVE_POSITION_FAIL_ONCE"] = str(fail_once_marker)

        first = _run(plugin, env, sid)

        assert first.returncode != 0 and first.returncode != EXIT_RETRYABLE_BEFORE_WRITE, (
            "the simulated save-position failure happens AFTER the append -- "
            f"_WROTE_ENTRY must keep this off the retryable code: {first.returncode}\n"
            f"{subprocess_failure_detail(first, project / '.remember')}"
        )
        memory_after_first = _memory_text(project)
        assert "did some real work" in memory_after_first, (
            "the append itself must have landed before the position write "
            f"failed\n{subprocess_failure_detail(first, project / '.remember')}"
        )
        assert memory_after_first.count("did some real work") == 1
        assert _saved_position(project) is None, (
            "the position write failed -- last-save.json must not exist yet"
        )
        assert _pending_marker(project).is_file(), (
            "a pending-position marker must be left behind for the next run"
        )

        # Second run: the stub's own fail-once marker is already consumed,
        # so save-position now succeeds -- but this run's OWN fresh
        # extraction must not add a SECOND entry. Haiku returns SKIP for
        # this call so that distinction is visible in now.md's content.
        second_env = dict(env)
        del second_env["STUB_HAIKU_TEXT"]
        second_env["STUB_HAIKU_SKIP"] = "1"

        second = _run(plugin, second_env, sid)

        assert second.returncode == 0, subprocess_failure_detail(second, project / ".remember")
        memory_after_second = _memory_text(project)
        assert memory_after_second.count("did some real work") == 1, (
            "recovering the pending position write must not re-append the "
            f"already-saved entry:\n{memory_after_second}"
        )
        assert _saved_position(project) == 900, (
            "recovery must finish the position write for the span that was "
            f"already saved: {_saved_position(project)!r}"
        )
        assert not _pending_marker(project).is_file(), (
            "the pending marker must be cleared once recovery succeeds"
        )
