"""The three Antigravity (agy) hook adapters: agy-session-start-hook.sh,
agy-pre-invocation-hook.sh, agy-stop-hook.sh (#563).

Two live defects were found and fixed while driving these against a real
`agy` process (not reproducible from unit tests of pipeline/host.py alone,
which only exercise the transcript-parsing half):

1. Antigravity's own hook executor parses a command hook's STDOUT as
   protojson against ITS OWN schema, not Claude Code's. Delegating straight
   to session-start-hook.sh / user-prompt-hook.sh let their Claude
   Code-shaped stdout (plain-text context injection; a
   `{"hookSpecificOutput": ...}` envelope) reach agy, which logged
   "failed to unmarshal result ... via protojson" for every SessionStart
   and PreInvocation. Fixed by discarding the delegate's stdout.
2. A plain backgrounded subshell (`( cmd & )`) for the Stop-triggered save
   did not survive past the hook process exiting under a real `agy` run --
   no trace of save-session.sh ever starting. Fixed with `nohup ... &` +
   `disown`, the same defence scripts/post-tool-hook.sh and
   scripts/session-end-hook.sh already use for their own backgrounded
   saves.
3. save-session.sh has no stdin of its own to resolve PROJECT_DIR from when
   launched this way, and FATALs loudly if CLAUDE_PROJECT_DIR/
   REMEMBER_HOOK_CWD are both unset -- a FATAL the nohup redirect above was
   silently swallowing. Fixed by forwarding Antigravity's own
   `workspacePaths[0]` as CLAUDE_PROJECT_DIR.

These tests pin the composition each fix depends on using a FAKE
save-session.sh (so no Haiku call, no real pipeline extraction -- that half
is tests/test_antigravity_envelope_563.py's job) and assert on what
actually reached it: env vars and background-survival, which is exactly
the layer a pure pipeline.host/pipeline.extract unit test cannot see.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

_FAKE_SAVE_SESSION = """#!/bin/bash
# Records the env this fake was actually called with, then gets out of the
# way -- no lock, no Haiku, no real extraction. That is deliberately not
# this script's job (see the module docstring).
{
  echo "ARGV=$*"
  echo "CLAUDE_PROJECT_DIR=$CLAUDE_PROJECT_DIR"
  echo "REMEMBER_TRANSCRIPT_PATH=$REMEMBER_TRANSCRIPT_PATH"
} > "$FAKE_SAVE_SESSION_CALLED"
"""

_FAKE_SESSION_START = """#!/bin/bash
cat >/dev/null
echo "=== REMEMBER ==="
echo "not json"
"""

_FAKE_USER_PROMPT = """#!/bin/bash
cat >/dev/null
echo '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}'
"""


def _sandbox(tmp_path: Path) -> Path:
    """A scripts/ directory holding the three real adapters beside a FAKE
    save-session.sh/session-start-hook.sh/user-prompt-hook.sh -- the
    adapters derive their own directory from BASH_SOURCE, so copying them
    elsewhere is enough to sandbox the whole thing; nothing this test does
    touches the real repo's own scripts/ or any real memory store."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("agy-session-start-hook.sh", "agy-pre-invocation-hook.sh", "agy-stop-hook.sh"):
        dst = scripts / name
        shutil.copyfile(REPO_ROOT / "scripts" / name, dst)
        dst.chmod(0o755)
    (scripts / "save-session.sh").write_text(_FAKE_SAVE_SESSION)
    (scripts / "save-session.sh").chmod(0o755)
    (scripts / "session-start-hook.sh").write_text(_FAKE_SESSION_START)
    (scripts / "session-start-hook.sh").chmod(0o755)
    (scripts / "user-prompt-hook.sh").write_text(_FAKE_USER_PROMPT)
    (scripts / "user-prompt-hook.sh").chmod(0o755)
    return scripts


def _run(script: Path, stdin_obj: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], input=json.dumps(stdin_obj), env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )


def _wait_for(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


# --- SessionStart / PreInvocation: stdout is discarded ---

def test_session_start_adapter_discards_delegates_stdout(tmp_path):
    scripts = _sandbox(tmp_path)
    env = {**os.environ}
    result = _run(scripts / "agy-session-start-hook.sh",
                  {"conversationId": "abc", "transcriptPath": "/x", "workspacePaths": []}, env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert result.stdout == "", (
        "SessionStart adapter must not forward the delegate's own stdout -- "
        "a real agy process fails to protojson-parse it (#563)"
    )


def test_pre_invocation_adapter_discards_delegates_stdout(tmp_path):
    scripts = _sandbox(tmp_path)
    env = {**os.environ}
    result = _run(scripts / "agy-pre-invocation-hook.sh",
                  {"workspacePaths": []}, env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert result.stdout == ""


# --- Stop: field renaming, nohup survival, project-dir forwarding ---

def test_stop_adapter_forwards_transcript_path_and_project_dir(tmp_path):
    """The must-fire case: a well-formed Stop payload reaches save-session.sh
    with REMEMBER_TRANSCRIPT_PATH and CLAUDE_PROJECT_DIR set from Antigravity's
    own field names (transcriptPath / workspacePaths[0]), and survives the
    hook process exiting (nohup + disown, #563)."""
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run(scripts / "agy-stop-hook.sh", {
        "conversationId": "0b04d3f2-c231-4ee0-8337-076e220bd1ad",
        "transcriptPath": "/some/real/transcript.jsonl",
        "workspacePaths": ["/some/project"],
        "terminationReason": "NO_TOOL_CALL",
    }, env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert _wait_for(marker), "save-session.sh was never called (background job did not survive)"
    content = marker.read_text()
    assert "ARGV=0b04d3f2-c231-4ee0-8337-076e220bd1ad" in content
    assert "CLAUDE_PROJECT_DIR=/some/project" in content
    assert "REMEMBER_TRANSCRIPT_PATH=/some/real/transcript.jsonl" in content


def test_stop_adapter_must_not_fire_without_transcript_path(tmp_path):
    """The paired must-not-fire control: a Stop payload with no transcript
    path at all must not call save-session.sh -- proven alongside the
    positive case above in the same file, so a stub that always "succeeds"
    silently could not pass both."""
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run(scripts / "agy-stop-hook.sh", {
        "conversationId": "0b04d3f2-c231-4ee0-8337-076e220bd1ad",
        "transcriptPath": "",
        "workspacePaths": ["/some/project"],
    }, env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    time.sleep(0.3)
    assert not marker.exists(), "save-session.sh must not be called with no transcript path"


def test_stop_adapter_must_not_fire_without_conversation_id(tmp_path):
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run(scripts / "agy-stop-hook.sh", {
        "conversationId": "",
        "transcriptPath": "/some/real/transcript.jsonl",
        "workspacePaths": ["/some/project"],
    }, env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    time.sleep(0.3)
    assert not marker.exists(), "save-session.sh must not be called with no conversation id"


def test_stop_adapter_survives_without_workspace_paths(tmp_path):
    """CLAUDE_PROJECT_DIR forwarding is best-effort -- an empty
    workspacePaths (the common case observed live: populated only via
    `agy --add-dir`, not a bare cwd, #563) must not crash the adapter or
    stop it calling save-session.sh; the fake here simply records that
    CLAUDE_PROJECT_DIR was not set to anything, which is the honest
    outcome, not a fabricated fallback."""
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    env.pop("CLAUDE_PROJECT_DIR", None)
    result = _run(scripts / "agy-stop-hook.sh", {
        "conversationId": "0b04d3f2-c231-4ee0-8337-076e220bd1ad",
        "transcriptPath": "/some/real/transcript.jsonl",
        "workspacePaths": [],
    }, env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert _wait_for(marker)
    content = marker.read_text()
    assert "CLAUDE_PROJECT_DIR=" in content
    assert "CLAUDE_PROJECT_DIR=/some/project" not in content
