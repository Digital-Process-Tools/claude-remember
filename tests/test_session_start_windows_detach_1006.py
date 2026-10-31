"""SessionStart's consolidation launch must go through the hidden Windows
launcher, not the bare `nohup ... & disown` line that still flashes a
console on Windows (#1002, deferred for this one site in #1006).

Dispatch decision at the REAL call site (session-start-hook.sh's own
staging-backlog branch), via the real scripts/consolidation-launch.sh --
not a reimplementation of either. Modeled on
test_windows_hidden_detach_1002.py's post-tool-hook.sh pair and
test_session_start_fd_leak_646.py's own plugin-root/env fixtures.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + stub-based Windows-route simulation",
)

SESSION = "ffffffff-0000-4000-8000-000000001006"
MARKER = "=== MEMORY CONSOLIDATION ==="


def _stub(path: Path, body: str) -> None:
    # #1002 CI: write with newline="" so a real Windows runner's universal-
    # newline translation never corrupts the shebang into "#!/bin/sh\r".
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    # A stale (not-today) staging file is what the trigger looks for.
    (remember / "today-2020-01-01.md").write_text("STALE-STAGING-1006\n", encoding="utf-8")
    return home, project, remember


def _fake_plugin_root(tmp_path, run_consolidation_body):
    """The real plugin root, with scripts/run-consolidation.sh replaced by
    a stub -- the real consolidation-launch.sh, lib-detach.sh and
    session-start-hook.sh all run unmodified against it."""
    root = tmp_path / "plugin-root"
    (root / "scripts").mkdir(parents=True)
    for entry in REPO_ROOT.iterdir():
        if entry.name != "scripts":
            (root / entry.name).symlink_to(entry)
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.name != "run-consolidation.sh":
            (root / "scripts" / entry.name).symlink_to(entry)
    stub = root / "scripts" / "run-consolidation.sh"
    stub.write_text(run_consolidation_body, encoding="utf-8")
    stub.chmod(0o755)
    return root


def _payload():
    import json

    return json.dumps(
        {
            "session_id": SESSION,
            "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": "/does/not/matter",
        }
    )


def test_session_start_consolidation_uses_hidden_launcher_on_windows(tmp_path):
    home, project, remember = _store(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured = tmp_path / "captured.txt"
    _stub(bindir / "wscript.exe", 'printf "%s\n" "$@" > "' + str(captured) + '"')
    _stub(bindir / "cygpath", 'shift; printf "%s" "$1"')
    marker = tmp_path / "ran.marker"
    plugin_root = _fake_plugin_root(
        tmp_path, f'#!/usr/bin/env bash\ntouch "{marker}"\n'
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "TMPDIR": str(tmp_path),
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "OS": "Windows_NT",
    }
    proc = subprocess.run(
        ["bash", str(SESSION_START)], input=_payload(), env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    # No sleep needed here (self-review finding, Explore): unlike
    # post-tool-hook.sh's own backgrounded nohup call, this whole chain
    # (session-start-hook.sh -> consolidation-launch.sh ->
    # _remember_detach_windows_impl) runs SYNCHRONOUSLY -- the function
    # itself `wait`s on the backgrounded wscript.exe before returning, so
    # by the time subprocess.run() above has returned, the stub has
    # already run and written captured.txt.
    assert MARKER in proc.stdout, proc.stdout + proc.stderr
    assert captured.exists(), (
        "wscript.exe stub was never invoked -- consolidation-launch.sh did "
        "not take the hidden route\n" + proc.stderr
    )
    assert not marker.exists(), (
        "the nohup fallback ran even though the hidden route was available "
        "and Windows was detected"
    )


def test_session_start_consolidation_uses_nohup_without_windows(tmp_path):
    """Positive control for the test above: with no Windows signal at all
    (the environment this suite actually runs under), the launch must go
    through the unchanged nohup fallback inside consolidation-launch.sh,
    and the wscript.exe stub -- present on PATH here too -- must NEVER be
    invoked."""
    home, project, remember = _store(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured = tmp_path / "captured.txt"
    _stub(bindir / "wscript.exe", 'printf "%s\n" "$@" > "' + str(captured) + '"')
    _stub(bindir / "cygpath", 'shift; printf "%s" "$1"')
    _stub(bindir / "uname", "echo Linux")
    marker = tmp_path / "ran.marker"
    plugin_root = _fake_plugin_root(
        tmp_path, f'#!/usr/bin/env bash\ntouch "{marker}"\n'
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "TMPDIR": str(tmp_path),
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "OS": "",
    }
    proc = subprocess.run(
        ["bash", str(SESSION_START)], input=_payload(), env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    deadline = time.monotonic() + 5.0
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert MARKER in proc.stdout, proc.stdout + proc.stderr
    assert marker.exists(), (
        "the nohup fallback never ran even though Windows was not "
        "detected -- the consolidation launch was lost entirely\n" + proc.stderr
    )
    assert not captured.exists(), (
        "wscript.exe stub was invoked even though Windows was not detected\n"
        + proc.stderr
    )
