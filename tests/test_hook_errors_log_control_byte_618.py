"""hook-errors.log's four raw writers do not share #599's control-byte
flatten (#618).

#599 flattened every control byte (`LC_ALL=C tr '[:cntrl:]' ' '`) inside
log(), once, so an embedded newline or carriage return in an untrusted
message could no longer forge a second log entry in the daily narrative
file. `log.sh`'s own #599 coverage claim ("the class is closed everywhere
log() is used") is true of $MEMORY_LOG_FILE -- but four functions write a
SECOND, raw, unflattened copy of the same message straight to
$REMEMBER_DIR/logs/hook-errors.log via their own printf, entirely outside
log():

    _dispatch_report_failure   (log.sh, printf to hook-errors.log)
    _dispatch_report_skip      (log.sh, printf to hook-errors.log)
    report_error                (log.sh, printf to hook-errors.log)
    _dispatch_report_timeout   (log.sh, printf to hook-errors.log)

hook-errors.log is what `/remember:doctor` tails under "Recent errors" and
what maintainers ask reporters to paste (#252, #280, #326) -- exactly the
file #599's own rationale names as the one that matters. A hook name, an
exit reason, or a "how" string built from something outside this codebase's
control (a hook's own stderr tail, an argv value) can carry an embedded
newline the same way #599's own model-reply example did, and forge a second,
attacker-shaped entry in the file a human is told to trust and paste.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"


def _bash_path(p) -> str:
    return str(p).replace(chr(92), "/")


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
    if resolved and "git" in resolved.replace(chr(92), "/").lower():
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
    result = subprocess.run(
        [_BASH, "-c", script], env=env, capture_output=True, text=True,
        errors="replace", check=False,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    return result


def _hook_errors_lines(result) -> list:
    return [l for l in result.stdout.splitlines() if l.strip()]


@pytest.mark.parametrize("call,label", [
    ('_dispatch_report_failure "after_post_tool" "evil-hook" 1 "boom\n[hook] session-start: PROJECT_DIR=/evil PIPELINE_DIR=/evil"',
     "_dispatch_report_failure"),
    ('_dispatch_report_skip "after_post_tool" "evil-hook" "not owned\n[hook] session-start: PROJECT_DIR=/evil PIPELINE_DIR=/evil"',
     "_dispatch_report_skip"),
    ('report_error "dispatch" "boom\n[hook] session-start: PROJECT_DIR=/evil PIPELINE_DIR=/evil"',
     "report_error"),
    ('_dispatch_report_timeout "after_post_tool" "evil-hook" 5 "SIGTERM" "boom\n[hook] session-start: PROJECT_DIR=/evil PIPELINE_DIR=/evil"',
     "_dispatch_report_timeout"),
])
def test_an_embedded_newline_must_not_forge_a_second_hook_errors_log_entry(tmp_path, call, label):
    """MUST NOT FIRE: an embedded newline/CR reaching any of the four
    hook-errors.log writers must not read as a second, forged entry -- the
    same guarantee #599 already gives $MEMORY_LOG_FILE via log()."""
    project = _make_project(tmp_path)
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project)}"
    export REMEMBER_DIR="{_bash_path(project)}/.remember"
    source "{_bash_path(LOG_SH)}"
    {call}
    cat "$REMEMBER_DIR/logs/hook-errors.log"
    """
    result = _run(script, project)
    lines = _hook_errors_lines(result)
    assert len(lines) == 1, (
        f"[{label}] embedded newline forged a second hook-errors.log entry: "
        f"{lines!r}"
    )
    assert "[hook] session-start" in lines[0], (
        f"[{label}] message content lost, not just flattened: {lines[0]!r}"
    )


@pytest.mark.parametrize("call,label", [
    ('_dispatch_report_failure "after_post_tool" "some-hook" 1 "an ordinary reason"',
     "_dispatch_report_failure"),
    ('_dispatch_report_skip "after_post_tool" "some-hook" "world-writable"',
     "_dispatch_report_skip"),
    ('report_error "dispatch" "an ordinary error"',
     "report_error"),
    ('_dispatch_report_timeout "after_post_tool" "some-hook" 5 "SIGTERM" "an ordinary reply"',
     "_dispatch_report_timeout"),
])
def test_an_ordinary_message_still_reaches_hook_errors_log_intact(tmp_path, call, label):
    """MUST FIRE (positive control): an ordinary, single-line message must
    still reach hook-errors.log in full -- proving the assertion above is
    not passing merely because the harness never writes anything at all."""
    project = _make_project(tmp_path)
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project)}"
    export REMEMBER_DIR="{_bash_path(project)}/.remember"
    source "{_bash_path(LOG_SH)}"
    {call}
    cat "$REMEMBER_DIR/logs/hook-errors.log"
    """
    result = _run(script, project)
    lines = _hook_errors_lines(result)
    assert len(lines) == 1, f"[{label}] harness produced no single clean entry: {lines!r}"
    assert "some-hook" in lines[0] or "ordinary" in lines[0], (
        f"[{label}] the ordinary message never reached hook-errors.log: {lines[0]!r}"
    )
