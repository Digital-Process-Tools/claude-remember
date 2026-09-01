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

import json
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
# (Codex/Gemini never set it), only REMEMBER_HOOK_CWD -- the shape
# post-tool-hook.sh and (since #479) user-prompt-hook.sh offer from their own
# stdin `cwd` (#411, #444). Before #479, user-prompt-hook.sh set
# REMEMBER_HOOK_CWD from stdin only AFTER its own cache-load attempt had
# already run and failed, so this harness's premise did not hold for that
# hook -- see test_user_prompt_hook_hits_cache_on_second_invocation_with_no_claude_project_dir
# below for the end-to-end regression against the real script, not this
# hand-rolled simulation. Mirrors what resolve-paths.sh itself does on a
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


# Reviewer finding (self-review round, #469): the key computed at LOAD time
# (raw REMEMBER_HOOK_CWD, before resolve-paths.sh has run) and the key
# _remember_env_cache_publish would compute if it recomputed instead of
# reusing the pin (from CLAUDE_PROJECT_DIR, which resolve-paths.sh has by
# then exported as the RESOLVED value) can disagree on a platform where
# resolve-paths.sh's Windows drive-letter normalisation actually changes the
# string. macOS/Linux can still exercise the exact bash branch this depends
# on: `_remember_normalize_win_path`'s `case "$OSTYPE" in msys|cygwin)` reads
# `$OSTYPE` as an ordinary (exportable) variable, not a compiled-in constant
# -- forcing it to `msys` here runs the real Windows code path on whatever
# host this test happens to execute on, the same technique
# tests/test_windows_native_hook_cwd_448.py already relies on.
_NORMALIZE = r"""
_remember_normalize_win_path() {{
    local _in="$1" _drive="" _rest=""
    if [[ "$_in" =~ ^/([a-zA-Z])/(.*)$ ]]; then
        _drive="${{BASH_REMATCH[1]}}"
        _rest="${{BASH_REMATCH[2]}}"
    fi
    if [ -n "$_drive" ]; then
        _drive=$(printf '%s' "$_drive" | tr '[:lower:]' '[:upper:]')
        _rest="${{_rest//\//\\}}"
        printf '%s' "${{_drive}}:\\${{_rest}}"
        return 0
    fi
    printf '%s' "$_in"
}}
"""

_RUN_WINDOWS_ONE = _NORMALIZE + r"""
source "{lib}"
_remember_env_cache_load
_rc=$?
echo "LOAD_RC=$_rc"
if [ "$_rc" != "0" ]; then
    # The real resolve-paths.sh sequence on Windows/Git-Bash: REMEMBER_HOOK_CWD
    # arrives POSIX-styled from stdin, gets normalised to a drive-letter
    # spelling, THAT becomes PROJECT_DIR, and CLAUDE_PROJECT_DIR is exported
    # as the normalised form -- a genuinely different string than the raw
    # REMEMBER_HOOK_CWD the earlier (failed) load call above keyed on.
    _normalized="$(_remember_normalize_win_path "$REMEMBER_HOOK_CWD")"
    export CLAUDE_PROJECT_DIR="$_normalized"
    export CLAUDE_PLUGIN_ROOT="{pipeline}"
    PROJECT_DIR="$_normalized"
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


def test_windows_normalized_project_dir_does_not_orphan_the_hook_cwd_key(tmp_path):
    """#469 self-review finding: without pinning the key once per process, a
    publish call after resolve-paths.sh's Windows normalisation would key the
    cache under the NORMALISED CLAUDE_PROJECT_DIR while the load call moments
    earlier, in the same process, keyed under the raw REMEMBER_HOOK_CWD --
    write and read would target different files, forever, on exactly the
    platform this fix exists to help. OSTYPE=msys is forced so this runs the
    real drive-letter-rewriting branch regardless of the host this test
    executes on (see tests/test_windows_native_hook_cwd_448.py for the same
    technique)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "tmp").mkdir()
    pipeline = tmp_path / "pipeline"
    (pipeline / "pipeline").mkdir(parents=True)
    (pipeline / "pipeline" / "haiku.py").write_text("", encoding="utf-8")
    remember_dir = tmp_path / "remember"
    remember_dir.mkdir()

    # A POSIX-styled drive path, exactly the form resolve-paths.sh's own
    # comments say Codex/Gemini's stdin `cwd` arrives in on Windows -- and
    # exactly the form _remember_normalize_win_path rewrites.
    raw_hook_cwd = "/c/fake/windows/project"

    script = _RUN_WINDOWS_ONE.format(lib=LIB_ENV_CACHE, pipeline=pipeline, remember_dir=remember_dir)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT")}
    env.update({
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "REMEMBER_HOOK_CWD": raw_hook_cwd,
        "OSTYPE": "msys",
        # Set on EVERY invocation, exactly as Codex sets it -- see the
        # matching note in _run() above for the non-Windows tests.
        "CLAUDE_PLUGIN_ROOT": str(pipeline),
    })

    first = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert first.returncode == 0, first.stderr
    assert "LOAD_RC=1" in first.stdout, first.stdout + first.stderr
    assert "PUBLISHED=1" in first.stdout, first.stdout + first.stderr

    second = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert second.returncode == 0, second.stderr
    assert "LOAD_RC=0" in second.stdout, (
        "the second invocation, same raw REMEMBER_HOOK_CWD, must hit the "
        "cache the first invocation published -- a mismatch here means the "
        "publish call keyed on the normalised CLAUDE_PROJECT_DIR instead of "
        "reusing the pinned raw key, orphaning the file on Windows: "
        + second.stdout + second.stderr
    )

# --- #479: the real hook, not the hand-rolled simulation above ------------
#
# The simulation above proves lib-env-cache.sh itself is correctly keyed. It
# says nothing about whether user-prompt-hook.sh ever supplies the key that
# early -- and before #479 it did not: REMEMBER_HOOK_CWD was read from stdin
# only inside the "cache missed" branch, after _remember_env_cache_load had
# already been called and failed. This drives the real script twice, via
# `bash -x`, and tells hit from miss by whether the slow path's own
# `source .../resolve-paths.sh` line appears in the trace -- the fast path
# never reaches it (scripts/user-prompt-hook.sh:207-220, the
# _remember_env_cache_load gate and the slow-path source call it guards).
USER_PROMPT_HOOK = REPO_ROOT / "scripts" / "user-prompt-hook.sh"


def _user_prompt_payload(cwd: str) -> str:
    return json.dumps({
        "session_id": "479-e2e-session",
        "transcript_path": "/does/not/matter.jsonl",
        "hook_event_name": "UserPromptSubmit",
        "cwd": cwd,
        "prompt": "hello",
    })


def test_user_prompt_hook_hits_cache_on_second_invocation_with_no_claude_project_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    tmp_dir = home / "tmp"
    tmp_dir.mkdir()
    project = tmp_path / "codex-project"
    project.mkdir()

    env = {
        **os.environ,
        "HOME": str(home),
        "TMPDIR": str(tmp_dir),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    }
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("REMEMBER_HOOK_CWD", None)

    payload = _user_prompt_payload(str(project))

    def _run():
        return subprocess.run(
            ["bash", "-x", str(USER_PROMPT_HOOK)], env=env, input=payload,
            cwd=str(project), capture_output=True, text=True, timeout=60,
            check=False, errors="replace",
        )

    first = _run()
    assert first.returncode == 0, first.stderr
    assert "resolve-paths.sh" in first.stderr, (
        "first (cold) invocation should take the slow path and source "
        "resolve-paths.sh -- if it never shows up here the harness itself "
        "is broken, not the fix: " + first.stderr
    )

    second = _run()
    assert second.returncode == 0, second.stderr
    assert "resolve-paths.sh" not in second.stderr, (
        "second invocation, same stdin cwd, no CLAUDE_PROJECT_DIR anywhere: "
        "must be a cache HIT (fast path), never re-sourcing resolve-paths.sh. "
        "If this fires, REMEMBER_HOOK_CWD is still unset at the point "
        "_remember_env_cache_load runs, inside user-prompt-hook.sh itself "
        "(#479): " + second.stderr
    )


