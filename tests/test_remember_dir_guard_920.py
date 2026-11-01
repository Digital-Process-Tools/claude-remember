"""#920: three #902-guard gaps the maintainer's triage comment confirmed at HEAD.

1. bootstrap-dirs.sh's own `mkdir -p "$REMEMBER_DIR/..."` (and several sibling
   sites across post-tool-hook.sh/session-start-hook.sh/session-end-hook.sh/
   write-handoff.sh) run with no unsafe-dir check at all -- #902's guard only
   ever covered log.sh's own mkdir and post-tool-hook.sh's twin at the single
   call site #902 itself named.
2. The FATAL that reaches ``log()`` once a sink is active never reaches
   stderr: ``echo "$message" >> "$MEMORY_LOG_FILE" 2>/dev/null`` always
   *succeeds* when MEMORY_LOG_FILE is /dev/null, so the ``|| echo ... >&2``
   fallback never runs.
3. A UNC project dir (two leading backslashes, server, share) is
   misclassified as unsafe by both guard predicates, which test only for a
   leading forward slash or a drive letter.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"
DETECT_TOOLS = REPO_ROOT / "scripts" / "detect-tools.sh"

_BASH = _find_bash()
pytestmark = pytest.mark.skipif(_BASH is None, reason="Git Bash not found (Windows without Git for Windows)")

UNC_VALUE = chr(92) * 2 + "server" + chr(92) + "share" + chr(92) + "proj/.remember"
UNC_WITH_NEWLINE = chr(92) * 2 + "server" + chr(92) + "share" + chr(10) + "x"


def _extract_function(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    marker = name + "() {"
    start = text.index(marker)
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


class TestBootstrapDirsIsGuarded:
    """Gap 1: bootstrap-dirs.sh built the tree with no unsafe-dir check of its
    own, one line before log.sh/post-tool-hook.sh ever get a chance to
    refuse. Fixed by exporting REMEMBER_DIR_UNSAFE from lib-memory-dir.sh
    (the single place every one of these scripts resolves REMEMBER_DIR) and
    gating bootstrap-dirs.sh's own mkdir on it."""

    def _source_bootstrap(self, tmp_path: Path, *, project_dir: str, data_dir):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        pipeline = tmp_path / "pipeline"
        pipeline.mkdir(parents=True, exist_ok=True)
        if data_dir is not None:
            (pipeline / "config.json").write_text(json.dumps({"data_dir": data_dir}), encoding="utf-8")
        cwd_marker = tmp_path / "cwd"
        cwd_marker.mkdir(parents=True, exist_ok=True)
        # Not resolve-paths.sh/detect-tools.sh: resolve-paths.sh re-derives
        # PIPELINE_DIR from its OWN location on disk rather than trusting an
        # exported value, which would silently point this test's config.json
        # plant at the real repo's config instead. bootstrap-dirs.sh only
        # needs PROJECT_DIR/PIPELINE_DIR set and session_dir_slug defined
        # (via lib-memory-dir.sh's own source of lib-slug.sh) -- the same
        # minimal setup test_remember_dir_guard_902.py's _source_log_sh uses.
        script = f"""
        set +e
        export HOME="{_bash_path(home)}"
        export PROJECT_DIR="{project_dir}"
        export PIPELINE_DIR="{_bash_path(pipeline)}"
        source "{_bash_path(BOOTSTRAP)}"
        rc=$?
        echo "SOURCE_RC=$rc"
        echo "REMEMBER_DIR=$REMEMBER_DIR"
        echo "REMEMBER_DIR_UNSAFE=${{REMEMBER_DIR_UNSAFE:-}}"
        """
        result = subprocess.run(
            [_BASH, "-c", script], capture_output=True, text=True, timeout=60, check=False, cwd=str(cwd_marker),
        )
        return result, cwd_marker

    def test_must_fire_newline_data_dir_creates_nothing_under_cwd(self, tmp_path):
        result, cwd_marker = self._source_bootstrap(
            tmp_path,
            project_dir=_bash_path(tmp_path / "project"),
            data_dir="/tmp/fake\n23:59:00 evil",
        )
        assert "REMEMBER_DIR_UNSAFE=1" in result.stdout, (result.stdout, result.stderr)
        leaked = [p for p in cwd_marker.iterdir()]
        assert not leaked, f"bootstrap-dirs.sh created a directory under cwd from the bad value: {leaked}"

    def test_must_not_fire_ordinary_absolute_path_still_creates_tree(self, tmp_path):
        """Positive control: an ordinary project must still get its tree."""
        project = tmp_path / "project"
        result, _ = self._source_bootstrap(tmp_path, project_dir=_bash_path(project), data_dir=None)
        assert "REMEMBER_DIR_UNSAFE=" in result.stdout and "REMEMBER_DIR_UNSAFE=1" not in result.stdout, (
            result.stdout, result.stderr,
        )
        assert (project / ".remember" / "tmp").is_dir(), (result.stdout, result.stderr)
        assert (project / ".remember" / "logs" / "autonomous").is_dir(), (result.stdout, result.stderr)

    def test_must_fire_mkdir_call_site_is_actually_gated(self):
        """Structural pin, same shape as #902's own: the mkdir must be
        preceded, within a few lines, by a check of the new flag -- not
        merely have the flag exported somewhere unrelated in the file."""
        lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
        mkdir_idx = next(
            i for i, line in enumerate(lines)
            if line.strip().startswith("mkdir -p")
        )
        preceding = "\n".join(lines[max(0, mkdir_idx - 6):mkdir_idx + 1])
        assert "REMEMBER_DIR_UNSAFE" in preceding, (
            "bootstrap-dirs.sh's mkdir is not gated on REMEMBER_DIR_UNSAFE\n" + preceding
        )


class TestFatalReachesStderr:
    """Gap 2: log()'s own write-to-sink path treats a successful append to
    /dev/null as success, so the `|| echo ... >&2` fallback never runs and
    a FATAL logged once the sink is active is lost entirely."""

    def _source_and_log(self, tmp_path: Path, *, project_dir: str):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        pipeline = tmp_path / "pipeline"
        pipeline.mkdir(parents=True, exist_ok=True)
        script = f"""
        set +e
        export HOME="{_bash_path(home)}"
        export PROJECT_DIR="{project_dir}"
        export PIPELINE_DIR="{_bash_path(pipeline)}"
        source "{_bash_path(LOG_SH)}" 2>/dev/null
        log "hook" "FATAL: unsafe REMEMBER_DIR ($REMEMBER_DIR) -- refusing to mkdir or save (#902)"
        echo "LOG_RC=$?"
        """
        return subprocess.run([_BASH, "-c", script], capture_output=True, text=True, timeout=60, check=False)

    def test_must_fire_fatal_through_log_reaches_stderr(self, tmp_path):
        result = self._source_and_log(tmp_path, project_dir="relative/project")
        assert "LOG_RC=0" in result.stdout, (result.stdout, result.stderr)
        assert "FATAL: unsafe REMEMBER_DIR" in result.stderr, (
            "a FATAL logged while the sink is active never reached stderr\n"
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_must_not_fire_ordinary_log_still_writes_the_file_not_stderr(self, tmp_path):
        """Positive control: an ordinary, safe REMEMBER_DIR must keep writing
        to its daily log file, never spill to stderr."""
        project = tmp_path / "project"
        result = self._source_and_log(tmp_path, project_dir=_bash_path(project))
        assert "LOG_RC=0" in result.stdout, (result.stdout, result.stderr)
        assert "FATAL: unsafe REMEMBER_DIR" not in result.stderr, result.stderr
        assert any((project / ".remember" / "logs").glob("memory-*.log")), "log() wrote nothing for a safe path"


class TestUncPathIsSafe:
    """Gap 3: a UNC project dir (two leading backslashes, server, share)
    passes _remember_normalize_win_path unchanged and is then classified
    unsafe by both guard predicates, which only recognise a leading '/' or
    a drive letter as absolute."""

    def _run_guards(self, tmp_path: Path, value: str) -> str:
        # The value travels through an ENVIRONMENT VARIABLE, not argv (#920
        # CI round 2): a raw embedded newline byte in a bare (unquoted-by-
        # list2cmdline, since it has no space/tab/quote to force quoting)
        # argv element is lost somewhere between Python's subprocess list
        # and the child's own argv on windows-latest -- observed as both
        # "hook=1"/"log=1" (SAFE) for a value that must stay refused,
        # reproduced identically on every windows-latest Python leg, never
        # on ubuntu/macos. The guard predicates themselves are unchanged and
        # correct POSIX; only the value's transport was unsafe for a
        # control byte. An environment variable's value is not re-tokenized
        # the way a win32 command line is, so it survives intact.
        script = tmp_path / "unc_probe.sh"
        body = (
            "set +e\n"
            'REMEMBER_DIR="$REMEMBER_DIR_PROBE_VALUE"\n'
            '_remember_dir_is_unsafe; echo "hook=$?"\n'
            '_remember_log_dir_unsafe; echo "log=$?"\n'
        )
        script.write_text(
            _extract_function(HOOK, "_remember_dir_is_unsafe")
            + _extract_function(LOG_SH, "_remember_log_dir_unsafe")
            + body,
            encoding="utf-8",
        )
        env = dict(os.environ, REMEMBER_DIR_PROBE_VALUE=value)
        result = subprocess.run(
            [_BASH, _bash_path(script)], capture_output=True, text=True, timeout=30, check=False, env=env,
        )
        return result.stdout

    def test_must_fire_unc_path_is_accepted_as_safe(self, tmp_path):
        out = self._run_guards(tmp_path, UNC_VALUE)
        assert "hook=1" in out, (UNC_VALUE, out)   # 1 = safe
        assert "log=1" in out, (UNC_VALUE, out)

    def test_must_not_fire_relative_path_stays_refused(self, tmp_path):
        """Positive control: widening the guard for UNC must not also widen
        it for an ordinary relative value."""
        out = self._run_guards(tmp_path, "relative/path")
        assert "hook=0" in out, out   # 0 = unsafe
        assert "log=0" in out, out

    def test_must_not_fire_embedded_newline_stays_refused(self, tmp_path):
        """Positive control: a UNC-shaped prefix must not smuggle a newline
        past the guard just because the leading backslashes now pass."""
        out = self._run_guards(tmp_path, UNC_WITH_NEWLINE)
        assert "hook=0" in out, out
        assert "log=0" in out, out
