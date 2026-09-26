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
