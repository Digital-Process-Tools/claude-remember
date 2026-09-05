"""agy-stop-hook.sh: three hardening findings on the same four lines
(#576, #578, #579).

#576 -- _CONVERSATION_ID reaches save-session.sh as argv[1] with no shape
requirement, and save-session.sh's own arg loop treats a literal "--force"
or "--dry" as a FLAG regardless of where it came from, not as an invalid
session id (which is only checked later, and only for the value that
actually lands in the positional slot). A conversationId of "--force" would
silently flip FORCE=true, bypassing the cooldown/min-message gates.

#578 -- the four-field newline-delimited stdout protocol between the python
extractor and the shell (`sed -n Np`) has no escaping: an embedded newline
in one field shifts every field after it by one line.

#579 -- (Windows/Git-Bash, reasoned not observed) python3 -c 'print(...)'
writes CRLF there; command substitution only strips a *trailing* newline
from the whole captured stream, so every field but the last would carry a
trailing \r no caller expects.
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


def _sandbox(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    dst = scripts / "agy-stop-hook.sh"
    shutil.copyfile(REPO_ROOT / "scripts" / "agy-stop-hook.sh", dst)
    dst.chmod(0o755)
    (scripts / "save-session.sh").write_text(_FAKE_SAVE_SESSION)
    (scripts / "save-session.sh").chmod(0o755)
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


# --- #576: a flag-shaped conversationId must never reach argv ---

@pytest.mark.parametrize("bad_id", ["--force", "--dry", ".", "..", ""])
def test_flag_shaped_conversation_id_is_rejected(tmp_path, bad_id):
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run_raw(scripts / "agy-stop-hook.sh", json.dumps({
        "conversationId": bad_id,
        "transcriptPath": "/some/real/transcript.jsonl",
        "workspacePaths": ["/some/project"],
    }), env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    time.sleep(0.3)
    assert not marker.exists(), (
        f"conversationId={bad_id!r} must not reach save-session.sh's argv -- "
        "save-session.sh reads a bare --force/--dry as a FLAG, not a session id"
    )


def test_ordinary_conversation_id_is_not_rejected(tmp_path):
    """Positive control paired with the parametrized rejection above -- an
    ordinary UUID-shaped id must still reach save-session.sh unmolested."""
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run_raw(scripts / "agy-stop-hook.sh", json.dumps({
        "conversationId": "0b04d3f2-c231-4ee0-8337-076e220bd1ad",
        "transcriptPath": "/some/real/transcript.jsonl",
        "workspacePaths": ["/some/project"],
    }), env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert _wait_for(marker)
    content = marker.read_text()
    assert "ARGV=0b04d3f2-c231-4ee0-8337-076e220bd1ad" in content


# --- #578: an embedded newline in one field must not shift the others ---

def test_embedded_newline_in_conversation_id_does_not_shift_fields(tmp_path):
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run_raw(scripts / "agy-stop-hook.sh", json.dumps({
        "conversationId": "abc\ndef",
        "transcriptPath": "/some/real/transcript.jsonl",
        "workspacePaths": ["/some/project"],
    }), env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    # An embedded newline makes conversationId invalid on its own (rejected by
    # #576's guard too, since "\n" is outside the allowed charset) -- the
    # thing under test is that this must NEVER be observable as
    # transcriptPath/workspacePath having shifted into the wrong field. If
    # fields shifted, save-session.sh could be invoked with the wrong
    # transcript path/project dir instead of not being invoked at all.
    time.sleep(0.3)
    if marker.exists():
        content = marker.read_text()
        assert "REMEMBER_TRANSCRIPT_PATH=/some/real/transcript.jsonl" in content
        assert "CLAUDE_PROJECT_DIR=/some/project" in content


def test_wellformed_payload_extracts_fields_correctly(tmp_path):
    """Positive control paired with the shift test above."""
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    env = {**os.environ, "FAKE_SAVE_SESSION_CALLED": str(marker)}
    result = _run_raw(scripts / "agy-stop-hook.sh", json.dumps({
        "conversationId": "0b04d3f2-c231-4ee0-8337-076e220bd1ad",
        "transcriptPath": "/some/real/transcript.jsonl",
        "workspacePaths": ["/some/project"],
    }), env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert _wait_for(marker)
    content = marker.read_text()
    assert "ARGV=0b04d3f2-c231-4ee0-8337-076e220bd1ad" in content
    assert "REMEMBER_TRANSCRIPT_PATH=/some/real/transcript.jsonl" in content
    assert "CLAUDE_PROJECT_DIR=/some/project" in content


# --- #579: a trailing \r on an extracted field must be stripped ---

def test_trailing_cr_is_stripped_from_extracted_field():
    """Platform-independent unit test on the stripping step itself, per the
    brief: constructs a string with an embedded \\r the way a CRLF-writing
    python3 on Windows/Git-Bash would produce, and runs the exact bash
    parameter-expansion strip agy-stop-hook.sh now applies."""
    script = """
value=$'0b04d3f2-c231-4ee0-8337-076e220bd1ad\\r'
value="${value%$'\\r'}"
printf '%s' "$value"
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == "0b04d3f2-c231-4ee0-8337-076e220bd1ad"
    assert "\r" not in result.stdout


def test_field_with_no_cr_is_unaffected_by_stripping():
    """Positive control for the stripping test above -- a field with no
    trailing \\r must pass through unchanged."""
    script = """
value="0b04d3f2-c231-4ee0-8337-076e220bd1ad"
value="${value%$'\\r'}"
printf '%s' "$value"
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == "0b04d3f2-c231-4ee0-8337-076e220bd1ad"
