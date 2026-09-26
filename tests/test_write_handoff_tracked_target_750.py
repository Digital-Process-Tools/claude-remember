"""
#750: write-handoff.sh must refuse to write into a target the repository's
own git index already tracks.

#721's read-side guard (_remember_may_inject, lib-memory-context.sh) refuses
to INJECT a memory file the repository's own git index tracks, on the theory
that a tracked file "was shipped by the repository, not written by your own
/remember". Before this fix, nothing on the WRITE side asked the same
question: write-handoff.sh would happily overwrite a tracked remember.md,
report success, and the very next SessionStart would then refuse to show the
operator's own note back to them -- falsely blaming a commit they never made.

Two properties:

  - a tracked target is REFUSED (nonzero exit, a REFUSED line on stderr) and
    its committed content is left untouched -- never silently overwritten
  - positive control: an untracked target in the very same git repository is
    still accepted and written -- so this suite cannot pass on a script that
    refuses every write once it merely sees a `.git` directory
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX write-handoff script — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WRITE_HANDOFF_SCRIPT = REPO_ROOT / "scripts" / "write-handoff.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _sandbox_git_repo(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")

    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)
    (home / ".remember" / "config.json").write_text(
        json.dumps({"data_dir": ".remember"})
    )
    (home / ".claude" / "projects").mkdir(parents=True)
    return project, home


def _write_handoff(project: Path, home: Path, note: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
    }
    return subprocess.run(
        ["bash", str(WRITE_HANDOFF_SCRIPT)],
        input=note,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestTrackedTargetIsRefused:

    def test_a_tracked_remember_md_is_refused_and_left_untouched(self, tmp_path):
        project, home = _sandbox_git_repo(tmp_path)
        remember_dir = project / ".remember"
        remember_dir.mkdir()
        target = remember_dir / "remember.md"
        target.write_text("shipped by the repository, not the operator\n")
        _git(project, "add", ".remember/remember.md")
        _git(project, "commit", "-q", "-m", "ship a tracked remember.md")

        result = _write_handoff(project, home, "operator's own note\n")

        assert result.returncode != 0, (
            f"a tracked target must be refused, not written.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "REFUSED" in result.stderr
        assert target.read_text() == "shipped by the repository, not the operator\n", (
            "the tracked file's committed content must be left untouched"
        )
        status = subprocess.run(
            ["git", "-C", str(project), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert status == "", (
            f"a refused write must not leave the tracked file modified in the "
            f"working tree.\ngit status --porcelain:\n{status}"
        )

    def test_an_untracked_target_in_the_same_repo_is_still_written(self, tmp_path):
        """Positive control: the same git repository, but the target itself
        is not tracked. Without this, the test above could pass on a script
        that refuses every write the instant it sees a `.git` directory
        anywhere above the target, not only a genuinely tracked one."""
        project, home = _sandbox_git_repo(tmp_path)
        # Something else committed, so the repository is not empty -- the
        # refusal above must be about THIS file being tracked, not merely
        # about the repository existing or being empty.
        (project / "README.md").write_text("unrelated\n")
        _git(project, "add", "README.md")
        _git(project, "commit", "-q", "-m", "unrelated commit")

        result = _write_handoff(project, home, "operator's own note\n")

        assert result.returncode == 0, (
            f"an untracked target in a real git repo must still be written.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        target = project / ".remember" / "remember.md"
        assert target.exists() and target.read_text() == "operator's own note\n"


class TestSymlinkedAncestorIsRefused:
    """#799: the read-side guard (_remember_may_inject, lib-memory-context.sh)
    already refuses to inject through a symlinked-ancestor directory --
    _remember_file_tracked_state_into can return that state whenever some
    directory between the target and the repository root (inclusive) is
    itself a symlink. Before this fix, write-handoff.sh's own case statement
    (mirroring the #750 guard above) had no arm for that state, so it fell
    through silently and the write proceeded straight through the symlink.

    Observed on macOS/Linux only (symlink handling on Windows Git Bash is not
    exercised by this suite -- reasoned, not observed -- consistent with the
    existing skip below and with tests/test_injection_guard_ancestor_symlink.py,
    which the read-side equivalent of this test lives in)."""

    def test_a_symlinked_remember_directory_is_refused_and_target_untouched(self, tmp_path):
        project, home = _sandbox_git_repo(tmp_path)
        outside = tmp_path / "outside-the-repo"
        outside.mkdir()
        (project / ".remember").symlink_to(outside, target_is_directory=True)

        result = _write_handoff(project, home, "operator's own note\n")

        assert result.returncode != 0, (
            f"a write through a symlinked ancestor directory must be refused, not written.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "REFUSED" in result.stderr
        target = outside / "remember.md"
        assert not target.exists(), (
            "a refused write must never create the note file through the symlink"
        )

    def test_an_untracked_target_with_no_symlinked_ancestor_is_still_written(self, tmp_path):
        """Positive control: an ordinary, non-symlinked .remember directory in
        the same kind of git repository is still written -- without this, the
        test above could pass on a script that refuses every write once any
        symlink exists anywhere nearby, not only one on the target's own
        ancestor chain."""
        project, home = _sandbox_git_repo(tmp_path)
        (project / ".remember").mkdir()

        result = _write_handoff(project, home, "operator's own note\n")

        assert result.returncode == 0, (
            f"an ordinary untracked target with no symlinked ancestor must still be written.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        target = project / ".remember" / "remember.md"
        assert target.exists() and target.read_text() == "operator's own note\n"
