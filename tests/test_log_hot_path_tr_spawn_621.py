"""log() forks `tr` unconditionally on every call, even when the message
carries no control byte to flatten (#621).

#599 taught log() to flatten every control byte via
`printf '%s' "$message" | LC_ALL=C tr '[:cntrl:]' ' '` -- a `printf | tr`
pipeline, forked on EVERY log() call. log() runs on the per-tool-call hot
path (post-tool-hook.sh logs at least once per invocation, more with any
WARNING branch taken), and this repository already treats hot-path spawns as
a costed invariant elsewhere in the same file (see the comments around
`[ -d ]` before `mkdir -p` in post-tool-hook.sh, and the whole `_LIB_MEMORY_
DIR_LOADED` re-source guard). The overwhelming majority of log messages this
codebase actually writes contain no control byte at all -- component names,
static prose, numeric positions -- so the fork is wasted work on nearly
every call.

log() must flatten only when the message actually contains a control byte,
via a cheap in-shell check (no subprocess) ahead of the fork, and must still
flatten -- correctly, byte-for-byte identical to before -- whenever one is
present.
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


def _fake_tr_dir(tmp_path, ledger):
    """A directory prepended to PATH holding a `tr` that records being
    called, then delegates to the real one so log()'s actual output is
    unaffected -- isolates "was tr invoked" from "did flattening work"."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    stub = bindir / "tr"
    real_tr = subprocess.run(
        [_BASH, "-c", "command -v tr"], capture_output=True, text=True, check=False,
    ).stdout.strip()
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "called\\n" >> "{_bash_path(ledger)}"\n'
        f'exec "{real_tr}" "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bindir


def _run(script, project_dir, path_prefix):
    env = {
        **os.environ,
        "PROJECT_DIR": _bash_path(project_dir),
        "PATH": f"{path_prefix}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run(
        [_BASH, "-c", script], env=env, capture_output=True, text=True,
        errors="replace", check=False,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    return result


def test_an_ordinary_message_does_not_fork_tr(tmp_path):
    """MUST NOT FIRE: no control byte in the message -- log() must not pay
    for a printf|tr pipeline it does not need."""
    project = _make_project(tmp_path)
    ledger = tmp_path / "tr-calls.log"
    bindir = _fake_tr_dir(tmp_path, ledger)
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    log "extract" "42 exchanges (7 human), position -> 1234, span quarantined"
    cat "$MEMORY_LOG_FILE"
    """
    result = _run(script, project, _bash_path(bindir))
    assert not ledger.exists(), (
        f"log() forked tr for a message with no control byte: {result.stdout!r}"
    )
    assert "42 exchanges" in result.stdout


def test_a_message_with_a_control_byte_still_forks_tr_and_flattens(tmp_path):
    """MUST FIRE (positive control): a message that DOES carry a control
    byte must still be flattened -- proving the cheap pre-check does not
    just skip the fork unconditionally."""
    project = _make_project(tmp_path)
    ledger = tmp_path / "tr-calls.log"
    bindir = _fake_tr_dir(tmp_path, ledger)
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    log "ndc" "REJECTED (provider: claude; not a summary): fake header
[hook] session-start: PROJECT_DIR=/evil PIPELINE_DIR=/evil"
    cat "$MEMORY_LOG_FILE"
    """
    result = _run(script, project, _bash_path(bindir))
    assert ledger.exists(), "log() never invoked tr for a message with an embedded newline"
    log_lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(log_lines) == 1, f"embedded newline forged a second log entry: {log_lines!r}"
    assert "[hook] session-start" in log_lines[0]


_CONTROL_BYTE_CASES = [
    ("tab, 0x09", "\t"),
    ("carriage return, 0x0d", "\r"),
    ("DEL, 0x7f", chr(0x7f)),
    ("unit separator, 0x1f -- the top of the C0 range", chr(0x1f)),
]


@pytest.mark.parametrize("byte_desc,byte_char", _CONTROL_BYTE_CASES)
def test_every_control_byte_class_still_forks_tr(tmp_path, byte_desc, byte_char):
    """MUST FIRE (positive control, broadened). Context: #621's own
    regression, reported against a live windows-latest CI run (all 4 Python
    versions, only that leg): the ORIGINAL pre-check used a POSIX bracket
    CLASS to test for a control byte -- a ctype/locale table lookup a shell
    build can implement however it likes -- and it silently never matched a
    real embedded newline under Git Bash's MSYS2-built bash, while every
    Linux/macOS leg (and this repo's own macOS dev machine, on an old
    bash 3.2.57) stayed green. The fix replaced the bracket class with a
    literal byte-value range test instead, which needs no ctype table at
    all. This test widens coverage across the byte range the new pattern
    claims to cover -- not just newline -- so a future edit that narrows
    the range back down has more than one single byte value pinning it."""
    project = _make_project(tmp_path)
    ledger = tmp_path / "tr-calls.log"
    bindir = _fake_tr_dir(tmp_path, ledger)
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project)}"
    source "{_bash_path(LOG_SH)}"
    log "ndc" "before{byte_char}after"
    cat "$MEMORY_LOG_FILE"
    """
    result = _run(script, project, _bash_path(bindir))
    assert ledger.exists(), (
        f"log() never invoked tr for a message carrying {byte_desc}: {result.stdout!r}"
    )
    assert byte_char not in result.stdout, (
        f"{byte_desc} reached the log file unflattened: {result.stdout!r}"
    )
    assert "before" in result.stdout and "after" in result.stdout
