"""post-tool-hook.sh must record for the session that invoked it (issue #212).

The hook used to derive its session id from the most recently modified
transcript in the project's session dir::

    LATEST_JSONL=$(ls -t "$SESSION_DIR"/*.jsonl | head -1)
    SESSION_ID=$(basename "$LATEST_JSONL" .jsonl)

With two sessions live in one project, whichever transcript was touched last
wins, so a tool call made by session A can be recorded against session B.

That was a transient inaccuracy while capture evidence lived in a single
last-write-wins slot. #206 made the store per-session
(``tmp/capture-alive.d/<session-id>``) and #211 made the capture-gap detector
membership-test it, so a marker written under the wrong id is now a durable
false claim: it vouches for a session that did no work, and if that session's
hooks really were dead the warning which exists to say so stays silent.

PostToolUse supplies ``session_id`` on stdin. These tests pin two things:

1. the marker, the line count and the forked save all follow the *invoking*
   session, not the newest file on disk; and
2. every way stdin can fail to answer -- absent, malformed, no ``session_id``,
   an id with no transcript here, an id that is not a safe path component,
   a stdin that never closes -- degrades to the old mtime behaviour and still
   captures. A hook that fails closed here means no capture at all, which is
   worse than the misattribution being fixed (cf. #204, #129).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"

from pipeline.slug import session_dir_slug as _slug  # noqa: E402
from .subprocess_helpers import subprocess_failure_detail  # noqa: E402

TOOL_LINE = '{"type":"assistant","message":{"content":"x"}}\n'

# The two ids used throughout. OLDER is the session the hook is invoked for;
# NEWER is a concurrent session whose transcript was touched more recently.
OLDER = "aaaaaaaa-0000-4000-8000-000000000001"
NEWER = "bbbbbbbb-0000-4000-8000-000000000002"


def _project(tmp_path: Path, *, older_lines: int, newer_lines: int):
    """A project with two live session transcripts, OLDER genuinely older."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    (remember / "config.json").write_text(
        json.dumps({"thresholds": {"delta_lines_trigger": 50}}), encoding="utf-8"
    )

    now = time.time()
    older = session_dir / f"{OLDER}.jsonl"
    older.write_text(TOOL_LINE * older_lines)
    os.utime(older, (now - 600, now - 600))
    newer = session_dir / f"{NEWER}.jsonl"
    newer.write_text(TOOL_LINE * newer_lines)
    os.utime(newer, (now, now))

    return home, project, remember, session_dir


def _env(home: Path, project: Path, remember: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _run(env: dict, stdin_payload):
    """Run the hook once.

    ``stdin_payload`` of None means no stdin at all (/dev/null) -- the older
    CLI, the test harness, the alternate invocation path.
    """
    kwargs = dict(env=env, capture_output=True, text=True, timeout=60)
    if stdin_payload is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_payload
    return subprocess.run(["bash", str(HOOK)], **kwargs)


def _payload(session_id: str) -> str:
    """A PostToolUse stdin payload, shaped like the real one."""
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/x"},
        "tool_response": {"ok": True},
    })


def _reap(remember: Path):
    """Wait out any forked save-session.sh so no stray process leaks."""
    pid_file = remember / "tmp" / "save-session.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


def _markers(remember: Path) -> set:
    store = remember / "tmp" / "capture-alive.d"
    return {p.name for p in store.iterdir()} if store.is_dir() else set()


# ---------------------------------------------------------------------------
# The central pin: two transcripts, the older one invoked the hook
# ---------------------------------------------------------------------------


def test_marker_lands_under_the_invoking_session_not_the_newest_transcript(tmp_path):
    """Session A makes a tool call while session B's transcript is newer.

    The evidence marker must vouch for A. A marker under B is a durable claim
    that B was captured, which silences the very warning #211 exists to give.
    """
    home, project, remember, _ = _project(tmp_path, older_lines=5, newer_lines=5)
    result = _run(_env(home, project, remember), _payload(OLDER))
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert _markers(remember) == {OLDER}, (
        "the hook vouched for the wrong session: it was invoked for "
        f"{OLDER} but the evidence store holds {_markers(remember)}"
    )
    assert (remember / "tmp" / "capture-alive").read_text() == OLDER, (
        "the legacy single slot still names the newest transcript's session"
    )


def test_the_line_count_measures_the_invoking_sessions_transcript(tmp_path):
    """The delta throttle is compared against a position keyed by session id,
    so it has to count the transcript belonging to that same session.

    Here the invoking session has barely moved (10 lines) while a concurrent
    session's transcript is long (200). Counting the newest file forks a save
    for a session that has nothing new to save, and forks it against the
    *other* session's position."""
    home, project, remember, _ = _project(tmp_path, older_lines=10, newer_lines=200)
    result = _run(_env(home, project, remember), _payload(OLDER))
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "forked a save on a 10-line session because another session's "
        "transcript had 200 lines"
    )


def test_a_save_still_fires_for_the_invoking_session(tmp_path):
    """The other side of the same measurement: the invoking session is well
    over the threshold while the newest transcript is not. Measuring the newest
    file would silently stop saving the session actually doing the work."""
    home, project, remember, _ = _project(tmp_path, older_lines=200, newer_lines=10)
    result = _run(_env(home, project, remember), _payload(OLDER))
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    pid_file = remember / "tmp" / "save-session.pid"
    assert pid_file.exists(), (
        "no save forked for a 200-line session, because a shorter transcript "
        "was newer"
    )


def test_the_saved_position_is_read_for_the_invoking_session(tmp_path):
    """last-save.json is keyed by session (#140). With a position recorded for
    the invoking session that leaves only a small delta, no save should fork --
    even though the *other* session has no position at all."""
    home, project, remember, _ = _project(tmp_path, older_lines=200, newer_lines=200)
    (remember / "tmp" / "last-save.json").write_text(
        json.dumps({"sessions": {OLDER: 190}}), encoding="utf-8"
    )
    result = _run(_env(home, project, remember), _payload(OLDER))
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "forked a save though the invoking session's own saved position "
        "leaves a delta of 10"
    )


# ---------------------------------------------------------------------------
# Degrade, never die: every way stdin can fail to answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stdin_payload,label", [
    (None, "no stdin at all"),
    ("", "empty stdin"),
    ("not json {{{", "malformed stdin"),
    (json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Read"}), "no session_id"),
    (json.dumps({"session_id": ""}), "empty session_id"),
    (json.dumps({"session_id": "cccccccc-0000-4000-8000-000000000003"}),
     "session_id with no transcript here"),
    (json.dumps({"session_id": "../../../etc/passwd"}), "session_id that is not a path component"),
    (json.dumps({"session_id": "sess/../escape"}), "session_id with a slash"),
])
def test_unusable_stdin_degrades_to_the_previous_behaviour(tmp_path, stdin_payload, label):
    """Capture must still happen, exactly as it did before this fix: the
    newest transcript, its id, its line count.

    This is the pin that keeps a correctness fix from becoming a silent total
    outage -- the failure this repo has already been bitten by twice."""
    home, project, remember, _ = _project(tmp_path, older_lines=5, newer_lines=200)
    result = _run(_env(home, project, remember), stdin_payload)
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert _markers(remember) == {NEWER}, (
        f"[{label}] capture did not fall back to the newest transcript: "
        f"markers are {_markers(remember)}"
    )
    assert (remember / "tmp" / "post-tool-ran").exists(), f"[{label}] hook did not run"
    assert (remember / "tmp" / "save-session.pid").exists(), (
        f"[{label}] the 200-line newest transcript did not fork a save -- "
        "capture stopped entirely"
    )


def test_an_unsafe_session_id_never_becomes_a_path(tmp_path):
    """stdin is not more trustworthy than a basename. A traversing id must
    neither be written as a marker nor used to build a transcript path."""
    home, project, remember, _ = _project(tmp_path, older_lines=5, newer_lines=5)
    escape = tmp_path / "escaped-marker"
    payload = json.dumps({"session_id": f"../../../../../..{escape}"})

    result = _run(_env(home, project, remember), payload)
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert not escape.exists(), "a stdin session id escaped the evidence store"
    assert _markers(remember) == {NEWER}, (
        "an unsafe id should fall back to the mtime behaviour, not disable capture"
    )


# ---------------------------------------------------------------------------
# ...and never hangs waiting for stdin that is not coming
# ---------------------------------------------------------------------------


def test_the_hook_does_not_block_on_an_open_stdin_that_is_never_written(tmp_path):
    """A pipe held open by the parent with nothing written must not stall the
    agent. Reading stdin is only safe if it cannot wait forever."""
    home, project, remember, _ = _project(tmp_path, older_lines=5, newer_lines=5)
    proc = subprocess.Popen(
        ["bash", str(HOOK)], env=_env(home, project, remember),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("the hook blocked on stdin — every tool call would stall")
    finally:
        if proc.stdin:
            proc.stdin.close()
    _reap(remember)
    assert proc.returncode == 0
    assert _markers(remember) == {NEWER}, "capture stopped when stdin said nothing"


def test_the_hook_does_not_block_on_a_terminal_stdin(tmp_path):
    """Invoked by hand from a shell, stdin is a tty nobody will type into."""
    # Imported here, not at module scope: `pty` pulls in `termios`, which does
    # not exist on Windows, and a module-level import runs at COLLECTION time —
    # before the module's own skipif(win32) can apply. That fails the whole
    # file on the Windows legs rather than skipping it.
    import pty

    home, project, remember, _ = _project(tmp_path, older_lines=5, newer_lines=5)
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["bash", str(HOOK)], env=_env(home, project, remember),
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("the hook blocked on a terminal stdin")
    finally:
        os.close(master)
        os.close(slave)
    _reap(remember)
    assert proc.returncode == 0
    assert _markers(remember) == {NEWER}, "capture stopped when stdin was a tty"
