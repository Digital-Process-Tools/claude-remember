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
import time
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


def _sandbox_git_repo_with_deep_subproject(tmp_path: Path):
    """Like _sandbox_git_repo_with_subproject, but with an extra directory
    (`pkg/deep`) below the subdirectory project the session actually
    started in -- the #1058 shape, ported to doctor.sh (#1082)."""
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

    deep = sub_project / "deep"
    deep.mkdir()

    return project, sub_project, deep, home


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


class TestDoctorFreshestHintWinsOnResume1058Review:
    """Review finding on the doctor.sh port of #1058/#1082: the write-handoff.sh
    sibling got a resume-shaped regression test (TestFreshestHintWinsOnResume1058Review
    in test_write_handoff_subdirectory_project_776.py) exercising the freshest-mtime
    vs. nearest-ancestor distinction; this file's own deeper-cwd test never put a
    hint at more than one ancestor at once, so it could not tell freshest-wins from
    nearest-wins. This class closes that gap directly."""

    def test_a_fresher_hint_at_the_toplevel_beats_a_stale_one_at_a_nearer_ancestor(self, tmp_path):
        project, sub_project, deep, home = _sandbox_git_repo_with_deep_subproject(tmp_path)

        sub_hint = sub_project / ".remember" / "tmp" / f"handoff-path.{SESSION_ID}"
        old_time = time.time() - 3600
        os.utime(sub_hint, (old_time, old_time))

        root_remember = project / ".remember"
        (root_remember / "tmp").mkdir(parents=True)
        (root_remember / "config.json").write_text(json.dumps({"data_dir": ".remember"}))
        (root_remember / "tmp" / f"handoff-path.{SESSION_ID}").write_text(
            str(root_remember / "remember.md")
        )

        report = _run_doctor_json(deep, home)

        assert report["project_dir"] == str(project), (
            "expected the FRESHER hint (the repo root) to win over the STALE "
            f"one at the nearer ancestor (pkg).\nreport: {report}"
        )

    def test_positive_control_the_only_hint_present_still_wins_regardless_of_mtime(self, tmp_path):
        _project, sub_project, deep, home = _sandbox_git_repo_with_deep_subproject(tmp_path)

        report = _run_doctor_json(deep, home)

        assert report["project_dir"] == str(sub_project)


class TestDoctorDeeperCwdThanSessionStartDir1058:
    """#1058/#1082: doctor.sh's resolver must report the same store
    write-handoff.sh would actually write into when $(pwd) has moved past
    the subdirectory project the session started in -- not just the
    original #827 two-candidate shape."""

    def test_a_cwd_deeper_than_the_session_start_dir_is_still_reported_correctly(self, tmp_path):
        _project, sub_project, deep, home = _sandbox_git_repo_with_deep_subproject(tmp_path)

        report = _run_doctor_json(deep, home)

        assert report["project_dir"] == str(sub_project), (
            "doctor.sh --json reported the enclosing repository's own "
            "store instead of the subdirectory project's own, from a cwd "
            "deeper than the session's start dir -- the #1058 defect, "
            "uncarried into doctor.sh.\n"
            f"report: {report}"
        )

    def test_positive_control_no_hint_anywhere_still_falls_back_to_git_root(self, tmp_path):
        project, sub_project, deep, home = _sandbox_git_repo_with_deep_subproject(tmp_path)
        (sub_project / ".remember" / "tmp" / f"handoff-path.{SESSION_ID}").unlink()

        report = _run_doctor_json(deep, home)

        assert report["project_dir"] == str(project), (
            "expected the fallback (git root) when no session hint exists "
            f"anywhere.\nreport: {report}"
        )
