"""Shell-level tests for the BRANCH= line in scripts/save-session.sh.

The line defines the ``| <branch>`` identity slot of each
``## HH:MM | <branch>`` memory header. It must satisfy a four-case truth
table:

  1. ``$REMEMBER_BRANCH`` is set       → use it (env wins).
  2. ``$REMEMBER_BRANCH`` is unset, ``$PROJECT_DIR`` is a git repo
                                       → use ``git branch --show-current``.
  3. ``$REMEMBER_BRANCH`` is unset, no git repo
                                       → fall back to the literal ``unknown``.
  4. ``$REMEMBER_BRANCH`` is set to the empty string
                                       → ``${VAR:-default}`` treats empty as
                                         unset, so we still walk down to the
                                         git/unknown fallback (NOT propagate
                                         the empty string into the header).

Tests source the exact ``BRANCH=`` line out of the live ``save-session.sh``
file rather than reasserting a copy here — if the line ever changes
without an intentional test update, these cases fail loudly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash dispatch + git command form — not portable to Windows Git Bash without fixtures",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_SH = REPO_ROOT / "scripts" / "save-session.sh"


def _extract_branch_line() -> str:
    """Return the live `BRANCH=` line from save-session.sh.

    Picking it out of the file (not asserting a hardcoded copy in the
    test) means future edits to the line break this test on intent,
    not on string-equality drift.
    """
    for raw in SAVE_SH.read_text().splitlines():
        line = raw.strip()
        if line.startswith("BRANCH="):
            return line
    raise AssertionError(f"No BRANCH= line found in {SAVE_SH}")


def _eval_branch(project_dir: Path, env_overrides: dict[str, str | None]) -> str:
    """Eval ONLY the patched BRANCH= line under controlled env, return the
    resolved value."""
    line = _extract_branch_line()
    script = f"""
set -e
export PROJECT_DIR={project_dir}
{line}
printf '%s' "$BRANCH"
"""
    env = {k: v for k, v in os.environ.items() if v is not None}
    # Strip any inherited REMEMBER_BRANCH so we start from a known state.
    env.pop("REMEMBER_BRANCH", None)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, f"BRANCH eval failed: {result.stderr}"
    return result.stdout


def _make_git_repo(tmp_path: Path, branch_name: str = "feature/test-branch") -> Path:
    """Initialize a tiny git repo on a known branch — git presence is what
    the fallback chain checks for."""
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "--quiet"],
        cwd=project, check=True,
    )
    # Local config so the commit succeeds without relying on the host's
    # user.email / user.name.
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    (project / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--quiet"],
        cwd=project, check=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", branch_name, "--quiet"],
        cwd=project, check=True,
    )
    return project


def test_env_var_wins_over_git_branch(tmp_path):
    """Case 1: $REMEMBER_BRANCH set AND repo has a real branch → env wins."""
    project = _make_git_repo(tmp_path, branch_name="feature/should-be-ignored")
    branch = _eval_branch(project, {"REMEMBER_BRANCH": "laptop"})
    assert branch == "laptop", (
        f"REMEMBER_BRANCH should override git branch lookup; got {branch!r}"
    )


def test_git_branch_used_when_env_unset(tmp_path):
    """Case 2: $REMEMBER_BRANCH unset + git repo present → git branch used."""
    project = _make_git_repo(tmp_path, branch_name="release/2026-06")
    branch = _eval_branch(project, {"REMEMBER_BRANCH": None})
    assert branch == "release/2026-06", (
        f"Expected git branch 'release/2026-06'; got {branch!r}"
    )


def test_unknown_fallback_when_no_git_and_no_env(tmp_path):
    """Case 3: $REMEMBER_BRANCH unset + $PROJECT_DIR not a git repo →
    literal 'unknown'.

    This is the rot the env var was added to address — surfacing it
    here so a future refactor of the fallback string doesn't silently
    flip the behavior.
    """
    project = tmp_path / "not-a-repo"
    project.mkdir()
    branch = _eval_branch(project, {"REMEMBER_BRANCH": None})
    assert branch == "unknown", (
        f"Expected literal 'unknown' fallback; got {branch!r}"
    )


def test_empty_env_var_treated_as_unset(tmp_path):
    """Case 4: $REMEMBER_BRANCH='' must NOT propagate the empty string.

    Bash's ``${VAR:-default}`` (the ``:-`` form, not ``-``) treats an
    empty value as unset. If someone accidentally exports
    ``REMEMBER_BRANCH=`` (e.g., a malformed shell rc line), the header
    must still fall back to git/unknown rather than write
    ``## HH:MM | `` with a bare separator.
    """
    project = tmp_path / "not-a-repo"
    project.mkdir()
    branch = _eval_branch(project, {"REMEMBER_BRANCH": ""})
    assert branch == "unknown", (
        f"Empty REMEMBER_BRANCH should be treated as unset (`:-` form), "
        f"falling back to 'unknown'; got {branch!r}"
    )


def test_branch_line_uses_safe_default_substitution_form():
    """Guard the operator: the line MUST use ``:-`` (treats empty as
    unset), not bare ``-`` (treats empty as set-to-empty).

    A drift from ``${REMEMBER_BRANCH:-...}`` to ``${REMEMBER_BRANCH-...}``
    would silently regress case 4. Pinning the operator here makes
    that drift visible in code review even before tests run.
    """
    line = _extract_branch_line()
    assert "${REMEMBER_BRANCH:-" in line, (
        f"BRANCH= line must use ':-' default-substitution form (treats "
        f"empty as unset); got: {line!r}"
    )
