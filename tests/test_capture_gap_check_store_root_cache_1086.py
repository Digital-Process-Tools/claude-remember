"""#1086 review (item 4): capture-gap-check.sh's env-cache fast path
(lib-env-cache.sh's warm cache) never carried REMEMBER_STORE_ROOT at all, so
_remember_write_slug_index's own `[ -n "$REMEMBER_STORE_ROOT" ]` guard
silently no-opped for every external {slug} store on a direct/standalone
invocation -- the exact shape every test fixture in this repo already uses
to drive capture-gap-check.sh (execing it directly, without first execing
session-start-hook.sh in the SAME process, which is the only way the OTHER
fast path -- PROJECT_DIR/REMEMBER_DIR inherited by export -- ever gets
REMEMBER_STORE_ROOT for free).

TDD shape: a positive control (the legacy/single-directory layout, where
REMEMBER_STORE_ROOT is EMPTY by design, #297, and no index is ever written)
is paired with the actual claim (an external {slug} store DOES get a
session-index row once the cache is warm) -- the "must not fire" / "must
fire" pairing this repo's own CLAUDE.md requires for a negative assertion.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.slug import session_dir_slug as _slug

from ._bash_runner import resolve_bash
from .test_capture_gap_check_env_cache_1079 import (
    CAPTURE_GAP_CHECK,
    _warm_the_cache,
)

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found -- these tests drive the real hook scripts",
)

_SESSION_ID = "f1086000-0000-4000-8000-000000001086"


def _external_store_project_and_home(tmp_path: Path, *, with_store_root: bool):
    """`with_store_root=True`: an EXTERNAL {slug} store -- the shipped
    recommended layout (config.user.example.json's own
    "data_dir": "~/.remember/{slug}"), just rooted under this test's own tmp
    home instead of a real ~. `with_store_root=False`: the legacy,
    single-directory layout, where REMEMBER_STORE_ROOT is always empty
    (#297) -- the positive control this test is paired with.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    if with_store_root:
        store_root = home / "remember-ext"
        remember = store_root / _slug(str(project))
        data_dir = f"{store_root.as_posix()}/{{slug}}"
    else:
        remember = project / ".remember"
        data_dir = ".remember"
    cfg = home / ".remember" / "config.json"
    cfg.write_text(json.dumps({"data_dir": data_dir}), encoding="utf-8")
    # Same mtime convention as test_capture_gap_check_env_cache_1079.py's own
    # _project_and_home: the env cache's `-nt` check works only to the
    # SECOND, so a config written in the same wall-clock second as the cache
    # published moments later can read as "not older" and fail validation.
    past = time.time() - 120
    os.utime(cfg, (past, past))
    return project, remember, home


def _run_capture_gap_check_standalone(
    project: Path, remember: Path, env: dict,
) -> subprocess.CompletedProcess:
    """Exercise lib-env-cache.sh's own cache LOAD specifically -- not the
    OTHER fast path (#1069), which never touches this cache at all because it
    is satisfied directly by export inheritance from a real
    session-start-hook.sh fork, something a fresh `subprocess.run` here can
    never reproduce regardless of what this helper strips (#1069's own
    `if [ -n "${PROJECT_DIR:-}" ] ...` guard requires those three set in the
    PROCESS'S OWN environment, not merely known to the test). Same
    convention test_capture_gap_check_env_cache_1079.py's own
    `_run_capture_gap_check_shimmed` already uses, for the identical reason
    (see its own docstring): PROJECT_DIR/PIPELINE_DIR/REMEMBER_DIR are set
    directly, standing in for the fork's free export inheritance, while
    REMEMBER_RECOVERY_ENABLED is left UNSET so the #1069 branch falls
    through to `_remember_env_cache_load` instead of short-circuiting on an
    already-known answer.
    """
    env2 = {
        **env,
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
    }
    env2.pop("REMEMBER_RECOVERY_ENABLED", None)
    result = subprocess.run(
        [BASH, CAPTURE_GAP_CHECK.as_posix(), _SESSION_ID],
        env=env2, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (
        f"capture-gap-check.sh exited {result.returncode}: {result.stderr[-2000:]}"
    )
    return result


def test_an_external_slug_store_gets_a_session_index_row_from_the_warm_cache_path(tmp_path):
    project, remember, home = _external_store_project_and_home(tmp_path, with_store_root=True)
    systmp = tmp_path / "systmp"
    env = _warm_the_cache(project, home, systmp)

    cache_files = list(systmp.glob("remember-env-*"))
    assert cache_files, (
        "precondition: the warm run did not publish the env cache -- this "
        "test cannot exercise the cache-load fast path without it"
    )

    # _warm_the_cache's own session-start-hook.sh run (REMEMBER_DEFER=0)
    # already invoked capture-gap-check.sh itself, inline, forked DIRECTLY
    # from that process -- the OTHER fast path (PROJECT_DIR/REMEMBER_DIR
    # inherited by export), which resolves REMEMBER_STORE_ROOT correctly
    # regardless of this fix, and would already have written a real index
    # row. Removed here so the assertion below can only be satisfied by THIS
    # test's own standalone/cache-load invocation, below -- otherwise the
    # warm-up's own write would make this pass vacuously even with the fast
    # path's REMEMBER_STORE_ROOT restoration broken.
    store_root = remember.parent
    index = store_root / "tmp" / "sessions"
    if index.exists():
        index.unlink()

    _run_capture_gap_check_standalone(project, remember, env)

    assert index.exists(), (
        "an external {slug} store never got a session index row from "
        "capture-gap-check.sh's standalone/cache-load fast path -- "
        f"REMEMBER_STORE_ROOT was not restored from the warm env cache "
        f"(expected store root {store_root})"
    )
    body = index.read_text(encoding="utf-8")
    assert f"project_dir={project}" in body, (
        f"the session index exists but does not name this project: {body!r}"
    )


def test_a_positive_control_the_legacy_layout_never_writes_an_index_at_all(tmp_path):
    """Paired with the test above: without this, that assertion could be
    passing because capture-gap-check.sh writes an index unconditionally --
    not because REMEMBER_STORE_ROOT was genuinely restored from the cache.
    """
    project, remember, home = _external_store_project_and_home(tmp_path, with_store_root=False)
    systmp = tmp_path / "systmp"
    env = _warm_the_cache(project, home, systmp)

    _run_capture_gap_check_standalone(project, remember, env)

    index = remember / "tmp" / "sessions"
    assert not index.exists(), (
        "the legacy/single-directory layout (REMEMBER_STORE_ROOT always "
        f"empty, #297) wrote a session index anyway: {index}"
    )
