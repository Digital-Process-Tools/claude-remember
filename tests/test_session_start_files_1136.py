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
    note) echo "note=$_REMEMBER_SESSION_FILES_NOTE" ;;
    warn) _remember_session_start_files_warn ;;
    budget)
        eval "_real_render() $(declare -f _remember_render_memory_section | tail -n +2)"
        _remember_render_memory_section() {{ echo x >> "$COUNTFILE"; _real_render "$@"; }}
        _body="HEAD"$'\n'"$(_real_render)"
        _remember_apply_session_start_budget _out 10 "$_body"
        ;;
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
    # nothing is injected, but the files are still named for a later read
    assert _present(r.stdout) == [], r.stdout
    assert "--- not injected (session_start_files)" in r.stdout
    assert "now.md" in r.stdout
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

POINTER = "--- not injected (session_start_files)"


def test_deselected_files_are_pointed_at(tmp_path):
    _, _, env = _store(tmp_path, {"session_start_files": ["now"]})
    out = _call("render", env).stdout
    assert POINTER in out
    for name in ("identity.md", "core-memories.md", "recent.md", "archive.md"):
        assert name in out.split(POINTER)[1], name
    # the injected file is not listed as left out
    assert "now.md" not in out.split(POINTER)[1]


def test_default_has_no_deselected_pointer(tmp_path):
    """Positive control for the pointer: nothing is left out by default."""
    _, _, env = _store(tmp_path, None)
    assert POINTER not in _call("render", env).stdout


def test_rotated_slices_follow_the_selection(tmp_path):
    _, _, env = _store(tmp_path / "a", {"session_start_files": ["now"]})
    (tmp_path / "a" / "project" / ".remember" / "archive-2026-01-01.md").write_text("old\n", encoding="utf-8")
    (tmp_path / "a" / "project" / ".remember" / "recent-2026-01-01.md").write_text("old\n", encoding="utf-8")
    assert "rotated memory slices" not in _call("render", env).stdout
    # positive control: the same slices are listed when archive is chosen
    _, _, env2 = _store(tmp_path / "b", {"session_start_files": ["now", "archive"]})
    (tmp_path / "b" / "project" / ".remember" / "archive-2026-01-01.md").write_text("old\n", encoding="utf-8")
    out = _call("render", env2).stdout
    assert "rotated memory slices" in out
    assert "archive-2026-01-01.md" in out


@pytest.mark.parametrize("value", [{"a": "now"}, ["now recent"], [["now"]], 5, ["bogus"]])
def test_unusable_values_warn_and_fall_back_to_all(tmp_path, value):
    _, _, env = _store(tmp_path, {"session_start_files": value})
    assert _present(_call("render", env).stdout) == ALL_HEADERS
    assert _call("note", env).stdout.strip() != "note="


def test_note_is_empty_for_good_values_and_set_for_unknown_names(tmp_path):
    _, _, env = _store(tmp_path / "a", {"session_start_files": ["now"]})
    assert _call("note", env).stdout.strip() == "note="
    _, _, env = _store(tmp_path / "b", {"session_start_files": ["now", "bogus"]})
    assert "bogus" in _call("note", env).stdout


def _log_text(remember: Path) -> str:
    return "".join(p.read_text(errors="replace") for p in remember.rglob("*.log"))


def test_only_the_session_start_path_logs_the_warning(tmp_path):
    remember, _, env = _store(tmp_path, {"session_start_files": ["bogus"]})
    _call("files", env)  # what save/consolidation do: resolve, never warn
    assert "session_start_files" not in _log_text(remember)
    _call("warn", env)  # positive control: the hook path does log it
    assert "session_start_files" in _log_text(remember)


def _jq_shim(base: Path, env: dict) -> Path:
    real = subprocess.run(["which", "jq"], capture_output=True, text=True, check=False).stdout.strip()
    if not real:
        pytest.skip("no jq on PATH to shim")
    shim_dir = base / "shim"
    shim_dir.mkdir()
    counter = base / "jq.count"
    shim = shim_dir / "jq"
    shim.write_text('#!/bin/sh\necho x >> "%s"\nexec "%s" "$@"\n' % (counter, real))
    shim.chmod(0o755)
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    return counter


def _jq_calls(counter: Path) -> int:
    return len(counter.read_text().split()) if counter.exists() else 0


def test_the_key_costs_no_extra_process(tmp_path):
    """Setting session_start_files (even to all six, as the example file does)
    must not add a jq/python launch to _remember_memory_paths: it runs on every
    SessionStart cache hit, save and consolidation (#660/#668)."""
    # the baseline has a project config too: merging the layers costs its own jq
    _, _, env_a = _store(tmp_path / "a", {"thresholds": {"session_start_max_bytes": 9000}})
    counter_a = _jq_shim(tmp_path / "a", env_a)
    _call("files", env_a)
    _, _, env_b = _store(
        tmp_path / "b",
        {"session_start_files": ["identity", "core", "today", "now", "recent", "archive"]},
    )
    counter_b = _jq_shim(tmp_path / "b", env_b)
    _call("files", env_b)
    _, _, env_c = _store(tmp_path / "c", {"session_start_files": ["now"]})
    counter_c = _jq_shim(tmp_path / "c", env_c)
    _call("files", env_c)
    assert _jq_calls(counter_a) >= 1, "positive control: the shim sees the flatten call"
    assert _jq_calls(counter_b) == _jq_calls(counter_a)
    assert _jq_calls(counter_c) == _jq_calls(counter_a)


def test_budget_loop_skips_deselected_files(tmp_path):
    def renders(config, sub):
        _, _, env = _store(tmp_path / sub, config)
        count = tmp_path / (sub + ".count")
        env["COUNTFILE"] = str(count)
        r = _call("budget", env)
        assert r.returncode == 0, r.stderr
        return len(count.read_text().split()) if count.exists() else 0

    everything = renders(None, "a")
    just_now = renders({"session_start_files": ["now"]}, "b")
    assert everything >= 4, "positive control: the default drops several sections"
    assert just_now < everything


def test_flatteners_agree_on_arrays():
    cfg = REPO_ROOT / "config.example.json"
    jq = subprocess.run(["jq", "-r", "-f", str(REPO_ROOT / "scripts" / "cfg_flatten.jq"), str(cfg)],
                        capture_output=True, text=True, check=False)
    py = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "cfg_flatten.py"), str(cfg)],
                        capture_output=True, text=True, check=False)
    if jq.returncode != 0:
        pytest.skip("no jq to compare against")
    assert jq.stdout.rstrip("\n") == py.stdout.rstrip("\n")
    assert "session_start_files\tidentity core today now recent archive" in py.stdout
    assert "_comments" not in py.stdout
