"""SessionEnd must return before its own preamble, not after it (#647, #560).

Claude Code gives SessionEnd a **1.5-second budget shared across every hook on
the event**. #560 measured this hook's synchronous preamble -- `resolve-paths.sh`,
`detect-tools.sh`'s python/jq probing, `bootstrap-dirs.sh`, `log.sh` -- at ~3.4s on
a slow Windows/Git-Bash machine, so the hook was cancelled before it ever reached
the fork, on every exit. #561 declared `"timeout": 10` to raise the ceiling. But
Claude Code's own reference says plugin-declared timeouts do not raise it, and
`hooks/hooks.json` ships inside a plugin: on the install route both reporters are
on, that declaration may do nothing. #647's reporter, on 0.29.1 via
`claude-plugins-official`, sees `Hook cancelled` on every session exit.

The fix that does not depend on Claude Code's timeout semantics: the hook process
does nothing but read its stdin and hand the whole job -- preamble, trace seed,
flush -- to a detached child, then exits. Tens of milliseconds on any machine, on
any install route, under any budget.

The fixture: a plugin root that is the real one except `detect-tools.sh` sleeps
STUB_PREAMBLE_S before doing its real work. Before the fix the hook cannot return
until that sleep is over; after it, the sleep happens in the child.

Two properties:
  - EOF on the hook's stdout (the #646 measurement, not the exit code -- a
    client reads to EOF, and an inherited fd would hold it open) arrives well
    inside STUB_PREAMBLE_S
  - the flush still happens afterwards (positive control: a stubbed
    `save-session.sh` leaves a marker, so a hook that returned fast by simply
    not doing the work fails here)
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
    reason="bash hook subprocess + POSIX detach semantics",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

SESSION = "ffffffff-0000-4000-8000-000000000560"

# What the stubbed preamble costs. Comfortably above EOF_BUDGET_S so that a hook
# which waits for its preamble cannot pass by luck on a fast runner.
STUB_PREAMBLE_S = 6
# What the hook itself may take to return. Above the 1.5s budget Claude Code
# actually applies, on purpose: this is a loaded-CI-runner budget for "did not
# wait for the preamble", not a claim that the hook fits 1.5s everywhere.
EOF_BUDGET_S = 3.0
# How long the detached child may take to run the stubbed preamble and flush.
FLUSH_DEADLINE_S = STUB_PREAMBLE_S + 20


def _plugin_root(tmp_path, marker_dir):
    root = tmp_path / "plugin-root"
    (root / "scripts").mkdir(parents=True)
    for entry in REPO_ROOT.iterdir():
        if entry.name != "scripts":
            (root / entry.name).symlink_to(entry)
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.name in ("detect-tools.sh", "save-session.sh"):
            continue
        (root / "scripts" / entry.name).symlink_to(entry)
    # Slow preamble: sleep, then do exactly what the real one does.
    slow = root / "scripts" / "detect-tools.sh"
    slow.write_text(
        "#!/usr/bin/env bash\n"
        f"sleep {STUB_PREAMBLE_S}\n"
        f'source "{REPO_ROOT / "scripts" / "detect-tools.sh"}"\n',
        encoding="utf-8",
    )
    slow.chmod(0o755)
    # No-op flush that proves it ran.
    stub = root / "scripts" / "save-session.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf flushed > "{marker_dir}/flushed"\n'
        "exit 0\n",
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
    return home, project, remember


def test_hook_returns_before_its_preamble_and_still_flushes(tmp_path):
    home, project, remember = _store(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    root = _plugin_root(tmp_path, marker_dir)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(root),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    payload = json.dumps(
        {
            "session_id": SESSION,
            "reason": "other",
            "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
            "cwd": str(project),
        }
    )
    # Invoked THROUGH the fake root so `${BASH_SOURCE[0]%/*}` resolves the
    # sibling scripts -- including the slow detect-tools.sh -- from there.
    proc = subprocess.Popen(
        ["bash", str(root / "scripts" / "session-end-hook.sh")],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    proc.stdin.write(payload)
    proc.stdin.close()
    try:
        proc.stdout.read()
    finally:
        eof_elapsed = time.monotonic() - started
    err = proc.stderr.read()
    proc.wait(timeout=FLUSH_DEADLINE_S)

    assert proc.returncode == 0, err
    assert eof_elapsed < EOF_BUDGET_S, (
        f"SessionEnd's stdout stayed open {eof_elapsed:.1f}s against a "
        f"{EOF_BUDGET_S}s budget, tracking its own {STUB_PREAMBLE_S}s preamble. "
        "The hook process is waiting for resolve-paths / detect-tools / "
        "bootstrap-dirs / log.sh before it forks anything, so on a machine "
        "where that preamble runs past Claude Code's 1.5s shared SessionEnd "
        "budget it is cancelled before the flush exists (#560, #647). The "
        "preamble belongs in the detached child, not in the hook.\n"
        "stderr: " + err[:800]
    )

    deadline = time.monotonic() + FLUSH_DEADLINE_S
    while not (marker_dir / "flushed").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert (marker_dir / "flushed").exists(), (
        "the hook returned inside the budget but the flush never ran -- a hook "
        "that is fast because it does nothing is not the fix. Within "
        f"{FLUSH_DEADLINE_S}s of the hook returning, the stubbed "
        "save-session.sh should have left its marker.\nstderr: " + err[:800]
    )
    logs = sorted((remember / "logs" / "autonomous").glob("session-end-*.log"))
    assert logs, (
        "the flush ran but no session-end-*.log trace was written, so "
        "/remember:doctor would still report SessionEnd as never having fired."
    )
