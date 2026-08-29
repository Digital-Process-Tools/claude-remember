"""#469 -- the #227 env-cache fast path is permanently dead on Codex and
Gemini CLI, because ``_remember_env_cache_path`` keys the cache file on the
raw ``CLAUDE_PROJECT_DIR``, and neither host ever sets it. #411/#444 already
gave every hook a ``REMEMBER_HOOK_CWD`` fallback for the exact same absence
-- this is that fallback missing from the one place a *fast-path* caller can
still supply it before a full resolution has run.

`scripts/resolve-paths.sh:270` exports the RESOLVED `CLAUDE_PROJECT_DIR`
before `_remember_env_cache_publish` writes the cache, so on those hosts a
cache file *is* written on every slow-path run -- and can never be found by
`_remember_env_cache_load`, which runs before resolution, in a fresh process
whose environment carries neither variable yet. Written every time, hit
never.

The bar here (per the issue) is a HIT-RATE assertion, not a write assertion:
a test that only checks the cache file exists on disk after a cold run
passes today, on the unfixed code, and says nothing about the bug.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess assertions -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_ENV_CACHE = REPO_ROOT / "scripts" / "lib-env-cache.sh"

# A minimal, self-contained simulation of one hook run: no CLAUDE_PROJECT_DIR
# (Codex/Gemini never set it), only REMEMBER_HOOK_CWD -- exactly the shape
# user-prompt-hook.sh and post-tool-hook.sh already offer from their own
# stdin `cwd` (#411, #444). Mirrors what resolve-paths.sh itself does on a
# successful resolution: it exports CLAUDE_PROJECT_DIR="$PROJECT_DIR" before
# anything publishes (resolve-paths.sh:270), so a real publish call always
# sees CLAUDE_PROJECT_DIR set -- this script reproduces that ordering by hand
# instead of sourcing the whole chain, to isolate the cache-key defect alone.
_RUN_ONE = r"""
source "{lib}"
_remember_env_cache_load
_rc=$?
echo "LOAD_RC=$_rc"
if [ "$_rc" != "0" ]; then
    # Simulate resolve-paths.sh having just resolved PROJECT_DIR from
    # REMEMBER_HOOK_CWD and exported it, then simulate log.sh's answers,
    # then publish -- the real sequence every slow-path hook run follows.
    export CLAUDE_PROJECT_DIR="$REMEMBER_HOOK_CWD"
    export CLAUDE_PLUGIN_ROOT="{pipeline}"
    PROJECT_DIR="$REMEMBER_HOOK_CWD"
    PIPELINE_DIR="{pipeline}"
    REMEMBER_DIR="{remember_dir}"
    REMEMBER_TZ="UTC"
    REMEMBER_PROMPT_STAMP="full"
    REMEMBER_SAVE_COOLDOWN="120"
    REMEMBER_DELTA_THRESHOLD="50"
    MEMORY_PROJECT_DIR="$PROJECT_DIR"
    _remember_env_cache_publish
    echo "PUBLISHED=1"
else
    echo "PROJECT_DIR=$PROJECT_DIR"
fi
"""


def _run(env, tmp_home, tmp_project, pipeline_dir, remember_dir):
    script = _RUN_ONE.format(lib=LIB_ENV_CACHE, pipeline=pipeline_dir, remember_dir=remember_dir)
    full_env = {
        **env,
        "HOME": str(tmp_home),
        "TMPDIR": str(tmp_home / "tmp"),
        "REMEMBER_HOOK_CWD": str(tmp_project),
        # Set on EVERY invocation, exactly as Codex sets it (resolve-paths.sh's
        # own ENVIRONMENT block: "also set by Codex as a compatibility alias")
        # -- it is CLAUDE_PROJECT_DIR that neither host ever sets, not this.
        "CLAUDE_PLUGIN_ROOT": str(pipeline_dir),
    }
    full_env.pop("CLAUDE_PROJECT_DIR", None)
    return subprocess.run(
        ["bash", "-c", script], env=full_env, cwd=str(tmp_project),
        capture_output=True, text=True, timeout=30, check=False,
    )


def test_second_invocation_with_no_claude_project_dir_hits_the_cache(tmp_path):
    """The bar the issue names: two consecutive invocations, CLAUDE_PROJECT_DIR
    unset throughout, only a stdin-sourced REMEMBER_HOOK_CWD -- the second
    invocation must be a cache HIT (LOAD_RC=0), not merely a cache write."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "tmp").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    pipeline = tmp_path / "pipeline"
    (pipeline / "pipeline").mkdir(parents=True)
    (pipeline / "pipeline" / "haiku.py").write_text("", encoding="utf-8")
    remember_dir = tmp_path / "remember"
    remember_dir.mkdir()

    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT")}

    first = _run(env, home, project, pipeline, remember_dir)
    assert first.returncode == 0, first.stderr
    assert "LOAD_RC=1" in first.stdout, (
        "first invocation should be a cold miss (nothing published yet): "
        + first.stdout + first.stderr
    )
    assert "PUBLISHED=1" in first.stdout, first.stdout + first.stderr

    second = _run(env, home, project, pipeline, remember_dir)
    assert second.returncode == 0, second.stderr
    assert "LOAD_RC=0" in second.stdout, (
        "second invocation, same REMEMBER_HOOK_CWD, no CLAUDE_PROJECT_DIR "
        "anywhere: must be a cache HIT, not a re-resolution. A test that "
        "only checks the cache file was written would pass on unfixed code; "
        "this asserts the hit itself. Got: " + second.stdout + second.stderr
    )
    assert f"PROJECT_DIR={project}" in second.stdout, second.stdout + second.stderr


def test_different_hook_cwd_is_a_correctly_keyed_miss(tmp_path):
    """Positive control for the hit assertion above: a DIFFERENT project's
    REMEMBER_HOOK_CWD must not accidentally hit a cache published for the
    first one -- proving the harness can tell hit from miss at all, rather
    than a broken harness that reports every run as a hit."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "tmp").mkdir()
    project_a = tmp_path / "project_a"
    project_a.mkdir()
    project_b = tmp_path / "project_b"
    project_b.mkdir()
    pipeline = tmp_path / "pipeline"
    (pipeline / "pipeline").mkdir(parents=True)
    (pipeline / "pipeline" / "haiku.py").write_text("", encoding="utf-8")
    remember_dir = tmp_path / "remember"
    remember_dir.mkdir()

    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT")}

    first = _run(env, home, project_a, pipeline, remember_dir)
    assert first.returncode == 0, first.stderr
    assert "PUBLISHED=1" in first.stdout, first.stdout + first.stderr

    second = _run(env, home, project_b, pipeline, remember_dir)
    assert second.returncode == 0, second.stderr
    assert "LOAD_RC=1" in second.stdout, (
        "a different REMEMBER_HOOK_CWD must not hit project_a's cache: "
        + second.stdout + second.stderr
    )
