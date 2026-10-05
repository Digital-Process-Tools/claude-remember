"""#902: log.sh must refuse to mkdir -p a REMEMBER_DIR that is not a real
absolute path, or that carries a newline/CR, rather than silently building a
directory tree under cwd from it.

## Background (read this before touching the guard)

A user on 0.33.0 reported the project root filling with thousands of
directories named after the literal CONTENT of an autonomous save-log line
(``"10:59:06 [post-tool] save triggered\\n"``), because a failed seed write to
``logs/autonomous/save-HHMMSS.log`` was followed by a `log()` call whose
``REMEMBER_LOG_DIR`` resolution had somehow picked up that file's content and
then ``mkdir -p``'d the result.

Three things were checked against current main (0.40.0) before writing this
file, and none of them reproduce the corruption:

  1. The cold resolver (``_resolve_remember_dir`` in lib-memory-dir.sh) builds
     REMEMBER_DIR from config/PROJECT_DIR string operations only -- no file
     content is ever read into it.
  2. lib-env-cache.sh's publish/load pair is race-safe: publish writes to a
     mktemp file and renames it into place (lib-env-cache.sh:376-432), and
     load parses the cache file strictly one KEY=VALUE per line, rejecting the
     whole file on any line it does not recognise (lib-env-cache.sh:217-276) --
     there is no way for a single cache line's value to smear across a
     newline into a neighbouring key.
  3. End to end: a stale, non-empty ``save-HHMMSS.log`` was planted at the
     exact name post-tool-hook.sh would pick for "now", made unwritable so the
     seed printf fails exactly as the report describes, and post-tool-hook.sh
     was run against it on the (cache-populated) fast path, which is the path
     the report's author suspected. No stray directory was created
     (``TestEndToEndSeedFailureNeverCorruptsPaths`` below pins this). Whether
     the WARNING itself reaches hook-errors.log is a separate, pre-existing
     question this fix does not change either way: every hook sources log.sh
     as ``source ... 2>/dev/null``, which swallows stderr for the whole
     sourced body including this guard's own FATAL line -- true of the
     mkdir-failure FATAL log.sh already carried before this diff, not a
     regression introduced here (self-review finding, #902).

## Second self-review finding: the guard only covered log.sh's own mkdir

The guard above protects `REMEMBER_LOG_DIR` ("$REMEMBER_DIR/logs"), resolved
and mkdir'd inside log.sh. It does NOT protect the OTHER `mkdir -p
"$REMEMBER_DIR/..."` call sites this repo has -- most importantly
`post-tool-hook.sh`'s own `mkdir -p "$REMEMBER_DIR/logs/autonomous"`, which is
the literal call site issue #902 names and runs on the fast path BEFORE
log.sh is ever sourced. Fixed in the same commit: a twin
``_remember_dir_is_unsafe()`` check, defined in post-tool-hook.sh itself
(same logic, same LC_ALL=C scoping, #695), gates that mkdir directly
(``TestFastPathMkdirSiteIsGuardedToo`` below). Several other `mkdir -p
"$REMEMBER_DIR/..."` sites remain unguarded (session-start-hook.sh,
session-end-hook.sh, write-handoff.sh, and three more in post-tool-hook.sh
itself for `$REMEMBER_DIR/tmp`) -- reported to the maintainer rather than
patched here, since centralising this check across five files is an
architecture decision (where the shared helper lives, whether REMEMBER_DIR
resolution itself should refuse to proceed) beyond this issue's own scope.

So the planted-bad-value tests below exercise the new guard directly, via the
one real path that can still hand ``_resolve_remember_dir`` an attacker- or
corruption-controlled string on any shipped version: a ``data_dir`` value in
config.json. jq reads it with ``-r`` (raw string), so a JSON ``"...\\n..."``
escape becomes a literal embedded newline byte once it reaches REMEMBER_DIR --
a real, already-wired path for exactly the character class this guard exists
to refuse, independent of whatever warm-hook mechanism produced the
corruption the original report saw.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_log_sh import _bash_path, _find_bash
from test_post_tool_hook_spawns import _env, _project, _reap, _run

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"

_BASH = _find_bash()
pytestmark = pytest.mark.skipif(_BASH is None, reason="Git Bash not found (Windows without Git for Windows)")


def _source_log_sh(tmp_path: Path, *, project_dir: str, data_dir: str | None):
    """Source log.sh with PROJECT_DIR/PIPELINE_DIR pointed at a scratch tree,
    optionally forcing config.json's data_dir to an arbitrary (possibly
    dangerous) string, and report what it did -- without ever touching the
    real cwd this test process is running in."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir(parents=True, exist_ok=True)
    if data_dir is not None:
        (pipeline / "config.json").write_text(json.dumps({"data_dir": data_dir}), encoding="utf-8")
    cwd_marker = tmp_path / "cwd"
    cwd_marker.mkdir(parents=True, exist_ok=True)

    script = f"""
    set +e
    export HOME="{_bash_path(home)}"
    export PROJECT_DIR="{project_dir}"
    export PIPELINE_DIR="{_bash_path(pipeline)}"
    cd "{_bash_path(cwd_marker)}"
    source "{_bash_path(LOG_SH)}"
    rc=$?
    echo "SOURCE_RC=$rc"
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    # The refusal must not cost callers their functions (#912 CI): every hook
    # sources log.sh and then calls log/dispatch/report_error. A guard that
    # aborts the source leaves them undefined and the hook dies on 127.
    for _f in log report_error dispatch _remember_date config; do
        declare -F "$_f" >/dev/null 2>&1 || echo "MISSING_FN=$_f"
    done
    echo "MEMORY_LOG_FILE=$MEMORY_LOG_FILE"
    log "probe" "a line that must land nowhere under cwd" 2>/dev/null
    echo "LOG_RC=$?"
    """
    result = subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True, timeout=60, check=False,
    )
    return result, cwd_marker


class TestMustFireAbsoluteButNewlineBearing:
    def test_must_fire_embedded_newline_is_refused(self, tmp_path):
        """data_dir carrying an embedded newline is absolute (starts with
        '/'), so only the new control-character check can catch it. Must be
        refused, and nothing may be created under cwd from it."""
        result, cwd_marker = _source_log_sh(
            tmp_path,
            project_dir=_bash_path(tmp_path / "project"),
            data_dir="/tmp/fake\n23:59:00 [post-tool] save triggered\n/evil",
        )
        assert "FATAL: unsafe REMEMBER_DIR" in result.stderr, result.stderr
        # Refused, but still a complete library: the source finishes and every
        # function its callers use is defined (#912 -- an early `return 1`
        # here is what broke #294's newline-project SessionStart run).
        assert "SOURCE_RC=0" in result.stdout, result.stdout
        assert "MISSING_FN=" not in result.stdout, result.stdout
        assert "LOG_RC=0" in result.stdout, result.stdout
        # The file write goes to a no-op sink, never to a path built from
        # the refused value.
        assert "MEMORY_LOG_FILE=/dev/null\n" in result.stdout, result.stdout
        leaked = [p for p in cwd_marker.iterdir()]
        assert not leaked, f"a directory was created under cwd from the bad value: {leaked}"


class TestMustFireNotAbsolute:
    def test_must_fire_relative_remember_dir_is_refused(self, tmp_path):
        """A relative PROJECT_DIR (never expected in production, but nothing
        upstream of log.sh currently enforces it) must still be refused
        before mkdir -p, rather than building a tree under cwd."""
        result, cwd_marker = _source_log_sh(
            tmp_path, project_dir="relative/project", data_dir=None,
        )
        assert "FATAL: unsafe REMEMBER_DIR" in result.stderr, result.stderr
        assert "SOURCE_RC=0" in result.stdout, result.stdout
        assert "MISSING_FN=" not in result.stdout, result.stdout
        assert "LOG_RC=0" in result.stdout, result.stdout
        assert "MEMORY_LOG_FILE=/dev/null\n" in result.stdout, result.stdout
        leaked = [p for p in cwd_marker.iterdir()]
        assert not leaked, f"a directory was created under cwd from the bad value: {leaked}"

    def test_must_fire_dispatch_does_not_mkdir_tmp_under_cwd(self, tmp_path):
        """Now that a refused log.sh still defines dispatch() (#912), its own
        `mkdir -p "$REMEMBER_DIR/tmp"` -- reached the first time a listener is
        found -- must be refused too, or the guard just moved the leak."""
        home = tmp_path / "home"
        home.mkdir()
        pipeline = tmp_path / "pipeline"
        pipeline.mkdir()
        hooks = tmp_path / "hooks.d"
        (hooks / "probe").mkdir(parents=True)
        listener = hooks / "probe" / "10-echo.sh"
        listener.write_text("#!/bin/sh\necho LISTENER_RAN\n", encoding="utf-8")
        listener.chmod(0o755)
        cwd_marker = tmp_path / "cwd"
        cwd_marker.mkdir()
        script = f"""
        set +e
        export HOME="{_bash_path(home)}"
        export PROJECT_DIR="relative/project"
        export PIPELINE_DIR="{_bash_path(pipeline)}"
        cd "{_bash_path(cwd_marker)}"
        source "{_bash_path(LOG_SH)}" 2>/dev/null
        REMEMBER_HOOKS_DIR="{_bash_path(hooks)}"
        dispatch probe
        echo "DISPATCH_RC=$?"
        """
        result = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, timeout=60, check=False)
        # Positive control: the listener really was found, so the mkdir
        # branch was reached -- an empty cwd below is not a dispatch that
        # never got that far. With no tmp/ to capture into, dispatch takes
        # its existing "output NOT SHOWN" path rather than creating one.
        assert "hooks.d: probe/10-echo.sh" in result.stdout, (result.stdout, result.stderr)
        assert "DISPATCH_RC=0" in result.stdout, (result.stdout, result.stderr)
        leaked = [p for p in cwd_marker.iterdir()]
        assert not leaked, f"dispatch created a directory under cwd from the bad value: {leaked}"


class TestMustNotFireOrdinaryPaths:
    def test_must_not_fire_plain_absolute_path(self, tmp_path):
        """Positive control: an ordinary absolute REMEMBER_DIR, the common
        case on every platform, must be accepted unchanged -- without this,
        the must-fire tests above would prove nothing about precision."""
        project = tmp_path / "project"
        result, _ = _source_log_sh(tmp_path, project_dir=_bash_path(project), data_dir=None)
        assert "FATAL: unsafe REMEMBER_DIR" not in result.stderr, result.stderr
        assert "SOURCE_RC=0" in result.stdout, result.stdout
        assert f"REMEMBER_DIR={_bash_path(project)}/.remember" in result.stdout, result.stdout
        assert "MISSING_FN=" not in result.stdout, result.stdout
        # Unchanged for an ordinary path: the daily log under <store>/logs/.
        assert f"MEMORY_LOG_FILE={_bash_path(project)}/.remember/logs/memory-" in result.stdout, result.stdout
        assert any((project / ".remember" / "logs").glob("memory-*.log")), "log() wrote nothing for a safe path"

    def test_must_not_fire_windows_drive_colon_slash(self, tmp_path):
        """C:/x -- the drive-letter-colon-slash form _resolve_remember_dir
        already treats as absolute -- must still be accepted by the new
        guard, not newly refused."""
        result, _ = _source_log_sh(
            tmp_path, project_dir=_bash_path(tmp_path / "project"), data_dir="C:/fakestore",
        )
        assert "FATAL: unsafe REMEMBER_DIR" not in result.stderr, result.stderr
        assert "SOURCE_RC=0" in result.stdout, result.stdout
        assert "REMEMBER_DIR=C:/fakestore" in result.stdout, result.stdout

    def test_must_not_fire_msys_style_slash_c_path(self, tmp_path):
        """/c/x -- Git-Bash's own absolute spelling of a Windows path --
        already starts with '/' and must still be accepted BY THE GUARD.
        On this (POSIX) test box /c/fakestore is not a real, writable
        location -- mkdir legitimately fails there with the pre-existing
        "cannot create" FATAL, same as it always did -- so only the new
        guard's own verdict is asserted here, not that the directory was
        actually created."""
        result, _ = _source_log_sh(
            tmp_path, project_dir=_bash_path(tmp_path / "project"), data_dir="/c/fakestore",
        )
        assert "FATAL: unsafe REMEMBER_DIR" not in result.stderr, result.stderr
        assert "REMEMBER_DIR=/c/fakestore" in result.stdout, result.stdout


class TestEndToEndSeedFailureNeverCorruptsPaths:
    """The exact reported scenario, driven through the real hook: a stale,
    non-empty save-HHMMSS.log already sitting at the name post-tool-hook.sh
    is about to pick, made unwritable so the seed printf fails exactly as
    described, run on the fast (env-cache) path the report's author
    suspected. Documents what was ruled out: on current main this produces a
    logged WARNING and NO stray directory, cache-warm or cold."""

    def test_must_fire_stale_unwritable_save_log_produces_no_stray_dir(self, tmp_path):
        home, project, remember = _project(tmp_path, jsonl_lines=200)
        env = _env(tmp_path, home, project)
        # Opt INTO the cache this time (#350's tests pin it off) -- the
        # report's author explicitly suspected the warm/env-cache path.
        env.pop("REMEMBER_ENV_CACHE", None)
        env.pop("REMEMBER_CONFIG_CACHE", None)

        # First run: cold, populates the env cache for the second run.
        first = _run(env)
        assert first.returncode == 0, first.stderr[:300]
        _reap(remember)
        pid_file = remember / "tmp" / "save-session.pid"
        if pid_file.exists():
            pid_file.unlink()

        # Plant a stale, non-empty save log at whatever name "now" resolves
        # to, then make it unwritable so the seed's printf fails exactly as
        # the report describes -- while its OWN content is the thing that,
        # per the report, ends up inside the corrupted path.
        autonomous = remember / "logs" / "autonomous"
        import time
        hhmmss = time.strftime("%H%M%S")
        stale = autonomous / f"save-{hhmmss}.log"
        stale.write_text(f"{time.strftime('%H:%M:%S')} [post-tool] save triggered\n", encoding="utf-8")
        os.chmod(stale, 0o444)
        try:
            second = _run(env)
            assert second.returncode == 0, second.stderr[:300]
        finally:
            os.chmod(stale, 0o644)

        leaked = [
            p for p in project.iterdir()
            if p.name not in (".remember",) and p.name != ".claude"
        ]
        assert not leaked, f"stray path created under the project root: {leaked}"
        claude_dir = project / ".claude"
        if claude_dir.exists():
            stray_in_claude = [p for p in claude_dir.iterdir()]
            assert not stray_in_claude, f"stray path created under .claude: {stray_in_claude}"
        _reap(remember)


class TestFastPathMkdirSiteIsGuardedToo:
    """Self-review finding #2: the guard above protects only log.sh's own
    mkdir -- the literal call site issue #902 names,
    post-tool-hook.sh's own `mkdir -p "$REMEMBER_DIR/logs/autonomous"`, runs
    on the fast path BEFORE log.sh is ever sourced and was NOT covered by it.
    Fixed with a twin `_remember_dir_is_unsafe()` defined directly in
    post-tool-hook.sh. Tested two ways: the function's own logic in
    isolation (would still pass if the WIRING at the mkdir call site were
    ripped out), and a structural check that the call site is actually
    gated by it (would still pass if the FUNCTION were deleted but a stray
    reference remained) -- together they cover what either check alone
    would miss."""

    def _extract_function(self) -> str:
        text = HOOK.read_text(encoding="utf-8")
        start = text.index("_remember_dir_is_unsafe() {")
        end = text.index("\n}\n", start) + len("\n}\n")
        return text[start:end]

    def test_must_fire_function_logic_matches_log_sh_twin(self, tmp_path):
        bad_newline_path = "/abs/with" + "\n" + "newline"
        script = f"""
        set +e
        {self._extract_function()}
        REMEMBER_DIR="relative/path"
        _remember_dir_is_unsafe; echo "relative=$?"
        REMEMBER_DIR="/abs/safe/path"
        _remember_dir_is_unsafe; echo "absolute=$?"
        REMEMBER_DIR='{bad_newline_path}'
        _remember_dir_is_unsafe; echo "newline=$?"
        REMEMBER_DIR="C:/windows/safe"
        _remember_dir_is_unsafe; echo "windows=$?"
        """
        result = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, timeout=30, check=False)
        assert "relative=0" in result.stdout, result.stdout   # 0 = unsafe (bash truthy "fires")
        assert "absolute=1" in result.stdout, result.stdout   # 1 = safe
        assert "newline=0" in result.stdout, result.stdout
        assert "windows=1" in result.stdout, result.stdout

    def test_must_fire_mkdir_call_site_is_actually_gated(self):
        """Structural pin: the exact mkdir named in #902 must be preceded,
        within a few lines, by a call to the guard -- not merely have the
        guard function defined somewhere else in the file."""
        lines = HOOK.read_text(encoding="utf-8").splitlines()
        mkdir_idx = next(
            i for i, line in enumerate(lines)
            if 'mkdir -p "$REMEMBER_DIR/logs/autonomous"' in line
        )
        preceding = "\n".join(lines[max(0, mkdir_idx - 6):mkdir_idx])
        assert "_remember_dir_is_unsafe" in preceding, (
            "the save-log mkdir is no longer gated by the #902 guard -- "
            "this is the exact call site the issue reported\n" + preceding
        )

