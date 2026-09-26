"""
#802: scripts/doctor.sh resolves CLAUDE_PROJECT_DIR with a raw $PWD fallback in
two places (the --json path and the human-readable path) when the variable is
unset -- the same shape #743 fixed in write-handoff.sh. If an earlier `cd` in
the same shell/tool call already moved the cwd into a subdirectory, doctor.sh
silently reports on that subdirectory instead of the real project root.

Fix: prefer `git rev-parse --show-toplevel` over the raw $PWD at both fallback
sites, falling back to $PWD only when the cwd is not inside a git repo.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX doctor.sh -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"


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


def _run_doctor_no_project_dir(cwd: Path, home: Path, *extra_args):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(DOCTOR), *extra_args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


class TestJsonProjectDirFallsBackToGitRootNotStalePwd:

    def test_a_subdirectory_cwd_resolves_to_the_git_root(self, tmp_path):
        """CLAUDE_PROJECT_DIR unset, cwd is a subdirectory of a git repo -- the
        shape a `cd` earlier in the same Bash-tool call leaves behind. The
        --json report's project_dir must be the repo root, not the
        subdirectory -- the exact #802 miswrite."""
        project, home, sub = _sandbox_git_repo(tmp_path)

        result = _run_doctor_no_project_dir(sub, home, "--json")

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload.get("project_dir") == str(project), (
            f"expected project_dir to be the git root {project!r}, "
            f"got {payload.get('project_dir')!r} (cwd was the subdirectory "
            f"{sub!r}) -- stderr: {result.stderr!r}"
        )

    def test_positive_control_project_root_cwd_still_works(self, tmp_path):
        """Positive control: running from the project root itself (no `cd`
        drift) must still resolve correctly -- otherwise a fix could pass the
        subdirectory case above by breaking the common case instead."""
        project, home, _sub = _sandbox_git_repo(tmp_path)

        result = _run_doctor_no_project_dir(project, home, "--json")

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload.get("project_dir") == str(project)


class TestHumanReportProjectDirFallsBackToGitRootNotStalePwd:

    def test_a_subdirectory_cwd_resolves_to_the_git_root(self, tmp_path):
        """Same #802 shape, human-readable report path (doctor.sh:224-229):
        the resolved project root reported in the plain-text output must be
        the git root, not the subdirectory cwd."""
        project, home, sub = _sandbox_git_repo(tmp_path)

        result = _run_doctor_no_project_dir(sub, home)

        assert result.returncode == 0, result.stderr
        assert f"Project dir: {project}" in result.stdout or str(project) in result.stdout, (
            f"expected the human report to name the git root {project!r}, "
            f"not the subdirectory cwd {sub!r}.\nstdout: {result.stdout!r}"
        )
        assert str(sub) not in result.stdout, (
            "the report named the subdirectory cwd instead of the git root -- "
            "exactly the #802 miswrite"
        )

    def test_positive_control_project_root_cwd_still_works(self, tmp_path):
        """Positive control for the human-readable path."""
        project, home, _sub = _sandbox_git_repo(tmp_path)

        result = _run_doctor_no_project_dir(project, home)

        assert result.returncode == 0, result.stderr
        assert str(project) in result.stdout
