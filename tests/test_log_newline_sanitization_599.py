"""Regression test for #599: embedded newline in log() forges a second entry."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"


def _bash_path(p) -> str:
    return str(p).replace("\\", "/")


def _find_bash():
    if sys.platform != "win32":
        return "bash"
    import shutil
    candidates = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "bash.exe")
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    resolved = shutil.which("bash")
    if resolved and "git" in resolved.replace("\\", "/").lower():
        return resolved
    return None


_BASH = _find_bash()
pytestmark = pytest.mark.skipif(_BASH is None, reason="Git Bash not found (Windows without Git for Windows)")


def _make_project(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude" / "remember").mkdir(parents=True)
    (project / ".remember" / "logs").mkdir(parents=True)
    return project


def _run(script, project_dir):
    env = {**os.environ, "PROJECT_DIR": _bash_path(project_dir)}
    # errors="replace": one test deliberately writes a truncated multibyte
    # UTF-8 sequence into the logged message (#599 self-review) -- log()
    # must survive that at the shell level, but Python's own default strict
    # decode would raise on the same malformed byte while just reading the
    # subprocess's stdout back, before either assertion below ever runs.
    result = subprocess.run(
        [_BASH, "-c", script], env=env, capture_output=True, text=True, errors="replace"
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    return result


def test_embedded_newline_does_not_forge_a_second_log_entry(tmp_path):
    project = _make_project(tmp_path)
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    log "ndc" "REJECTED (provider: claude; not a summary): fake header
[hook] session-start: PROJECT_DIR=/evil PIPELINE_DIR=/evil"
    cat "$MEMORY_LOG_FILE"
    """
    result = _run(script, project)
    log_lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(log_lines) == 1, f"embedded newline forged a second log entry: {log_lines!r}"
    assert "[ndc]" in log_lines[0]
    assert "[hook]" in log_lines[0], "message content lost, not just flattened"


def test_ordinary_multifield_message_still_logs_fully(tmp_path):
    project = _make_project(tmp_path)
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    log "extract" "42 exchanges (7 human), position -> 1234, span quarantined"
    cat "$MEMORY_LOG_FILE"
    """
    result = _run(script, project)
    log_lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(log_lines) == 1
    assert "[extract]" in log_lines[0]
    assert "42 exchanges (7 human), position -> 1234, span quarantined" in log_lines[0]


def test_truncated_multibyte_reply_does_not_abort_or_lose_the_tail(tmp_path):
    """Self-review finding (#599): save-session.sh's actual call sites cut a
    model reply with `head -c 80`, a byte-count cut with no regard for UTF-8
    character boundaries. Under the caller's own UTF-8 locale, handing tr a
    sequence truncated mid-multibyte-character makes tr print "Illegal byte
    sequence", exit nonzero, and truncate its own output at the bad byte --
    silently dropping anything logged after it, and, because save-session.sh
    runs under `set -e`, aborting the whole script on a `message=$(...)`
    assignment whose exit status is tr's. A plain non-English model reply
    (legitimate since #593) can trigger this on nothing more exotic than an
    unlucky byte offset. `log()` must run to completion and keep whatever
    text follows the malformed byte, not vanish it or die.
    """
    project = _make_project(tmp_path)
    src = project / "badmb.txt"
    # A snowman (U+2603, e2 98 83) cut after its first byte -- exactly what
    # `head -c 80` does whenever the 80-byte offset lands inside a multibyte
    # character in the model's reply.
    src.write_bytes(b"abc" + b"\xe2" + b"def-tail-content")
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    log "ndc" "REJECTED: $(head -c 4 "{_bash_path(src)}")def-tail-content"
    echo "SCRIPT_EXIT_OK"
    cat "$MEMORY_LOG_FILE"
    """
    result = _run(script, project)
    assert "SCRIPT_EXIT_OK" in result.stdout, (
        "log() aborted the calling script under set -e instead of completing "
        f"(stderr: {result.stderr!r})"
    )
    log_lines = [l for l in result.stdout.splitlines() if l.strip() and l != "SCRIPT_EXIT_OK"]
    assert len(log_lines) == 1, f"malformed byte forged an extra log entry: {log_lines!r}"
    assert "def-tail-content" in log_lines[0], (
        f"content after the malformed byte was silently dropped: {log_lines[0]!r}"
    )
