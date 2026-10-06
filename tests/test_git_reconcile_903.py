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


# ── The conflict notice actually reaches the human ───────────────────────────


class TestNoticeIsWired:

    def test_git_reconcile_notice_is_in_the_consumption_loop(self):
        """git-reconcile-notice is written by the hook on a real conflict
        (see TestDivergedReconcile.test_conflict_aborts...) -- but writing a
        tmp/*-notice file does nothing on its own. scripts/user-prompt-hook.sh
        is what turns one into a systemMessage, through a hardcoded loop of
        names; a notice this hook writes and that loop does not name is
        written to disk and never shown to anyone."""
        text = (REPO_ROOT / "scripts" / "user-prompt-hook.sh").read_text(encoding="utf-8")
        assert "git-reconcile-notice" in text, (
            "git-reconcile-notice is never consumed by user-prompt-hook.sh -- "
            "the conflict report this hook is designed to surface would sit "
            "in tmp/ forever and never reach the human"
        )
