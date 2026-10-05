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
     the report's author suspected. No stray directory was created; the
     WARNING landed in hook-errors.log as intended
     (``TestEndToEndSeedFailureNeverCorruptsPaths`` below pins this).

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
        assert "SOURCE_RC=0" not in result.stdout, result.stdout
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
        leaked = [p for p in cwd_marker.iterdir()]
        assert not leaked, f"a directory was created under cwd from the bad value: {leaked}"


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
