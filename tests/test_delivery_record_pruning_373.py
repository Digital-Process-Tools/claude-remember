"""Regression tests for #373: with handoff_mode: "per_session",
remember.delivered.<session_id> files accumulate forever in
$REMEMBER_DIR/tmp -- nothing ever pruned them, the same directory (and
class of leak) #362 already fixed once, under a different filename.

The chosen coupling is deliberately NOT the paired remember.<session_id>.md
handoff slot -- that survives forever on purpose (#221), so tying the
record's life to it would reproduce unbounded growth under a new name. It is
coupled instead to the one fact that actually answers "is this session
over": whether Claude Code's own transcript for that session id still
exists under $SESSIONS_DIR (the same directory session-start-hook.sh already
reads via `previous_transcript`).

Four states, tested here as four cases over one fixture shape (a fourth,
#393, joined the original three below):
  - the session's transcript is gone AND its record is old enough to be
    outside the #393 startup grace window -> its delivery record is pruned
    (the "must fire" case)
  - the session's transcript is gone but its record is still inside that
    grace window -> its delivery record survives (#393's own "must not
    fire" case -- a session still starting up is indistinguishable from a
    dead one on the transcript check alone)
  - the session's transcript still exists -> its delivery record survives
    (the paired "must not fire" case -- the positive control that a broken
    or over-eager sweep would fail)
  - $SESSIONS_DIR itself cannot be listed at all -> nothing is pruned,
    because "could not tell whether the session is over" must never render
    as either "pruned" or "confirmed still active"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX session-start hook -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START_SCRIPT = REPO_ROOT / "scripts" / "session-start-hook.sh"

from pipeline.slug import session_dir_slug as _slug

# Mirrors the grace window scripts/session-start-hook.sh's #393 fix uses to
# arbitrate the sweep -- see the comment beside GRACE_MIN there for why this
# duration (reasoned, not measured against real Claude Code timing).
_GRACE_MIN = 5


def _age_record(record: Path, minutes: float) -> None:
    """Backdate a record's mtime so the #393 grace-window check reads it as
    old enough that "no transcript yet" can only mean "session is over" --
    never a session still inside its own startup window."""
    import time
    old = time.time() - (minutes * 60)
    os.utime(record, (old, old))


def _sandbox(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)

    cfg = {"features": {"recovery": False}, "handoff_mode": "per_session", "data_dir": ".remember"}
    (home / ".remember" / "config.json").write_text(json.dumps(cfg))

    slug = _slug(str(project))
    sessions_dir = home / ".claude" / "projects" / slug
    sessions_dir.mkdir(parents=True)

    remember_dir = project / ".remember"
    remember_dir.mkdir(parents=True, exist_ok=True)
    return project, home, remember_dir, sessions_dir


def _set_handoff_mode(home: Path, mode: str) -> None:
    cfg_path = home / ".remember" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["handoff_mode"] = mode
    cfg_path.write_text(json.dumps(cfg))


def _payload(session_id: str) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })


def _session_start(project: Path, home: Path, session_id: str | None) -> str:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
    }
    kwargs = {"env": env, "capture_output": True, "text": True, "timeout": 60}
    if session_id is not None:
        kwargs["input"] = _payload(session_id)
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    result = subprocess.run(
        ["bash", str(SESSION_START_SCRIPT)], check=False, **kwargs
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr[:500]}"
    return result.stdout


def _seed_delivery_record(remember_dir: Path, session_id: str) -> Path:
    """A per-session handoff plus one delivered-once run against it -- the
    ordinary way a remember.delivered.<id> record comes to exist."""
    (remember_dir / f"remember.{session_id}.md").write_text(
        f"Handoff for {session_id}.\n"
    )
    record = remember_dir / "tmp" / f"remember.delivered.{session_id}"
    return record


class TestStaleDeliveryRecordsArePruned:

    def test_record_for_a_session_with_no_transcript_is_pruned(self, tmp_path):
        """MUST FIRE: session AAA delivered once and left a record behind;
        its transcript is gone (never existed, or was cleaned up elsewhere),
        and the record is old enough that #393's grace window no longer
        applies. The next session start must remove AAA's stale record."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)

        out_a = _session_start(project, home, "sess-aaa")
        assert "Handoff for" not in out_a  # nothing written yet on first run

        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        # Deliver it once for real, so the record this test prunes is the
        # genuine artifact the hook itself writes, not a hand-built stand-in.
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"
        # Past the #393 grace window -- old enough that "no transcript" can
        # only mean the session is over, never that it is still starting up.
        _age_record(record_a, _GRACE_MIN + 1)

        # sess-aaa's transcript never existed under sessions_dir -- simulates
        # a session that is over and gone. A different, unrelated session
        # (BBB) starts next.
        _session_start(project, home, "sess-bbb")

        assert not record_a.exists(), (
            "a delivery record for a session with no transcript on disk, "
            "old enough to be outside the startup grace window, survived "
            "a later session start -- this is the #373 leak"
        )

    def test_fresh_record_with_no_transcript_yet_survives(self, tmp_path):
        """MUST NOT FIRE (#393 positive control): session AAA's record was
        JUST written and its transcript does not exist yet -- indistinguish-
        able, on the sweep's only signal, from a session that is over. A
        different session (BBB) starts inside that same window. AAA's
        record must survive: at source=startup Claude Code creates a
        session's own transcript only AFTER this hook has already run, so a
        record this young is not evidence the session is gone."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"
        # record_a's mtime is left exactly as the hook just wrote it --
        # inside the grace window, deliberately not backdated.

        # sess-aaa's transcript still does not exist -- the exact #393 shape:
        # absent transcript, freshly-written record. A different session
        # (BBB) starts inside the window.
        _session_start(project, home, "sess-bbb")

        assert record_a.exists(), (
            "a delivery record younger than the #393 startup grace window "
            "was pruned on the strength of an absent transcript alone -- "
            "a live session starting up is indistinguishable from a dead "
            "one on that signal, so this is the #393 race"
        )

    def test_record_for_a_session_whose_transcript_still_exists_survives(self, tmp_path):
        """MUST NOT FIRE (positive control): same shape, except sess-aaa's
        transcript is still present under $SESSIONS_DIR when session BBB
        starts. Its delivery record must survive -- an over-eager sweep
        that ignores the transcript check would fail this."""
        project, home, remember_dir, sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"

        # sess-aaa's transcript IS present -- the session could still resume.
        (sessions_dir / "sess-aaa.jsonl").write_text('{"type":"user"}\n')

        _session_start(project, home, "sess-bbb")

        assert record_a.exists(), (
            "a delivery record for a session whose transcript still exists "
            "was pruned -- the sweep must not delete state for a session "
            "that could still be active"
        )

    def test_could_not_tell_leaves_records_untouched(self, tmp_path):
        """Third state: $SESSIONS_DIR itself is gone (unreadable), so there
        is no way to tell whether sess-aaa is over. The record must survive
        -- could-not-tell must never render as "pruned"."""
        project, home, remember_dir, sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"

        # Remove the whole sessions directory -- "cannot tell", not "empty".
        import shutil
        shutil.rmtree(sessions_dir)

        _session_start(project, home, "sess-bbb")

        assert record_a.exists(), (
            "a delivery record was pruned even though $SESSIONS_DIR could "
            "not be read at all -- could-not-tell must not act like pruned"
        )

    def test_own_record_is_never_pruned_by_the_sweep(self, tmp_path):
        """The running session's own record (just written this same
        invocation) must never be swept as though it belonged to some other,
        absent session."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")

        assert record_a.exists(), (
            "a session's own just-written delivery record was removed by "
            "its own invocation's sweep"
        )

    def test_records_left_over_after_switching_back_to_single_mode_are_still_pruned(self, tmp_path):
        """The sweep must not be gated on THIS session's own handoff_mode.
        A record left behind during an earlier per_session period does not
        stop existing just because the user later switched handoff_mode
        back to "single" -- a sweep gated on the current mode would leak
        exactly those records forever, reproducing #373 under a config
        toggle instead of fixing it."""
        project, home, remember_dir, sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"
        # Past the #393 grace window -- an old per_session-era leftover, not
        # a session still inside its own startup.
        _age_record(record_a, _GRACE_MIN + 1)

        # sess-aaa's transcript is gone, and the user has switched back to
        # single mode before the next session starts.
        _set_handoff_mode(home, "single")
        (remember_dir / "remember.md").write_text("Shared note.\n")

        _session_start(project, home, "sess-bbb")

        assert not record_a.exists(), (
            "a stale per-session delivery record survived a session start "
            "under handoff_mode: \"single\" -- the sweep must not be gated "
            "on the CURRENT session's own mode, only on whether the "
            "session in question is confirmed over"
        )
        assert sessions_dir.is_dir()  # sanity: the fixture itself is sound
