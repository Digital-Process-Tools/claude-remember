"""#973 -- the SESSION_DIR cache post-tool-hook.sh added for #913 is keyed on
$MEMORY_PROJECT_DIR, but the value it caches (SESSION_DIR) is computed from
$PROJECT. lib-memory-dir.sh deliberately makes MEMORY_PROJECT_DIR the SAME
value for every linked worktree of one repo (#56), precisely so memory is
shared rather than split per worktree -- but PROJECT stays worktree-specific.
Two worktrees of the same repo therefore share one session-dir-cache FILE
*and* one cache KEY, so whichever worktree wrote last wins for every reader:
worktree B can silently replay worktree A's SESSION_DIR.

Two properties, red before the fix:

  - worktree B's run, following worktree A's, must recompute SESSION_DIR
    rather than reuse A's cached value for a different project (the #973 bug)
  - positive control, same shape as #913's own tests: a FIRST call for either
    worktree, and a SECOND call for the SAME worktree immediately after, pin
    that the stub is reachable at all and that the #913 cache-hit behaviour
    (no recompute on a true repeat) still holds -- without this, "B
    recomputed" would prove nothing if cygpath were simply unreachable, and
    "A's warm call is still fast" would silently regress while #973 is fixed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.slug import session_dir_slug as _slug
from tests.env_cache import write_config
from tests.test_post_tool_fast_path_350 import TRANSCRIPT_LINE, _prime, _reap, _run
from tests.test_session_dir_cache_913 import _cygpath_calls, _with_cygpath_stub


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(project), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _worktree_pair(tmp_path: Path):
    """A main checkout plus two LINKED worktrees of it -- both redirect
    MEMORY_PROJECT_DIR to the same main checkout path (#56), while each
    keeps its own, distinct $PROJECT (the worktree path itself)."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "seed.txt").write_text("seed\n")
    _git(main, "add", "seed.txt")
    _git(main, "commit", "-q", "-m", "init")

    wt_a = tmp_path / "wt-a"
    wt_b = tmp_path / "wt-b"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", "-b", "a", str(wt_a)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", "-b", "b", str(wt_b)],
        check=True, capture_output=True,
    )
    return main, wt_a, wt_b


def _shared_home(tmp_path: Path, main: Path, wt_a: Path, wt_b: Path) -> Path:
    """One HOME used by both worktree sessions -- the ordinary case this bug
    hits: the same person, two worktrees of the same repo, same machine."""
    home = tmp_path / "home"
    (main / ".remember" / "tmp").mkdir(parents=True)
    write_config(
        main / ".remember" / "config.json",
        {"thresholds": {"delta_lines_trigger": 50}},
    )
    for wt in (wt_a, wt_b):
        session_dir = home / ".claude" / "projects" / _slug(str(wt))
        session_dir.mkdir(parents=True)
        (session_dir / "sess-1.jsonl").write_text(TRANSCRIPT_LINE * 10, encoding="utf-8")
    return home


def _wt_env(tmp_path: Path, home: Path, wt: Path) -> dict:
    tmpdir = tmp_path / "systmp"
    tmpdir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(wt),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "TMPDIR": str(tmpdir),
    }
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED", "REMEMBER_TZ",
                  "REMEMBER_NESTED_SUMMARIZER", "MEMORY_PROJECT_DIR"):
        env.pop(stale, None)
    return env


def test_worktree_b_recomputes_rather_than_replaying_worktree_as_session_dir(tmp_path):
    main, wt_a, wt_b = _worktree_pair(tmp_path)
    home = _shared_home(tmp_path, main, wt_a, wt_b)

    env_a = _wt_env(tmp_path, home, wt_a)
    env_b = _wt_env(tmp_path, home, wt_b)
    _prime(env_a)
    _prime(env_b)

    log = tmp_path / "cygpath-calls.log"
    stubbed_a = _with_cygpath_stub(tmp_path, env_a, log)
    stubbed_b = _with_cygpath_stub(tmp_path, env_b, log)

    # --- cold call for worktree A: nothing cached yet ---
    first_a = _run(stubbed_a)
    assert first_a.returncode == 0, "worktree A run failed: " + repr(first_a.stderr[:400])
    assert _cygpath_calls(log), (
        "the stub cygpath was never invoked on worktree A's first run -- the "
        "harness is not exercising the code path this test claims to measure"
    )

    # POSITIVE CONTROL (#913's own claim, must still hold): a SECOND call for
    # the SAME worktree must replay the cache, not recompute.
    log.write_text("", encoding="utf-8")
    second_a = _run(stubbed_a)
    assert second_a.returncode == 0, "worktree A warm run failed: " + repr(second_a.stderr[:400])
    assert _cygpath_calls(log) == [], (
        "a second post-tool-hook.sh call for the SAME worktree recomputed "
        "SESSION_DIR instead of replaying the cache -- the #913 fix this "
        "cache exists for has regressed: " + repr(_cygpath_calls(log))
    )

    # --- THE #973 CLAIM: worktree B, right after worktree A, must recompute
    # its own SESSION_DIR rather than silently reusing A's cached value.
    # MEMORY_PROJECT_DIR (and therefore the cache FILE) is shared between A
    # and B by design (#56); only PROJECT differs. A cache keyed on
    # MEMORY_PROJECT_DIR cannot tell the two apart and serves A's cached
    # SESSION_DIR to B's reader -- silently, with no error, because A's
    # session directory genuinely exists.
    log.write_text("", encoding="utf-8")
    first_b = _run(stubbed_b)
    assert first_b.returncode == 0, "worktree B run failed: " + repr(first_b.stderr[:400])
    b_calls = _cygpath_calls(log)
    _reap(main / ".remember")

    assert b_calls, (
        "worktree B's first post-tool-hook.sh call did not recompute "
        "SESSION_DIR -- it replayed worktree A's cached value instead, "
        "because the cache key (MEMORY_PROJECT_DIR) is shared across "
        "worktrees of one repo while the cached value (SESSION_DIR) is "
        "derived from the worktree-specific $PROJECT (#973): " + repr(b_calls)
    )
