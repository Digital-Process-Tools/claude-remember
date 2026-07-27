"""Tests for the capture-gap notice (issue #200).

A plugin enabled mid-session has none of its hooks registered for that session,
so PostToolUse never fires and capture silently does nothing. The reporter lost
a day to it: `logs/` held one session-start line, `hook-errors.log` was empty,
and no memory was ever written.

It cannot be detected while it is happening — nothing inside a hook can see
which hooks are registered, and SessionStart's `source` does not distinguish a
plugin-enable from a fresh start. What CAN be detected, afterwards, is the
signature: a session where SessionStart ran and PostToolUse never did.

These pin both halves of that, plus the delivery path — `systemMessage`, which
is the only hook output the human sees. A notice only the model sees is how
this stayed invisible in the first place.
"""

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
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
POST_TOOL = REPO_ROOT / "scripts" / "post-tool-hook.sh"
USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"

from pipeline.slug import session_dir_slug as _slug  # noqa: E402

TOOL_USE_LINE = '{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}\n'
PLAIN_LINE = '{"type":"assistant","message":{"content":"just talking"}}\n'


def _env(home: Path, project: Path, remember: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    # Recovery forks save-session.sh in the background; these tests are about
    # the notice, not recovery, and a stray fork would outlive them.
    (remember / "config.json").write_text(
        json.dumps({"features": {"recovery": False}}), encoding="utf-8"
    )
    return home, project, remember, session_dir


def _transcripts(session_dir: Path, *, previous: str, current: str = PLAIN_LINE):
    """Two transcripts. `ls -t` orders by mtime, so the current one is newest.

    The hooks take the second-newest as the previous session — same convention
    the recovery block already uses.
    """
    prev = session_dir / "sess-prev.jsonl"
    prev.write_text(previous)
    time.sleep(0.05)
    cur = session_dir / "sess-cur.jsonl"
    cur.write_text(current)
    return prev, cur


def _run(script: Path, env: dict):
    return subprocess.run(["bash", str(script)], env=env,
                          capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
# PostToolUse leaves a sign of life
# ---------------------------------------------------------------------------

def test_post_tool_records_that_it_ran_at_all(tmp_path):
    """Unconditional, before any throttle: the question is "did it run", not
    "did it save". A marker written only on save would call a correctly-wired
    but idle session broken."""
    home, project, remember, session_dir = _project(tmp_path)
    (session_dir / "sess-1.jsonl").write_text(PLAIN_LINE * 5)

    _run(POST_TOOL, _env(home, project, remember))

    alive = remember / "tmp" / "capture-alive"
    assert alive.exists(), (
        "PostToolUse ran but left no sign of life — the gap check has nothing "
        "to distinguish a wired session from an unwired one"
    )
    assert alive.read_text().strip() == "sess-1", (
        "the marker must name the session it saw; a bare touch cannot tell "
        "'ran for THIS session' from 'ran once, months ago'"
    )


# ---------------------------------------------------------------------------
# SessionStart detects the gap
# ---------------------------------------------------------------------------

def test_a_previous_session_that_never_captured_raises_the_notice(tmp_path):
    """The reported failure: SessionStart ran last time, PostToolUse never did."""
    home, project, remember, session_dir = _project(tmp_path)
    _transcripts(session_dir, previous=TOOL_USE_LINE * 5)
    # A previous SessionStart, and no sign of life after it.
    (remember / "tmp" / "capture-session-start").write_text(str(int(time.time())))

    result = _run(SESSION_START, _env(home, project, remember))
    assert result.returncode == 0, result.stderr

    notice = remember / "tmp" / "capture-gap-notice"
    assert notice.exists(), (
        "a session that ran SessionStart and never PostToolUse produced no "
        "notice — this is exactly the silent no-op #200 reports"
    )
    assert "doctor" in notice.read_text(), "notice does not say how to diagnose"


def test_a_healthy_previous_session_raises_nothing(tmp_path):
    """PostToolUse recorded the previous session's own id: capture was wired.

    Written as an identity rather than a timestamp on purpose — the first cut
    compared mtimes and reported this exact case as broken, because bash 3.2's
    `-nt` resolves to the second and the sign of life landed inside the same
    one as the stamp.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _transcripts(session_dir, previous=TOOL_USE_LINE * 5)
    (remember / "tmp" / "capture-session-start").write_text(str(int(time.time())))
    (remember / "tmp" / "capture-alive").write_text("sess-prev")

    _run(SESSION_START, _env(home, project, remember))

    assert not (remember / "tmp" / "capture-gap-notice").exists(), (
        "warned about a session that captured normally — a false alarm here "
        "trains people to ignore the real one"
    )


def test_a_previous_session_with_no_tool_calls_raises_nothing(tmp_path):
    """A conversation with no tool calls produces no PostToolUse either, and
    that is not a fault. Only the transcript can tell the two apart."""
    home, project, remember, session_dir = _project(tmp_path)
    _transcripts(session_dir, previous=PLAIN_LINE * 5)
    (remember / "tmp" / "capture-session-start").write_text(str(int(time.time())))

    _run(SESSION_START, _env(home, project, remember))

    assert not (remember / "tmp" / "capture-gap-notice").exists(), (
        "cried wolf over a session that simply used no tools"
    )


def test_the_first_ever_session_raises_nothing(tmp_path):
    """No prior stamp means no prior session to judge. A fresh install must not
    greet its user with a warning about something that never happened."""
    home, project, remember, session_dir = _project(tmp_path)
    _transcripts(session_dir, previous=TOOL_USE_LINE * 5)

    _run(SESSION_START, _env(home, project, remember))

    assert not (remember / "tmp" / "capture-gap-notice").exists()


def test_session_start_always_refreshes_its_stamp(tmp_path):
    """Without this the check has no clock and every later session re-reports
    the same gap forever."""
    home, project, remember, session_dir = _project(tmp_path)
    _transcripts(session_dir, previous=PLAIN_LINE)

    _run(SESSION_START, _env(home, project, remember))

    assert (remember / "tmp" / "capture-session-start").exists()


# ---------------------------------------------------------------------------
# Delivery: systemMessage is the only output the human sees
# ---------------------------------------------------------------------------

def test_the_notice_is_delivered_as_a_system_message(tmp_path):
    home, project, remember, session_dir = _project(tmp_path)
    notice = remember / "tmp" / "capture-gap-notice"
    notice.write_text("capture did not run in your previous session")

    result = _run(USER_PROMPT, _env(home, project, remember))
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert "capture did not run" in payload["systemMessage"], (
        "the notice did not reach systemMessage — additionalContext is seen "
        "only by the model, which is how this went unnoticed for a day"
    )
    # The timestamp injection this hook exists for must survive the switch.
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert payload["hookSpecificOutput"]["additionalContext"].strip(), (
        "context injection was dropped on the notice path"
    )
    assert not notice.exists(), "notice was not consumed — it would repeat forever"


def test_the_ordinary_path_stays_plain_text(tmp_path):
    """No notice: byte-for-byte what it always printed. Emitting JSON here
    would silently change what every session injects into context."""
    home, project, remember, session_dir = _project(tmp_path)

    result = _run(USER_PROMPT, _env(home, project, remember))

    assert result.returncode == 0, result.stderr
    assert result.stdout.lstrip().startswith("["), result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
