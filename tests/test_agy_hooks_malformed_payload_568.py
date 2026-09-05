"""A malformed agy hook payload must not render identically to an empty-but-
valid one (#568).

``scripts/agy-stop-hook.sh``, ``scripts/agy-session-start-hook.sh`` and
``scripts/agy-pre-invocation-hook.sh`` each normalise stdin with
``python3 -c '...' 2>/dev/null) || _FIELDS=""``, wrapping an inner
``except Exception: d = {}``. Before this fix, a malformed payload (bad
JSON, or no ``python3`` at all) produced EXACTLY the same output as an
empty-but-valid ``{}`` payload -- the stop hook then took its ``exit 0``
arm having captured nothing, with no receipt anywhere that it happened.

The three cases this test distinguishes, matching the issue's own
reproduction:

  A) malformed payload   -- must warn on stderr, must not call the delegate
     (stop hook) / must still call the delegate silently-empty (start &
     pre-invocation hooks, which always forward to their delegate).
  B) empty object ``{}``  -- the ordinary, honest "nothing to capture"
     case -- must stay SILENT (the must-not-fire half paired with A).
  C) a valid, fully-populated payload -- the positive control: must fire
     exactly as before, with no warning at all.

Settled by #568 itself: a log line on the failure arm is safe here --
``agy`` parses these hooks' STDOUT as protojson, but the scripts already
redirect stdout to the delegate/nothing, so stderr is free to use.
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
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

_FAKE_SAVE_SESSION = """#!/bin/bash
{
  echo "ARGV=$*"
  echo "CLAUDE_PROJECT_DIR=$CLAUDE_PROJECT_DIR"
  echo "REMEMBER_TRANSCRIPT_PATH=$REMEMBER_TRANSCRIPT_PATH"
} > "$FAKE_SAVE_SESSION_CALLED"
"""

_FAKE_SESSION_START = """#!/bin/bash
cat > "$FAKE_DELEGATE_STDIN"
echo "=== REMEMBER ==="
echo "not json"
"""

_FAKE_USER_PROMPT = """#!/bin/bash
cat > "$FAKE_DELEGATE_STDIN"
echo '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}'
"""


def _sandbox(tmp_path: Path) -> Path:
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


def _run_raw(script: Path, raw_stdin: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], input=raw_stdin, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )


def _wait_for(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


# --- agy-stop-hook.sh: A vs B vs C ---

def test_stop_adapter_malformed_payload_warns_and_captures_nothing(tmp_path):
    """A) malformed payload: must warn on stderr and must not call
    save-session.sh -- distinct from the empty-object case below."""
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run_raw(scripts / "agy-stop-hook.sh", "{not valid json", env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    time.sleep(0.3)
    assert not marker.exists(), "save-session.sh must not be called from a malformed payload"
    assert result.stderr.strip() != "", (
        "a malformed payload must leave a receipt on stderr -- silence here "
        "is indistinguishable from the empty-object case (#568)"
    )


def test_stop_adapter_empty_object_stays_silent(tmp_path):
    """B) the must-not-fire control paired with A: a genuinely empty, valid
    payload is the ORDINARY case (nothing to capture) and must stay silent
    -- the fix must not turn every legitimate empty Stop into noise."""
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run_raw(scripts / "agy-stop-hook.sh", "{}", env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    time.sleep(0.3)
    assert not marker.exists()
    assert result.stderr.strip() == "", (
        f"an empty-but-valid payload must not warn -- got: {result.stderr!r}"
    )


def test_stop_adapter_valid_payload_still_fires_with_no_warning(tmp_path):
    """C) the positive control: a real payload must still reach
    save-session.sh, unaffected, and silently."""
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run_raw(scripts / "agy-stop-hook.sh", json.dumps({
        "conversationId": "abc",
        "transcriptPath": "/t.jsonl",
        "workspacePaths": [],
    }), env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert _wait_for(marker)
    assert result.stderr.strip() == ""


# --- agy-session-start-hook.sh: same three cases ---

def test_session_start_adapter_malformed_payload_warns(tmp_path):
    scripts = _sandbox(tmp_path)
    delegate_stdin = tmp_path / "delegate-stdin.json"
    env = {**os.environ, "FAKE_DELEGATE_STDIN": str(delegate_stdin)}
    result = _run_raw(scripts / "agy-session-start-hook.sh", "not json at all", env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert result.stderr.strip() != "", (
        "a malformed SessionStart payload must leave a receipt on stderr (#568)"
    )


def test_session_start_adapter_empty_object_stays_silent(tmp_path):
    scripts = _sandbox(tmp_path)
    delegate_stdin = tmp_path / "delegate-stdin.json"
    env = {**os.environ, "FAKE_DELEGATE_STDIN": str(delegate_stdin)}
    result = _run_raw(scripts / "agy-session-start-hook.sh", "{}", env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert result.stderr.strip() == "", (
        f"an empty-but-valid SessionStart payload must not warn -- got: {result.stderr!r}"
    )


def test_session_start_adapter_valid_payload_no_warning(tmp_path):
    scripts = _sandbox(tmp_path)
    delegate_stdin = tmp_path / "delegate-stdin.json"
    env = {**os.environ, "FAKE_DELEGATE_STDIN": str(delegate_stdin)}
    result = _run_raw(scripts / "agy-session-start-hook.sh", json.dumps({
        "conversationId": "abc", "transcriptPath": "/x", "workspacePaths": ["/proj"],
    }), env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert result.stderr.strip() == ""


# --- agy-pre-invocation-hook.sh: same three cases ---

def test_pre_invocation_adapter_malformed_payload_warns(tmp_path):
    scripts = _sandbox(tmp_path)
    delegate_stdin = tmp_path / "delegate-stdin.json"
    env = {**os.environ, "FAKE_DELEGATE_STDIN": str(delegate_stdin)}
    result = _run_raw(scripts / "agy-pre-invocation-hook.sh", "[1, 2, ", env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert result.stderr.strip() != "", (
        "a malformed PreInvocation payload must leave a receipt on stderr (#568)"
    )


def test_pre_invocation_adapter_empty_object_stays_silent(tmp_path):
    scripts = _sandbox(tmp_path)
    delegate_stdin = tmp_path / "delegate-stdin.json"
    env = {**os.environ, "FAKE_DELEGATE_STDIN": str(delegate_stdin)}
    result = _run_raw(scripts / "agy-pre-invocation-hook.sh", "{}", env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert result.stderr.strip() == "", (
        f"an empty-but-valid PreInvocation payload must not warn -- got: {result.stderr!r}"
    )


def test_pre_invocation_adapter_valid_payload_no_warning(tmp_path):
    scripts = _sandbox(tmp_path)
    delegate_stdin = tmp_path / "delegate-stdin.json"
    env = {**os.environ, "FAKE_DELEGATE_STDIN": str(delegate_stdin)}
    result = _run_raw(scripts / "agy-pre-invocation-hook.sh", json.dumps({
        "workspacePaths": ["/proj"],
    }), env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert result.stderr.strip() == ""
