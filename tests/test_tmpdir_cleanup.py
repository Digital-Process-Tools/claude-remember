"""Regression tests: no remember-config-<pid>.json may survive.

lib-memory-dir.sh creates a per-invocation merged-config file at source time
and registers a chaining EXIT trap to remove it.  Scripts that later install
their own plain ``trap ... EXIT`` (save-session.sh, run-consolidation.sh)
REPLACE that trap — bash keeps a single EXIT trap — so the merged-config file
used to be orphaned on every exit taken after the script's own trap was
installed.  These tests run the real scripts in an isolated TMPDIR and assert
nothing is left behind.

Since #362 the file itself normally lives under ``$REMEMBER_DIR/tmp`` (a
directory this plugin owns) rather than directly in ``$TMPDIR``, falling back
to ``$TMPDIR`` only when ``$REMEMBER_DIR/tmp`` cannot be created.  These tests
check both locations, so a regression to either the new home or the old
fallback still fails loudly here rather than passing on a stray-count of zero
that nothing was ever written to explain.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_SH = REPO_ROOT / "scripts" / "save-session.sh"
CONSOLIDATION_SH = REPO_ROOT / "scripts" / "run-consolidation.sh"
RESOLVE_PATHS_SH = REPO_ROOT / "scripts" / "resolve-paths.sh"
BOOTSTRAP_DIRS_SH = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"


def _bash_path(p) -> str:
    """`C:\\Users\\x` -> `C:/Users/x`; unchanged on POSIX (see test_security_fixes)."""
    return str(p).replace("\\", "/")


def _find_bash():
    if sys.platform != "win32":
        return "bash"
    import shutil
    candidates = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "bash.exe")
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    resolved = shutil.which("bash")
    if resolved and "git" in resolved.replace("\\", "/").lower():
        return resolved
    return None


_BASH = _find_bash()
pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")


def _run_and_collect_strays(tmp_path, inner_commands: str):
    """Source resolve-paths + bootstrap-dirs in an isolated TMPDIR, run
    ``inner_commands``, and return the remember-config-* files that survive
    after the outer shell (and everything it started) has exited."""
    isolated_tmp = tmp_path / "tmp"
    isolated_tmp.mkdir()
    project = tmp_path / "proj"
    (project / ".claude" / "remember").mkdir(parents=True)
    (project / ".remember").mkdir(parents=True)

    script = f"""
set -e
export CLAUDE_PROJECT_DIR="{_bash_path(project)}"
export PIPELINE_DIR="{_bash_path(REPO_ROOT)}"
source "{_bash_path(RESOLVE_PATHS_SH)}"
export TMPDIR="{_bash_path(isolated_tmp)}"
source "{_bash_path(BOOTSTRAP_DIRS_SH)}"
{inner_commands}
"""
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": _bash_path(tmp_path)},
        check=False,
    )
    assert result.returncode == 0, (
        f"script failed: rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # #362: the file's normal home is now $REMEMBER_DIR/tmp, with $TMPDIR only
    # as a fallback -- check both, so a stray in either place is caught.
    remember_tmp = project / ".remember" / "tmp"
    return sorted(isolated_tmp.glob("remember-config-*.json")) + sorted(
        remember_tmp.glob("remember-config-*.json")
    )


def test_save_session_exit_cleans_merged_config(tmp_path):
    """save-session.sh must not orphan its remember-config file on the
    lock-skip path (any path after its ``trap cleanup EXIT`` leaks alike)."""
    strays = _run_and_collect_strays(
        tmp_path,
        f"""
mkdir -p "$REMEMBER_DIR/tmp"
echo $$ > "$REMEMBER_DIR/tmp/save.lock"
bash "{_bash_path(SAVE_SH)}"
""",
    )
    assert strays == [], (
        f"save-session.sh orphaned merged-config file(s) in TMPDIR: {strays} "
        "— its plain EXIT trap replaced lib-memory-dir.sh's cleanup trap"
    )


def test_run_consolidation_exit_cleans_merged_config(tmp_path):
    """run-consolidation.sh must not orphan its remember-config file on the
    no-staging path (taken after its lock-cleanup ``trap ... EXIT``)."""
    strays = _run_and_collect_strays(
        tmp_path,
        f'bash "{_bash_path(CONSOLIDATION_SH)}"',
    )
    assert strays == [], (
        f"run-consolidation.sh orphaned merged-config file(s) in TMPDIR: {strays} "
        "— its lock-cleanup EXIT trap replaced lib-memory-dir.sh's cleanup trap"
    )
