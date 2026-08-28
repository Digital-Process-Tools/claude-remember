"""#411 -- resolve-paths.sh can only resolve PROJECT_DIR from
CLAUDE_PROJECT_DIR, which Codex never sets and Gemini CLI documents no
environment variables for at all. Every hook this plugin registers already
reads `session_id` and `transcript_path` (#206, #407) off the SessionStart /
SessionEnd stdin payload; this adds the payload's `cwd` field as the fallback,
exported by the hook as REMEMBER_HOOK_CWD and consulted by resolve-paths.sh
between CLAUDE_PROJECT_DIR and the existing .claude/remember layout guess.

Precedence: CLAUDE_PROJECT_DIR, then REMEMBER_HOOK_CWD, then the local-install
derivation, then the existing loud failure. A stdin cwd disagreeing with a SET
CLAUDE_PROJECT_DIR must not win -- CLAUDE_PROJECT_DIR is the more specific
signal on the host that publishes it.

Every "falls back" case here is paired with a "does not fall back" case in the
same fixture shape, because "REMEMBER_HOOK_CWD is not used" also passes when
resolve-paths.sh crashed before it ever got there.

This is simulated, not observed: no Codex or Gemini binary is installed on
this machine. Unsetting CLAUDE_PROJECT_DIR and supplying REMEMBER_HOOK_CWD
directly is a real test of the precedence chain in resolve-paths.sh -- it is
not a test that either host's hook actually populates that variable the way
this suite assumes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"


def _run(tmp_path: Path, cwd: Path, *, claude_project_dir: str | None,
         remember_hook_cwd: str | None):
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_PATHS_SOFT_FAIL": "1",
    }
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("REMEMBER_HOOK_CWD", None)
    if claude_project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = claude_project_dir
    if remember_hook_cwd is not None:
        env["REMEMBER_HOOK_CWD"] = remember_hook_cwd
    script = (
        f'source "{RESOLVE_PATHS}"; echo "STATUS=$?"; '
        f'echo "PROJECT_DIR=${{PROJECT_DIR:-unset}}"'
    )
    return subprocess.run(["bash", "-c", script], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=30, check=False)


# --- positive control: CLAUDE_PROJECT_DIR alone still resolves -------------

def test_claude_project_dir_alone_still_resolves(tmp_path):
    project = tmp_path / "claude-project"
    project.mkdir()
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()

    result = _run(tmp_path, cwd, claude_project_dir=str(project),
                  remember_hook_cwd=None)

    assert f"PROJECT_DIR={project}" in result.stdout, result.stdout + result.stderr


# --- the fallback: REMEMBER_HOOK_CWD alone resolves what CLAUDE_PROJECT_DIR
# used to leave unresolved -- this is the #411 gap itself -------------------

def test_hook_cwd_resolves_when_claude_project_dir_is_unset(tmp_path):
    project = tmp_path / "codex-project"
    project.mkdir()
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()

    result = _run(tmp_path, cwd, claude_project_dir=None,
                  remember_hook_cwd=str(project))

    assert "STATUS=0" in result.stdout, result.stdout + result.stderr
    assert f"PROJECT_DIR={project}" in result.stdout, result.stdout + result.stderr


# --- precedence: a SET CLAUDE_PROJECT_DIR wins over a disagreeing cwd ------

def test_claude_project_dir_wins_over_disagreeing_hook_cwd(tmp_path):
    winner = tmp_path / "claude-project"
    winner.mkdir()
    loser = tmp_path / "stdin-cwd"
    loser.mkdir()
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()

    result = _run(tmp_path, cwd, claude_project_dir=str(winner),
                  remember_hook_cwd=str(loser))

    assert f"PROJECT_DIR={winner}" in result.stdout, result.stdout + result.stderr
    assert str(loser) not in result.stdout


# --- an unusable REMEMBER_HOOK_CWD must degrade to today's behaviour -------

@pytest.mark.parametrize("value", [
    pytest.param("", id="empty"),
    pytest.param(None, id="unset"),
])
def test_blank_or_unset_hook_cwd_falls_back_to_the_existing_failure(tmp_path, value):
    """No CLAUDE_PROJECT_DIR, no usable REMEMBER_HOOK_CWD, and PIPELINE_DIR
    is not a local .claude/remember/ layout (CLAUDE_PLUGIN_ROOT is the
    checkout itself) -- the same "cannot guess" branch the existing suite
    pins. Must still refuse, not silently resolve to something."""
    cwd = tmp_path / "somewhere"
    cwd.mkdir()

    result = _run(tmp_path, cwd, claude_project_dir=None, remember_hook_cwd=value)

    assert "STATUS=1" in result.stdout, result.stdout + result.stderr
    assert "PROJECT_DIR=unset" in result.stdout


def test_nonexistent_hook_cwd_falls_back_to_the_existing_failure(tmp_path):
    """A directory that does not exist is not silently accepted as the
    project root -- same bar REMEMBER_TRANSCRIPT_PATH's own validation
    holds (#407: an unusable supplied value must degrade to derivation, not
    be trusted verbatim)."""
    cwd = tmp_path / "somewhere"
    cwd.mkdir()
    gone = tmp_path / "never-created"

    result = _run(tmp_path, cwd, claude_project_dir=None, remember_hook_cwd=str(gone))

    assert "STATUS=1" in result.stdout, result.stdout + result.stderr
    assert "PROJECT_DIR=unset" in result.stdout


def test_a_file_as_hook_cwd_falls_back_to_the_existing_failure(tmp_path):
    """A regular file exists and is readable; it is still not a directory a
    project root can be."""
    cwd = tmp_path / "somewhere"
    cwd.mkdir()
    not_a_dir = tmp_path / "a-file"
    not_a_dir.write_text("", encoding="utf-8")

    result = _run(tmp_path, cwd, claude_project_dir=None,
                  remember_hook_cwd=str(not_a_dir))

    assert "STATUS=1" in result.stdout, result.stdout + result.stderr
    assert "PROJECT_DIR=unset" in result.stdout
