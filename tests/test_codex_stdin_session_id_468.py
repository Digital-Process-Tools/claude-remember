"""Per-tool-call capture on Codex still forked save-session.sh into a
silent rejection, after #459 was supposed to fix exactly this (#468).

#459 taught post-tool-hook.sh to read `transcript_path` off stdin instead of
reconstructing a Claude Code session directory -- but the fix for the
transcript path discarded `STDIN_SESSION_ID`, which the hook had already
extracted and validated four lines earlier. `SESSION_ID` at
post-tool-hook.sh:552-553 stayed derived from the *transcript's own
basename*:

    SESSION_ID="${TRANSCRIPT##*/}"
    SESSION_ID="${SESSION_ID%.jsonl}"

On Claude Code that basename IS the session id, because Claude Code names its
own transcripts `<session-id>.jsonl`. On Codex it is
`rollout-<date>-<uuid>.jsonl` -- and save-session.sh:191 only accepts
`[a-f0-9-]+`. So every PostToolUse on Codex forked save-session.sh with an id
it was always going to reject, into an autonomous log nobody reads.

SessionEnd is unaffected (it passes the stdin UUID directly), which is why a
Codex session still looks captured end to end -- the earlier verification of
this exact area watched SessionEnd and read the absence of the old "no
session dir" WARNING as the fix working. It was not: what silently broke is
*incremental* capture, and the warning that used to say so is gone precisely
because #459 fixed the OTHER half of this same code path.

The assertion below is that a save is *attempted and accepted* -- not merely
that no warning is logged, which is the distinction #459's own tests could
not make (they never drove the id far enough to hit save-session.sh's own
validation gate). Paired with a positive control that isolates the gate
itself: proof the harness can see this exact rejection when it truly occurs,
so the "must not fire" half above is not vacuously green.
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
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"
SAVE_SESSION = REPO_ROOT / "scripts" / "save-session.sh"

from .subprocess_helpers import subprocess_failure_detail

TOOL_LINE = '{"payload": {"type": "response_item", "role": "assistant"}}\n'
SESSION_ID = "01a04d92-ea74-7253-92f7-bd7918847ffb"
ROLLOUT_BASENAME = f"rollout-2026-08-29T14-51-09-{SESSION_ID}"


def _env(home: Path, project: Path, remember: Path, extra: dict | None = None) -> dict:
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    if extra:
        env.update(extra)
    return env


def _run(env: dict, stdin_payload):
    kwargs = {"env": env, "capture_output": True, "text": True, "timeout": 60}
    if stdin_payload is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_payload
    return subprocess.run(["bash", str(HOOK)], check=False, **kwargs)


def _reap(remember: Path):
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


def _memory_logs(remember: Path) -> str:
    """The daily narrative log `log()` writes to -- distinct from
    hook-errors.log, which only `report_error`/`_dispatch_report_*` reach.
    save-session.sh's own session-id rejection goes through plain `log`, so
    this is the file that actually carries it."""
    logs_dir = remember / "logs"
    if not logs_dir.is_dir():
        return "(no logs dir)"
    return "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(logs_dir.glob("*.log"))
    )


def _codex_project(tmp_path: Path, *, lines: int):
    """A project whose only transcript is Codex-shaped: the rollout file's
    OWN basename is not a valid session id, though stdin's session_id is."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (home / ".claude" / "projects").mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    (remember / "config.json").write_text(
        json.dumps({"thresholds": {"delta_lines_trigger": 50}}), encoding="utf-8"
    )

    codex_sessions = tmp_path / "codex-home" / "sessions" / "2026" / "08" / "29"
    codex_sessions.mkdir(parents=True)
    rollout = codex_sessions / f"{ROLLOUT_BASENAME}.jsonl"
    rollout.write_text(TOOL_LINE * lines)

    return home, project, remember, rollout


def _codex_payload(session_id: str, transcript_path: Path) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/x"},
        "tool_response": {"ok": True},
    })


# ---------------------------------------------------------------------------
# The positive control, isolated from the hook entirely: proof the harness
# CAN see save-session.sh reject a rollout-shaped id, so the absence asserted
# below is not vacuous.
# ---------------------------------------------------------------------------


def test_control_save_session_rejects_a_rollout_shaped_id_directly(tmp_path):
    """MUST FIRE. Calling save-session.sh with the id post-tool-hook.sh used
    to derive (the transcript's own basename, unchanged) hits the validation
    gate at save-session.sh:191 head-on, with nothing else in the way."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)

    result = subprocess.run(
        ["bash", str(SAVE_SESSION), ROLLOUT_BASENAME, "--dry"],
        env=_env(home, project, remember),
        capture_output=True, text=True, timeout=60, check=False,
    )

    assert result.returncode == 1, subprocess_failure_detail(result, remember)
    logs = _memory_logs(remember)
    assert "ERROR: invalid session ID" in logs, (
        f"the positive control did not reproduce the rejection at all -- "
        f"the harness cannot tell a rejected save from an accepted one:\n{logs}"
    )


# ---------------------------------------------------------------------------
# The actual bug: driven through the real hook, on a Codex-shaped payload.
# ---------------------------------------------------------------------------


def test_codex_save_is_attempted_and_accepted_via_stdin_session_id(tmp_path):
    """MUST NOT FIRE. A Codex-shaped PostToolUse payload -- a valid UUID on
    stdin, a transcript_path whose OWN basename is not one -- must still
    reach save-session.sh with an id it accepts. Absence of the "no session
    dir" WARNING (#459's own test) is a different, weaker claim: it says
    nothing about whether the fork that followed was ever accepted."""
    home, project, remember, rollout = _codex_project(tmp_path, lines=200)
    payload = _codex_payload(SESSION_ID, rollout)

    result = _run(_env(home, project, remember), payload)
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert (remember / "tmp" / "save-session.pid").exists(), (
        "no save forked at all for a 200-line Codex session: stdin's "
        f"session_id/transcript_path were not used. logs:\n{_memory_logs(remember)}"
    )
    logs = _memory_logs(remember)
    assert "ERROR: invalid session ID" not in logs, (
        "save-session.sh rejected the id post-tool-hook.sh handed it -- the "
        f"basename derivation is still in use instead of stdin's session_id:\n{logs}"
    )

    per_session = remember / "tmp" / "capture-alive.d" / SESSION_ID
    assert per_session.exists(), (
        "the capture-alive marker was not written under the stdin session id "
        f"-- the hook derived a different SESSION_ID internally. Present: "
        f"{sorted(p.name for p in (remember / 'tmp').iterdir())}"
    )


def test_bogus_stdin_session_id_still_falls_back_to_the_basename(tmp_path):
    """The other half of the same pairing, from #212/#459's own direction:
    when stdin's session_id is unusable, the fix must not disable capture --
    it degrades to the transcript basename exactly as before. On a Codex
    payload with no usable session_id this basename is rejected by
    save-session.sh's own gate, same as the positive control above, and that
    is the PRE-EXISTING failure mode this issue does not touch.

    This does not merely check that SOME save was forked (a save forked
    with an EMPTY session id would pass that alone) -- it checks which id
    was actually used, via the capture-alive marker the hook itself writes
    under the derived SESSION_ID before ever forking anything. An empty
    SESSION_ID writes no marker at all (the empty-string arm of the path
    guard at post-tool-hook.sh's capture-alive block), so this is exactly
    the assertion that would have caught #468's own first-pass regression:
    STDIN_SESSION_ID_TRUSTED going true whenever STDIN_TRANSCRIPT_PATH was
    present, with no check that a session_id had actually arrived alongside
    it -- which handed save-session.sh an empty id instead of falling back
    to the basename, silently misattributing whatever session happened to
    be newest by mtime rather than degrading safely."""
    home, project, remember, rollout = _codex_project(tmp_path, lines=200)
    payload = json.dumps({
        "session_id": "",
        "transcript_path": str(rollout),
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
    })

    result = _run(_env(home, project, remember), payload)
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert (remember / "tmp" / "save-session.pid").exists(), (
        "no save forked when stdin offered no usable session_id -- capture "
        "must still be ATTEMPTED via the basename fallback, even though "
        "save-session.sh will go on to reject it"
    )
    marker = remember / "tmp" / "capture-alive.d" / ROLLOUT_BASENAME
    assert marker.exists(), (
        "the derived SESSION_ID was not the transcript's own basename -- "
        "likely an EMPTY id instead (a transcript_path present with no "
        "session_id must not be treated as trusted). capture-alive.d "
        f"holds: {sorted(p.name for p in (remember / 'tmp' / 'capture-alive.d').iterdir()) if (remember / 'tmp' / 'capture-alive.d').is_dir() else '(missing)'}"
    )
