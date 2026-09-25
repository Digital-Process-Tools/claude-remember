"""Injection guard follow-up: a directory BETWEEN the memory file and the
repository root -- most commonly `.remember` itself -- committed as a
symlink to somewhere outside the repository.

The per-file symlink check (tests/test_injection_guard_754_755_756.py,
`TestSymlinkedMemoryFile`) only ever sees the file already resolved, at
the far end of the link -- a regular file living past a symlinked
directory reads as "not a symlink". The tracked-file check does not catch
it either: git records a symlinked directory as one blob with nothing
"under" it, so a pathspec scoped to that directory (`:(icase).remember/`)
matches nothing, and the file reads as "not tracked". Both checks
individually correct, the file falls through both.

Same subprocess-based, win32-skipped shape as
tests/test_injection_guard_754_755_756.py, for the same reason (a real
POSIX session-start-hook.sh run, not portable to Windows runners, #79).

Every case here is red before the fix and green after -- see the branch's
own report for the paired subprocess.run output.
"""

from __future__ import annotations

import json
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX session-start hook — not portable to Windows runners (#79)",
)

from .test_injection_guard_754_755_756 import (
    REPO_ROOT,
    _git,
    _home_for,
    _session_start,
)


class TestSymlinkedAncestorDirectory:
    """A committed `.remember` DIRECTORY symlink, not a symlinked leaf
    file -- the leaf underneath it (now.md) is an ordinary regular file
    reached through the link."""

    def test_symlinked_remember_directory_is_not_injected(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        # A real repository -- the walk that finds it (no `git` spawn, see
        # _remember_repo_root_walk_into's own comment) is what scopes this
        # check to a repository-controlled tree in the first place.
        _git(project, "init", "-q")
        outside = tmp_path / "outside-the-repo"
        outside.mkdir()
        (outside / "now.md").write_text("AKIA-FAKE-SECRET-DO-NOT-LEAK\n")
        # .remember itself is the symlink -- now.md at the far end of it
        # is a perfectly ordinary regular file.
        (project / ".remember").symlink_to(outside, target_is_directory=True)
        home = _home_for(tmp_path, project)

        out = _session_start(project, home)

        assert "AKIA-FAKE-SECRET-DO-NOT-LEAK" not in out, (
            f"now.md reached through a symlinked .remember DIRECTORY had "
            f"its content injected.\noutput: {out[:800]}"
        )
        assert "refused" in out.lower(), (
            f"a refused injection must say so, not silently emit nothing.\n"
            f"output: {out[:800]}"
        )
        assert "symlink" in out.lower()

    def test_untracked_in_project_store_is_still_delivered(self, tmp_path):
        """Positive control: an ordinary, non-symlinked in-project store
        keeps working -- the fix must not become 'never inject anything
        under .remember', which would pass on a harness that died before
        it spoke."""
        project = tmp_path / "proj"
        (project / ".remember").mkdir(parents=True)
        home = _home_for(tmp_path, project)
        (project / ".remember" / "now.md").write_text("Working on the parser fix.\n")

        out = _session_start(project, home)

        assert "Working on the parser fix." in out
        assert "refused" not in out.lower()

    def test_committed_remember_directory_symlink_from_a_subdirectory(self, tmp_path):
        """The same shape one directory further from the repository root
        (#754's own scenario): .remember sits under a project
        SUBDIRECTORY, and it is .remember itself, not now.md, that git
        records as a symlink blob."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "seed.txt").write_text("seed\n")
        _git(repo, "add", "seed.txt")
        _git(repo, "commit", "-q", "-m", "init")

        subdir = repo / "pkg"
        subdir.mkdir()
        outside = tmp_path / "outside-the-repo"
        outside.mkdir()
        (outside / "now.md").write_text("AKIA-FAKE-SECRET-DO-NOT-LEAK\n")
        (subdir / ".remember").symlink_to(outside, target_is_directory=True)
        home = _home_for(tmp_path, subdir)

        out = _session_start(subdir, home)

        assert "AKIA-FAKE-SECRET-DO-NOT-LEAK" not in out
        assert "refused" in out.lower()
        assert "symlink" in out.lower()


class TestExternalStorageThroughASymlinkedDataDirStillInjects:
    """Positive control the F2 fix must not break: a user's own external
    data_dir reached through a symlink (the ~/Dropbox-linked-elsewhere
    case) is TRUSTED, not repository-controlled, and must keep injecting.
    No repository sits above it at all here, so the new directory-symlink
    check (gated on a repository being found first, same as the tracked
    check) never even engages -- but this pins the observable behaviour,
    not the internal gating."""

    def test_external_storage_via_symlinked_data_dir_still_injects(self, tmp_path):
        home = tmp_path / "home"
        project = tmp_path / "proj"
        project.mkdir()
        real_store_base = tmp_path / "real-dropbox"
        real_store_base.mkdir()
        linked_store_base = tmp_path / "dropbox-link"
        linked_store_base.symlink_to(real_store_base, target_is_directory=True)

        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            json.dumps(
                {
                    "data_dir": str(linked_store_base) + "/{slug}",
                    "features": {"recovery": False},
                }
            )
        )
        sys.path.insert(0, str(REPO_ROOT))
        from pipeline.slug import session_dir_slug as _slug

        (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)

        ext_dir = real_store_base / _slug(str(project))
        ext_dir.mkdir(parents=True)
        (ext_dir / "now.md").write_text("Working on the parser fix.\n")

        out = _session_start(project, home)

        assert "Working on the parser fix." in out, (
            f"a legitimate external data_dir reached through a symlink "
            f"(e.g. a Dropbox-linked path) stopped injecting -- the "
            f"directory-symlink guard must be scoped to a "
            f"repository-controlled tree, not to 'any symlinked "
            f"directory'.\noutput: {out[:800]}"
        )
        assert "refused" not in out.lower()
