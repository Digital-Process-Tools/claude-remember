"""Tests for scripts/resolve-paths.sh — pins the strict no-guessing refusal
that doctor.sh's #207 fix must not weaken.

doctor.sh now defaults CLAUDE_PROJECT_DIR to $PWD when unset, but that default
lives in doctor.sh only. Every other caller (the three Claude Code hooks) must
keep getting the loud, no-guess failure — a wrong guess there writes memory
into the wrong project, which is worse than a crash.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"


def _run_soft_fail(tmp_path: Path, cwd: Path):
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_PATHS_SOFT_FAIL": "1",
    }
    env.pop("CLAUDE_PROJECT_DIR", None)
    script = f'source "{RESOLVE_PATHS}"; echo "STATUS=$?"; echo "PROJECT_DIR=${{PROJECT_DIR:-unset}}"'
    return subprocess.run(["bash", "-c", script], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=30)


def test_soft_fail_caller_still_refuses_to_guess_without_claude_project_dir(tmp_path):
    """REPO_ROOT (CLAUDE_PLUGIN_ROOT) is the checkout itself, not a
    $PROJECT/.claude/remember layout, so this hits the same "cannot guess"
    branch doctor.sh used to fatal on. A non-doctor caller must still get
    STATUS=1 and an unset PROJECT_DIR — no default, no guess."""
    cwd = tmp_path / "somewhere"
    cwd.mkdir(parents=True)

    result = _run_soft_fail(tmp_path, cwd)

    assert "STATUS=1" in result.stdout, (
        f"resolve-paths.sh stopped refusing to guess for a non-doctor caller:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "PROJECT_DIR=unset" in result.stdout


def test_loud_default_still_exits_the_caller(tmp_path):
    """Without the soft-fail opt-in (the default, and what every caller other
    than the three hooks and doctor.sh gets), resolution failure must still
    exit the caller with 1 — not return control with unresolved paths."""
    cwd = tmp_path / "somewhere"
    cwd.mkdir(parents=True)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    }
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("REMEMBER_PATHS_SOFT_FAIL", None)

    result = subprocess.run(
        ["bash", "-c", f'source "{RESOLVE_PATHS}"; echo "UNREACHABLE"'],
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 1
    assert "UNREACHABLE" not in result.stdout
    assert "FATAL: Cannot resolve project root" in result.stderr
