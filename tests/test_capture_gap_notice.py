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

from .subprocess_helpers import subprocess_failure_detail

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
    # NOTE: this config write is INERT under this harness, and saying so is the
    # point. `_LIB_MEMORY_DIR_LOADED=1` in the env below makes lib-memory-dir.sh
    # return before it merges the config layers, so REMEMBER_CONFIG is never
    # exported and log.sh's config() falls back to each caller's default —
    # `features.recovery` reads as true regardless of what is written here.
    #
    # Recovery still does not fire, but for a different reason: it also needs
    # tmp/last-save.json, which these fixtures do not create. Anyone copying
    # this pattern into a test that DOES create last-save.json will get a
    # stray background save-session.sh fork, not the suppression this looks
    # like it buys.
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
    cur = session_dir / "sess-cur.jsonl"
    cur.write_text(current)
    # Explicit, widely spaced mtimes rather than a sleep. `ls -t` orders by
    # mtime, and this feature has already been bitten once by assuming
    # sub-second mtime resolution — bash's `-nt` works to the second, which is
    # why the hook compares identities now. A 50ms sleep would reproduce that
    # same fragility in the test harness on a coarse filesystem or a loaded
    # runner, so the ordering is stated outright instead of raced for.
    now = int(time.time())
    os.utime(prev, (now - 120, now - 120))
    os.utime(cur, (now, now))
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
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

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


def test_the_check_is_not_gated_on_having_run_before(tmp_path):
    """The whole point, and the thing the first cut got backwards.

    That version required a prior session-start stamp, so a fresh install
    would not be warned about a session that predated it. It sounds right and
    it defeats the feature: during a mid-session enable NO hook runs, so no
    stamp is ever written, so the one incident this exists to report is the
    exact case it stayed silent for. It could only have caught a recurrence.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _transcripts(session_dir, previous=TOOL_USE_LINE * 5)
    # No stamp, no capture-alive: the state a mid-session enable leaves behind.

    _run(SESSION_START, _env(home, project, remember))

    notice = remember / "tmp" / "capture-gap-notice"
    assert notice.exists(), (
        "stayed silent for the originating incident — the only one that "
        "actually happened to the reporter"
    )
    assert "installed or enabled" in notice.read_text(), (
        "a fresh install sees this too, so the wording has to cover both "
        "without alarming someone whose plugin is working fine"
    )


# ---------------------------------------------------------------------------
# The evidence store must answer "was session X captured" (issue #206)
# ---------------------------------------------------------------------------
#
# The #200 cut stored ONE session id, last-write-wins. That answers a different
# question — "which session most recently made a tool call" — and the two come
# apart the moment any session writes after X did. Then the evidence that X was
# captured is simply gone, and the check reads its own amnesia as a fault in X.
#
# Two independent reproductions, both from real installs:
#
#   * `/clear` does not mint a new session id. It fires SessionStart while the
#     transcript, the id and the .jsonl all stay the same — and by then the
#     CURRENT session has already made tool calls, so the one slot holds the
#     current id. The previous session's record was overwritten hours ago, so
#     the mismatch is structurally guaranteed regardless of capture health.
#     (Same for `compact` and `fork` — every same-session SessionStart source.)
#
#   * A session captured by the NEXT session's recovery block rather than by
#     its own live saves — which, given the default delta/cooldown thresholds,
#     is the ordinary case for short sessions, not an edge case. Reported
#     independently by ca-sringert on #206.
#
# Neither is fixed by looking at SessionStart's `source`. The store is the
# defect; the source field is just where the loudest instance surfaced.


def _stamp(*files):
    """Fix mtimes oldest-to-newest, widely spaced. See _transcripts on why the
    ordering is stated rather than raced for."""
    now = int(time.time())
    for offset, path in enumerate(reversed(files)):
        t = now - offset * 120
        os.utime(path, (t, t))


def test_evidence_of_capture_survives_a_later_session_making_tool_calls(tmp_path):
    """The `/clear` false positive, end to end and store-agnostic.

    Nothing here writes a marker by hand — the previous session is captured by
    running the real PostToolUse hook, exactly as a healthy session captures
    itself. Then the current session makes its own tool calls, which is the
    precondition `/clear` guarantees: SessionStart runs with the same session
    id still current and its record already written.

    Under a single-slot store the second PostToolUse run destroys the first's
    evidence and this warns every single time. It has to survive.
    """
    home, project, remember, session_dir = _project(tmp_path)
    env = _env(home, project, remember)

    prev = session_dir / "sess-prev.jsonl"
    prev.write_text(TOOL_USE_LINE * 5)
    _run(POST_TOOL, env)

    cur = session_dir / "sess-cur.jsonl"
    cur.write_text(TOOL_USE_LINE * 5)
    _stamp(prev, cur)
    _run(POST_TOOL, env)

    _run(SESSION_START, env)

    assert not (remember / "tmp" / "capture-gap-notice").exists(), (
        "warned about a session the PostToolUse hook demonstrably ran for — "
        "its record was overwritten by the current session's own tool calls, "
        "which is what /clear guarantees and what makes this fire forever"
    )


def test_a_session_captured_only_by_recovery_is_not_flagged(tmp_path):
    """ca-sringert's repro: a short session that never crossed its own save
    thresholds, picked up whole by the next session's recovery block.

    `save-session.sh`'s recovery path never touches capture-alive — only
    post-tool-hook.sh does — so the one slot still names some unrelated older
    session. But last-save.json records the session as fully captured, and it
    is the same source the recovery block itself already trusts. A detector
    that contradicts the save record is reporting on its own bookkeeping, not
    on whether anything was lost.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _transcripts(session_dir, previous=TOOL_USE_LINE * 5)

    (remember / "tmp" / "last-save.json").write_text(
        json.dumps({"sessions": {"sess-prev": 72}, "session": "sess-prev", "line": 72}),
        encoding="utf-8",
    )
    (remember / "tmp" / "capture-alive").write_text("sess-some-older-one")

    _run(SESSION_START, _env(home, project, remember))

    assert not (remember / "tmp" / "capture-gap-notice").exists(), (
        "flagged a session last-save.json records as captured 72/72 — the "
        "content is in memory, so 'was not captured' is simply false"
    )


def test_a_real_capture_gap_still_warns(tmp_path):
    """The pin that stops this fix degenerating into deleting the warning.

    PostToolUse ran — for an older session, so the store is populated and
    working — and never once for the previous session. That is the #200
    signature exactly: hooks not registered for that session. It must still be
    reported, or the whole feature is theatre.
    """
    home, project, remember, session_dir = _project(tmp_path)
    env = _env(home, project, remember)

    old = session_dir / "sess-old.jsonl"
    old.write_text(TOOL_USE_LINE * 5)
    _run(POST_TOOL, env)

    prev = session_dir / "sess-prev.jsonl"
    prev.write_text(TOOL_USE_LINE * 5)
    cur = session_dir / "sess-cur.jsonl"
    cur.write_text(PLAIN_LINE)
    _stamp(old, prev, cur)

    _run(SESSION_START, env)

    notice = remember / "tmp" / "capture-gap-notice"
    assert notice.exists(), (
        "silent about a session that ran tools and never once fired "
        "PostToolUse — a detector that cannot say this is worse than none"
    )
    assert "doctor" in notice.read_text()


def test_the_same_gap_is_reported_once_not_on_every_restart(tmp_path):
    """A gap is a fact about one past session, not a condition to re-announce.

    Restarts are cheap and frequent — /clear, /compact, a resume — and each one
    re-examines the same previous session. Saying it again on every one is how
    a true positive gets trained into background noise.
    """
    home, project, remember, session_dir = _project(tmp_path)
    env = _env(home, project, remember)
    _transcripts(session_dir, previous=TOOL_USE_LINE * 5)

    _run(SESSION_START, env)
    notice = remember / "tmp" / "capture-gap-notice"
    assert notice.exists(), "precondition: the first run must report it"
    notice.unlink()  # user-prompt-hook.sh consumes it on delivery

    _run(SESSION_START, env)

    assert not notice.exists(), (
        "re-reported an already-delivered gap about the same session"
    )


def test_the_legacy_single_slot_marker_still_counts_as_evidence(tmp_path):
    """Migration. Existing installs have a `capture-alive` FILE and no
    per-session store, and the first run after an upgrade must not invent a
    warning out of the store simply being empty. The old marker keeps its
    meaning for the one session it can still speak for."""
    home, project, remember, session_dir = _project(tmp_path)
    _transcripts(session_dir, previous=TOOL_USE_LINE * 5)
    (remember / "tmp" / "capture-alive").write_text("sess-prev")
    assert not (remember / "tmp" / "capture-alive.d").exists(), (
        "precondition: this is the pre-upgrade state, no per-session store"
    )

    _run(SESSION_START, _env(home, project, remember))

    assert not (remember / "tmp" / "capture-gap-notice").exists(), (
        "the upgrade itself produced a warning — a fix whose first act is a "
        "false positive teaches the exact lesson it was meant to unteach"
    )


def test_the_evidence_store_stays_bounded(tmp_path):
    """One marker per session, forever, in a tmp dir nothing else prunes. The
    store is a cache of recent sessions, not an archive."""
    home, project, remember, session_dir = _project(tmp_path)
    env = _env(home, project, remember)
    store = remember / "tmp" / "capture-alive.d"
    store.mkdir(parents=True)
    now = int(time.time())
    for i in range(400):
        marker = store / f"sess-{i:04d}"
        marker.touch()
        os.utime(marker, (now - (400 - i), now - (400 - i)))
    _transcripts(session_dir, previous=PLAIN_LINE * 5)

    _run(SESSION_START, env)

    assert len(list(store.iterdir())) < 400, "store is never pruned"
    assert (store / "sess-0399").exists(), (
        "pruned the most recent markers — those are the ones the check reads"
    )


# ---------------------------------------------------------------------------
# Delivery: systemMessage is the only output the human sees
# ---------------------------------------------------------------------------

def test_the_notice_is_delivered_as_a_system_message(tmp_path):
    home, project, remember, session_dir = _project(tmp_path)
    notice = remember / "tmp" / "capture-gap-notice"
    notice.write_text("capture did not run in your previous session")

    result = _run(USER_PROMPT, _env(home, project, remember))
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

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


def test_a_failing_jq_can_never_eat_the_prompt(tmp_path):
    """On UserPromptSubmit, exit 2 blocks the prompt AND ERASES what the user
    typed. Left as the script's last command, jq's own status became the
    hook's — so a jq usage error (exit 2) would destroy the user's input on
    every prompt with a notice pending. A cosmetic notice must never be able
    to do that.
    """
    home, project, remember, session_dir = _project(tmp_path)
    (remember / "tmp" / "capture-gap-notice").write_text("something to say")

    bindir = tmp_path / "badjq"
    bindir.mkdir()
    fake = bindir / "jq"
    fake.write_text('#!/bin/sh\necho "jq: Unknown option" >&2\nexit 2\n')
    fake.chmod(0o755)

    env = _env(home, project, remember)
    env["JQ"] = str(fake)
    result = subprocess.run(["bash", str(USER_PROMPT)], env=env,
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, (
        f"exit {result.returncode} — on UserPromptSubmit that blocks and "
        "erases the user's prompt"
    )
    assert result.stdout.strip(), "swallowed the context injection entirely"
    assert "something to say" in result.stdout, (
        "lost the notice as well as the JSON — the fallback must still say it"
    )


def test_the_ordinary_path_stays_plain_text(tmp_path):
    """No notice: byte-for-byte what it always printed. Emitting JSON here
    would silently change what every session injects into context."""
    home, project, remember, session_dir = _project(tmp_path)

    result = _run(USER_PROMPT, _env(home, project, remember))

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert result.stdout.lstrip().startswith("["), result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
