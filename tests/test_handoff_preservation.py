"""The handoff slot must survive a session that never writes one back (#221).

`SessionStart` used to emit `remember.md` and then truncate it unconditionally.
Any session that merely passed through the project — a scheduled/headless task
with a read-only prompt, a `claude -p` one-shot, an aborted session — consumed
the handoff meant for the next human session and left a 0-byte file behind,
with nothing anywhere saying it had happened.

The fix keeps the handoff on disk until a *replacement* is written. That is
only half of it: a handoff that is re-emitted forever, with no way to tell a
fresh one from one you already acted on, is the same silent lie wearing new
clothes. So delivery is *recorded* rather than destructive, and any re-delivery
of content that was already delivered is labelled as such.

Three properties, one per failure mode:
  - a pass-through session does not destroy the pending handoff
  - a re-delivered handoff says so, and says how long it has been pending
  - an ordinary session still gets its handoff, and a fresh one replaces the
    old cleanly (characterization — held before the fix and after it)
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX session-start hook — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START_SCRIPT = REPO_ROOT / "scripts" / "session-start-hook.sh"

from pipeline.slug import session_dir_slug as _slug  # noqa: E402


def _sandbox(tmp_path: Path):
    """A legacy-mode store: {project}/.remember/, recovery off, no session log."""
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)
    (home / ".remember" / "config.json").write_text(
        json.dumps({"data_dir": ".remember", "features": {"recovery": False}})
    )
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    handoff = project / ".remember" / "remember.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    return project, home, handoff


def _session_start(project: Path, home: Path) -> str:
    """Fire the SessionStart hook once. Returns its stdout (context injection)."""
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
    }
    result = subprocess.run(
        ["bash", str(SESSION_START_SCRIPT)], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr[:500]}"
    return result.stdout


class TestHandoffSurvivesPassThroughSession:

    def test_pass_through_session_does_not_destroy_pending_handoff(self, tmp_path):
        """A session that never writes a handoff back must not consume the one
        waiting for the next human session (#221).

        Session 1 is the scheduled task: it starts, the hook fires, its prompt
        is read-only and never calls /remember. Session 2 is the human. Session
        2 must still see what session 1 was handed.
        """
        project, home, handoff = _sandbox(tmp_path)
        handoff.write_text("Next: finish the migration guard, see PR #218.\n")

        _session_start(project, home)  # the scheduled task passes through

        assert handoff.exists() and handoff.read_text().strip(), (
            "the pass-through session truncated the pending handoff — "
            f"file is now {handoff.read_text()!r}"
        )

        human = _session_start(project, home)
        assert "finish the migration guard" in human, (
            "the human session did not receive the handoff the scheduled task "
            f"consumed.\noutput: {human[:800]}"
        )


class TestRedeliveryIsLabelled:

    def test_second_delivery_is_marked_already_delivered(self, tmp_path):
        """Preserving the handoff must not mean lying about its freshness.

        The first delivery reads as new. Any later delivery of the *same*
        content says it has already been delivered and is awaiting a
        replacement, so a reader can tell it apart from a fresh handoff.
        """
        project, home, handoff = _sandbox(tmp_path)
        handoff.write_text("Next: finish the migration guard.\n")

        first = _session_start(project, home)
        assert "=== LAST HANDOFF ===" in first
        assert "already delivered" not in first.lower(), (
            f"first delivery must read as fresh.\noutput: {first[:800]}"
        )

        second = _session_start(project, home)
        assert "finish the migration guard" in second, "content dropped on re-delivery"
        assert "already delivered" in second.lower(), (
            "a re-delivered handoff must say so — otherwise a stale note is "
            f"indistinguishable from a fresh one.\noutput: {second[:800]}"
        )
        assert "pending replacement" in second.lower(), (
            f"re-delivery should name what it is waiting for.\noutput: {second[:800]}"
        )

    def test_redelivery_notice_counts_deliveries(self, tmp_path):
        """The staleness notice grows a delivery count, so 'pending since
        forever' is visible rather than inferred."""
        project, home, handoff = _sandbox(tmp_path)
        handoff.write_text("Next: finish the migration guard.\n")

        _session_start(project, home)
        _session_start(project, home)
        third = _session_start(project, home)

        assert "3 times" in third, (
            f"third delivery should report its delivery count.\noutput: {third[:800]}"
        )


class TestOrdinaryHandoffLifecycle:
    """Characterization — these held before #221's fix and must still hold."""

    def test_fresh_handoff_replaces_the_old_one_and_is_delivered_once(self, tmp_path):
        """The normal path: session writes a handoff, next session receives it,
        that session writes its own, the one after receives the *new* one — and
        no session is handed old content dressed as new."""
        project, home, handoff = _sandbox(tmp_path)
        handoff.write_text("HANDOFF-ALPHA: land the parser fix.\n")

        first = _session_start(project, home)
        assert "HANDOFF-ALPHA" in first, "handoff not injected at session start"
        assert "already delivered" not in first.lower()

        # This session ends with /remember, which overwrites the same slot.
        handoff.write_text("HANDOFF-BETA: review the parser fix.\n")

        second = _session_start(project, home)
        assert "HANDOFF-BETA" in second, "fresh handoff not delivered"
        assert "already delivered" not in second.lower(), (
            "a replaced handoff is a fresh one — it must not be labelled stale."
        )
        assert "HANDOFF-ALPHA" not in second, (
            "the superseded handoff must not be re-emitted alongside its replacement"
        )

    def test_empty_handoff_slot_emits_nothing(self, tmp_path):
        """No handoff, no block — an empty slot must not produce a header, and
        must not resurrect a previously delivered note."""
        project, home, handoff = _sandbox(tmp_path)
        handoff.write_text("HANDOFF-ALPHA: land the parser fix.\n")
        _session_start(project, home)

        handoff.write_text("")  # user cleared it by hand
        out = _session_start(project, home)
        assert "=== LAST HANDOFF ===" not in out, f"emitted an empty handoff.\n{out[:800]}"
        assert "HANDOFF-ALPHA" not in out, "cleared handoff came back from the dead"
