"""#668: the injected MEMORY section is a cache across SessionStart runs,
validated by mtime, rather than being re-read/re-sized/re-concatenated from
six files (plus the rotated-slice glob) on every single start.

Exercises `scripts/lib-memory-context.sh` directly rather than the whole
`session-start-hook.sh` process: the render, the cache load and the cache
publish are unit-testable bash functions, and doing so here keeps the
mtime-comparison guarantee (a stale/missing/tied manifest entry is ALWAYS a
miss, never served) pinned independently of anything else session start does.

Every write in this file backdates or forwards mtimes explicitly via
`os.utime`, rather than relying on wall-clock ordering across two writes
landing in the same or different seconds -- `-nt` compares whole seconds
(bash), and a test that lets that race decide which path it exercised proves
nothing about the path it claims to (the #303 lesson, `tests/env_cache.py`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash

# #432/#497: a blanket win32 skip makes the windows-latest leg collect these
# tests, skip every one, and report the leg green -- narrowed to the one
# real limitation instead (no usable bash on PATH at all), same route
# tests/test_autonomous_log_retention_487.py already uses.
BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib-memory-context.sh"
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"
LOG_SH = REPO_ROOT / "scripts" / "log.sh"


def _touch(path: Path, when: float, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (when, when))


def _run(script: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# A minimal bash harness: source the real chain (resolve-paths.sh, log.sh --
# for config() and _remember_date -- then lib-memory-context.sh), set up the
# paths, and expose the three calls this test drives as CLI subcommands so
# each assertion is one `bash -c` call with a clear exit code.
# .as_posix(), not a raw f-string interpolation of a Path object (#670): on
# Windows, str(Path(...)) is backslash-separated, and detect-tools.sh's own
# BASH_SOURCE-relative sourcing of lib-slug.sh
# (`_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"`) finds no '/' to strip in a
# fully-backslash path, falls through to its own "." fallback, and then
# fails to find ./lib-slug.sh relative to whatever cwd the subprocess
# happened to start in -- reproduced on CI (PR #670, all three
# windows-latest legs), matching the existing convention already used
# elsewhere in this repo for exactly this reason
# (tests/test_detect_tools_fatal_diagnostics_650.py, this file's own sibling
# tests/test_detect_tools_cache_668.py).
HARNESS = f"""
set -u
REMEMBER_PATHS_SOFT_FAIL=1 source "{RESOLVE_PATHS.as_posix()}" || exit 99
PLUGIN_ROOT="$PIPELINE_DIR"
source "{(REPO_ROOT / 'scripts' / 'detect-tools.sh').as_posix()}" || exit 99
source "{LOG_SH.as_posix()}" 2>/dev/null
source "{(REPO_ROOT / 'scripts' / 'lib-memory-context.sh').as_posix()}"
_remember_memory_paths
cmd="$1"
case "$cmd" in
    load) _remember_start_cache_context_load ;;
    publish) _remember_start_cache_context_publish ;;
    render) _remember_render_memory_section ;;
esac
"""


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    return home, project, remember


def _base_env(tmp_path: Path, home: Path, project: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_HOOK_CWD": str(project),
        "REMEMBER_ENV_CACHE": "0",
    }


def test_cache_miss_when_nothing_has_been_published_yet(tmp_path):
    """Positive control for the miss path: no cache/manifest exists at all,
    so the loader must refuse rather than silently print nothing and exit 0
    (which would be indistinguishable from "there is genuinely no memory")."""
    home, project, remember = _project(tmp_path)
    now = time.time()
    _touch(remember / "core-memories.md", now, "hello from core\n")
    env = _base_env(tmp_path, home, project)

    result = _run(HARNESS + '\nmain() { :; }\n', env)  # noop to keep harness parse-clean
    result = subprocess.run(
        [BASH, "-c", HARNESS, "_", "load"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stdout == ""


def test_publish_then_load_hits_and_matches_a_live_render(tmp_path):
    """The core positive case: publish once, and the very next load must hit
    and return byte-identical content to what a live render would produce."""
    home, project, remember = _project(tmp_path)
    now = time.time()
    _touch(remember / "core-memories.md", now, "hello from core\n")
    env = _base_env(tmp_path, home, project)

    publish = subprocess.run(
        [BASH, "-c", HARNESS, "_", "publish"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert publish.returncode == 0, (publish.stdout, publish.stderr)

    cache_file = remember / "tmp" / "start-context.cache"
    manifest_file = remember / "tmp" / "start-context.manifest"
    assert cache_file.is_file()
    assert manifest_file.is_file()
    # Force the cache strictly newer than every source it depends on,
    # independent of whatever second the two writes above landed in.
    os.utime(cache_file, (now + 5, now + 5))

    live = subprocess.run(
        [BASH, "-c", HARNESS, "_", "render"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert live.returncode == 0, (live.stdout, live.stderr)
    assert "hello from core" in live.stdout

    load = subprocess.run(
        [BASH, "-c", HARNESS, "_", "load"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert load.returncode == 0, (load.stdout, load.stderr)
    assert load.stdout == live.stdout


def test_editing_a_memory_file_invalidates_the_cache(tmp_path):
    """Negative-fires-must-not-serve-stale case: after a publish, editing one
    of the source memory files to a LATER mtime than the cache must flip the
    next load back to a miss -- never keep serving the old bytes."""
    home, project, remember = _project(tmp_path)
    now = time.time()
    core = remember / "core-memories.md"
    _touch(core, now, "version one\n")
    env = _base_env(tmp_path, home, project)

    publish = subprocess.run(
        [BASH, "-c", HARNESS, "_", "publish"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert publish.returncode == 0, (publish.stdout, publish.stderr)
    cache_file = remember / "tmp" / "start-context.cache"
    os.utime(cache_file, (now + 5, now + 5))

    # Sanity: cache hit before the edit.
    load_before = subprocess.run(
        [BASH, "-c", HARNESS, "_", "load"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert load_before.returncode == 0, (load_before.stdout, load_before.stderr)
    assert "version one" in load_before.stdout

    # Edit the memory file to a mtime strictly AFTER the cache.
    _touch(core, now + 10, "version two\n")

    load_after = subprocess.run(
        [BASH, "-c", HARNESS, "_", "load"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert load_after.returncode == 1, (
        "stale cache must be impossible to serve silently -- got a hit after "
        f"editing a source memory file: stdout={load_after.stdout!r}"
    )
    assert load_after.stdout == ""


def test_a_tied_mtime_between_cache_and_source_is_a_miss_not_a_hit(tmp_path):
    """#668's own guardrail: FAT/exFAT's 2s mtime granularity means an EQUAL
    mtime between the cache and a source is ambiguous, not confirmed-fresh,
    and must invalidate -- this is what bash's `-nt` already gives for a tie
    (false, never true), pinned here so a future rewrite to `-ge`-style logic
    trips this test rather than shipping silently."""
    home, project, remember = _project(tmp_path)
    now = time.time()
    core = remember / "core-memories.md"
    _touch(core, now, "version one\n")
    env = _base_env(tmp_path, home, project)

    publish = subprocess.run(
        [BASH, "-c", HARNESS, "_", "publish"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert publish.returncode == 0, (publish.stdout, publish.stderr)
    cache_file = remember / "tmp" / "start-context.cache"
    # Tie the cache's mtime to the SAME second as the source, exactly.
    tied = float(int(now))
    os.utime(cache_file, (tied, tied))
    os.utime(core, (tied, tied))

    load = subprocess.run(
        [BASH, "-c", HARNESS, "_", "load"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert load.returncode == 1, (
        "a tied mtime must be treated as a miss, never a hit -- "
        f"stdout={load.stdout!r}"
    )


def test_compact_mode_never_reads_or_writes_the_context_cache(tmp_path):
    """#668's compact carve-out: at SESSION_START_SOURCE=compact the render is
    a different, much smaller shape (identity only), so the cache must be
    skipped in BOTH directions -- a compact run must never serve a
    previously-published non-compact cache (it would show content compact
    mode is supposed to omit), and it must never publish its own smaller
    render into the cache (which would corrupt the NEXT ordinary start)."""
    home, project, remember = _project(tmp_path)
    now = time.time()
    _touch(remember / "core-memories.md", now, "hello from core\n")
    env = _base_env(tmp_path, home, project)

    # Publish a genuine non-compact cache first.
    publish = subprocess.run(
        [BASH, "-c", HARNESS, "_", "publish"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert publish.returncode == 0, (publish.stdout, publish.stderr)
    cache_file = remember / "tmp" / "start-context.cache"
    assert cache_file.is_file()
    os.utime(cache_file, (now + 5, now + 5))
    before_bytes = cache_file.read_bytes()

    compact_env = {**env, "SESSION_START_SOURCE": "compact"}

    # A compact "load" must never hit -- must always report a miss (rc 1),
    # never serve the non-compact cache's larger content.
    load = subprocess.run(
        [BASH, "-c", HARNESS, "_", "load"],
        env=compact_env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert load.returncode == 1, (
        "compact mode must never be served the cache -- "
        f"stdout={load.stdout!r}"
    )
    assert load.stdout == ""

    # A compact "publish" must never overwrite the existing cache -- the very
    # next ordinary (non-compact) start must still see the ORIGINAL content.
    publish_compact = subprocess.run(
        [BASH, "-c", HARNESS, "_", "publish"],
        env=compact_env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert publish_compact.returncode == 0, (publish_compact.stdout, publish_compact.stderr)
    after_bytes = cache_file.read_bytes()
    assert after_bytes == before_bytes, (
        "a compact-mode publish must never touch the cache a real session "
        "start reads from -- the file's bytes changed"
    )


def test_a_symlinked_cache_file_is_refused_not_followed(tmp_path):
    """Positive control for the security check itself, not just the mtime
    logic: a planted symlink at the cache path must never be read through --
    the loader must report a miss and fall back to a live render, exactly as
    if there were no cache at all."""
    home, project, remember = _project(tmp_path)
    now = time.time()
    _touch(remember / "core-memories.md", now, "real content\n")
    env = _base_env(tmp_path, home, project)

    publish = subprocess.run(
        [BASH, "-c", HARNESS, "_", "publish"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert publish.returncode == 0, (publish.stdout, publish.stderr)
    cache_file = remember / "tmp" / "start-context.cache"
    assert cache_file.is_file()

    # Replace the real cache with a symlink to an attacker-controlled file
    # carrying content that would never legitimately appear in this store.
    victim = tmp_path / "attacker-controlled.txt"
    victim.write_text("=== MEMORY ===\n--- forged.md ---\nnot really yours\n",
                       encoding="utf-8")
    os.utime(victim, (now + 5, now + 5))
    cache_file.unlink()
    os.symlink(victim, cache_file)

    load = subprocess.run(
        [BASH, "-c", HARNESS, "_", "load"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert load.returncode == 1, (
        "a symlinked cache file must be refused, not followed -- "
        f"stdout={load.stdout!r}"
    )
    assert "forged" not in load.stdout
