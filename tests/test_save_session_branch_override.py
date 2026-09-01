"""Shell-level tests for the BRANCH resolution block in scripts/save-session.sh.

The block defines the ``| <branch>`` identity slot of each
``## HH:MM | <branch>`` memory header. It must satisfy a five-case truth
table:

  1. ``$REMEMBER_BRANCH`` is set                 -> use it (env wins).
  2. ``$REMEMBER_BRANCH`` unset, ``$REMEMBER_BRANCH_CMD`` set and it
     prints a non-empty value on stdout            -> use that value.
  3. ``$REMEMBER_BRANCH`` unset, ``$REMEMBER_BRANCH_CMD`` set but it exits
     non-zero, or prints nothing                    -> fall through past it.
  4. Neither set, ``$PROJECT_DIR`` is a git repo   -> ``git branch --show-current``.
  5. Neither set, no git repo                       -> literal ``unknown``.

Plus: ``$REMEMBER_BRANCH`` set to the empty string is treated as unset
(``${VAR:-default}``, not ``${VAR-default}``), and ``$REMEMBER_BRANCH_CMD``
is invoked as ``$REMEMBER_BRANCH_CMD "$SESSION_ID"`` (#481) -- concurrent
sessions of one project share ``$PROJECT_DIR``, so the git fallback alone
cannot distinguish them; ``$SESSION_ID`` is the value already in scope that
does.

Tests source the exact resolution block out of the live ``save-session.sh``
file (between the ``BRANCH RESOLUTION START``/``END`` markers) rather than
reasserting a copy here -- if the block ever changes without an intentional
test update, these cases fail loudly.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash dispatch + git command form -- not portable to Windows Git Bash without fixtures",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_SH = REPO_ROOT / "scripts" / "save-session.sh"

START_MARKER = "# BRANCH RESOLUTION START"
END_MARKER = "# BRANCH RESOLUTION END"


def _extract_branch_block() -> str:
    """Return the live BRANCH-resolution block from save-session.sh.

    Picking it out of the file (not asserting a hardcoded copy in the
    test) means future edits to the block break this test on intent,
    not on string-equality drift.
    """
    lines = SAVE_SH.read_text().splitlines()
    start = end = None
    for i, raw in enumerate(lines):
        if raw.strip().startswith(START_MARKER):
            start = i
        elif raw.strip().startswith(END_MARKER):
            end = i
            break
    if start is None or end is None:
        raise AssertionError(
            f"Could not find {START_MARKER!r}/{END_MARKER!r} markers in {SAVE_SH}"
        )
    return "\n".join(lines[start : end + 1])


def _eval_branch_raw(
    project_dir: Path,
    env_overrides: dict[str, str | None],
    session_id: str = "abc123",
) -> subprocess.CompletedProcess[str]:
    """Eval ONLY the patched BRANCH-resolution block under controlled env,
    return the full CompletedProcess (stdout=$BRANCH, stderr=log() calls).

    The block now calls the real ``log`` function on a configured-but-failing
    ``$REMEMBER_BRANCH_CMD`` (#481 follow-up) -- under ``set -e`` an
    undefined ``log`` would abort this isolated eval with "command not
    found" the moment that path is exercised, so a stub that mirrors
    log.sh's signature (``log "$component" "$message"``) is required here,
    not optional. Writing it to stderr (not swallowing it) is what lets
    ``test_branch_cmd_failure_is_logged`` below assert on it.
    """
    block = _extract_branch_block()
    script = f"""
set -e
log() {{ printf 'LOG %s: %s\n' "$1" "$2" 1>&2; }}
export PROJECT_DIR={project_dir}
export SESSION_ID={session_id}
{block}
printf '%s' "$BRANCH"
"""
    env = {k: v for k, v in os.environ.items() if v is not None}
    # Strip any inherited override vars so we start from a known state.
    env.pop("REMEMBER_BRANCH", None)
    env.pop("REMEMBER_BRANCH_CMD", None)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, f"BRANCH eval failed: {result.stderr}"
    return result


def _eval_branch(
    project_dir: Path,
    env_overrides: dict[str, str | None],
    session_id: str = "abc123",
) -> str:
    """Convenience wrapper over ``_eval_branch_raw`` for tests that only
    care about the resolved $BRANCH value, not what got logged."""
    return _eval_branch_raw(project_dir, env_overrides, session_id).stdout


def _make_git_repo(tmp_path: Path, branch_name: str = "feature/test-branch") -> Path:
    """Initialize a tiny git repo on a known branch -- git presence is what
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


def _make_resolver(tmp_path: Path, body: str) -> Path:
    """Write an executable shell script standing in for REMEMBER_BRANCH_CMD."""
    script = tmp_path / "resolve-identity"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_env_var_wins_over_git_branch(tmp_path):
    """Case 1: $REMEMBER_BRANCH set AND repo has a real branch -> env wins."""
    project = _make_git_repo(tmp_path, branch_name="feature/should-be-ignored")
    branch = _eval_branch(project, {"REMEMBER_BRANCH": "laptop"})
    assert branch == "laptop", (
        f"REMEMBER_BRANCH should override git branch lookup; got {branch!r}"
    )


def test_env_var_wins_over_branch_cmd(tmp_path):
    """$REMEMBER_BRANCH also wins over $REMEMBER_BRANCH_CMD -- env is step 1,
    the command is step 2; a set env var never even reaches the command."""
    project = tmp_path / "not-a-repo"
    project.mkdir()
    resolver = _make_resolver(tmp_path, 'echo "from-cmd"')
    branch = _eval_branch(
        project, {"REMEMBER_BRANCH": "laptop", "REMEMBER_BRANCH_CMD": str(resolver)}
    )
    assert branch == "laptop", f"expected env to win over the command; got {branch!r}"


def test_git_branch_used_when_env_unset(tmp_path):
    """Case 4: neither var set + git repo present -> git branch used."""
    project = _make_git_repo(tmp_path, branch_name="release/2026-06")
    branch = _eval_branch(project, {"REMEMBER_BRANCH": None})
    assert branch == "release/2026-06", (
        f"Expected git branch 'release/2026-06'; got {branch!r}"
    )


def test_unknown_fallback_when_no_git_and_no_env(tmp_path):
    """Case 5: neither var set + $PROJECT_DIR not a git repo -> literal
    'unknown'.

    This is the rot the env var was added to address -- surfacing it
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
    """Case: $REMEMBER_BRANCH='' must NOT propagate the empty string.

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


def test_branch_cmd_used_when_env_unset(tmp_path):
    """Case 2: $REMEMBER_BRANCH unset, $REMEMBER_BRANCH_CMD set and prints a
    value -> use it, even though $PROJECT_DIR IS a git repo (the command
    wins over the git fallback -- it is step 2, git is step 3+)."""
    project = _make_git_repo(tmp_path, branch_name="feature/some-branch")
    resolver = _make_resolver(tmp_path, 'echo "minor-24"')
    branch = _eval_branch(project, {"REMEMBER_BRANCH_CMD": str(resolver)})
    assert branch == "minor-24", (
        f"expected the resolver's output to be used; got {branch!r}"
    )


def test_branch_cmd_receives_session_id(tmp_path):
    """The whole point of #481: the command must be handed $SESSION_ID as
    its first argument, since that is the value that differs per writer
    when several sessions share one $PROJECT_DIR."""
    project = tmp_path / "not-a-repo"
    project.mkdir()
    resolver = _make_resolver(tmp_path, 'echo "session-is-$1"')
    branch = _eval_branch(
        project, {"REMEMBER_BRANCH_CMD": str(resolver)}, session_id="deadbeef"
    )
    assert branch == "session-is-deadbeef", (
        f"resolver did not receive $SESSION_ID as $1; got {branch!r}"
    )


def test_branch_cmd_nonzero_exit_falls_through_to_git(tmp_path):
    """Case 3a: the command exits non-zero -> fall through past it to the
    git branch (not to 'unknown', since a real git repo IS present), AND
    the failure is logged -- a configured resolver that starts failing must
    read as a reported fault, not silently as "never configured" (audit
    finding on #481: the original fall-through was completely silent)."""
    project = _make_git_repo(tmp_path, branch_name="release/2026-06")
    resolver = _make_resolver(tmp_path, 'echo "should-not-be-used"; exit 1')
    result = _eval_branch_raw(project, {"REMEMBER_BRANCH_CMD": str(resolver)})
    assert result.stdout == "release/2026-06", (
        f"a failing resolver must not win; expected git fallback, got {result.stdout!r}"
    )
    assert "REMEMBER_BRANCH_CMD" in result.stderr and "branch" in result.stderr, (
        f"a configured-but-failing resolver must be logged; got stderr={result.stderr!r}"
    )


def test_branch_cmd_empty_stdout_falls_through_to_git(tmp_path):
    """Case 3b: the command exits 0 but prints nothing -> fall through past
    it to the git branch, same as a non-zero exit, and logged the same way.
    An empty identity slot (`## HH:MM | ` with nothing after the separator)
    is exactly the failure mode #481 exists to prevent, arriving through
    this path."""
    project = _make_git_repo(tmp_path, branch_name="release/2026-06")
    resolver = _make_resolver(tmp_path, "true")
    result = _eval_branch_raw(project, {"REMEMBER_BRANCH_CMD": str(resolver)})
    assert result.stdout == "release/2026-06", (
        f"an empty-stdout resolver must not win; expected git fallback, got {result.stdout!r}"
    )
    assert "REMEMBER_BRANCH_CMD" in result.stderr and "branch" in result.stderr, (
        f"an empty-stdout resolver failure must be logged; got stderr={result.stderr!r}"
    )


def test_branch_cmd_unset_does_not_change_existing_behavior(tmp_path):
    """Positive control for the two 'falls through' tests above: with NO
    $REMEMBER_BRANCH_CMD at all, the git branch is still used exactly as
    before, AND nothing is logged -- proves the new step 2 is a true no-op
    when unconfigured (no spurious WARNING) rather than the git lookup
    silently breaking for an unrelated reason."""
    project = _make_git_repo(tmp_path, branch_name="release/2026-06")
    result = _eval_branch_raw(project, {"REMEMBER_BRANCH_CMD": None})
    assert result.stdout == "release/2026-06", (
        f"expected unchanged git-branch behavior with no resolver configured; "
        f"got {result.stdout!r}"
    )
    assert result.stderr == "", (
        f"no REMEMBER_BRANCH_CMD configured must not log anything; got {result.stderr!r}"
    )


def test_branch_cmd_success_does_not_log(tmp_path):
    """Positive control for the two failure-logs-a-WARNING tests above: a
    resolver that succeeds must not also log -- the WARNING is for the
    fall-through case specifically, not printed unconditionally whenever
    $REMEMBER_BRANCH_CMD is configured."""
    project = tmp_path / "not-a-repo"
    project.mkdir()
    resolver = _make_resolver(tmp_path, 'echo "minor-24"')
    result = _eval_branch_raw(project, {"REMEMBER_BRANCH_CMD": str(resolver)})
    assert result.stdout == "minor-24"
    assert result.stderr == "", (
        f"a succeeding resolver must not log a warning; got {result.stderr!r}"
    )


def test_branch_block_uses_safe_default_substitution_form():
    """Guard the operator: REMEMBER_BRANCH must be tested with ``-n`` on
    ``${REMEMBER_BRANCH:-}`` (empty counts as unset), not a bare
    existence check that would treat ``REMEMBER_BRANCH=""`` as set.

    A drift here would silently regress the empty-string case above.
    """
    block = _extract_branch_block()
    assert '${REMEMBER_BRANCH:-}' in block, (
        f"BRANCH resolution block must test REMEMBER_BRANCH via "
        f"'${{REMEMBER_BRANCH:-}}' (empty treated as unset); got: {block!r}"
    )
