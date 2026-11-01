"""save-session.sh's own sourcing preamble forked `dirname "$0"` six times
-- once per sibling library it sources -- on EVERY invocation (#1056, #660).

Under an ordinary single session this is noise; under the scenario #1056
actually reports (an app restart reconnecting 10-20 sessions in one project,
each one ending, each one flushing) it is six more process-creation attempts
PER SESSION competing for the same exhausted MSYS fork table that is already
producing "fork: retry: Resource temporarily unavailable" in this exact
script. session-start-hook.sh, session-end-hook.sh, post-tool-hook.sh and
user-prompt-hook.sh already replaced this pattern with parameter expansion
(#230) -- save-session.sh, the one script invoked by BOTH SessionStart
(recovery) and SessionEnd (the flush #1056 is about), still had the old form.

TDD shape per CLAUDE.md: a positive control (the shim harness can see
`dirname` when something still forks it) paired with the actual assertion
(a real, stubbed-pipeline run of save-session.sh no longer does).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from spawn_counting import make_shim_dir, spawns
from test_save_session_gates import _make_env, _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Git/bin/bash.exe prepends /usr/bin ahead of the shim dir, so "
    "coreutils spawns (dirname included) are not observable on Windows "
    "(#679) -- every test in this file is skipped there, unlike "
    "test_session_start_spawn_reduction_679.py's byte-identity test, which "
    "has no such dependency; this fix's own dirname-fork-count guard has no "
    "coverage on the one platform #1056/#660 are actually about.",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_SH = REPO_ROOT / "scripts" / "save-session.sh"


def _spawns_for_a_real_run(tmp_path: Path) -> list[str]:
    env, _project, plugin, _calls, sid = _make_env(tmp_path, exchanges=0, humans=0)
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    env = {**env, "SPAWN_LOG": str(log), "PATH": f"{shims}{os.pathsep}{env['PATH']}"}
    result = _run(plugin, env, sid)
    assert result.returncode == 0, result.stderr[-2000:]
    return spawns(log)


def test_positive_control_shim_sees_dirname_when_something_still_forks_it(tmp_path):
    """Proves the harness itself can observe a `dirname` fork -- run `dirname`
    directly through the same shim PATH the real test below uses. Without
    this, a shim dir that silently failed to intercept anything would make
    the assertion below pass for the wrong reason."""
    import subprocess

    log = tmp_path / "probe.log"
    shims = make_shim_dir(tmp_path, log)
    result = subprocess.run(
        ["bash", "-c", 'dirname "$1"', "_", "/a/b/c"],
        env={**os.environ, "SPAWN_LOG": str(log),
             "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0
    recorded = spawns(log)
    assert any(s.startswith("dirname ") for s in recorded), (
        f"positive control failed -- the shim harness never saw the dirname "
        f"call it was asked to make: {recorded}"
    )


def test_save_session_no_longer_forks_dirname_to_find_its_own_sibling_libraries_1056(tmp_path):
    recorded = _spawns_for_a_real_run(tmp_path)
    # Scoped to the sourcing block's own six calls (each named "dirname
    # .../save-session.sh", the script locating ITSELF) -- not lib-lock.sh's
    # unrelated `dirname "$_dir"` inside lock acquisition, which this issue
    # does not touch.
    dirname_calls = [
        s for s in recorded
        if s.startswith("dirname ") and s.rstrip().endswith("save-session.sh")
    ]
    assert not dirname_calls, (
        f"save-session.sh still forks dirname to locate its sibling libraries "
        f"-- six of these on every single invocation, SessionStart recovery "
        f"and SessionEnd flush alike (#1056/#660): {dirname_calls}"
    )


def test_save_session_still_sources_every_sibling_library(tmp_path):
    """Paired negative-result control: the fix must not just delete the
    source lines -- resolution has to still work, or the run above would
    exit non-zero for a completely different reason (missing log()/lock
    functions) and the dirname-count assertion would be moot."""
    recorded = _spawns_for_a_real_run(tmp_path)
    # A `date`/`jq` spawn appearing at all proves log.sh (which both define)
    # was actually sourced and ran, not merely that the process exited 0
    # before reaching anything meaningful.
    assert any(s.startswith("jq ") for s in recorded) or any(
        s.startswith("git ") for s in recorded
    ), f"save-session.sh exited 0 suspiciously early -- no sign it ran at all: {recorded}"
