"""#700: `_REMEMBER_PHASE` is assigned at three deferred-fork sites in
`session-start-hook.sh` and never `export`ed, so no child process spawned
from inside one of those subshells could ever see it -- the label the
comment on `_remember_deferred_phase` describes wanting ("a trace cannot
tell a deferred child from a command substitution by pid alone") never
reached anything a tracer could observe.

This pins the fix for the `before_session_start` deferral (line ~419): a
`hooks.d/before_session_start/` listener, dispatched as a real child process
from inside the `{ ... } & disown` subshell that sets `_REMEMBER_PHASE`, must
see it in its own environment.

Uses a full plugin copy (`_create_full_plugin_copy`, the same helper
`tests/test_dispatch_timeout_286.py` and `tests/test_path_resolution.py`
use) rather than the real repo's own `hooks.d/`, so the listener that
observes the environment can be a small stub instead of the real
`50-git-restore.sh` -- and so this test never depends on, or risks
disturbing, anything under the real `hooks.d/` tree. `git_restore.enabled`
is left at its shipped default (false, per `config.example.json`), which is
also the condition the deferral itself requires: `session-start-hook.sh`
only defers `before_session_start` when exactly one script sits in that
directory and it is named `50-git-restore.sh` (any other single-listener
install runs in the foreground, unlabelled, on purpose -- an unknown
listener's stdout may be context the hook still injects live).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_path_resolution import _create_full_plugin_copy, _create_full_project

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + disowned background fork -- not portable to Windows",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# How long the polled assertion waits for the disowned, backgrounded dispatch
# to actually run the listener and write the marker. Generous: a bound on a
# hang, not a measurement of anything (same convention as
# tests/test_capture_gap_notice.py's DEFERRED_TIMEOUT).
MARKER_TIMEOUT = 15

STUB_LISTENER = """#!/bin/bash
# Test stub standing in for hooks.d/before_session_start/50-git-restore.sh
# (tests/test_remember_phase_export_700.py). The real file's own name is
# required -- session-start-hook.sh only defers a lone before_session_start
# listener named exactly that (see this test module's own docstring).
if [ -n "${_REMEMBER_PHASE+set}" ]; then
    printf 'PHASE=%s\\n' "$_REMEMBER_PHASE" > "$REMEMBER_TEST_MARKER"
else
    printf 'PHASE=UNSET\\n' > "$REMEMBER_TEST_MARKER"
fi
"""


def _setup(tmp_path: Path):
    plugin = tmp_path / "plugin"
    project = tmp_path / "project"
    home = tmp_path / "home"
    plugin.mkdir()
    project.mkdir()
    home.mkdir()
    _create_full_plugin_copy(str(plugin))
    _create_full_project(str(project))

    listener = plugin / "hooks.d" / "before_session_start" / "50-git-restore.sh"
    listener.write_text(STUB_LISTENER, encoding="utf-8")
    listener.chmod(listener.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return plugin, project, home


def _env(plugin: Path, project: Path, home: Path, marker: Path) -> dict:
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "REMEMBER_TEST_MARKER": str(marker),
    }
    env.pop("REMEMBER_NESTED_SUMMARIZER", None)
    return env


def _payload(session_id: str = "sess-700") -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(Path("/does/not/matter")),
    })


def _wait_for_marker(marker: Path) -> bool:
    deadline = time.time() + MARKER_TIMEOUT
    while time.time() < deadline:
        if marker.is_file() and marker.read_text(encoding="utf-8").strip():
            return True
        time.sleep(0.1)
    return False


def test_deferred_before_session_start_dispatch_exports_the_phase(tmp_path):
    """The listener dispatched from the `{ _REMEMBER_PHASE=deferred; dispatch
    ... } & disown` subshell must see `_REMEMBER_PHASE=deferred` in its own
    environment -- that is the whole point of the label, per the comment on
    `_remember_deferred_phase`: a trace cannot otherwise tell a deferred
    child from a command substitution by pid alone.

    Would this test still pass if the code did nothing? No: before the fix,
    a plain (non-exported) `_REMEMBER_PHASE=deferred` never leaves the
    subshell that sets it, so the stub listener -- a genuinely separate
    process, `exec`'d by `dispatch()` -- observes an unset variable and
    writes `PHASE=UNSET`.
    """
    plugin, project, home = _setup(tmp_path)
    marker = tmp_path / "marker"

    result = subprocess.run(
        ["bash", str(plugin / "scripts" / "session-start-hook.sh")],
        input=_payload(), env=_env(plugin, project, home, marker),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (
        f"session-start-hook.sh exited {result.returncode}\\n"
        f"stdout: {result.stdout}\\nstderr: {result.stderr}"
    )

    assert _wait_for_marker(marker), (
        "the deferred before_session_start dispatch never ran the stub "
        "listener (or never wrote the marker) within "
        f"{MARKER_TIMEOUT}s -- either the deferral condition was not met "
        "(check git_restore.enabled and the listener's filename) or the "
        "dispatch itself failed"
    )
    assert marker.read_text(encoding="utf-8").strip() == "PHASE=deferred", (
        "the before_session_start listener, dispatched as a real child "
        "process from the deferred subshell, did not see _REMEMBER_PHASE "
        f"in its own environment -- got: {marker.read_text(encoding='utf-8')!r}"
    )
