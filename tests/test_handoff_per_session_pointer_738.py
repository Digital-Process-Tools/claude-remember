"""#738 — per_session mode's write-handoff pointer is a per-PROJECT file,
not a per-SESSION one, so the last session to start `SessionStart` owns it.

`handoff_mode: "per_session"` gives each session its own target,
`remember.<session_id>.md` — but until this fix, `session-start-hook.sh`
published that target to exactly ONE file, `tmp/handoff-path`, every time
ANY session started. `write-handoff.sh` then read that one shared file with
no idea which session was actually calling it. Sequence that breaks it:

  1. Session A starts (per_session mode) -> tmp/handoff-path says
     remember.A.md
  2. Session B starts -> tmp/handoff-path is OVERWRITTEN to say
     remember.B.md
  3. Session A runs /remember -> write-handoff.sh reads the (now stale)
     shared pointer and writes into remember.B.md, not remember.A.md

The defect class: a value that is per-SESSION (the resolved handoff path)
was carried over a channel that is per-PROJECT (one file, last writer
wins) — the pointer became the shared, clobberable resource, not the file
it names. The fix threads `CLAUDE_CODE_SESSION_ID` (a live env var Claude
Code sets for the Bash tool, not a value the model reads/asserts) through
to write-handoff.sh, which prefers a SESSION-KEYED hint file
(`tmp/handoff-path.<id>`) over the shared one whenever it exists.

Three properties, mirroring test_handoff_per_session_363.py's own shape:
  - two sessions started in sequence (A then B) do not make A's own write
    land in B's file merely because B started more recently
  - positive control: default/single mode is unaffected -- both sessions'
    writes still land in the one shared remember.md
  - positive control: the correct session's file DOES get written (not
    merely "not the wrong one" -- a broken pointer that refused everything
    would also make the negative assertion pass for the wrong reason)
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
    reason="bash subprocess + POSIX hook/script — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START_SCRIPT = REPO_ROOT / "scripts" / "session-start-hook.sh"
WRITE_HANDOFF_SCRIPT = REPO_ROOT / "scripts" / "write-handoff.sh"

from pipeline.slug import session_dir_slug as _slug


def _sandbox(tmp_path: Path, *, handoff_mode: str | None = None):
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)

    cfg: dict = {"features": {"recovery": False}}
    if handoff_mode is not None:
        cfg["handoff_mode"] = handoff_mode
    cfg["data_dir"] = ".remember"
    (home / ".remember" / "config.json").write_text(json.dumps(cfg))

    slug = _slug(str(project))
    sessions_dir = home / ".claude" / "projects" / slug
    sessions_dir.mkdir(parents=True)

    remember_dir = project / ".remember"
    remember_dir.mkdir(parents=True, exist_ok=True)
    return project, home, remember_dir, sessions_dir


def _payload(session_id: str) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })


def _session_start(project: Path, home: Path, session_id: str) -> str:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
    }
    result = subprocess.run(
        ["bash", str(SESSION_START_SCRIPT)],
        input=_payload(session_id),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr[:500]}"
    return result.stdout


def _write_handoff(project: Path, home: Path, note: str, *, session_id: str | None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
    }
    if session_id is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    else:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    return subprocess.run(
        ["bash", str(WRITE_HANDOFF_SCRIPT)],
        input=note,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestEarlierSessionIsNotClobberedByALaterOnesStart:

    def test_session_a_writes_into_its_own_file_after_session_b_starts(self, tmp_path):
        """The load-bearing case: A starts, B starts (B's SessionStart is the
        LAST one to touch the project-wide pointer), then A -- not B -- runs
        /remember. Under the bug A's write lands in remember.B.md, because
        write-handoff.sh has no idea which session is actually calling it."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path, handoff_mode="per_session")

        _session_start(project, home, "sess-aaa")
        _session_start(project, home, "sess-bbb")  # last writer of the shared pointer

        result = _write_handoff(project, home, "Session A's own note.\n", session_id="sess-aaa")

        assert result.returncode == 0, result.stderr
        target_a = remember_dir / "remember.sess-aaa.md"
        target_b = remember_dir / "remember.sess-bbb.md"
        assert target_a.exists() and "Session A's own note" in target_a.read_text(), (
            f"session A's write did not land in its own file.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}\n"
            f"remember.sess-aaa.md exists: {target_a.exists()}\n"
            f"remember.sess-bbb.md exists: {target_b.exists()}"
        )
        assert not target_b.exists() or "Session A's own note" not in target_b.read_text(), (
            "session A's note leaked into session B's file"
        )

    def test_session_b_also_writes_into_its_own_file(self, tmp_path):
        """Positive control for the case above: B, the session that DID start
        last, still gets its own correct file too -- this is not fixed by
        making every write fall through to a shared/default location."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path, handoff_mode="per_session")

        _session_start(project, home, "sess-aaa")
        _session_start(project, home, "sess-bbb")

        result = _write_handoff(project, home, "Session B's own note.\n", session_id="sess-bbb")

        assert result.returncode == 0, result.stderr
        target_b = remember_dir / "remember.sess-bbb.md"
        assert target_b.exists() and "Session B's own note" in target_b.read_text(), (
            f"session B's write did not land in its own file.\nstdout: {result.stdout}"
        )


class TestSingleModeStillSharesOneFile:

    def test_single_mode_both_sessions_still_write_the_shared_file(self, tmp_path):
        """Positive control: default (single) mode is unaffected by this fix
        -- both sessions still resolve to, and write, the one shared file."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)  # default: single

        _session_start(project, home, "sess-aaa")
        _session_start(project, home, "sess-bbb")

        result = _write_handoff(project, home, "Shared note from A.\n", session_id="sess-aaa")

        assert result.returncode == 0, result.stderr
        shared = remember_dir / "remember.md"
        assert shared.exists() and "Shared note from A" in shared.read_text()
        assert not (remember_dir / "remember.sess-aaa.md").exists()
        assert not (remember_dir / "remember.sess-bbb.md").exists()


_GRACE_MIN = 5


def _age(path: Path, minutes: float) -> None:
    """Backdate a hint file's mtime past session-start-hook.sh's #393 grace
    window, the same helper test_delivery_record_pruning_373.py uses for the
    sibling remember.delivered.<id> sweep this one mirrors."""
    import time
    old = time.time() - (minutes * 60)
    os.utime(path, (old, old))


class TestStaleSessionKeyedHintsArePruned:
    """#738 also asks: don't let tmp/ grow unbounded now that every session
    leaves a handoff-path.<id> file behind. session-start-hook.sh sweeps
    them the same way #373 already sweeps remember.delivered.<id> -- keyed
    to whether that session's own transcript still exists under
    $SESSIONS_DIR, with the same #393 grace window."""

    def test_hint_for_a_session_with_no_transcript_is_pruned(self, tmp_path):
        """MUST FIRE: sess-aaa's hint is old enough to be outside the grace
        window and its transcript never existed -- the next session's start
        must remove it."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path, handoff_mode="per_session")

        _session_start(project, home, "sess-aaa")
        hint_a = remember_dir / "tmp" / "handoff-path.sess-aaa"
        assert hint_a.exists(), "setup did not produce a session-keyed hint"
        _age(hint_a, _GRACE_MIN + 1)

        _session_start(project, home, "sess-bbb")

        assert not hint_a.exists(), (
            "a session-keyed handoff-path hint for a session with no "
            "transcript on disk, old enough to be outside the startup "
            "grace window, survived a later session start"
        )

    def test_hint_for_a_session_whose_transcript_still_exists_survives(self, tmp_path):
        """MUST NOT FIRE (positive control): sess-aaa's transcript is still
        present, so its hint must survive even past the grace window -- the
        session could still resume and call /remember."""
        project, home, remember_dir, sessions_dir = _sandbox(tmp_path, handoff_mode="per_session")

        _session_start(project, home, "sess-aaa")
        hint_a = remember_dir / "tmp" / "handoff-path.sess-aaa"
        assert hint_a.exists(), "setup did not produce a session-keyed hint"
        _age(hint_a, _GRACE_MIN + 1)
        (sessions_dir / "sess-aaa.jsonl").write_text('{"type":"user"}\n')

        _session_start(project, home, "sess-bbb")

        assert hint_a.exists(), (
            "a session-keyed hint for a session whose transcript still "
            "exists was pruned -- the sweep must not delete state for a "
            "session that could still be active"
        )
