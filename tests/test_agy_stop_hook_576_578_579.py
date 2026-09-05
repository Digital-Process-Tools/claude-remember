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


_FAKE_PYTHON3_TEMPLATE = """#!/bin/bash
# Translates every LF the real interpreter writes on stdout into CRLF -- the
# way python3 writes lines on native Windows -- so the real #579 fix can be
# exercised on POSIX CI without a Windows runner. Same technique
# tests/test_config_dir_normalisation.py already uses for a different
# Windows-only branch: stub the platform-specific piece, run the real script.
exec "__REAL_PYTHON3__" "$@" | sed "s/$/$(printf '\\\\r')/"
"""


def test_windows_crlf_python3_output_is_stripped_before_reaching_save_session(tmp_path):
    """Self-review (oss:auditor) flagged the two tests above as too weak: they
    reimplement the fix's own `${VAR%$'\\r'}` line by hand rather than
    exercising it, so neither would fail if that line were deleted from
    agy-stop-hook.sh itself, and the whole module skips on win32 anyway (the
    same reason every bash-hook test in this repo does), so #579 as fixed had
    no path from ANY CI leg to the actual code it changed.

    This closes that gap the way tests/test_config_dir_normalisation.py
    already does for a different Windows-only branch (see its own module
    docstring): stub the platform-specific tool -- here, a `python3` that
    translates the real interpreter's LF line endings to CRLF -- and run the
    REAL script against it on POSIX CI. The branch under test is
    Windows-only; the logic is not.

    Without the fix (the four `${VAR%$'\\r'}` lines in agy-stop-hook.sh), this
    fake python3 would leave a trailing CR on _CONVERSATION_ID, which the
    #576 charset guard then rejects outright (CR is not in
    [A-Za-z0-9._-]) -- so a regression here does not merely leave a stray
    CR in the ARGV recorded below, it makes the hook silently stop calling
    save-session.sh at all, and the marker file this test waits for would
    never appear.

    read_text(newline="") is NOT a stylistic choice: plain read_text() applies
    Python's universal-newline translation, which silently rewrites CRLF to
    LF on the way in -- the very CR this test exists to catch would then be
    invisible to the assertion below. Confirmed live: this test passed
    against the UNFIXED hook (git show HEAD~1:scripts/agy-stop-hook.sh) with
    a plain read_text() call, even though `od -c` on the same marker file
    showed a real 0x0D byte right after the conversation id -- until the read
    was changed to newline=""."""
    scripts = _sandbox(tmp_path)
    marker = tmp_path / "fake-save-session-called.log"
    real_python = shutil.which("python3") or shutil.which("python")
    assert real_python, "no python3 (or python) found on PATH to wrap"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_python3 = fakebin / "python3"
    fake_python3.write_text(_FAKE_PYTHON3_TEMPLATE.replace("__REAL_PYTHON3__", real_python))
    fake_python3.chmod(0o755)
    env = {
        **os.environ,
        "FAKE_SAVE_SESSION_CALLED": str(marker),
        "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
    }
    result = _run_raw(scripts / "agy-stop-hook.sh", json.dumps({
        "conversationId": "0b04d3f2-c231-4ee0-8337-076e220bd1ad",
        "transcriptPath": "/some/real/transcript.jsonl",
        "workspacePaths": ["/some/project"],
    }), env)
    assert result.returncode == 0, subprocess_failure_detail(result, tmp_path)
    assert _wait_for(marker), (
        "save-session.sh was never called -- a CRLF-writing python3 left a "
        "trailing CR on conversationId, which the #576 charset guard then "
        "rejected, taking the hook's early exit instead of firing"
    )
    content = marker.read_text(newline="")
    assert "\x0d" not in content, f"a trailing CR survived into save-session.sh's own env/argv: {content!r}"
    assert "ARGV=0b04d3f2-c231-4ee0-8337-076e220bd1ad" in content
    assert "REMEMBER_TRANSCRIPT_PATH=/some/real/transcript.jsonl" in content
    assert "CLAUDE_PROJECT_DIR=/some/project" in content
