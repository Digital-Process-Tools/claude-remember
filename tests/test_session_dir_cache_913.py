"""#913: SESSION_DIR (`claude_projects_dir() / session_dir_slug(PROJECT)`) was
recomputed on EVERY post-tool-hook.sh invocation, fast path included, because
the line sits below the `_REMEMBER_FAST` if/else rather than inside it. On
Windows/Git Bash each recomputation forks `cygpath` twice (once inside each
function) plus a `sed` -- measured at ~384 ms of a 986 ms per-tool-call trace
on a 260 MB transcript, independent of and additional to the transcript-size
cost #913 also reports for `wc -l`.

Neither `claude_projects_dir()` nor `session_dir_slug()` reads anything but
PROJECT_DIR and CLAUDE_CONFIG_DIR (and HOME/OSTYPE, which do not move inside a
session), so the pair any one invocation resolves to is the pair every other
invocation for the same project resolves to. The fix caches the resolved
SESSION_DIR in `$REMEMBER_DIR/tmp/session-dir-cache`, keyed on those same two
values, independent of `lib-env-cache.sh` (whose own invalidation does not
track CLAUDE_CONFIG_DIR at all).

No real Windows box is available to this suite (the repo's own convention:
every bash-subprocess test here is `skipif(win32)`), so this measures the
POSIX-portable half of the claim -- `command -v cygpath` finding a stub binary
that mimics the two calls at issue -- while the win32 skip leaves the native
Windows path to the reporter's own account in #913.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.test_post_tool_fast_path_350 import _env, _prime, _project, _reap, _run

# A passthrough stand-in for cygpath: no real Windows path exists in this
# POSIX test environment, so there is nothing to convert -- only whether it
# was CALLED matters here. Each invocation appends one line to $CYGPATH_LOG
# before echoing its argument back, same shape as tests/spawn_counting.py's
# shims but scoped to this one binary so the test owns its own log file
# rather than sharing make_shim_dir's COUNTED-command log.
CYGPATH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "cygpath $*" >> "$CYGPATH_LOG"
if [ "$1" = "-u" ] || [ "$1" = "-w" ]; then
    printf '%s\\n' "$2"
    exit 0
fi
exit 1
"""


def _with_cygpath_stub(tmp_path: Path, env: dict, log: Path) -> dict:
    bindir = tmp_path / "cygpath-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "cygpath"
    stub.write_text(CYGPATH_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    log.write_text("", encoding="utf-8")
    return {
        **env,
        "PATH": str(bindir) + os.pathsep + env["PATH"],
        "CYGPATH_LOG": str(log),
    }


def _cygpath_calls(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def test_a_second_warm_call_does_not_recompute_session_dir_via_cygpath(tmp_path):
    """The claim this test pins: with a stub `cygpath` standing in for the
    Windows binary the issue measured, a FIRST post-tool-hook.sh call for a
    project may legitimately call it (there is nothing cached yet) but a
    SECOND call against the same store must not call it again -- SESSION_DIR
    for this (PROJECT, CLAUDE_CONFIG_DIR) pair was already resolved and
    written to session-dir-cache.
    """
    home, project, remember = _project(tmp_path)
    env = _env(tmp_path, home, project)
    _prime(env)

    log = tmp_path / "cygpath-calls.log"
    stubbed_env = _with_cygpath_stub(tmp_path, env, log)

    first = _run(stubbed_env)
    assert first.returncode == 0, "first run failed: " + repr(first.stderr[:400])
    first_calls = _cygpath_calls(log)

    # POSITIVE CONTROL: the stub is actually being found and invoked by the
    # code path under test. Without this, an assertion of "zero calls on the
    # second run" would pass just as well if cygpath were never reachable at
    # all -- a positive-control-free negative assertion the house style
    # (CLAUDE.md) explicitly calls out.
    assert first_calls, (
        "the stub cygpath was never invoked on the FIRST run -- the harness "
        "is not exercising the code path this test claims to measure"
    )

    log.write_text("", encoding="utf-8")
    second = _run(stubbed_env)
    assert second.returncode == 0, "second run failed: " + repr(second.stderr[:400])
    second_calls = _cygpath_calls(log)
    _reap(remember)

    assert second_calls == [], (
        "a second post-tool-hook.sh call for the same (PROJECT, "
        "CLAUDE_CONFIG_DIR) pair re-invoked cygpath instead of replaying the "
        "cached SESSION_DIR: " + repr(second_calls)
    )

    cache_file = remember / "tmp" / "session-dir-cache"
    assert cache_file.exists(), (
        "session-dir-cache was never written, so the warm run above proved "
        "nothing about a cache that does not exist"
    )


def test_a_changed_config_dir_recomputes_rather_than_reusing_a_stale_cache(tmp_path):
    """The other direction of the same claim: the cache key is (PROJECT,
    CLAUDE_CONFIG_DIR), not PROJECT alone. Changing CLAUDE_CONFIG_DIR between
    two calls for the same project must still recompute -- a cache that
    ignored this would silently resolve the wrong session directory for
    anyone who runs more than one CLAUDE_CONFIG_DIR against the same
    project.
    """
    home, project, remember = _project(tmp_path)
    env = _env(tmp_path, home, project)
    _prime(env)

    log = tmp_path / "cygpath-calls.log"
    stubbed_env = _with_cygpath_stub(tmp_path, env, log)

    first = _run(stubbed_env)
    assert first.returncode == 0, "first run failed: " + repr(first.stderr[:400])
    assert _cygpath_calls(log), (
        "the stub cygpath was never invoked on the priming run -- the "
        "harness is not exercising the code path this test claims to measure"
    )

    other_config_dir = tmp_path / "other-claude-config"
    other_config_dir.mkdir()
    log.write_text("", encoding="utf-8")
    changed_env = {**stubbed_env, "CLAUDE_CONFIG_DIR": str(other_config_dir)}
    second = _run(changed_env)
    assert second.returncode == 0, "second run failed: " + repr(second.stderr[:400])
    second_calls = _cygpath_calls(log)
    _reap(remember)

    assert second_calls, (
        "CLAUDE_CONFIG_DIR changed between calls but the cache was reused "
        "anyway -- SESSION_DIR for the new config dir was never recomputed: "
        + repr(second_calls)
    )
