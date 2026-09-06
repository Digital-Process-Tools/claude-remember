"""post-tool-hook.sh's STDIN_SESSION_ID guard lets a flag-shaped session_id
reach save-session.sh's argv, unchanged, in the background nohup save path
(#610 -- same class as #576/#600, a third call site neither of those two
fixes reached. #576 (agy-stop-hook.sh) is merged; #600 (session-end-hook.sh)
is fixed in PR #609, not yet merged as of this commit.)

The character class at the point of entry (``[!A-Za-z0-9._-]``) has never
excluded a *leading* dash, so a PostToolUse payload carrying
``session_id: "--dry"`` alongside a real, existing ``transcript_path`` passes
the guard untouched. Once ``STDIN_TRANSCRIPT_PATH`` names a file that exists,
the ``STDIN_SESSION_ID_TRUSTED`` branch (post-tool-hook.sh:564-576) asks only
whether *some* session id was supplied -- never whether that id actually
names the resolved transcript -- so the untouched value is trusted and handed
to ``nohup "$SAVE_SCRIPT" "$SESSION_ID" ... &`` (post-tool-hook.sh:908) as
positional argument 1.

save-session.sh's own arg loop (``--dry) DRY_RUN=true ;;``) reads a
leading-dash value as a FLAG rather than a session id. With no other
positional argument, its own ``SESSION_ID`` stays empty after the loop and
falls back to auto-detecting the newest transcript under this project's real
session directory (save-session.sh:184-187) -- which is exactly the crafted
payload's own real, existing ``transcript_path``, correctly placed there, so
the session gets identified correctly. What differs is DRY_RUN: silently
true. The delta-triggered save that should have written a summary and
advanced the position instead prints a preview banner to its own log and
exits -- no summary, no advanced position, and nothing about the hook's own
exit code (always 0; it only forked a background process) reveals it.

This is a background (``nohup ... &``) code path, distinct from #600's
synchronous session-end-hook.sh fixture: the corruption is only observable
in what the forked process actually did, which is why this fixture drives
the real save-session.sh to completion (reaping the forked pid) and inspects
its own log and side effects, rather than the hook's exit code.
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

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

# Same shape #204's containment tests already use to stand in for the real
# `claude` CLI (tests/test_spawn_containment.py) -- a full end-to-end run
# through the real, unstubbed save-session.sh, with only the Haiku spawn
# itself faked out.
_JSON_REPLY = (
    '{"result":"## 10:00 | main\\n\\n- did some work",'
    '"usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0}}'
)

# All-assistant lines, no recognizable human turns: HUMAN_COUNT ends up 0
# (well under the default min_human_messages=3 gate) while EXCHANGE_COUNT
# clears the default min_exchanges_without_human=30 "agentic session" escape
# hatch. Both the exploited run and the positive control take the identical
# "agentic session, saving anyway" branch inside save-session.sh BEFORE
# reaching the `if [ "$DRY_RUN" = true ]` fork -- isolating the guard defect
# under test from every other gate in the script.
ASSISTANT_LINE = '{"type":"assistant","message":{"content":"x"}}\n'
TRANSCRIPT_LINES = 100

REAL_SESSION_ID = "cccccccc-0000-4000-8000-000000000003"


def _fake_claude(tmp_path: Path, ledger: Path) -> Path:
    """A stand-in `claude` that answers like the real one and records the call."""
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/bash\n"
        "cat > /dev/null\n"
        f'echo spawn >> "{ledger}"\n'
        f"printf '%s' '{_JSON_REPLY}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _setup(tmp_path: Path):
    """A project with ONE real, correctly-placed session transcript -- the
    realistic shape a genuine PostToolUse payload's transcript_path always
    names (a file already under this project's own Claude Code session
    directory), so save-session.sh's own auto-detect fallback (triggered
    whenever its argv carries no positional session id, exactly what a
    flag-only argv leaves it with) finds the SAME real session the payload
    named -- proving the defect is DRY_RUN hijacking, not misattribution."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)

    transcript = session_dir / f"{REAL_SESSION_ID}.jsonl"
    transcript.write_text(ASSISTANT_LINE * TRANSCRIPT_LINES, encoding="utf-8")

    ledger = tmp_path / "haiku-calls.log"
    return home, project, remember, transcript, ledger


def _env(home: Path, project: Path, remember: Path, claude_bin: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "REMEMBER_CLAUDE_BIN": str(claude_bin),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _payload(session_id: str, transcript_path: Path) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/x"},
        "tool_response": {"ok": True},
    })


def _run(env: dict, stdin_payload: str):
    return subprocess.run(
        ["bash", str(HOOK)], env=env, input=stdin_payload,
        capture_output=True, text=True, timeout=60, check=False,
    )


def _reap(remember: Path, timeout: float = 30) -> None:
    """Wait for the forked save-session.sh to exit, so its side effects (the
    log file, the ledger, last-save.json) are all settled before assertions
    run."""
    pid_file = remember / "tmp" / "save-session.pid"
    deadline = time.monotonic() + timeout
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


def _save_log_text(remember: Path) -> str:
    autonomous = remember / "logs" / "autonomous"
    if not autonomous.is_dir():
        return ""
    logs = sorted(autonomous.glob("*.log"))
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)


def test_a_flag_shaped_session_id_must_not_turn_the_background_save_into_a_dry_run(tmp_path):
    """MUST NOT FIRE: the exploit. session_id="--dry" travels with a REAL,
    existing transcript_path -- exactly the payload shape the
    STDIN_SESSION_ID_TRUSTED branch (post-tool-hook.sh:564-576) accepts on
    the strength of the transcript_path alone. Before the fix, this silently
    turns the delta-triggered background save into a `--dry` preview: no
    Haiku call, no summary, no advanced position -- the guard at
    post-tool-hook.sh:419-421 must be the reason it no longer can."""
    home, project, remember, transcript, ledger = _setup(tmp_path)
    claude_bin = _fake_claude(tmp_path, ledger)

    result = _run(_env(home, project, remember, claude_bin),
                   _payload("--dry", transcript))
    assert result.returncode == 0, result.stderr
    _reap(remember)

    log_text = _save_log_text(remember)
    assert "=== DRY RUN ===" not in log_text, (
        "session_id=\"--dry\" reached save-session.sh's argv unrejected and "
        "was read as the --dry FLAG (not a positional session id), silently "
        "turning a real delta-triggered save into a no-op preview -- this is "
        "the #610 guard gap: post-tool-hook.sh:419-421 must reject a "
        "leading dash the same way #576 already does (merged) and #600 will "
        "once PR #609 merges "
        f"at its own sibling call site.\n\n--- save-session.sh log ---\n{log_text}"
    )
    assert ledger.read_text().count("spawn") >= 1 if ledger.exists() else False, (
        "the crafted session_id did not trigger the dry-run banner, but also "
        "never reached a real Haiku call either -- something else swallowed "
        f"the save.\n\n--- save-session.sh log ---\n{log_text}"
    )


def test_an_ordinary_session_id_still_flows_through_to_a_real_save(tmp_path):
    """MUST FIRE (positive control): an ordinary, non-adversarial session_id
    paired with the same real transcript_path must still produce a genuine
    save -- proving the assertion above is not passing merely because the
    harness itself never reaches a real save at all."""
    home, project, remember, transcript, ledger = _setup(tmp_path)
    claude_bin = _fake_claude(tmp_path, ledger)

    result = _run(_env(home, project, remember, claude_bin),
                   _payload(REAL_SESSION_ID, transcript))
    assert result.returncode == 0, result.stderr
    _reap(remember)

    log_text = _save_log_text(remember)
    assert ledger.exists() and ledger.read_text().count("spawn") >= 1, (
        f"an ordinary session_id never reached a real Haiku call -- the "
        f"harness itself is broken, not just the guard under test.\n\n"
        f"--- save-session.sh log ---\n{log_text}"
    )
    assert "=== DRY RUN ===" not in log_text
