"""#1136: `session_start_files` chooses which memory files SessionStart injects.

Drives scripts/lib-memory-context.sh directly (same harness shape as
tests/test_start_context_cache_668.py). Every "file X is absent" assertion is
paired with a positive control that the file IS injected when selected.
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

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = f"""
set -u
REMEMBER_PATHS_SOFT_FAIL=1 source "{(REPO_ROOT / 'scripts' / 'resolve-paths.sh').as_posix()}" || exit 99
PLUGIN_ROOT="$PIPELINE_DIR"
source "{(REPO_ROOT / 'scripts' / 'detect-tools.sh').as_posix()}" || exit 99
source "{(REPO_ROOT / 'scripts' / 'log.sh').as_posix()}" 2>/dev/null
source "{(REPO_ROOT / 'scripts' / 'lib-memory-context.sh').as_posix()}"
_remember_memory_paths
cmd="$1"
case "$cmd" in
    load) _remember_start_cache_context_load ;;
    publish) _remember_start_cache_context_publish ;;
    render) _remember_render_memory_section ;;
    files) echo "count=${{#MEMORY_FILES[@]}}" ;;
esac
"""

ALL_HEADERS = [
    "--- identity.md ---",
    "--- core-memories.md ---",
    "--- today-",
    "--- now.md ---",
    "--- recent.md ---",
    "--- archive.md ---",
]


def _store(tmp_path: Path, config):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    when = time.time() - 100
    today = time.strftime("%Y-%m-%d")
    for name in ("identity.md", "core-memories.md", f"today-{today}.md",
                 "now.md", "recent.md", "archive.md"):
        p = remember / name
        p.write_text("body of " + name + "\n", encoding="utf-8")
        os.utime(p, (when, when))
    cfg = remember / "config.json"
    if config is not None:
        cfg.write_text(json.dumps(config), encoding="utf-8")
        os.utime(cfg, (when, when))
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_HOOK_CWD": str(project),
        "REMEMBER_ENV_CACHE": "0",
        "REMEMBER_CONFIG_CACHE": "0",
    }
    return remember, cfg, env


def _call(cmd: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-c", HARNESS, "_", cmd], env=env,
                          capture_output=True, text=True, timeout=60, check=False)


def _present(out: str) -> list:
    return [h for h in ALL_HEADERS if h in out]


def test_default_injects_all_six(tmp_path):
    """Positive control: no key set means today's behaviour, all six files."""
    _, _, env = _store(tmp_path, None)
    r = _call("render", env)
    assert r.returncode == 0, r.stderr
    assert _present(r.stdout) == ALL_HEADERS, r.stdout


def test_only_now_selected(tmp_path):
    _, _, env = _store(tmp_path, {"session_start_files": ["now"]})
    r = _call("render", env)
    assert r.returncode == 0, r.stderr
    assert _present(r.stdout) == ["--- now.md ---"], r.stdout


def test_selection_is_a_set_not_an_order(tmp_path):
    _, _, env = _store(tmp_path, {"session_start_files": ["recent", "core"]})
    r = _call("render", env)
    assert _present(r.stdout) == ["--- core-memories.md ---", "--- recent.md ---"]


def test_unknown_values_are_ignored_not_fatal(tmp_path):
    _, _, env = _store(tmp_path, {"session_start_files": ["now", "bogus"]})
    r = _call("render", env)
    assert r.returncode == 0, r.stderr
    assert _present(r.stdout) == ["--- now.md ---"]


def test_only_unknown_values_fall_back_to_all(tmp_path):
    _, _, env = _store(tmp_path, {"session_start_files": ["nwo"]})
    r = _call("render", env)
    assert r.returncode == 0, r.stderr
    assert _present(r.stdout) == ALL_HEADERS


def test_empty_array_injects_nothing(tmp_path):
    _, _, env = _store(tmp_path, {"session_start_files": []})
    r = _call("render", env)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    # set -u on an empty array must not blow up the other consumers either
    assert _call("publish", env).returncode == 0
    assert _call("files", env).stdout.strip() == "count=0"


def test_single_string_is_accepted(tmp_path):
    _, _, env = _store(tmp_path, {"session_start_files": "now"})
    r = _call("render", env)
    assert _present(r.stdout) == ["--- now.md ---"]


def test_config_change_invalidates_cache_even_with_old_mtime(tmp_path):
    """The cache key includes the selection: a config edit whose mtime is
    older than the cache (restored from git, copied with -p) must still miss."""
    remember, cfg, env = _store(tmp_path, None)
    assert _call("publish", env).returncode == 0
    cache = remember / "tmp" / "start-context.cache"
    t = time.time() + 5
    os.utime(cache, (t, t))
    hit = _call("load", env)  # positive control: unchanged config is a hit
    assert hit.returncode == 0 and "--- recent.md ---" in hit.stdout

    cfg.write_text(json.dumps({"session_start_files": ["now"]}), encoding="utf-8")
    old = time.time() - 1000
    os.utime(cfg, (old, old))
    miss = _call("load", env)
    assert miss.returncode == 1, miss.stdout
