"""SessionStart must not hold its stdout pipe open until consolidation ends (#646).

The hook announces `=== MEMORY CONSOLIDATION ===` and launches
`run-consolidation.sh` detached, then returns within milliseconds. The reporter
measured the hook taking **98.62s** on a run where consolidation itself took 93s,
and 3.2-3.5s on every run where it did not fire. Claude Code's VS Code extension
gates subprocess initialization at 60s, so the editor refuses to start with
"Subprocess initialization did not complete within 60000ms -- check
authentication and network connectivity", pointing the user at credentials and
the network for what is neither.

The reporter could not isolate the mechanism and suspected a shared lock. It is
not a lock, and it is not a Git Bash detach failure (their own generic probe of
the same `nohup ... & disown` construct returned in 0.11s):

  scripts/session-start-hook.sh:1171  `exec 3>&1`

buffers the hook's own output to a temp file and keeps the REAL stdout -- the
pipe Claude Code reads -- alive on fd 3. The spawn at line ~1663 redirects only
fds 0, 1 and 2:

    nohup "$PLUGIN_ROOT/scripts/run-consolidation.sh" </dev/null >/dev/null 2>&1 & disown

so the consolidation child inherits fd 3 and holds the client's pipe open for its
entire life. The hook process exits immediately; the *reader* does not see EOF
until the last holder of the write end goes away, which is the child. That is
exactly the reporter's "the hook returned within ~2s of it completing".

Nothing here is Windows-specific -- the leak is POSIX fd inheritance and
reproduces on macOS and Linux. Windows only supplies the 60s deadline that turns
a long wait into a failed launch.

Two properties, one fixture:
  - EOF on the hook's stdout must arrive promptly, while consolidation runs
  - at that same moment the consolidation child must still be running
    (positive control: without it, a stub that crashed on startup would close
    the pipe early and pass the first assertion for the wrong reason)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX fd-inheritance semantics",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

SESSION = "ffffffff-0000-4000-8000-000000000646"
MARKER = "=== MEMORY CONSOLIDATION ==="

# How long the stubbed consolidation runs. Must comfortably exceed EOF_BUDGET so
# that "the child is still running when EOF arrives" is a real observation and
# not a race.
STUB_RUNTIME_S = 20
# The hook's own work, measured at 3.2-3.5s by the reporter on a cold Windows
# box. A budget well under STUB_RUNTIME_S but generous enough not to flake on a
# loaded CI runner: any value in between separates "returned on its own" from
# "waited for the child".
EOF_BUDGET_S = 10.0


def _fake_plugin_root(tmp_path, marker_dir):
    """A plugin root that is the real one, except run-consolidation.sh is slow.

    Every entry of the repo is symlinked in, so every `$PLUGIN_ROOT/...` the
    hook resolves is the real file; only `scripts/run-consolidation.sh` is
    replaced, by a stub that announces itself, sleeps, and announces again.
    """
    root = tmp_path / "plugin-root"
    (root / "scripts").mkdir(parents=True)
    for entry in REPO_ROOT.iterdir():
        if entry.name != "scripts":
            (root / entry.name).symlink_to(entry)
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.name != "run-consolidation.sh":
            (root / "scripts" / entry.name).symlink_to(entry)
    stub = root / "scripts" / "run-consolidation.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf started > "{marker_dir}/started"\n'
        f"sleep {STUB_RUNTIME_S}\n"
        f'printf finished > "{marker_dir}/finished"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return root


def _store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    # A stale (not-today) staging file is what the trigger looks for.
    (remember / "today-2020-01-01.md").write_text("STALE-STAGING-646\n", encoding="utf-8")
    return home, project, remember


def _payload():
    return json.dumps(
        {
            "session_id": SESSION,
            "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": "/does/not/matter",
        }
    )


def test_stdout_pipe_closes_while_consolidation_still_runs(tmp_path):
    home, project, remember = _store(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    plugin_root = _fake_plugin_root(tmp_path, marker_dir)

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }

    proc = subprocess.Popen(
        ["bash", str(SESSION_START)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    proc.stdin.write(_payload())
    proc.stdin.close()

    # Read to EOF, the way any client consuming a hook's stdout does. This is
    # the measurement: the write end stays open as long as ANY process holds a
    # dup of it, the hook's own exit notwithstanding.
    try:
        out = proc.stdout.read()
    finally:
        eof_elapsed = time.monotonic() - started

    # The child must be alive AFTER EOF -- otherwise a prompt EOF would prove
    # nothing about fd hygiene, since a child that died on startup closes the
    # inherited fd just as effectively as one that never inherited it.
    #
    # Sampling `started` at the exact instant of EOF is a race the fix itself
    # creates: once fd 3 is closed the hook returns in milliseconds, often
    # before the freshly `nohup`-ed child has been scheduled at all. So poll for
    # the marker instead of snapshotting it. `finished` cannot appear for
    # STUB_RUNTIME_S seconds, so "started present, finished absent" past a short
    # grace still means: the child outlived the hook and is running now.
    deadline = time.monotonic() + 5.0
    while not (marker_dir / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child_still_running = (marker_dir / "started").exists() and not (
        marker_dir / "finished"
    ).exists()

    err = proc.stderr.read()
    proc.wait(timeout=STUB_RUNTIME_S + 30)

    assert MARKER in out, (
        "consolidation never triggered, so this fixture measured nothing.\n"
        "stdout: " + out[:800] + "\nstderr: " + err[:800]
    )
    # Timing first: an EOF that arrived late is the defect itself, whatever the
    # child's state by then. The control below only guards the PASS direction.
    assert eof_elapsed < EOF_BUDGET_S, (
        f"the hook's stdout stayed open {eof_elapsed:.1f}s, past the "
        f"{EOF_BUDGET_S}s budget, tracking the backgrounded consolidation child's "
        f"own {STUB_RUNTIME_S}s runtime -- a client reading to EOF blocks for the "
        "child's whole run even though the hook process itself exited at once. "
        "This is #646: the child inherits fd 3, the dup of the real stdout made "
        "at session-start-hook.sh:1171, because the spawn redirects only fds 0, "
        "1 and 2."
    )
    assert child_still_running, (
        "EOF arrived inside the budget, but the stubbed consolidation child was "
        "not running at that moment, so the prompt EOF is not evidence of fd "
        "hygiene -- the child may simply have died. started="
        f"{(marker_dir / 'started').exists()} finished="
        f"{(marker_dir / 'finished').exists()}\nstderr: " + err[:800]
    )
