"""PostToolUse must resolve a transcript on a host whose sessions do not
live under ~/.claude/projects/<slug> (#459).

Claude Code's own layout -- ``~/.claude/projects/<slug of PROJECT_DIR>`` --
is what ``SESSION_DIR`` in ``scripts/post-tool-hook.sh`` derives. Codex
writes its rollouts to ``~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl``
instead, so on Codex that directory never exists and never will: per-tool-call
capture was silently, permanently inert on that host, though the WARNING
that says so is loud and correct.

The fix reads ``transcript_path`` from the hook's own stdin payload -- present
on every hook event, on every host -- and uses it directly rather than
reconstructing a location. These tests pin:

    * a Codex-shaped payload, with a transcript that exists nowhere near
      ~/.claude/projects/, still gets captured -- the positive control;
    * the WARNING this issue is about is absent in exactly that case,
      paired with the existing regression control (test_non_ascii_paths.py)
      that still expects it when nothing usable is on stdin at all -- the
      "must not fire" half of the same pair;
    * the historical Claude Code path -- SESSION_DIR match by slug -- keeps
      working when stdin's transcript_path happens to be unusable, which is
      the "must still fire" half of the #212 regression control in
      tests/test_post_tool_session_id.py.
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

from .subprocess_helpers import subprocess_failure_detail

TOOL_LINE = '{"payload": {"type": "response_item", "role": "assistant"}}\n'
SESSION_ID = "01a04d64-6160-73d0-b2f0-5b6a0bb09fdb"


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


def _logs(remember: Path) -> str:
    return "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted((remember / "logs").glob("*.log"))
    )


def _codex_project(tmp_path: Path, *, lines: int):
    """A project whose ONLY transcript lives in a Codex-shaped rollout
    directory, nowhere under ~/.claude/projects/ -- the exact shape #459
    describes: the slugged Claude Code directory exists (created by an
    earlier SessionStart-less run, or simply never created at all) but has
    nothing in it, while a real transcript sits somewhere the hook's old
    SESSION_DIR derivation could never look."""
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
    rollout = codex_sessions / f"rollout-2026-08-29T00-00-00-{SESSION_ID}.jsonl"
    rollout.write_text(TOOL_LINE * lines)

    return home, project, remember, rollout


def _codex_payload(session_id: str, transcript_path: Path) -> str:
    """A PostToolUse stdin payload shaped like Codex's own (#443's envelope
    table): session_id and transcript_path are both present, same as every
    other host, but transcript_path names a Codex rollout file."""
    return json.dumps({
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/x"},
        "tool_response": {"ok": True},
    })


def test_codex_transcript_is_captured_via_stdin_transcript_path(tmp_path):
    """The positive control: a Codex session, whose transcript lives nowhere
    ~/.claude/projects/ derivation could find it, still gets captured once
    it crosses the delta threshold."""
    home, project, remember, rollout = _codex_project(tmp_path, lines=200)
    payload = _codex_payload(SESSION_ID, rollout)

    result = _run(_env(home, project, remember), payload)
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert (remember / "tmp" / "save-session.pid").exists(), (
        "no save forked for a 200-line Codex session: stdin's transcript_path "
        f"was not used. logs:\n{_logs(remember)}"
    )


def test_codex_transcript_path_suppresses_the_no_session_dir_warning(tmp_path):
    """The must-not-fire half paired with the positive control above: the
    WARNING #459 is about must not appear once stdin's transcript_path is
    usable, even though the slugged Claude Code directory this project would
    derive is empty."""
    home, project, remember, rollout = _codex_project(tmp_path, lines=5)
    payload = _codex_payload(SESSION_ID, rollout)

    result = _run(_env(home, project, remember), payload)
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    logs = _logs(remember)
    assert "no session dir" not in logs, (
        f"the hook still warned about the Claude Code layout though a usable "
        f"Codex transcript_path was on stdin:\n{logs}"
    )


def test_bogus_stdin_transcript_path_falls_back_to_session_dir(tmp_path):
    """The must-still-fire half of the SAME pairing, from the other
    direction: a transcript_path on stdin that does not point at a real file
    (an older CLI quirk, a stale value, a value this hook cannot open) must
    not disable capture -- it degrades to the historical SESSION_DIR/slug
    lookup, exactly as an absent transcript_path already does in
    tests/test_post_tool_session_id.py."""
    from pipeline.slug import session_dir_slug as _slug

    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    (remember / "config.json").write_text(
        json.dumps({"thresholds": {"delta_lines_trigger": 50}}), encoding="utf-8"
    )
    real = session_dir / f"{SESSION_ID}.jsonl"
    real.write_text('{"type":"assistant","message":{"content":"x"}}\n' * 200)

    payload = json.dumps({
        "session_id": SESSION_ID,
        "transcript_path": "/nowhere/on/disk/does-not-exist.jsonl",
        "hook_event_name": "PostToolUse",
    })
    result = _run(_env(home, project, remember), payload)
    _reap(remember)

    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert (remember / "tmp" / "save-session.pid").exists(), (
        "an unusable stdin transcript_path disabled capture instead of "
        "falling back to the SESSION_DIR/slug lookup"
    )
