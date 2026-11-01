"""
#776: write-handoff.sh's #743 git-root fallback conflicts with subdirectory
projects.

#743 made write-handoff.sh prefer `git rev-parse --show-toplevel` over a
possibly-stale $PWD whenever CLAUDE_PROJECT_DIR is unset -- fixing a real bug
(a `cd` earlier in the same Bash-tool call leaving $PWD pointed at a
subdirectory the operator never asked to write into). But a project can also
be started FROM a repository subdirectory ON PURPOSE -- a supported shape
per tests/test_injection_guard_754_755_756.py's TestSubdirectoryHandoff --
and for that shape the blanket git-root preference sends every write to the
enclosing repository's own store instead of the subdirectory project's own,
silently misrouting the operator's note (and, per #776's own report, the
`mkdir -p` at the enclosing store creates it with no protective .gitignore).

The fix disambiguates using evidence nothing else could have produced: THIS
session's own SessionStart already published a hint file keyed by THIS
session's own id (#738) into whichever REMEMBER_DIR it actually resolved.
Two properties:

  - when the subdirectory project's own store carries this session's hint
    but the enclosing repo's store does not, the write goes to the
    subdirectory project's store, not the repo root
  - positive control: the #743 shape (an accidental `cd`, no session hint
    published anywhere for this session) is UNCHANGED -- the write still
    goes to the git root, exactly as #743 fixed it
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
    reason="bash subprocess + POSIX write-handoff script — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WRITE_HANDOFF_SCRIPT = REPO_ROOT / "scripts" / "write-handoff.sh"
SESSION_ID = "subdir-session-abc123"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _sandbox_git_repo_with_subproject(tmp_path: Path):
    """A git repository whose root is `project`, with a project deliberately
    started from `project/pkg` -- the shape #776 is about. `pkg` gets its
    own `.remember` with a `config.json` (so lib-memory-dir.sh's own
    resolution needs no help from a parent config), and this session's
    session-keyed handoff hint is planted there, exactly as
    session-start-hook.sh would have published it for a SessionStart that
    ran with CLAUDE_PROJECT_DIR=project/pkg."""
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
    started in -- the #1058 shape. The #776 fix only ever compares $(pwd)
    against the git toplevel; when $(pwd) is `pkg/deep`, neither candidate
    is `pkg` (the directory holding the session-keyed hint), so #776's own
    disambiguation cannot see it."""
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


def _write_handoff(cwd: Path, home: Path, note: str, session_id: str = SESSION_ID) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env["HOME"] = str(home)
    if session_id:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    else:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    return subprocess.run(
        ["bash", str(WRITE_HANDOFF_SCRIPT)],
        input=note,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestSubdirectoryProjectIsRoutedToItsOwnStore:

    def test_a_subdirectory_project_with_a_published_session_hint_is_honoured(self, tmp_path):
        project, sub_project, home = _sandbox_git_repo_with_subproject(tmp_path)

        result = _write_handoff(sub_project, home, "note from the subdirectory project\n")

        assert result.returncode == 0, (
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        sub_target = sub_project / ".remember" / "remember.md"
        root_target = project / ".remember" / "remember.md"
        assert sub_target.exists() and "note from the subdirectory project" in sub_target.read_text(), (
            f"expected the note at the subdirectory project's own store.\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        assert not root_target.exists(), (
            "note was misrouted to the enclosing repository's own store "
            "instead of the subdirectory project's -- exactly the #776 defect"
        )

    def test_positive_control_the_743_accidental_cd_shape_is_unchanged(self, tmp_path):
        """No session hint exists ANYWHERE for this session (the #743 shape:
        an accidental `cd`, not a deliberate subdirectory project) -- the
        write must still go to the git root, exactly as #743 left it. This
        is what stops the #776 fix from just always preferring $PWD."""
        project, sub_project, home = _sandbox_git_repo_with_subproject(tmp_path)
        # No hint file for this session in EITHER store -- remove the one
        # the fixture planted under the subdirectory project.
        (sub_project / ".remember" / "tmp" / f"handoff-path.{SESSION_ID}").unlink()

        result = _write_handoff(sub_project, home, "note from an accidental cd\n")

        assert result.returncode == 0, (
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        root_target = project / ".remember" / "remember.md"
        sub_target = sub_project / ".remember" / "remember.md"
        assert root_target.exists() and "note from an accidental cd" in root_target.read_text(), (
            f"expected the #743 behaviour (git root) when no session hint "
            f"exists anywhere.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        assert not sub_target.exists()


class TestFreshestHintWinsOnResume1058Review:
    """Review finding on #1058 / PR #1081: the ancestor walk must pick the
    FRESHEST session hint among every ancestor that carries one, not just
    the nearest to $(pwd). session-start-hook.sh republishes
    tmp/handoff-path.<id> on every SessionStart for a given session id --
    including a `--resume` that keeps the same id but runs from a
    DIFFERENT directory than the one the session originally started in --
    and the earlier directory's hint is not pruned just because a later
    SessionStart happened somewhere else. "Closest ancestor with a hint"
    can therefore pick a directory the session has since moved on from,
    over the one it most recently resolved to."""

    def test_a_fresher_hint_at_the_toplevel_beats_a_stale_one_at_a_nearer_ancestor(self, tmp_path):
        project, sub_project, deep, home = _sandbox_git_repo_with_deep_subproject(tmp_path)

        # The subdirectory project's hint (planted by the shared fixture)
        # is the STALE one -- back-date its mtime well outside any
        # ordinary clock-skew tolerance.
        sub_hint = sub_project / ".remember" / "tmp" / f"handoff-path.{SESSION_ID}"
        old_time = time.time() - 3600
        os.utime(sub_hint, (old_time, old_time))

        # The enclosing repository's own store ALSO carries this session's
        # hint, written more recently -- the #776/#1058 "resume" shape: the
        # same session id resumed from the repo root after originally
        # starting in `pkg`.
        root_remember = project / ".remember"
        (root_remember / "tmp").mkdir(parents=True)
        (root_remember / "config.json").write_text(json.dumps({"data_dir": ".remember"}))
        root_target = root_remember / "remember.md"
        (root_remember / "tmp" / f"handoff-path.{SESSION_ID}").write_text(str(root_target))

        result = _write_handoff(deep, home, "note after a resume at the repo root\n")

        assert result.returncode == 0, (
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        sub_target = sub_project / ".remember" / "remember.md"
        assert root_target.exists() and "note after a resume at the repo root" in root_target.read_text(), (
            f"expected the FRESHER hint (the repo root, re-published by the "
            f"resume) to win over the STALE one at the nearer ancestor.\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        assert not sub_target.exists(), (
            "note was misrouted to the stale nearer ancestor instead of the "
            "freshest hint -- the #1058/PR #1081 review regression"
        )

    def test_positive_control_the_only_hint_present_still_wins_regardless_of_mtime(self, tmp_path):
        """Only one ancestor has a hint at all (the plain #1058 shape, no
        resume involved) -- freshest-wins must still find and use it,
        exactly as closest-wins did. Guards against a "freshest" rewrite
        that accidentally requires two or more candidates to work at all."""
        project, sub_project, deep, home = _sandbox_git_repo_with_deep_subproject(tmp_path)

        result = _write_handoff(deep, home, "note with only one hint anywhere\n")

        assert result.returncode == 0, (
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        sub_target = sub_project / ".remember" / "remember.md"
        root_target = project / ".remember" / "remember.md"
        assert sub_target.exists() and "note with only one hint anywhere" in sub_target.read_text()
        assert not root_target.exists()

    def test_a_fresh_near_hint_beats_a_stale_far_one_for_the_right_reason(self, tmp_path):
        """The mirror image of the resume test above: this time the NEAR
        ancestor's hint is the fresh, correct one, and the FAR ancestor (the
        git toplevel) carries a stale one left over from some earlier
        session or an accidental stray write. Maintainer-requested
        regression test: a hypothetical "farthest ancestor with any hint
        wins" implementation, or one that tie-breaks on walk ORDER rather
        than on mtime, would pick the toplevel here and fail this test --
        the only way to pass both this test and the resume test above is to
        genuinely compare mtimes."""
        project, sub_project, deep, home = _sandbox_git_repo_with_deep_subproject(tmp_path)

        # The subdirectory project's hint (planted by the shared fixture) is
        # the FRESH, correct one -- leave its mtime untouched (just written
        # by the fixture, so it is newer than anything backdated below).

        # The enclosing repository's own store carries a STALE hint --
        # back-dated well outside any ordinary clock-skew tolerance.
        root_remember = project / ".remember"
        (root_remember / "tmp").mkdir(parents=True)
        (root_remember / "config.json").write_text(json.dumps({"data_dir": ".remember"}))
        root_target = root_remember / "remember.md"
        root_hint = root_remember / "tmp" / f"handoff-path.{SESSION_ID}"
        root_hint.write_text(str(root_target))
        old_time = time.time() - 3600
        os.utime(root_hint, (old_time, old_time))

        result = _write_handoff(deep, home, "note from the fresh near ancestor\n")

        assert result.returncode == 0, (
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        sub_target = sub_project / ".remember" / "remember.md"
        assert sub_target.exists() and "note from the fresh near ancestor" in sub_target.read_text(), (
            f"expected the FRESH near ancestor to win over the STALE far "
            f"(toplevel) one.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        assert not root_target.exists(), (
            "note was misrouted to the stale toplevel store instead of the "
            "fresh nearer one -- walk-order or farthest-wins regression"
        )


class TestDeeperCwdThanSessionStartDir1058:
    """#1058: #776's own disambiguation only ever compares $(pwd) against the
    git toplevel. When a Bash `cd` has moved $(pwd) past the subdirectory
    project the session actually started in -- deeper still, not just a
    sibling -- neither of those two candidates carries the session-keyed
    hint, and the #776 block falls through to the git toplevel exactly as if
    no disambiguation existed at all."""

    def test_a_cwd_deeper_than_the_session_start_dir_still_finds_its_store(self, tmp_path):
        project, sub_project, deep, home = _sandbox_git_repo_with_deep_subproject(tmp_path)

        result = _write_handoff(deep, home, "note from three levels deep\n")

        assert result.returncode == 0, (
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        sub_target = sub_project / ".remember" / "remember.md"
        root_target = project / ".remember" / "remember.md"
        assert sub_target.exists() and "note from three levels deep" in sub_target.read_text(), (
            f"expected the note at the subdirectory project's own store, "
            f"not the enclosing repository's -- the #1058 defect.\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        assert not root_target.exists(), (
            "note was misrouted to the enclosing repository's own store "
            "instead of the subdirectory project's -- the #1058 defect"
        )

    def test_positive_control_no_hint_anywhere_still_falls_back_to_git_root(self, tmp_path):
        """No session hint exists ANYWHERE (the #743 shape) -- a deep cwd
        must still fall back to the git root, exactly as before. Pairs with
        the test above so a walk that always prefers $(pwd) rather than the
        nearest ancestor WITH a hint cannot pass silently."""
        project, sub_project, deep, home = _sandbox_git_repo_with_deep_subproject(tmp_path)
        (sub_project / ".remember" / "tmp" / f"handoff-path.{SESSION_ID}").unlink()

        result = _write_handoff(deep, home, "note from an accidental deep cd\n")

        assert result.returncode == 0, (
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        root_target = project / ".remember" / "remember.md"
        sub_target = sub_project / ".remember" / "remember.md"
        assert root_target.exists() and "note from an accidental deep cd" in root_target.read_text(), (
            f"expected the #743 behaviour (git root) when no session hint "
            f"exists anywhere.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        assert not sub_target.exists()
