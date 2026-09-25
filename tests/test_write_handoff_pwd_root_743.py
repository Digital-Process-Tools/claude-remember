"""
#743: write-handoff.sh rooted resolution at $PWD when CLAUDE_PROJECT_DIR was
unset -- but the Bash tool's cwd persists after a `cd` earlier in the same
tool call, so a session that changed directory before running /remember
wrote the note under that subdirectory's own .remember/, a place SessionStart
(keyed to the project root) never reads.

Fix: when CLAUDE_PROJECT_DIR is unset, prefer `git rev-parse --show-toplevel`
over the raw $PWD, falling back to $PWD only when that is not a git repo.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX write-handoff script -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WRITE_HANDOFF_SCRIPT = REPO_ROOT / "scripts" / "write-handoff.sh"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _sandbox_git_repo(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _git("init", "-q", cwd=project)
    _git("config", "user.email", "test@example.com", cwd=project)
    _git("config", "user.name", "Test", cwd=project)
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    sub = project / "src" / "sub"
    sub.mkdir(parents=True)
    return project, home, sub


def _write_handoff_no_project_dir(cwd: Path, home: Path, note: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(WRITE_HANDOFF_SCRIPT)],
        input=note,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestProjectDirFallsBackToGitRootNotStalePwd:

    def test_a_subdirectory_cwd_resolves_to_the_git_root(self, tmp_path):
        """CLAUDE_PROJECT_DIR unset, cwd is a subdirectory of a git repo --
        the shape a `cd` earlier in the same Bash-tool call leaves behind.
        The note must land at the repo root's .remember/, not the
        subdirectory's own -- the exact miswrite #743 reported."""
        project, home, sub = _sandbox_git_repo(tmp_path)

        result = _write_handoff_no_project_dir(sub, home, "note from a subdir\n")

        assert result.returncode == 0, result.stderr
        root_target = project / ".remember" / "remember.md"
        stray_target = sub / ".remember" / "remember.md"
        assert root_target.exists() and "note from a subdir" in root_target.read_text(), (
            f"expected the note at the git root.\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        assert not stray_target.exists(), (
            "note was written under the subdirectory instead of the git "
            "root -- exactly the #743 miswrite"
        )

    def test_positive_control_project_root_cwd_still_works(self, tmp_path):
        """Positive control: running from the project root itself (no `cd`
        drift) must still resolve correctly -- otherwise a fix could pass
        the subdirectory test above by breaking the common case instead."""
        project, home, _sub = _sandbox_git_repo(tmp_path)

        result = _write_handoff_no_project_dir(project, home, "note from root\n")

        assert result.returncode == 0, result.stderr
        target = project / ".remember" / "remember.md"
        assert target.exists() and "note from root" in target.read_text()
