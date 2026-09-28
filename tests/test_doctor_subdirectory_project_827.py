"""
#827 (finding 2 of the carried-forward v0.35.1 release audit): doctor.sh's
#802 git-root preference does not carry #776's subdirectory-project
disambiguation.

write-handoff.sh's own #776 fix (tests/test_write_handoff_subdirectory_project_776.py)
disambiguates a project deliberately started from a repository subdirectory by
checking which candidate directory's own REMEMBER_DIR already carries THIS
session's session-keyed handoff hint (#738). doctor.sh's --json and human-report
project-dir resolution (scripts/doctor.sh, added by #802) copied #743's blanket
git-root preference without carrying that disambiguation, so `/remember:doctor`
run from inside such a subdirectory project (with CLAUDE_PROJECT_DIR unset)
reports on the enclosing repository's own store instead of the project's own.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX doctor.sh script -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR_SCRIPT = REPO_ROOT / "scripts" / "doctor.sh"
SESSION_ID = "subdir-session-xyz789"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _sandbox_git_repo_with_subproject(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _git("init", "-q", cwd=project)
    _git("config", "user.email", "test@example.com", cwd=project)
    _git("config", "user.name", "Test", cwd=project)

    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)

    sub_project = project / "pkg"
    sub_remember = sub_project / ".remember"
    (sub_remember / "tmp").mkdir(parents=True)
    (sub_remember / "config.json").write_text(json.dumps({"data_dir": ".remember"}))
    sub_target = sub_remember / "remember.md"
    (sub_remember / "tmp" / f"handoff-path.{SESSION_ID}").write_text(str(sub_target))

    return project, sub_project, home


def _run_doctor_json(cwd: Path, home: Path, session_id: str = SESSION_ID) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env["HOME"] = str(home)
    if session_id:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    else:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    result = subprocess.run(
        ["bash", str(DOCTOR_SCRIPT), "--json"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    return json.loads(result.stdout)


class TestDoctorSubdirectoryProjectIsRoutedToItsOwnStore:

    def test_a_subdirectory_project_with_a_published_session_hint_is_honoured(self, tmp_path):
        project, sub_project, home = _sandbox_git_repo_with_subproject(tmp_path)

        report = _run_doctor_json(sub_project, home)

        assert report["project_dir"] == str(sub_project), (
            "doctor.sh --json reported the enclosing repository's root as "
            "project_dir instead of the subdirectory project's own -- the "
            "#776 defect, uncarried into doctor.sh's #802 fix.\n"
            f"report: {report}"
        )

    def test_positive_control_no_session_hint_still_prefers_git_root(self, tmp_path):
        """No session hint exists ANYWHERE for this session (the #743/#802
        shape: an accidental `cd`, not a deliberate subdirectory project) --
        project_dir must still be the git root, exactly as #802 left it."""
        project, sub_project, home = _sandbox_git_repo_with_subproject(tmp_path)
        assert project.is_dir()  # used below for the assertion; named for clarity
        (sub_project / ".remember" / "tmp" / f"handoff-path.{SESSION_ID}").unlink()

        report = _run_doctor_json(sub_project, home)

        assert report["project_dir"] == str(project), (
            "expected the #802 behaviour (git root) when no session hint "
            f"exists anywhere.\nreport: {report}"
        )
