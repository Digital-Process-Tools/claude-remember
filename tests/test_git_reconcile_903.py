"""Tests for hooks.d/after_save/60-git-reconcile.sh (#903).

Off by default (git_reconcile.enabled). 50-git-backup.sh keeps its
commit-and-push-only promise from #253, and 50-git-restore.sh stays
fast-forward-only -- a diverged store is still refused there. This hook is
the only place a rebase happens in the plugin, and only once a human has
opted in: it fast-forwards when purely behind (so a store running without
git_restore.enabled still catches up on save), and rebases onto the remote
tip and pushes when both ahead and behind, aborting cleanly on a real
conflict and changing nothing.

Real repositories throughout, per the #253/#723 test convention this file
inherits: a real bare remote, advanced by a real second clone. Every "must
not touch the tree" assertion is paired with a "must touch the tree" one
(TestOffByDefault vs TestFastForward), so a broken harness that runs nothing
at all cannot pass by looking identical to a correctly declining hook.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics — not portable to Windows runners (#79)",
)

from .test_git_backup_hook import (
    REPO_ROOT,
    _git,
    hook_state,
    make_external_remember_repo,
    wait_for_lock_release,
)
from .test_git_backup_push_rejected_253 import (
    _advance_remote,
    _remote_head,
)

HOOK = REPO_ROOT / "hooks.d" / "after_save" / "60-git-reconcile.sh"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, **git_reconcile) -> Path:
    cfg = tmp_path / "remember-config.json"
    body: dict = {}
    if git_reconcile:
        body["git_reconcile"] = git_reconcile
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def _enabled_config(tmp_path: Path, **extra) -> Path:
    return _config(tmp_path, enabled=True, **extra)


def _store(tmp_path: Path):
    """A memory store with one slug, a bare remote, and a project dir."""
    home, remember, remote = make_external_remember_repo(tmp_path)
    slug_dir = remember / "test-slug"
    slug_dir.mkdir()
    (slug_dir / "now.md").write_text("## 10:00 | test\nlocal memory\n", encoding="utf-8")
    _git(remember, ["add", "-A"])
    _git(remember, ["commit", "-q", "-m", "local"])
    _git(remember, ["push", "-q", "origin", "main"])
    project = tmp_path / "project"
    project.mkdir()
    return home, remember, remote, slug_dir, project


def _run(slug_dir: Path, project: Path, home: Path, cfg: Path | None = None, **extra):
    env = {
        **os.environ,
        "HOME": str(home),
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(REPO_ROOT),
        "REMEMBER_DIR": str(slug_dir),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_PROJECT": str(project),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    if cfg is not None:
        env["REMEMBER_CONFIG"] = str(cfg)
    env.update(extra)
    return subprocess.run(["bash", str(HOOK)], env=env, capture_output=True,
                          text=True, timeout=120, check=False)


def _wait_quiesce(remember: Path) -> None:
    lock = hook_state(remember, ".git-reconcile.lock")
    try:
        wait_for_lock_release(lock)
    except TimeoutError:
        pass


def _head(repo: Path) -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       capture_output=True, text=True, check=False)
    return r.stdout.strip()


def _save_locally(remember: Path, slug_dir: Path, n: int = 1) -> None:
    (slug_dir / "now.md").write_text(
        f"## 10:0{n} | test\nmore local memory {n}\n", encoding="utf-8")
    _git(remember, ["add", "-A"])
    _git(remember, ["commit", "-q", "-m", f"local {n}"])


# ── Off by default ───────────────────────────────────────────────────────────


class TestOffByDefault:

    def test_disabled_does_nothing(self, tmp_path):
        """Positive control for every other test in this file: with
        git_reconcile.enabled unset, nothing here touches the store -- no
        fetch, no lock file, no rewrite."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        before = _head(remember)
        _advance_remote(tmp_path, remote)
        cfg = _config(tmp_path)  # enabled not set -> default false

        result = _run(slug_dir, project, home, cfg)

        assert result.returncode == 0
        assert _head(remember) == before, "a disabled hook touched the tree"
        assert not hook_state(remember, ".git-reconcile.lock").exists()

    def test_explicit_false_also_does_nothing(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        before = _head(remember)
        _advance_remote(tmp_path, remote)
        cfg = _config(tmp_path, enabled=False)

        _run(slug_dir, project, home, cfg)

        assert _head(remember) == before


# ── Fast-forward (behind only) ───────────────────────────────────────────────


class TestFastForward:

    def test_behind_only_fast_forwards(self, tmp_path):
        """Positive control, paired with TestOffByDefault: the SAME scenario
        (purely behind) DOES get fast-forwarded once enabled."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        remote_tip = _advance_remote(tmp_path, remote)
        cfg = _enabled_config(tmp_path)

        _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        assert _head(remember) == remote_tip, (
            "purely-behind store was not fast-forwarded to the remote tip"
        )

    def test_ahead_only_does_not_rebase_or_reset(self, tmp_path):
        """Nothing to reconcile when only ahead -- 50-git-backup.sh already
        pushes that. The local commit must survive untouched."""
        home, remember, _remote, slug_dir, project = _store(tmp_path)
        _save_locally(remember, slug_dir, n=1)
        local_head = _head(remember)
        cfg = _enabled_config(tmp_path)

        _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        assert _head(remember) == local_head, (
            "an ahead-only store was rebased or reset by a hook that should "
            "have been a no-op"
        )


# ── Diverged: rebase and push, or abort cleanly ──────────────────────────────


class TestDivergedReconcile:

    def test_ahead_and_behind_rebases_and_pushes(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        remote_tip = _advance_remote(tmp_path, remote)
        _save_locally(remember, slug_dir, n=1)
        cfg = _enabled_config(tmp_path)

        _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        is_ancestor = subprocess.run(
            ["git", "-C", str(remember), "merge-base", "--is-ancestor",
             remote_tip, "HEAD"], check=False)
        assert is_ancestor.returncode == 0, (
            "remote commit is not an ancestor of HEAD -- no rebase happened"
        )
        assert _remote_head(remote) == _head(remember), (
            "the rebased commit was never pushed"
        )

    def test_conflict_aborts_and_leaves_tree_exactly_as_it_was(self, tmp_path):
        """The load-bearing one: a real conflict (both sides edit the same
        line of the same file) must abort the rebase, change NOTHING, and
        name the file through the notice."""
        home, remember, remote, slug_dir, project = _store(tmp_path)

        other = tmp_path / "other-machine"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)],
                       check=True, capture_output=True)
        _git(other, ["config", "user.email", "other@test"])
        _git(other, ["config", "user.name", "Other"])
        (other / "test-slug" / "now.md").write_text(
            "## 10:00 | test\nFROM THE OTHER MACHINE\n", encoding="utf-8")
        _git(other, ["add", "-A"])
        _git(other, ["commit", "-q", "-m", "other machine edit"])
        _git(other, ["push", "-q", "origin", "main"])

        (slug_dir / "now.md").write_text(
            "## 10:00 | test\nFROM THIS MACHINE\n", encoding="utf-8")
        _git(remember, ["add", "-A"])
        _git(remember, ["commit", "-q", "-m", "this machine edit"])
        local_head = _head(remember)
        local_content = (slug_dir / "now.md").read_text(encoding="utf-8")

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        assert _head(remember) == local_head, (
            "a conflicting rebase changed HEAD -- it must abort and leave "
            "the tree exactly as it was"
        )
        assert (slug_dir / "now.md").read_text(encoding="utf-8") == local_content, (
            "the conflicting file was modified despite the abort"
        )

        # --path-format=absolute is required here, not optional: plain
        # --git-path returns a path relative to THIS PROCESS's cwd (pytest's
        # own), not to the -C target repo, so without it this assertion
        # checks the wrong directory regardless of what the hook actually
        # left behind. Same bug class the hook's own _grc_rebase_in_progress
        # was fixed against (see its comment).
        rebase_merge = subprocess.run(
            ["git", "-C", str(remember), "rev-parse", "--path-format=absolute",
             "--git-path", "rebase-merge"],
            capture_output=True, text=True, check=False).stdout.strip()
        assert rebase_merge and not Path(rebase_merge).exists(), (
            "a rebase was left in progress after a conflict"
        )

        notice = slug_dir / "tmp" / "git-reconcile-notice"
        assert notice.exists(), "no notice was written for the conflict"
        assert "now.md" in notice.read_text(encoding="utf-8")

        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files, "hook wrote no log at all"
        log_text = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
        assert "CONFLICT" in log_text
        assert "now.md" in log_text

    def test_concurrent_write_to_another_slugs_file_survives_the_abort(self, tmp_path):
        """#939: REPO_ROOT can hold more than this run's own slug -- a store
        shared by several slugs is exactly the arrangement _store() builds
        when a second slug directory is added as a sibling of test-slug.
        Nothing locked here stops a DIFFERENT slug's own writer (this
        hook's git-reconcile.lock and consolidation.lock are both scoped to
        the slug running THIS invocation) from touching its own tracked
        file while this slug's rebase is in flight. `git rebase --abort`
        resets the WHOLE tracked tree to ORIG_HEAD, not just the files this
        rebase touched -- so a write that lands in that window must not be
        silently discarded.

        A real git binary still performs every git operation, including the
        abort itself; only ONE invocation is intercepted (the
        `diff --name-only --diff-filter=U` call this hook already makes,
        unconditionally, the moment it notices the conflict and before any
        abort decision) so the race lands deterministically instead of
        depending on winning a sub-millisecond window on every CI runner."""
        home, remember, remote, slug_dir, project = _store(tmp_path)

        other_slug_dir = remember / "other-slug"
        other_slug_dir.mkdir()
        (other_slug_dir / "now.md").write_text("other slug base\n", encoding="utf-8")
        _git(remember, ["add", "-A"])
        _git(remember, ["commit", "-q", "-m", "other slug base"])
        _git(remember, ["push", "-q", "origin", "main"])

        other = tmp_path / "other-machine"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)],
                       check=True, capture_output=True)
        _git(other, ["config", "user.email", "other@test"])
        _git(other, ["config", "user.name", "Other"])
        (other / "test-slug" / "now.md").write_text(
            "## 10:00 | test\nFROM THE OTHER MACHINE\n", encoding="utf-8")
        _git(other, ["add", "-A"])
        _git(other, ["commit", "-q", "-m", "other machine edit"])
        _git(other, ["push", "-q", "origin", "main"])

        (slug_dir / "now.md").write_text(
            "## 10:00 | test\nFROM THIS MACHINE\n", encoding="utf-8")
        _git(remember, ["add", "-A"])
        _git(remember, ["commit", "-q", "-m", "this machine edit"])

        real_git = shutil.which("git")
        assert real_git, "no git on PATH -- fixture assumption broken"
        bin_dir = tmp_path / "shim-bin"
        bin_dir.mkdir()
        shim = bin_dir / "git"
        shim_lines = [
            "#!/bin/sh",
            'if [ "$1" = "-C" ] && [ "$3" = "diff" ] && [ "$4" = "--name-only" ]; then',
            "    printf '%s\n' \"CONCURRENT WRITE FROM OTHER SLUG\" > \"$RACE_OTHER_SLUG_NOW\"",
            "fi",
            f'exec "{real_git}" "$@"',
            "",
        ]
        shim.write_text("\n".join(shim_lines), encoding="utf-8")
        shim.chmod(0o755)

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg,
             PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
             RACE_OTHER_SLUG_NOW=str(other_slug_dir / "now.md"))
        _wait_quiesce(remember)

        assert (other_slug_dir / "now.md").read_text(encoding="utf-8") == (
            "CONCURRENT WRITE FROM OTHER SLUG\n"
        ), (
            "the concurrent write to the OTHER slug's tracked file was "
            "discarded by this slug's `git rebase --abort` -- #939"
        )

        # Review finding (#939): an earlier version of this fix closed the
        # discard by leaving the rebase IN PROGRESS instead of aborting --
        # which traded it for a worse failure (50-git-backup.sh commits
        # this slug's own unresolved conflict markers on the very next
        # save, since it has no notion of an in-progress rebase). The
        # shipped fix always aborts; only the foreign write is special-
        # cased. So the rebase must be gone here, same as the plain
        # conflict case in test_conflict_aborts_and_leaves_tree_exactly_as_it_was.
        rebase_merge = subprocess.run(
            ["git", "-C", str(remember), "rev-parse", "--path-format=absolute",
             "--git-path", "rebase-merge"],
            capture_output=True, text=True, check=False).stdout.strip()
        assert rebase_merge and not Path(rebase_merge).exists(), (
            "the rebase was left in progress -- 50-git-backup.sh's next "
            "per-slug commit has no notion of an in-progress rebase and "
            "would commit this slug's own unresolved conflict markers"
        )

    def test_foreign_path_containing_a_literal_arrow_is_not_misread_as_this_runs_own(self, tmp_path):
        """Review finding (#939): an earlier version of _grc_foreign_changes
        split any porcelain line containing the literal substring " -> "
        on that substring, assuming it was always a rename -- but porcelain
        quotes any path containing a space, rename or not, so a plain
        (non-renamed) foreign path whose own name happens to contain
        " -> " was wrongly truncated to whatever followed it and could
        collide with $SLUG, waving a real foreign write through as this
        run's own. The fix keys the split on the status CODE (does it
        contain 'R'?), never on the path text. This constructs exactly
        that untracked, non-renamed path and checks it survives the abort
        the same way the plain concurrent-write case above does."""
        home, remember, remote, slug_dir, project = _store(tmp_path)

        other_slug_dir = remember / "other-slug"
        other_slug_dir.mkdir()
        # The TRACKED path's own name contains the literal substring
        # " -> test-slug" (test-slug == $SLUG) -- an earlier, broken
        # version of the parser would truncate this to "test-slug" and
        # wave it through as this run's own slug. It must be tracked
        # (committed here, then overwritten below) for `git rebase
        # --abort` to be able to discard it at all: an untracked file is
        # never touched by --abort regardless of this classification, so
        # an untracked repro would pass even against the broken parser
        # and prove nothing.
        weird_dir = other_slug_dir
        weird_name = "weird -> test-slug"
        (weird_dir / weird_name).write_text("other slug base\n", encoding="utf-8")
        _git(remember, ["add", "-A"])
        _git(remember, ["commit", "-q", "-m", "other slug base"])
        _git(remember, ["push", "-q", "origin", "main"])

        other = tmp_path / "other-machine"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)],
                       check=True, capture_output=True)
        _git(other, ["config", "user.email", "other@test"])
        _git(other, ["config", "user.name", "Other"])
        (other / "test-slug" / "now.md").write_text(
            "## 10:00 | test\nFROM THE OTHER MACHINE\n", encoding="utf-8")
        _git(other, ["add", "-A"])
        _git(other, ["commit", "-q", "-m", "other machine edit"])
        _git(other, ["push", "-q", "origin", "main"])

        (slug_dir / "now.md").write_text(
            "## 10:00 | test\nFROM THIS MACHINE\n", encoding="utf-8")
        _git(remember, ["add", "-A"])
        _git(remember, ["commit", "-q", "-m", "this machine edit"])

        real_git = shutil.which("git")
        assert real_git, "no git on PATH -- fixture assumption broken"
        bin_dir = tmp_path / "shim-bin"
        bin_dir.mkdir()
        shim = bin_dir / "git"
        shim_lines = [
            "#!/bin/sh",
            'if [ "$1" = "-C" ] && [ "$3" = "diff" ] && [ "$4" = "--name-only" ]; then',
            "    printf '%s\n' \"CONCURRENT WRITE FROM OTHER SLUG\" > \"$RACE_OTHER_SLUG_NOW\"",
            "fi",
            f'exec "{real_git}" "$@"',
            "",
        ]
        shim.write_text("\n".join(shim_lines), encoding="utf-8")
        shim.chmod(0o755)

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg,
             PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
             RACE_OTHER_SLUG_NOW=str(weird_dir / weird_name))
        _wait_quiesce(remember)

        assert (weird_dir / weird_name).read_text(encoding="utf-8") == (
            "CONCURRENT WRITE FROM OTHER SLUG\n"
        ), (
            "a foreign (non-renamed) TRACKED path whose own name contains "
            "the literal substring ' -> test-slug' was misread as this "
            "run's own slug and discarded by the abort -- #939"
        )


# ── #943: closing the TOCTOU gap the #939/#942 snapshot-and-restore fix
# could not close, by never letting the abort touch anything outside
# $SLUG/ in the first place (git rebase --quit + a scoped checkout,
# instead of git rebase --abort). #942's own exit-status concerns (what
# if the backup/restore copy fails?) are now moot by construction -- there
# is no backup or restore step any more to fail -- but the SAME exit-
# status discipline is re-applied to the new sequence's own two steps
# (quit, scoped checkout), which is what the tests below cover.


class TestScopedAbort:

    def _diverged_with_foreign_write(self, tmp_path):
        """Shared setup for every test below: a store with two slugs, a
        real conflict for test-slug, and a concurrent tracked write to
        other-slug's own file landing before the scoped-abort sequence
        runs -- the same shape as
        test_concurrent_write_to_another_slugs_file_survives_the_abort
        above, reused here because the abort machinery is the part under
        test, not the race itself."""
        home, remember, remote, slug_dir, project = _store(tmp_path)

        other_slug_dir = remember / "other-slug"
        other_slug_dir.mkdir()
        (other_slug_dir / "now.md").write_text("other slug base\n", encoding="utf-8")
        _git(remember, ["add", "-A"])
        _git(remember, ["commit", "-q", "-m", "other slug base"])
        _git(remember, ["push", "-q", "origin", "main"])

        other = tmp_path / "other-machine"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)],
                       check=True, capture_output=True)
        _git(other, ["config", "user.email", "other@test"])
        _git(other, ["config", "user.name", "Other"])
        (other / "test-slug" / "now.md").write_text(
            "## 10:00 | test\nFROM THE OTHER MACHINE\n", encoding="utf-8")
        _git(other, ["add", "-A"])
        _git(other, ["commit", "-q", "-m", "other machine edit"])
        _git(other, ["push", "-q", "origin", "main"])

        (slug_dir / "now.md").write_text(
            "## 10:00 | test\nFROM THIS MACHINE\n", encoding="utf-8")
        _git(remember, ["add", "-A"])
        _git(remember, ["commit", "-q", "-m", "this machine edit"])

        return home, remember, remote, slug_dir, project, other_slug_dir

    def test_write_in_the_toctou_window_survives_the_scoped_abort(self, tmp_path):
        """#943: the shim here injects the foreign write immediately before
        `git rebase --quit` runs -- i.e. in the exact window the #939/#942
        snapshot-then-abort approach could not close, since the snapshot
        (_grc_foreign_changes, a `git status --porcelain` call) necessarily
        runs BEFORE the abort step and cannot see a write that lands after
        it. The scoped-abort fix never reads or relies on a snapshot taken
        before the abort at all, so there is nothing for this write to be
        missing FROM -- it must survive regardless of when it lands."""
        home, remember, _remote, slug_dir, project, other_slug_dir = (
            self._diverged_with_foreign_write(tmp_path))

        real_git = shutil.which("git")
        assert real_git, "no git on PATH -- fixture assumption broken"
        bin_dir = tmp_path / "shim-bin"
        bin_dir.mkdir()

        git_shim = bin_dir / "git"
        git_shim_lines = [
            "#!/bin/sh",
            'if [ "$1" = "-C" ] && [ "$3" = "rebase" ] && [ "$4" = "--quit" ]; then',
            "    printf '%s\n' \"CONCURRENT WRITE FROM OTHER SLUG\" > \"$RACE_OTHER_SLUG_NOW\"",
            "fi",
            f'exec "{real_git}" "$@"',
            "",
        ]
        git_shim.write_text("\n".join(git_shim_lines), encoding="utf-8")
        git_shim.chmod(0o755)

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg,
             PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
             RACE_OTHER_SLUG_NOW=str(other_slug_dir / "now.md"))
        _wait_quiesce(remember)

        assert (other_slug_dir / "now.md").read_text(encoding="utf-8") == (
            "CONCURRENT WRITE FROM OTHER SLUG\n"
        ), (
            "a write that landed AFTER any snapshot the hook might have "
            "taken, immediately before the abort sequence runs, was "
            "discarded -- the exact TOCTOU gap #943 describes"
        )

        rebase_merge = subprocess.run(
            ["git", "-C", str(remember), "rev-parse", "--path-format=absolute",
             "--git-path", "rebase-merge"],
            capture_output=True, text=True, check=False).stdout.strip()
        assert rebase_merge and not Path(rebase_merge).exists(), (
            "the rebase must still be cleanly gone afterward"
        )

        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        log_text = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
        assert "never touched" in log_text or "restored" in log_text.lower(), (
            "the surviving foreign write should be named in the log"
        )

    def test_quit_failure_leaves_rebase_stuck_and_is_reported_honestly(self, tmp_path):
        """`git rebase --quit` replaces `git rebase --abort` as the first
        step. Its own exit status is now what must be checked (#942's
        exit-status discipline, carried over to the new sequence): a
        failure there (e.g. a concurrent index.lock) must leave the
        rebase genuinely stuck and be reported as such, never as
        'aborted, the tree is unchanged'."""
        home, remember, _remote, slug_dir, project, other_slug_dir = (
            self._diverged_with_foreign_write(tmp_path))

        real_git = shutil.which("git")
        assert real_git, "no git on PATH -- fixture assumption broken"
        bin_dir = tmp_path / "shim-bin"
        bin_dir.mkdir()

        git_shim = bin_dir / "git"
        git_shim_lines = [
            "#!/bin/sh",
            'if [ "$1" = "-C" ] && [ "$3" = "rebase" ] && [ "$4" = "--quit" ]; then',
            "    exit 1",
            "fi",
            f'exec "{real_git}" "$@"',
            "",
        ]
        git_shim.write_text("\n".join(git_shim_lines), encoding="utf-8")
        git_shim.chmod(0o755)

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg,
             PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
             RACE_OTHER_SLUG_NOW=str(other_slug_dir / "now.md"))
        _wait_quiesce(remember)

        rebase_merge = subprocess.run(
            ["git", "-C", str(remember), "rev-parse", "--path-format=absolute",
             "--git-path", "rebase-merge"],
            capture_output=True, text=True, check=False).stdout.strip()
        assert rebase_merge and Path(rebase_merge).exists(), (
            "the shim made `rebase --quit` itself fail -- the rebase must "
            "genuinely still be stuck for this test to mean anything"
        )

        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files, "hook wrote no log at all"
        log_text = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
        assert "unchanged" not in log_text, (
            "a `rebase --quit` that FAILED must never be reported as "
            "'aborted, the tree is unchanged'"
        )
        assert "FAILED" in log_text or "failed" in log_text, (
            "the quit's own failure must be surfaced in the log"
        )

    def test_file_added_by_the_upstream_side_before_the_conflict_is_pruned(self, tmp_path):
        """Review finding (Explore, self-review of #943): `git checkout
        <tree> -- <pathspec>` only ever RESTORES a path that exists in
        <tree> -- it never REMOVES a path present in the index/working
        tree but absent from it. The upstream side being rebased onto can
        have its own earlier commit that cleanly adds a new file under
        $SLUG/ before a later commit conflicts; once the rebase sets up
        that new base to replay onto, the new file is already checked
        into the index as an ADDITION relative to this run's own
        pre-rebase HEAD. `git rebase --quit` does not touch it, and the
        scoped checkout does not remove it either, since HEAD's own
        pre-rebase tree never had it -- unlike `git rebase --abort`
        (effectively a `reset --hard` across the whole tree), which would
        have pruned it. Left unpruned, that stray addition reads as
        test-slug/'s own untouched content and gets silently committed by
        the very next ordinary save."""
        home, remember, remote, slug_dir, project = _store(tmp_path)

        other = tmp_path / "other-machine"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)],
                       check=True, capture_output=True)
        _git(other, ["config", "user.email", "other@test"])
        _git(other, ["config", "user.name", "Other"])
        # First commit on the upstream side: a clean addition under
        # test-slug/ -- no conflict with anything local.
        (other / "test-slug" / "added-upstream.md").write_text(
            "added by the upstream side\n", encoding="utf-8")
        _git(other, ["add", "-A"])
        _git(other, ["commit", "-q", "-m", "upstream adds a new file"])
        # Second commit on the upstream side: conflicts with the local
        # edit to now.md made below.
        (other / "test-slug" / "now.md").write_text(
            "## 10:00 | test\nFROM THE OTHER MACHINE\n", encoding="utf-8")
        _git(other, ["add", "-A"])
        _git(other, ["commit", "-q", "-m", "other machine edit (conflicts)"])
        _git(other, ["push", "-q", "origin", "main"])

        (slug_dir / "now.md").write_text(
            "## 10:00 | test\nFROM THIS MACHINE\n", encoding="utf-8")
        _git(remember, ["add", "-A"])
        _git(remember, ["commit", "-q", "-m", "this machine edit"])
        local_head = _head(remember)

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        assert _head(remember) == local_head, (
            "a conflicting rebase changed HEAD -- it must abort and leave "
            "the tree exactly as it was"
        )
        assert not (slug_dir / "added-upstream.md").exists(), (
            "a file added by the UPSTREAM side's own earlier commit, "
            "never part of this run's pre-rebase HEAD, was left behind "
            "as a stray tracked addition after the scoped abort"
        )
        status = subprocess.run(
            ["git", "-C", str(remember), "status", "--porcelain"],
            capture_output=True, text=True, check=False).stdout
        assert status == "", (
            f"the tree must be exactly clean after the abort, got: {status!r}"
        )

    def test_scoped_checkout_failure_is_reported_honestly(self, tmp_path):
        """The scoped `git checkout HEAD -- "$SLUG"` that restores
        $SLUG/'s own pre-rebase content is the second step with an exit
        status that must be checked: a failure there must never be
        reported as a clean abort, since $SLUG/ may still carry conflict
        markers."""
        home, remember, _remote, slug_dir, project, other_slug_dir = (
            self._diverged_with_foreign_write(tmp_path))

        real_git = shutil.which("git")
        assert real_git, "no git on PATH -- fixture assumption broken"
        bin_dir = tmp_path / "shim-bin"
        bin_dir.mkdir()

        git_shim = bin_dir / "git"
        git_shim_lines = [
            "#!/bin/sh",
            'if [ "$1" = "-C" ] && [ "$3" = "checkout" ] && [ "$4" = "HEAD" ]; then',
            "    exit 1",
            "fi",
            f'exec "{real_git}" "$@"',
            "",
        ]
        git_shim.write_text("\n".join(git_shim_lines), encoding="utf-8")
        git_shim.chmod(0o755)

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg,
             PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
             RACE_OTHER_SLUG_NOW=str(other_slug_dir / "now.md"))
        _wait_quiesce(remember)

        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files, "hook wrote no log at all"
        log_text = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
        assert "unchanged" not in log_text, (
            "a scoped checkout that FAILED must never be reported as "
            "'aborted, the tree is unchanged' -- test-slug/ may still "
            "carry conflict markers"
        )
        assert "FAILED" in log_text or "failed" in log_text, (
            "the scoped checkout's own failure must be surfaced in the log"
        )


# ── Must not run while consolidation holds its own lock ─────────────────────


class TestConsolidationLock:

    def test_declines_while_consolidation_holds_its_lock(self, tmp_path):
        """Paired with test_behind_only_fast_forwards above (same scenario,
        lock free): with consolidation's own lock held for this slug, the
        reconcile must do nothing at all."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        before = _head(remember)
        _advance_remote(tmp_path, remote)

        consolidation_lock = slug_dir / "tmp" / "consolidation.lock"
        consolidation_lock.mkdir(parents=True)
        (consolidation_lock / "pid").write_text(str(os.getpid()), encoding="utf-8")

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        assert _head(remember) == before, (
            "reconcile ran while consolidation's own lock was held for this slug"
        )


# ── Must bump NDC's own generation marker on every rewrite ──────────────────
#
# #932: NDC (save-session.sh's background compression) and reconcile are
# both writers of now.md, but NDC's own staleness guards (live size vs.
# snapshot, a generation marker) only ever detected another NDC round -- a
# reconcile fast-forward/rebase landing mid-commit was invisible to them.
# reconcile cannot simply share save-session.sh's save.lock the way it
# shares consolidation's: save-session.sh holds save.lock across its ENTIRE
# run, spanning the window this hook always runs in, so that would decline
# almost every save rather than only the rare racing one (caught in review).
# Instead reconcile bumps NDC's own generation marker on every successful
# rewrite, so NDC's EXISTING guard also catches this hook as a writer.


class TestNdcGenerationBump:

    def test_fast_forward_bumps_ndc_generation(self, tmp_path):
        """Paired with test_behind_only_fast_forwards above (same scenario):
        a fast-forward must also bump the generation marker NDC's commit
        step reads, not just move HEAD."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        gen_file = slug_dir / "tmp" / "ndc-generation"
        assert not gen_file.exists(), "marker must start absent for this test"

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        assert gen_file.exists(), (
            "a fast-forward landed but NDC's generation marker was never "
            "bumped -- an in-flight NDC commit has no way to notice"
        )
        assert gen_file.read_text(encoding="utf-8").strip() == "1"

    def test_ahead_only_does_not_bump_ndc_generation(self, tmp_path):
        """Negative control, paired with the positive case above: nothing
        was rewritten (ahead-only is a no-op here), so the marker must stay
        untouched -- a bump here would be a false positive that makes a
        concurrent NDC round skip a commit for no reason."""
        home, remember, _remote, slug_dir, project = _store(tmp_path)
        _save_locally(remember, slug_dir, n=1)
        gen_file = slug_dir / "tmp" / "ndc-generation"

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        assert not gen_file.exists(), (
            "the generation marker was bumped despite reconcile doing "
            "nothing (ahead-only, 50-git-backup.sh's job)"
        )

    def test_diverged_rebase_bumps_ndc_generation(self, tmp_path):
        """Same positive-control reasoning as the fast-forward case, for the
        rebase-and-push path."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _save_locally(remember, slug_dir, n=1)
        gen_file = slug_dir / "tmp" / "ndc-generation"

        cfg = _enabled_config(tmp_path)
        _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        assert gen_file.exists(), (
            "a rebase+push landed but NDC's generation marker was never "
            "bumped"
        )
        assert gen_file.read_text(encoding="utf-8").strip() == "1"

    def test_fifo_at_marker_path_does_not_hang_or_get_opened(self, tmp_path):
        """#625/#634/#642/#653-shaped guard, reused here: a FIFO with no
        reader blocks forever in open(2) -- `cat` or `>` on it hangs this
        backgrounded subshell rather than failing fast. The bump must check
        the file's type BEFORE opening it, the same way every other
        accessor of this marker file in save-session.sh already does."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        tmp_dir = slug_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        gen_fifo = tmp_dir / "ndc-generation"
        os.mkfifo(gen_fifo)

        cfg = _enabled_config(tmp_path)
        # subprocess.run's own timeout=120 in _run() would turn an actual
        # hang into a TimeoutExpired failure here rather than a false green.
        result = _run(slug_dir, project, home, cfg)
        _wait_quiesce(remember)

        assert stat.S_ISFIFO(gen_fifo.stat().st_mode), (
            "the FIFO was replaced -- the guard must refuse to open it, "
            "not clear it out of the way"
        )
        assert result.returncode == 0

        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files, "hook wrote no log at all"
        log_text = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
        assert "could not bump" in log_text
        assert "not a regular file" in log_text


# ── The conflict notice actually reaches the human ───────────────────────────


class TestNoticeIsWired:

    def test_git_reconcile_notice_is_in_the_consumption_loop(self):
        """git-reconcile-notice is written by the hook on a real conflict
        (see TestDivergedReconcile.test_conflict_aborts...) -- but writing a
        tmp/*-notice file does nothing on its own. scripts/user-prompt-hook.sh
        is what turns one into a systemMessage, through a hardcoded loop of
        names; a notice this hook writes and that loop does not name is
        written to disk and never shown to anyone.

        Anchored on the `for _notice_name in ...` line itself, not merely on
        the string appearing anywhere in the file -- a plain substring check
        is also satisfied by the doc comment above the loop, and would stay
        green even if someone later reverted just the loop entry while
        leaving the comment describing it untouched."""
        text = (REPO_ROOT / "scripts" / "user-prompt-hook.sh").read_text(encoding="utf-8")
        loop_lines = [ln for ln in text.splitlines() if ln.startswith("for _notice_name in")]
        assert loop_lines, "no 'for _notice_name in ...' loop found at all -- fixture assumption broken"
        assert "git-reconcile-notice" in loop_lines[0], (
            "git-reconcile-notice is not named in the for _notice_name in ... "
            "loop itself (only elsewhere in the file, e.g. a doc comment) -- "
            "the conflict report this hook is designed to surface would sit "
            "in tmp/ forever and never reach the human: " + loop_lines[0]
        )
