"""A store that already tracked logs/ and tmp/ must stop pushing them (#288).

#287 made the backup hook maintain `/<slug>/logs/` and `/<slug>/tmp/` in the
store's `$GIT_DIR/info/exclude`. An exclude rule has no effect on a path git
already tracks, which #287 knew — so it also runs a `git rm --cached`. On a
store that predates #287 that untracking does not land, for two independent
reasons, and the logs keep being committed and pushed on every save:

  * `git rm --cached -- "$SLUG/logs/"` on a directory fails outright without
    `-r` (*fatal: not removing '…' recursively without -r*), and the hook
    discards the error;
  * and even with `-r`, the commit is `git commit -- "$SLUG/"`, a PARTIAL
    commit, which takes its content from the working tree rather than the
    index — so it restores the very paths the removal staged. This is the
    same mechanism #287 documented for the delivery record, whose answer was
    to make the path stop existing in the working tree. `logs/` and `tmp/`
    cannot take that answer: they are live directories the plugin writes to
    every session.

The consequence is worse than the history #288 reports. On exactly the stores
#288 is about, `tmp/` is still tracked, and `tmp/` is where #287 moved the
handoff delivery record — so #285's inversion is not fixed there either.

These tests assert the OBSERVABLE: what the remote receives. They deliberately
also assert that history is left alone, because the fix for what is already
pushed is a user's decision to rewrite (README, "logs in an older backup"),
never something a hook does to someone's repository behind their back.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX git semantics — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_HOOK = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"

SLUG = "c--projects-legacy"
LOG_NAME = "pipeline-2026-08-01.log"


def _git(repo: Path, args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo)] + args, check=True, capture_output=True, text=True
    )


def _env(slug_dir: Path, project: Path, home: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "PROJECT_DIR": str(project),
        "CLAUDE_PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(REPO_ROOT),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(slug_dir),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_PROJECT": str(project),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }


def _backup(slug_dir: Path, project: Path, home: Path) -> None:
    """Run the backup hook and wait for the PUSH, not the commit.

    The hook works in a detached subshell; the remote ref is the observable
    that means "the other machine can see this".
    """
    repo = slug_dir.parent
    # The 15-minute backup cooldown would make a second call in the same test a
    # silent no-op, and these tests are about what the SECOND save does.
    for marker in repo.glob(".git/remember/last-git-backup-ts"):
        marker.unlink()
    remote = _git(repo, ["remote", "get-url", "origin"]).stdout.strip()
    before = _git(repo, ["ls-remote", remote, "refs/heads/main"]).stdout.split("\t")[0]

    subprocess.run(
        ["bash", str(BACKUP_HOOK)],
        env=_env(slug_dir, project, home),
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        now = _git(repo, ["ls-remote", remote, "refs/heads/main"]).stdout.split("\t")[0]
        if now and now != before:
            return
        time.sleep(0.2)
    raise TimeoutError(
        "the backup never reached the remote — the test cannot say anything "
        "about what the remote holds.\nlocal log:\n"
        + _git(repo, ["log", "--oneline"]).stdout
    )


def _remote_tip_paths(remote: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def legacy_store(tmp_path):
    """A store as it exists on any machine that ran the backup before #287.

    Built the way the pre-#287 hook built it: the whole slug subtree staged
    with no exclusion, so `logs/` and `tmp/` are tracked and pushed alongside
    memory.
    """
    remote = tmp_path / "backup.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    home = tmp_path / "machine"
    home.mkdir()
    store = home / ".remember"
    store.mkdir()
    _git(store, ["init", "-q", "-b", "main"])
    _git(store, ["config", "user.email", "test@test"])
    _git(store, ["config", "user.name", "Test"])
    _git(store, ["remote", "add", "origin", str(remote)])

    slug_dir = store / SLUG
    (slug_dir / "logs").mkdir(parents=True)
    (slug_dir / "tmp").mkdir(parents=True)
    (slug_dir / "remember.md").write_text("# memory\n- a thing worth keeping\n")
    (slug_dir / "logs" / LOG_NAME).write_text("session log line\n")
    (slug_dir / "tmp" / "last-save.json").write_text('{"ts":1}\n')
    (store / "config.json").write_text("{}\n")
    _git(store, ["add", "-A"])
    _git(store, ["commit", "-qm", "legacy state"])
    _git(store, ["push", "-q", "-u", "origin", "main"])

    tracked = _git(store, ["ls-files"]).stdout
    assert f"{SLUG}/logs/{LOG_NAME}" in tracked, (
        "the fixture must reproduce the reported store, in which logs/ IS "
        f"tracked. tracked:\n{tracked}"
    )

    project = tmp_path / "project"
    project.mkdir()
    return store, slug_dir, project, home, remote


class TestLegacyStoreStopsPushingLogs:

    def test_the_upgrade_untracks_logs_and_tmp(self, legacy_store):
        """The mechanism: after one backup, git no longer tracks either path."""
        store, slug_dir, project, home, _remote = legacy_store

        (slug_dir / "remember.md").write_text("# memory\n- a thing\n- another\n")
        _backup(slug_dir, project, home)

        tracked = _git(store, ["ls-files"]).stdout
        assert f"{SLUG}/logs/" not in tracked, (
            "logs/ is still tracked after the upgrade, so every later save "
            f"commits and pushes it again. tracked:\n{tracked}"
        )
        assert f"{SLUG}/tmp/" not in tracked, (
            "tmp/ is still tracked after the upgrade — and tmp/ is where #287 "
            "moved the handoff delivery record, so #285's inversion is not "
            f"fixed on this store either. tracked:\n{tracked}"
        )
        assert f"{SLUG}/remember.md" in tracked, (
            "and memory must still be tracked, so this fails for the right "
            f"reason rather than because nothing is backed up. tracked:\n{tracked}"
        )

    def test_the_store_comes_to_rest_clean(self, legacy_store):
        """A permanently staged deletion is a worse outcome than the bug.

        Untracking a path that still exists in the working tree is exactly the
        shape that leaves a `D` sitting in the user's index forever — noise in
        their own `git status`, and a name a later `merge --ff-only` can refuse
        over (#287).
        """
        store, slug_dir, project, home, _remote = legacy_store

        (slug_dir / "remember.md").write_text("# memory\n- a thing\n- another\n")
        _backup(slug_dir, project, home)

        status = _git(store, ["status", "--porcelain"]).stdout
        assert status.strip() == "", (
            "the store must come to rest clean: no staged deletion left "
            f"behind, and no untracked noise. status:\n{status}"
        )

    def test_logs_written_after_the_upgrade_never_reach_the_remote(
        self, legacy_store
    ):
        """The behaviour, in the terms #288 is about: what leaves the machine.

        Two backups, because the first is the migration and the second is
        every ordinary save after it. A log line written between them must not
        be in what the remote holds.
        """
        store, slug_dir, project, home, remote = legacy_store

        (slug_dir / "remember.md").write_text("# memory\n- a thing\n- another\n")
        _backup(slug_dir, project, home)

        (slug_dir / "logs" / LOG_NAME).write_text("a line written after upgrading\n")
        (slug_dir / "remember.md").write_text("# memory\n- a thing\n- a third\n")
        _backup(slug_dir, project, home)

        paths = _remote_tip_paths(remote)
        assert f"{SLUG}/logs/" not in paths, (
            "the remote's current tip still carries logs/, so the store is "
            f"still pushing session logs after upgrading. tip:\n{paths}"
        )
        assert f"{SLUG}/remember.md" in paths, (
            f"memory must still be reaching the remote. tip:\n{paths}"
        )

    def test_history_is_left_alone(self, legacy_store):
        """What is already pushed stays pushed — that is #288's whole point.

        Nothing here may rewrite the user's history: it is their repository,
        a rewrite means a force-push, and a force-push breaks every other
        clone. The README tells them; the hook does not decide for them.
        """
        store, slug_dir, project, home, _remote = legacy_store

        (slug_dir / "remember.md").write_text("# memory\n- a thing\n- another\n")
        _backup(slug_dir, project, home)

        past = _git(store, ["log", "--oneline", "--", f"{SLUG}/logs/"]).stdout
        assert past.strip() != "", (
            "the commits that already carried logs/ must still be there. A "
            "hook that silently rewrote them would be a far worse bug than "
            "the one being fixed."
        )
