"""Every detached spawn must let the client's pipe close (#648).

#646 was a hook that returned in milliseconds and still blocked its caller for
93 seconds: `session-start-hook.sh` had done `exec 3>&1`, the consolidation
spawn redirected only fds 0/1/2, and the child inherited fd 3 -- a dup of the
client's stdout pipe -- for its whole life. 223 test files did not catch it,
because every one of them already read the hook to EOF but none ever gave a
detached child a *lifetime*: in a fresh tmp store the child returned in
milliseconds, so the leak had nothing to hold the pipe open with.

Two checks close that, for every spawn site rather than the one that bit:

**Static.** `_fd_leaks()` reads a script, tracks every fd >= 3 that an `exec`
opens, and flags any later background spawn on the same file that does not
close it. It is tested against two inline samples -- the #646 shape (must flag)
and its fix (must pass) -- and then run over every shell script in the repo.

**Dynamic.** For each hook that detaches work, the work is replaced by a stub
that sleeps STUB_RUNTIME_S, and EOF on the hook's stdout must arrive inside
EOF_BUDGET_S while the stub is still running. The consolidation spawn is
covered by tests/test_session_start_fd_leak_646.py and SessionEnd's detach by
tests/test_session_end_detach_before_preamble_647.py; this file takes the other
three: PostToolUse's save, SessionStart's recovery save, and the Antigravity
Stop hook's save.

None of the dynamic cases fails today. That is the point: the static check is
what fails the moment someone opens an fd above a spawn without closing it, and
the dynamic cases are what fail when the leak takes a shape the static check
does not read -- a spawn in a sourced library, an fd opened by a function.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_recovery_transcript_leak_407 import (
    CURRENT,
    _startup_project,
)
from test_recovery_transcript_leak_407 import (
    _payload as _recovery_payload,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

STUB_RUNTIME_S = 8
EOF_BUDGET_S = 4.0

# --- static -----------------------------------------------------------------

_OPEN = re.compile(r"\bexec\b[^#\n]*?(?<![0-9])([3-9])>&(?!-)")
_CLOSE = re.compile(r"(?<![0-9])([3-9])>&-")
_SPAWN = re.compile(r"(?<!&)&\s*(disown\b.*)?$")


def _fd_leaks(text: str) -> list[str]:
    """Lines that background a command while an fd >= 3 opened by `exec` is
    still open and not closed on that same line."""
    open_fds: set[str] = set()
    findings = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0] if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        for fd in _OPEN.findall(line):
            open_fds.add(fd)
        if re.search(r"\bexec\b", line):
            for fd in _CLOSE.findall(line):
                open_fds.discard(fd)
        if open_fds and _SPAWN.search(line.rstrip()) and not line.rstrip().endswith("&&"):
            closed_here = set(_CLOSE.findall(line))
            leaked = sorted(open_fds - closed_here)
            if leaked:
                findings.append(f"line {lineno}: fd {','.join(leaked)} open, spawn does not close it: {raw.strip()}")
    return findings


LEAKING_SAMPLE = """#!/bin/bash
exec 3>&1
exec > "$buf"
nohup ./child.sh </dev/null >/dev/null 2>&1 & disown
exec 1>&3 3>&-
"""

FIXED_SAMPLE = LEAKING_SAMPLE.replace("2>&1 & disown", "2>&1 3>&- & disown")


def test_static_check_flags_the_646_shape():
    findings = _fd_leaks(LEAKING_SAMPLE)
    assert findings and "fd 3" in findings[0] and "line 4" in findings[0], findings


def test_static_check_accepts_the_646_fix():
    assert _fd_leaks(FIXED_SAMPLE) == []


def test_static_check_ignores_a_spawn_before_the_open():
    text = "nohup ./a.sh >/dev/null 2>&1 &\nexec 3>&1\n"
    assert _fd_leaks(text) == []


def test_static_check_ignores_a_spawn_after_the_close():
    text = "exec 3>&1\nexec 1>&3 3>&-\nnohup ./a.sh >/dev/null 2>&1 &\n"
    assert _fd_leaks(text) == []


def _all_shell_scripts():
    seen = []
    for sub in ("scripts", "hooks.d", "hooks"):
        base = REPO_ROOT / sub
        if base.is_dir():
            seen.extend(sorted(p for p in base.rglob("*.sh") if p.is_file()))
    return seen


@pytest.mark.parametrize("script", _all_shell_scripts(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_shell_script_spawns_with_an_inherited_extra_fd(script):
    findings = _fd_leaks(script.read_text(encoding="utf-8", errors="replace"))
    assert not findings, (
        f"{script.relative_to(REPO_ROOT)} backgrounds a command while an fd it "
        "opened with `exec` is still open. The child inherits it; if it is a dup "
        "of the hook's real stdout the client waits for the child (#646). Add "
        "`N>&-` to the spawn's redirects:\n  " + "\n  ".join(findings)
    )


# --- dynamic ------------------------------------------------------------------

_needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fd-inheritance semantics")


def _plugin_root(tmp_path, stub_name: str, marker_dir: Path) -> Path:
    """The real plugin root, with scripts/<stub_name> replaced by a sleeper."""
    root = tmp_path / "plugin-root"
    (root / "scripts").mkdir(parents=True)
    for entry in REPO_ROOT.iterdir():
        if entry.name != "scripts":
            (root / entry.name).symlink_to(entry)
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.name != stub_name:
            (root / "scripts" / entry.name).symlink_to(entry)
    stub = root / "scripts" / stub_name
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf started > "{marker_dir}/started"\n'
        f"sleep {STUB_RUNTIME_S}\n"
        f'printf finished > "{marker_dir}/finished"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return root


def _measure(argv, env, payload: str, marker_dir: Path):
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    proc.stdin.write(payload)
    proc.stdin.close()
    try:
        out = proc.stdout.read()
    finally:
        eof = time.monotonic() - started
    err = proc.stderr.read()
    proc.wait(timeout=STUB_RUNTIME_S + 30)
    deadline = time.monotonic() + 5
    while not (marker_dir / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child_running = (marker_dir / "started").exists() and not (marker_dir / "finished").exists()
    return eof, child_running, out, err


def _assert_detached(name, eof, child_running, out, err):
    assert eof < EOF_BUDGET_S, (
        f"{name}: stdout stayed open {eof:.1f}s against a {EOF_BUDGET_S}s budget "
        f"while its detached child sleeps {STUB_RUNTIME_S}s -- the client waits "
        "for the child. An inherited fd (#646 shape).\nstdout: " + out[:500] + "\nstderr: " + err[:500]
    )
    assert child_running, (
        f"{name}: EOF was prompt but the stub child was not running at that "
        "point, so the prompt EOF proves nothing (positive control)."
    )


@_needs_posix
def test_post_tool_save_spawn(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    (session_dir / "sess-1.jsonl").write_text(
        '{"type":"assistant","message":{"content":"x"}}\n' * 60)
    (remember / "config.json").write_text(json.dumps({"thresholds": {"delta_lines_trigger": 50}}))
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    root = _plugin_root(tmp_path, "save-session.sh", marker_dir)
    env = {**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project),
           "CLAUDE_PLUGIN_ROOT": str(root), "REMEMBER_DIR": str(remember), "_LIB_MEMORY_DIR_LOADED": "1"}
    eof, running, out, err = _measure(["bash", str(root / "scripts" / "post-tool-hook.sh")], env, "", marker_dir)
    _assert_detached("post-tool-hook.sh", eof, running, out, err)


@_needs_posix
def test_session_start_recovery_spawn(tmp_path):
    home, project, remember, _session_dir, current = _startup_project(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    root = _plugin_root(tmp_path, "save-session.sh", marker_dir)
    env = {**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project),
           "CLAUDE_PLUGIN_ROOT": str(root), "REMEMBER_DIR": str(remember), "_LIB_MEMORY_DIR_LOADED": "1"}
    eof, running, out, err = _measure(["bash", str(root / "scripts" / "session-start-hook.sh")], env,
                                      _recovery_payload(CURRENT, str(current)), marker_dir)
    _assert_detached("session-start-hook.sh (recovery save)", eof, running, out, err)


@_needs_posix
def test_agy_stop_save_spawn(tmp_path):
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    root = _plugin_root(tmp_path, "save-session.sh", marker_dir)
    payload = json.dumps({"conversationId": "0b04d3f2-c231-4ee0-8337-076e220bd1ad",
                          "transcriptPath": "/some/real/transcript.jsonl",
                          "workspacePaths": [str(tmp_path / "project")]})
    (tmp_path / "project").mkdir()
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    eof, running, out, err = _measure(["bash", str(root / "scripts" / "agy-stop-hook.sh")], env, payload, marker_dir)
    _assert_detached("agy-stop-hook.sh", eof, running, out, err)
