"""#668: the flattened `_RCFG_*` config table `_config_load` (log.sh) builds
is cached across process invocations, keyed on the three config layers'
mtimes -- rather than forking `jq` (or Python, without jq) fresh on the
first `config()` call of every single hook process.

The cache is deliberately of the FLATTENED dump, not the raw merged
config.json: the raw merge can carry a live `haiku.oauth_token` and is
already a short-lived scratch file deleted at process exit
(lib-memory-dir.sh's own comment); a persistent cache of it would extend
that secret's on-disk lifetime. Both flatteners already drop the whole
"haiku" key before emitting a row, so the persisted cache never sees it.

#682: the cache used to live at `$REMEMBER_DIR/tmp/config.rcfg` -- inside
the PROJECT tree, a directory users commit and share -- and was loaded with
a bare `source`. A repository could ship that file and have every hook that
sources log.sh execute its contents as shell. The tests below cover both
halves of the fix: the cache moved to the system temp dir (never inside a
clonable project directory), keyed on `REMEMBER_DIR` so two projects never
share a file; and the loader validates every line's shape before assigning
any of it, rather than trusting `source` with an unread file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash
from config_cache import CACHE_GLOB, cache_files
from spawn_counting import make_shim_dir, spawns

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"
DETECT_TOOLS = REPO_ROOT / "scripts" / "detect-tools.sh"
BOOTSTRAP_DIRS = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"
LOG_SH = REPO_ROOT / "scripts" / "log.sh"

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no usable bash found")


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    return home, project, remember


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
set -eu
REMEMBER_PATHS_SOFT_FAIL=1 source "{RESOLVE_PATHS.as_posix()}" || exit 99
PLUGIN_ROOT="$PIPELINE_DIR"
source "{DETECT_TOOLS.as_posix()}" || exit 99
source "{BOOTSTRAP_DIRS.as_posix()}" || exit 99
source "{LOG_SH.as_posix()}" 2>/dev/null
config ".cooldowns.save_seconds" "default"
"""


def _run_with_shim(tmp_path: Path, home: Path, project: Path, *, sys_tmp: Path | None = None):
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    # A dedicated, per-test system tmp dir (#682): the cache now lives under
    # `${TMPDIR:-/tmp}`, so a test that left TMPDIR at its real system value
    # would read and write a real, shared, cross-test location -- isolate it
    # the same way tests/test_detect_tools_cache_668.py already does for the
    # sibling tools cache.
    sys_tmp = sys_tmp if sys_tmp is not None else (tmp_path / "systmp")
    sys_tmp.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_HOOK_CWD": str(project),
        "SPAWN_LOG": str(log),
        "TMPDIR": str(sys_tmp),
        "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(
        [BASH, "-c", HARNESS], env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    return spawns(log), result, sys_tmp


def test_first_run_is_a_miss_and_forks_jq_to_flatten(tmp_path):
    """Positive control: with no cache yet, config() must genuinely fork the
    flattener (jq, on a host that has it) -- a "zero forks" result here would
    prove the harness never really touched the flatten path at all."""
    home, project, remember = _project(tmp_path)
    (remember / "config.json").write_text(
        json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8"
    )
    lines, result, sys_tmp = _run_with_shim(tmp_path, home, project)
    assert result.returncode == 0, (result.stdout, result.stderr)
    jq_spawns = [l for l in lines if l.startswith("jq ")]
    assert jq_spawns, (
        "positive control failed: no jq fork observed on the first (cold) "
        f"run -- got spawns: {lines}"
    )
    assert cache_files(sys_tmp), (
        f"no flattened-config cache ({CACHE_GLOB}) was published under {sys_tmp}"
    )
    # #682: the cache must never land back inside the project tree.
    assert not (remember / "tmp" / "config.rcfg").exists()


def test_second_run_with_unchanged_config_skips_the_flatten_fork(tmp_path):
    """Core case: after one run has published the cache, a second run against
    the SAME config layers must not fork jq/python to reflatten at all."""
    home, project, remember = _project(tmp_path)
    cfg = remember / "config.json"
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8")

    _lines1, result1, sys_tmp = _run_with_shim(tmp_path, home, project)
    assert result1.returncode == 0, (result1.stdout, result1.stderr)
    caches = cache_files(sys_tmp)
    assert caches, f"no flattened-config cache ({CACHE_GLOB}) was published under {sys_tmp}"
    cache = caches[0]
    # Force the cache strictly newer than the config layer, independent of
    # which second the two writes above landed in.
    now = time.time()
    os.utime(cache, (now + 5, now + 5))

    lines2, result2, _sys_tmp2 = _run_with_shim(tmp_path, home, project, sys_tmp=sys_tmp)
    assert result2.returncode == 0, (result2.stdout, result2.stderr)
    assert result2.stdout == result1.stdout
    # `jq -s reduce ...` is lib-memory-dir.sh's own three-layer MERGE, which
    # runs on every process regardless of this cache (REMEMBER_CONFIG is a
    # fresh mktemp target every time) -- narrow to the FLATTEN program
    # specifically (its `paths(` signature), the one call #668 removes.
    flatten_spawns_2 = [l for l in lines2 if l.startswith("jq ") and "paths(" in l]
    py_flatten_spawns_2 = [
        l for l in lines2 if l.startswith(("python3 ", "python ")) and "walk(node" in l
    ]
    assert not flatten_spawns_2, f"cache hit must skip the jq reflatten: {lines2}"
    assert not py_flatten_spawns_2, f"cache hit must skip the python reflatten: {lines2}"


def test_editing_the_config_invalidates_the_flatten_cache(tmp_path):
    """Negative-fires-must-not-serve-stale case: after publishing a cache,
    editing the project config.json (a real value change) must flip the next
    config() call back to a miss -- never keep serving the old value."""
    home, project, remember = _project(tmp_path)
    cfg = remember / "config.json"
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8")

    _lines1, result1, sys_tmp = _run_with_shim(tmp_path, home, project)
    assert result1.returncode == 0, (result1.stdout, result1.stderr)
    assert "42" in result1.stdout
    caches = cache_files(sys_tmp)
    assert caches
    cache = caches[0]
    now = time.time()
    os.utime(cache, (now + 5, now + 5))

    # Edit the config to a mtime strictly AFTER the cache, with a new value.
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 99}}), encoding="utf-8")
    os.utime(cfg, (now + 10, now + 10))

    _lines2, result2, _sys_tmp2 = _run_with_shim(tmp_path, home, project, sys_tmp=sys_tmp)
    assert result2.returncode == 0, (result2.stdout, result2.stderr)
    assert "99" in result2.stdout, (
        "stale cache must be impossible to serve silently -- expected the "
        f"NEW value after editing config.json: {result2.stdout!r}"
    )


def test_haiku_key_never_reaches_the_persisted_cache(tmp_path):
    """Security property the cache design depends on, stated in both this
    file's own docstring and log.sh's own comment on the cache: a live
    haiku.oauth_token in config.json must never be written into the
    persisted config cache, which survives long past the single process the
    raw merged config.json scratch file is deleted at exit of. Both
    flatteners drop the whole "haiku" key before a row is ever emitted --
    this pins that guarantee against the PERSISTED file specifically, not
    just against config()'s own in-process read."""
    home, project, remember = _project(tmp_path)
    cfg = remember / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "cooldowns": {"save_seconds": 42},
                "haiku": {"oauth_token": "sk-super-secret-do-not-persist-me"},
            }
        ),
        encoding="utf-8",
    )

    _lines, result, sys_tmp = _run_with_shim(tmp_path, home, project)
    assert result.returncode == 0, (result.stdout, result.stderr)

    caches = cache_files(sys_tmp)
    assert caches
    cache_text = caches[0].read_text(encoding="utf-8")
    assert "oauth_token" not in cache_text, (
        "the haiku.oauth_token key leaked into the persisted config cache: "
        f"{cache_text!r}"
    )
    assert "sk-super-secret-do-not-persist-me" not in cache_text, (
        "the haiku oauth token VALUE leaked into the persisted config cache: "
        f"{cache_text!r}"
    )


def test_a_symlinked_config_cache_is_refused_not_followed(tmp_path):
    """Positive control for the -L/-O checks themselves: a planted symlink at
    the cache's real (new, #682) location must never be sourced -- the
    loader must fall back to a live reflatten rather than executing an
    attacker-controlled file as shell."""
    home, project, remember = _project(tmp_path)
    cfg = remember / "config.json"
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8")

    _lines, result, sys_tmp = _run_with_shim(tmp_path, home, project)
    assert result.returncode == 0, (result.stdout, result.stderr)
    caches = cache_files(sys_tmp)
    assert caches
    cache = caches[0]

    victim = tmp_path / "attacker-controlled.rcfg"
    victim.write_text("touch /tmp/pwned-668-poc\n_RCFG_cooldowns_save_seconds=666\n",
                       encoding="utf-8")
    cache.unlink()
    os.symlink(victim, cache)

    _lines2, result2, _sys_tmp2 = _run_with_shim(tmp_path, home, project, sys_tmp=sys_tmp)
    assert result2.returncode == 0, (result2.stdout, result2.stderr)
    assert "666" not in result2.stdout, (
        "a symlinked config cache was sourced instead of refused -- "
        f"stdout={result2.stdout!r}"
    )
    assert not Path("/tmp/pwned-668-poc").exists(), (
        "the symlinked cache's shell content actually executed"
    )
    assert "42" in result2.stdout


def test_planted_cache_in_old_project_path_is_never_executed(tmp_path):
    """#682's core reproduction. Before the fix, `_remember_cfg_flatten_
    cache_load` did `source "$REMEMBER_DIR/tmp/config.rcfg"` -- a path
    INSIDE the project tree, which a cloned repository can ship. `-O`
    (owned by the current user) passes for a file the user's own `git
    clone` wrote; `-L` passes for a regular file; `-nt` against an absent
    `.remember/config.json` (the common case with no project-level config)
    reads as "fresh". Planting a file there and running it through the
    real, unmodified detect-tools.sh -> bootstrap-dirs.sh -> log.sh chain
    must never execute its contents, no matter where the cache used to live.
    """
    home, project, remember = _project(tmp_path)
    cfg = remember / "config.json"
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8")

    marker = tmp_path / "pwned-682-marker"
    planted = remember / "tmp" / "config.rcfg"
    planted.write_text(
        f'touch "{marker.as_posix()}"\n_RCFG_cooldowns_save_seconds=42\n',
        encoding="utf-8",
    )

    # Positive control FIRST, and independent of the guard under test: the
    # marker-based assertion below is only meaningful if sourcing this exact
    # file, with nothing in the way, actually creates the marker. If it
    # doesn't, the negative assertion after it would pass for free on a
    # broken harness.
    assert not marker.exists()
    subprocess.run(
        [BASH, "-c", f'source "{planted.as_posix()}"'],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert marker.exists(), (
        "positive control failed: sourcing the planted file directly did "
        "not create the marker -- the harness cannot see execution"
    )
    marker.unlink()

    # The real case: the shipped chain must never read, let alone execute,
    # a config.rcfg that lives inside the project tree.
    lines, result, _sys_tmp = _run_with_shim(tmp_path, home, project)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not marker.exists(), (
        "the repo-shipped .remember/tmp/config.rcfg was executed as shell "
        f"by the real hook chain -- spawns: {lines}"
    )
    assert "42" in result.stdout, (
        "config resolution broke instead of just ignoring the planted file: "
        f"{result.stdout!r}"
    )


def test_malformed_line_in_new_cache_location_is_rejected_not_executed(tmp_path):
    """The second #682 half: even at the new (system-tmp-dir) location, the
    loader must not trust `source`. A line the publisher could never write
    -- here, one with an unquoted second word and a `;` -- must reject the
    WHOLE cache before anything is evaluated, remove the poisoned file, and
    still resolve the config correctly by falling through to a real
    flatten."""
    home, project, remember = _project(tmp_path)
    cfg = remember / "config.json"
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8")

    _lines1, result1, sys_tmp = _run_with_shim(tmp_path, home, project)
    assert result1.returncode == 0, (result1.stdout, result1.stderr)
    caches = cache_files(sys_tmp)
    assert caches, f"no flattened-config cache ({CACHE_GLOB}) was published under {sys_tmp}"
    cache = caches[0]

    marker = tmp_path / "pwned-682-malformed-marker"
    good = cache.read_text(encoding="utf-8")
    corrupted = good + f'SOME_VAR=x; touch "{marker.as_posix()}"\n'
    cache.write_text(corrupted, encoding="utf-8")
    now = time.time()
    os.utime(cache, (now + 5, now + 5))

    lines2, result2, _sys_tmp2 = _run_with_shim(tmp_path, home, project, sys_tmp=sys_tmp)
    assert result2.returncode == 0, (result2.stdout, result2.stderr)
    assert not marker.exists(), (
        f"a malformed cache line was evaluated as shell: {lines2}"
    )
    assert "42" in result2.stdout, (
        "the malformed cache was not cleanly rejected -- config resolution "
        f"did not fall through to a correct real flatten: {result2.stdout!r}"
    )
    # The loader removes the poisoned file before falling through, and
    # _config_load's own fallback path republishes a fresh cache at the
    # SAME name once it has re-flattened for real -- so "the file is gone"
    # is the wrong check; "the poison is gone" is the one that matters.
    if cache.exists():
        assert marker.as_posix() not in cache.read_text(encoding="utf-8"), (
            "the rejected cache was republished still carrying the "
            "malformed line -- the poison survived the rejection"
        )
    else:
        pass  # also acceptable: rejection removed it and nothing republished
