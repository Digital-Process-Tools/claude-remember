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


HARNESS = f"""
set -eu
REMEMBER_PATHS_SOFT_FAIL=1 source "{RESOLVE_PATHS}" || exit 99
PLUGIN_ROOT="$PIPELINE_DIR"
source "{DETECT_TOOLS}" || exit 99
source "{BOOTSTRAP_DIRS}" || exit 99
source "{LOG_SH}" 2>/dev/null
config ".cooldowns.save_seconds" "default"
"""


def _run_with_shim(tmp_path: Path, home: Path, project: Path):
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_HOOK_CWD": str(project),
        "SPAWN_LOG": str(log),
        "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(
        [BASH, "-c", HARNESS], env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    return spawns(log), result


def test_first_run_is_a_miss_and_forks_jq_to_flatten(tmp_path):
    """Positive control: with no cache yet, config() must genuinely fork the
    flattener (jq, on a host that has it) -- a "zero forks" result here would
    prove the harness never really touched the flatten path at all."""
    home, project, remember = _project(tmp_path)
    (remember / "config.json").write_text(
        json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8"
    )
    lines, result = _run_with_shim(tmp_path, home, project)
    assert result.returncode == 0, (result.stdout, result.stderr)
    jq_spawns = [l for l in lines if l.startswith("jq ")]
    assert jq_spawns, (
        "positive control failed: no jq fork observed on the first (cold) "
        f"run -- got spawns: {lines}"
    )
    assert (remember / "tmp" / "config.rcfg").is_file()


def test_second_run_with_unchanged_config_skips_the_flatten_fork(tmp_path):
    """Core case: after one run has published the cache, a second run against
    the SAME config layers must not fork jq/python to reflatten at all."""
    home, project, remember = _project(tmp_path)
    cfg = remember / "config.json"
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8")

    _lines1, result1 = _run_with_shim(tmp_path, home, project)
    assert result1.returncode == 0, (result1.stdout, result1.stderr)
    cache = remember / "tmp" / "config.rcfg"
    assert cache.is_file()
    # Force the cache strictly newer than the config layer, independent of
    # which second the two writes above landed in.
    now = time.time()
    os.utime(cache, (now + 5, now + 5))

    lines2, result2 = _run_with_shim(tmp_path, home, project)
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

    _lines1, result1 = _run_with_shim(tmp_path, home, project)
    assert result1.returncode == 0, (result1.stdout, result1.stderr)
    assert "42" in result1.stdout
    cache = remember / "tmp" / "config.rcfg"
    now = time.time()
    os.utime(cache, (now + 5, now + 5))

    # Edit the config to a mtime strictly AFTER the cache, with a new value.
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 99}}), encoding="utf-8")
    os.utime(cfg, (now + 10, now + 10))

    _lines2, result2 = _run_with_shim(tmp_path, home, project)
    assert result2.returncode == 0, (result2.stdout, result2.stderr)
    assert "99" in result2.stdout, (
        "stale cache must be impossible to serve silently -- expected the "
        f"NEW value after editing config.json: {result2.stdout!r}"
    )


def test_haiku_key_never_reaches_the_persisted_cache(tmp_path):
    """Security property the cache design depends on, stated in both this
    file's own docstring and log.sh's own comment on the cache: a live
    haiku.oauth_token in config.json must never be written into the
    persisted config.rcfg cache, which survives long past the single
    process the raw merged config.json scratch file is deleted at exit of.
    Both flatteners drop the whole "haiku" key before a row is ever emitted
    -- this pins that guarantee against the PERSISTED file specifically,
    not just against config()'s own in-process read."""
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

    _lines, result = _run_with_shim(tmp_path, home, project)
    assert result.returncode == 0, (result.stdout, result.stderr)

    cache = remember / "tmp" / "config.rcfg"
    assert cache.is_file()
    cache_text = cache.read_text(encoding="utf-8")
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
    config.rcfg must never be sourced -- the loader must fall back to a live
    reflatten rather than executing an attacker-controlled file as shell."""
    home, project, remember = _project(tmp_path)
    cfg = remember / "config.json"
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8")

    _lines, result = _run_with_shim(tmp_path, home, project)
    assert result.returncode == 0, (result.stdout, result.stderr)
    cache = remember / "tmp" / "config.rcfg"
    assert cache.is_file()

    victim = tmp_path / "attacker-controlled.rcfg"
    victim.write_text("touch /tmp/pwned-668-poc\n_RCFG_cooldowns_save_seconds=666\n",
                       encoding="utf-8")
    cache.unlink()
    os.symlink(victim, cache)

    _lines2, result2 = _run_with_shim(tmp_path, home, project)
    assert result2.returncode == 0, (result2.stdout, result2.stderr)
    assert "666" not in result2.stdout, (
        "a symlinked config cache was sourced instead of refused -- "
        f"stdout={result2.stdout!r}"
    )
    assert not Path("/tmp/pwned-668-poc").exists(), (
        "the symlinked cache's shell content actually executed"
    )
    assert "42" in result2.stdout
