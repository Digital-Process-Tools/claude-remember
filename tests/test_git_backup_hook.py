"""Tests for hooks.d/after_save/50-git-backup.sh.

Each test sets up a temp home with ~/.remember/ as its own git repo backed by a
bare remote.  The hook is invoked via subprocess with the env vars that
save-session.sh would normally provide.  _LIB_MEMORY_DIR_LOADED=1 prevents
lib-memory-dir.sh from overriding the REMEMBER_DIR we set explicitly.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics — not portable to Windows runners (#79)",
)

FLOCK_AVAILABLE = shutil.which("flock") is not None

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"


# ── Helpers ───────────────────────────────────────────────────────────────────


def state_dir(remember: Path, create: bool = False) -> Path:
    """Where the hooks keep their state since #261 — the git common dir, not
    the store root.

    Nothing there is ever tracked, merged or reported by `git status`, which is
    what stops an untracked state file colliding with a name the remote adds
    and making `merge --ff-only` refuse. Falls back to the store root, exactly
    as the hooks do when the common dir cannot be determined.
    """
    out = subprocess.run(
        ["git", "-C", str(remember), "rev-parse", "--path-format=absolute",
         "--git-common-dir"],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return remember
    d = Path(out.stdout.strip()) / "remember"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def hook_state(remember: Path, name: str, create_dir: bool = False) -> Path:
    """`.git-backup-rejected` at the store root → `git-backup-rejected` in the
    state dir. Call sites keep naming the historical dotted spelling."""
    return state_dir(remember, create=create_dir) / name.lstrip(".")


def _lock_is_free(lock_path: Path) -> bool:
    """Ask the lock whether it is held, rather than whether it exists."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        return False
    try:
        with open(lock_path, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return True
    except OSError:  # pragma: no cover - platform dependent
        return False


def wait_for_lock_release(lock_path: Path, timeout: float = 10, interval: float = 0.1) -> bool:
    """Poll until the backup's lock is no longer HELD.

    Two changes broke "the file is gone" as a completion signal, and both are
    silent: a caller naming a path that is never created returns instantly and
    every assertion after it then races the background subshell.

      * **#261** — the lock moved to the git common dir, so the store-root path
        these call sites pass no longer exists at any point.
      * **#258** — the flock path deliberately no longer unlinks the lock at
        all. flock's ownership is fd-based, so removing the path while holding
        it lets a second instance lock the now-unlinked inode while a third
        creates a fresh one at the same path.

    So on a platform with flock(1) — the platform whose hook path holds the lock
    on a descriptor — ask the lock. Where there is no flock(1) the hook runs its
    noclobber path, which does still unlink, and absence remains the honest
    test. Using the lock query on both would return True while a noclobber lock
    was held, which is the race this helper exists to prevent.
    """
    if lock_path.name == ".git-backup.lock":
        resolved = state_dir(lock_path.parent) / "git-backup.lock"
        if resolved != lock_path and resolved.exists():
            lock_path = resolved
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not lock_path.exists():
            return True
        if FLOCK_AVAILABLE and _lock_is_free(lock_path):
            return True
        time.sleep(interval)
    raise TimeoutError(f"Lock not released within {timeout}s: {lock_path}")


def _git(repo: Path, args: list) -> None:
    subprocess.run(["git", "-C", str(repo)] + args, check=True, capture_output=True)


def make_external_remember_repo(tmp_path: Path):
    """Create home/.remember/ as a git repo with a bare remote."""
    home = tmp_path / "home"
    remember = home / ".remember"
    remote = home / ".remember-remote.git"
    remember.mkdir(parents=True)
    _git(remember, ["init", "-q", "-b", "main"])
    _git(remember, ["config", "user.email", "test@test"])
    _git(remember, ["config", "user.name", "Test"])
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _git(remember, ["remote", "add", "origin", str(remote)])
    gitignore = remember / ".gitignore"
    gitignore.write_text(".git-backup.lock\n.last-git-backup-ts\n.git-backup-remote\n*/logs/\n*/tmp/\n")
    _git(remember, ["add", ".gitignore"])
    _git(remember, ["commit", "-q", "-m", "init"])
    _git(remember, ["push", "-q", "-u", "origin", "main"])
    return home, remember, remote


def _make_config(tmp_path: Path, cooldown: int = 900) -> Path:
    """Write a minimal REMEMBER_CONFIG with the given git_backup_seconds cooldown."""
    cfg = tmp_path / "remember-config.json"
    cfg.write_text(f'{{"cooldowns": {{"git_backup_seconds": {cooldown}}}}}')
    return cfg


def _run_hook(
    slug_dir: Path,
    project_dir: Path,
    home_dir: Path,
    extra_env: dict = None,
    config_path: Path = None,
) -> subprocess.CompletedProcess:
    """Run the git-backup hook with the environment save-session.sh would provide."""
    env = {
        **os.environ,
        "HOME": str(home_dir),
        "PROJECT_DIR": str(project_dir),
        "PIPELINE_DIR": str(REPO_ROOT),
        "REMEMBER_DIR": str(slug_dir),
        # Prevent lib-memory-dir.sh from overriding REMEMBER_DIR.
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_PROJECT": str(project_dir),
        # Ensure git commits work regardless of the user's global config.
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    if config_path is not None:
        env["REMEMBER_CONFIG"] = str(config_path)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", str(HOOK)], env=env, capture_output=True, text=True)


def _commit_log(repo: Path) -> list[str]:
    """Return oneline commit log for a repo (newest first). Empty list if no commits yet."""
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 128):
        raise RuntimeError(f"git log failed (rc={result.returncode}): {result.stderr}")
    return [l for l in result.stdout.strip().splitlines() if l]


def _files_in_commit(repo: Path, ref: str = "HEAD") -> list[str]:
    """Return list of files changed in a commit."""
    result = subprocess.run(
        ["git", "-C", str(repo), "diff-tree", "--no-commit-id", "-r", "--name-only", ref],
        capture_output=True, text=True, check=True,
    )
    return [l for l in result.stdout.strip().splitlines() if l]


# ── Test cases ────────────────────────────────────────────────────────────────


class TestGitBackupHook:

    def test_no_op_when_not_a_git_repo(self, tmp_path):
        """No-op when REPO_ROOT is not a git repo — no lock, no marker created."""
        home = tmp_path / "home"
        home.mkdir()
        remember = home / ".remember"
        remember.mkdir()
        slug_dir = remember / "test-slug"
        slug_dir.mkdir()
        project = tmp_path / "project"
        project.mkdir()

        result = _run_hook(slug_dir, project, home)

        assert result.returncode == 0
        assert not hook_state(remember, ".git-backup.lock").exists()
        assert not (hook_state(remember, ".last-git-backup-ts", create_dir=True)).exists()

    def test_no_op_in_legacy_mode(self, tmp_path):
        """No-op when REMEMBER_DIR is inside PROJECT_DIR — project repo is untouched."""
        project = tmp_path / "project"
        project.mkdir()
        _git(project, ["init", "-q"])
        _git(project, ["config", "user.email", "t@t"])
        _git(project, ["config", "user.name", "T"])
        remember_dir = project / ".remember"
        remember_dir.mkdir()

        result = _run_hook(remember_dir, project, tmp_path / "home")

        assert result.returncode == 0
        assert _commit_log(project) == []

    def test_no_op_when_not_at_git_toplevel(self, tmp_path):
        """No-op when REPO_ROOT is a subdir of a git repo (not its own toplevel)."""
        home = tmp_path / "home"
        home.mkdir()
        outer = home / "repos" / "some-repo"
        outer.mkdir(parents=True)
        _git(outer, ["init", "-q"])
        _git(outer, ["config", "user.email", "t@t"])
        _git(outer, ["config", "user.name", "T"])
        # .remember is nested inside the outer repo — not its own git toplevel.
        remember = outer / ".remember"
        remember.mkdir()
        slug_dir = remember / "test-slug"
        slug_dir.mkdir()
        project = tmp_path / "project"
        project.mkdir()

        result = _run_hook(slug_dir, project, home)

        assert result.returncode == 0
        assert _commit_log(outer) == []

    def test_happy_path_first_run(self, tmp_path):
        """First run: commits slug subtree with auto: message; marker and push succeed."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "test-project-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nSome memory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0

        wait_for_lock_release(remember / ".git-backup.lock")

        commits = _commit_log(remember)
        assert len(commits) == 2  # init + auto commit
        commit_msg = commits[0].split(" ", 1)[1]
        assert commit_msg.startswith(f"auto: {slug}")

        changed = _files_in_commit(remember)
        assert changed, "Expected at least one file in commit"
        assert all(f.startswith(f"{slug}/") for f in changed)

        assert (hook_state(remember, ".last-git-backup-ts", create_dir=True)).exists()
        # Not "the file is gone" (#258): the flock path deliberately keeps its
        # lock file, because flock's ownership is fd-based and unlinking the
        # path while holding it lets two instances lock two different inodes.
        # What must be true is that the lock is no longer HELD.
        _lock = hook_state(remember, ".git-backup.lock")
        assert not _lock.exists() or _lock_is_free(_lock)

    def test_nothing_to_commit_no_op(self, tmp_path):
        """Second run with no new changes: no commit added, cooldown marker unchanged."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        # First run — commits and sets the marker.
        _run_hook(slug_dir, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        # Backdate the marker to 0 so the cooldown check is definitely cleared.
        cooldown_marker = hook_state(remember, ".last-git-backup-ts", create_dir=True)
        cooldown_marker.write_text("0")
        marker_mtime_before = cooldown_marker.stat().st_mtime

        # Second run — no files changed.
        _run_hook(slug_dir, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        assert len(_commit_log(remember)) == 2  # init + first auto commit only

        # Cooldown marker must NOT be updated when nothing was committed.
        assert cooldown_marker.stat().st_mtime == marker_mtime_before

    def test_cooldown_respected(self, tmp_path):
        """Second invocation within the cooldown window exits early; marker stays unchanged."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        project = tmp_path / "project"
        project.mkdir()
        # 2s cooldown — first run sets the marker, second run fires before 2s elapse.
        cfg = _make_config(tmp_path, cooldown=2)

        _run_hook(slug_dir, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        cooldown_marker = hook_state(remember, ".last-git-backup-ts", create_dir=True)
        assert cooldown_marker.exists()
        marker_mtime_before = cooldown_marker.stat().st_mtime

        # Modify a file so there would be something to commit if not for cooldown.
        (slug_dir / "now.md").write_text("## 10:05 | test\nMore memory.\n")

        # Second run fires immediately — cooldown still active.
        _run_hook(slug_dir, project, home, config_path=cfg)
        # Cooldown exits before acquiring the lock, so no subshell to wait for.

        assert len(_commit_log(remember)) == 2  # init + first auto only

        # Cooldown marker must NOT be reset by the skipped run.
        assert cooldown_marker.stat().st_mtime == marker_mtime_before

    def test_per_slug_isolation(self, tmp_path):
        """Each slug gets its own commit containing only that slug's paths."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug_a = remember / "slug-a"
        slug_b = remember / "slug-b"
        slug_a.mkdir()
        slug_b.mkdir()
        (slug_a / "now.md").write_text("memory A\n")
        (slug_b / "now.md").write_text("memory B\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        _run_hook(slug_a, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        _run_hook(slug_b, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        commits = _commit_log(remember)
        assert len(commits) == 3  # init + slug-a + slug-b

        files_a = _files_in_commit(remember, "HEAD~1")
        assert files_a and all(f.startswith("slug-a/") for f in files_a)

        files_b = _files_in_commit(remember, "HEAD")
        assert files_b and all(f.startswith("slug-b/") for f in files_b)

    @pytest.mark.skipif(FLOCK_AVAILABLE, reason="noclobber path skipped when flock is present")
    def test_lock_contention_skips(self, tmp_path):
        """Hook exits silently without committing when lock is held by a live process (noclobber path)."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug_dir = remember / "test-slug"
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("memory\n")
        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        lock_file = hook_state(remember, ".git-backup.lock", create_dir=True)
        # Use the test runner's own PID — guaranteed to be alive.
        lock_file.write_text(str(os.getpid()))

        _run_hook(slug_dir, project, home, config_path=cfg)

        assert len(_commit_log(remember)) == 1  # init only
        # Lock must not be stolen from a live process.
        assert lock_file.exists()
        assert lock_file.read_text().strip() == str(os.getpid())

    @pytest.mark.skipif(FLOCK_AVAILABLE, reason="noclobber path skipped when flock is present")
    def test_stale_lock_takeover(self, tmp_path):
        """Hook takes over a lock held by a dead PID and commits successfully (noclobber path)."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug_dir = remember / "test-slug"
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("memory\n")
        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        lock_file = hook_state(remember, ".git-backup.lock", create_dir=True)
        # 999999 is an almost-certainly-dead PID on Linux.
        lock_file.write_text("999999")

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0

        wait_for_lock_release(lock_file)

        commits = _commit_log(remember)
        assert len(commits) == 2
        assert "auto:" in commits[0]
        assert not lock_file.exists()

    @pytest.mark.skipif(not FLOCK_AVAILABLE, reason="requires flock(1)")
    def test_flock_concurrent_only_one_wins(self, tmp_path):
        """With flock, two concurrent hook invocations produce exactly one commit."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("memory\n")
        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        results = []

        def run():
            r = _run_hook(slug_dir, project, home, config_path=cfg)
            results.append(r)

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        wait_for_lock_release(remember / ".git-backup.lock")

        commits = _commit_log(remember)
        # Exactly one hook committed — the other was blocked by flock and skipped.
        assert len(commits) == 2, f"Expected 2 commits (init + one auto), got {len(commits)}"
        assert "auto:" in commits[0]

    def test_push_failure_tolerated(self, tmp_path):
        """Local commit succeeds when push fails; log records 'push deferred'."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("memory\n")
        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        # Break the remote URL so push will fail.
        _git(remember, ["remote", "set-url", "origin", "/nonexistent/path.git"])

        _run_hook(slug_dir, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        # Local commit was made despite push failure.
        assert len(_commit_log(remember)) == 2

        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files, "Expected a log file in slug/logs/"
        log_content = log_files[0].read_text()
        assert "push deferred" in log_content


class TestGitBackupRemoteValidation:
    """Tests for the remote URL validation introduced in #67."""

    def test_first_push_records_remote_url(self, tmp_path):
        """First push writes the remote URL to .git-backup-remote and logs it prominently."""
        home, remember, remote = make_external_remember_repo(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")

        state_file = hook_state(remember, ".git-backup-remote", create_dir=True)
        assert state_file.exists(), ".git-backup-remote state file should be created on first push"
        recorded = state_file.read_text().strip()
        assert recorded == str(remote), f"Expected remote {remote!r}, got {recorded!r}"

        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files
        log_content = log_files[0].read_text()
        assert "git backup configured to push to:" in log_content

    def test_second_push_to_same_remote_succeeds(self, tmp_path):
        """Second push with unchanged remote URL commits and pushes without error."""
        home, remember, remote = make_external_remember_repo(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nFirst memory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        # First run — records remote URL.
        _run_hook(slug_dir, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        # Backdate cooldown marker so second run isn't skipped.
        (hook_state(remember, ".last-git-backup-ts", create_dir=True)).write_text("0")

        # Write new content so there's something to commit.
        (slug_dir / "now.md").write_text("## 10:05 | test\nSecond memory.\n")

        _run_hook(slug_dir, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        # Both auto commits should exist (init + first + second = 3 total).
        commits = _commit_log(remember)
        assert len(commits) == 3

        # No error in logs about remote change.
        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files
        log_content = log_files[0].read_text()
        assert "remote URL changed" not in log_content
        assert "push aborted" not in log_content

    def test_push_to_changed_remote_aborts(self, tmp_path):
        """Push is aborted when the remote URL changed and allow_remote_change is false."""
        home, remember, remote = make_external_remember_repo(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        # First run — records remote URL.
        _run_hook(slug_dir, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        # Simulate attacker changing the remote URL.
        evil_remote = tmp_path / "evil.git"
        subprocess.run(["git", "init", "-q", "--bare", str(evil_remote)], check=True, capture_output=True)
        _git(remember, ["remote", "set-url", "origin", str(evil_remote)])

        # Backdate cooldown marker and add new content.
        (hook_state(remember, ".last-git-backup-ts", create_dir=True)).write_text("0")
        (slug_dir / "now.md").write_text("## 10:05 | test\nMore memory.\n")

        _run_hook(slug_dir, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        # Local commit was made (no push, but commit still happens).
        commits = _commit_log(remember)
        assert len(commits) == 3  # init + first auto + second auto

        # Evil remote should have received nothing — verify it has no commits.
        evil_log = subprocess.run(
            ["git", "-C", str(evil_remote), "log", "--oneline"],
            capture_output=True, text=True,
        )
        assert evil_log.stdout.strip() == "", "Evil remote should not have received any commits"

        # Error should be logged.
        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files
        log_content = log_files[0].read_text()
        assert "remote URL changed" in log_content
        assert "push aborted" in log_content

    def test_push_to_changed_remote_allowed_when_override_set(self, tmp_path):
        """When allow_remote_change=true, a changed remote URL is accepted and push proceeds."""
        home, remember, remote = make_external_remember_repo(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        # First run — records remote URL.
        _run_hook(slug_dir, project, home, config_path=cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        # Set up a new (legitimate) remote and change the URL.
        new_remote = tmp_path / "new-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(new_remote)], check=True, capture_output=True)
        _git(remember, ["remote", "set-url", "origin", str(new_remote)])
        # Push current history to new remote so it accepts the push.
        _git(remember, ["push", "-q", "-u", str(new_remote), "main"])
        _git(remember, ["remote", "set-url", "origin", str(new_remote)])

        # Backdate cooldown marker and add new content.
        (hook_state(remember, ".last-git-backup-ts", create_dir=True)).write_text("0")
        (slug_dir / "now.md").write_text("## 10:05 | test\nMore memory.\n")

        # Override config: allow_remote_change = true.
        override_cfg = tmp_path / "override-config.json"
        override_cfg.write_text('{"cooldowns": {"git_backup_seconds": 0}, "git_backup": {"allow_remote_change": true}}')

        _run_hook(slug_dir, project, home, config_path=override_cfg)
        wait_for_lock_release(remember / ".git-backup.lock")

        # State file should now point to the new remote.
        state_file = hook_state(remember, ".git-backup-remote", create_dir=True)
        recorded = state_file.read_text().strip()
        assert recorded == str(new_remote), f"State file should be updated to new remote, got {recorded!r}"

        # Log should mention the change with allow note, not an error.
        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files
        log_content = log_files[0].read_text()
        assert "allow_remote_change=true" in log_content
        assert "push aborted" not in log_content


class TestGitBackupConfigurablePushTarget:
    """Tests for configurable git_backup.remote / git_backup.branch (#63)."""

    def test_push_routes_to_configured_remote(self, tmp_path):
        """git_backup.remote pushes to that remote, not the default origin, and logs the resolved target."""
        home, remember, origin = make_external_remember_repo(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        project = tmp_path / "project"
        project.mkdir()

        # A second, explicitly-configured remote distinct from origin.
        backup = tmp_path / "backup-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(backup)], check=True, capture_output=True)
        _git(remember, ["remote", "add", "backup", str(backup)])
        _git(remember, ["push", "-q", "-u", "backup", "main"])

        cfg = tmp_path / "remote-config.json"
        cfg.write_text('{"cooldowns": {"git_backup_seconds": 0}, "git_backup": {"remote": "backup"}}')

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")

        # Local commit was made.
        assert len(_commit_log(remember)) == 2

        # The configured remote received the auto commit; origin did NOT.
        # Bare repos: read the explicit main ref (their default HEAD may be master).
        def _ref_count(bare, ref="refs/heads/main"):
            r = subprocess.run(["git", "-C", str(bare), "rev-list", "--count", ref],
                               capture_output=True, text=True)
            return int(r.stdout.strip()) if r.returncode == 0 else 0
        assert _ref_count(backup) == 2, "configured 'backup' remote should have the auto commit"
        assert _ref_count(origin) == 1, "default origin must NOT receive the push when a remote is configured"

        # State file records the configured remote's URL, and the log names the resolved target.
        assert hook_state(remember, ".git-backup-remote").read_text().strip() == str(backup)
        log_files = list((slug_dir / "logs").glob("memory-*.log"))
        assert log_files
        log_content = log_files[0].read_text()
        assert "remote 'backup'" in log_content


def _configure_fake_gpg(remember: Path, tmp_path: Path) -> None:
    """Point the repo at a stub gpg that emits a fake signature and enable commit.gpgsign.

    Lets us assert signing behaviour deterministically in CI without a real GPG key:
    git embeds whatever the stub prints (and trusts the SIG_CREATED status line).
    """
    fake = tmp_path / "fakegpg.sh"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "[GNUPG:] SIG_CREATED G" >&2\n'
        "cat <<'SIG'\n"
        "-----BEGIN PGP SIGNATURE-----\n"
        "\n"
        "fakefakefake\n"
        "-----END PGP SIGNATURE-----\n"
        "SIG\n"
    )
    fake.chmod(0o755)
    _git(remember, ["config", "gpg.program", str(fake)])
    _git(remember, ["config", "commit.gpgsign", "true"])
    _git(remember, ["config", "user.signingkey", "FAKEKEY"])


def _is_signed(repo: Path, ref: str = "HEAD") -> bool:
    """True if the commit object carries a gpgsig header."""
    r = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-p", ref],
        capture_output=True, text=True, check=True,
    )
    return "gpgsig" in r.stdout


class TestGitBackupGpgSign:
    """Tests for configurable commit signing via git_backup.gpg_sign (#62)."""

    def test_default_commit_is_not_signed(self, tmp_path):
        """Default (no git_backup.gpg_sign): --no-gpg-sign is passed, overriding repo commit.gpgsign."""
        home, remember, remote = make_external_remember_repo(tmp_path)
        _configure_fake_gpg(remember, tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")

        assert len(_commit_log(remember)) == 2
        assert not _is_signed(remember), "default commit must stay unsigned (--no-gpg-sign)"

    def test_gpg_sign_true_honors_user_signing(self, tmp_path):
        """git_backup.gpg_sign=true omits --no-gpg-sign, so the repo's commit.gpgsign is honored."""
        home, remember, remote = make_external_remember_repo(tmp_path)
        _configure_fake_gpg(remember, tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = tmp_path / "gpg-config.json"
        cfg.write_text('{"cooldowns": {"git_backup_seconds": 0}, "git_backup": {"gpg_sign": true}}')

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")

        assert len(_commit_log(remember)) == 2
        assert _is_signed(remember), "gpg_sign=true must let the repo sign the commit"


def _make_worktree_project(tmp_path: Path):
    """Create a project repo with a linked worktree. Returns (main_checkout, worktree)."""
    main = tmp_path / "project"
    main.mkdir()
    _git(main, ["init", "-q", "-b", "main"])
    _git(main, ["config", "user.email", "t@t"])
    _git(main, ["config", "user.name", "T"])
    (main / "README.md").write_text("project\n")
    _git(main, ["add", "README.md"])
    _git(main, ["commit", "-q", "-m", "init"])
    worktree = tmp_path / "project-wt"
    _git(main, ["worktree", "add", "-q", "-b", "feature", str(worktree)])
    return main, worktree


class TestGitBackupNeverTouchesProjectRepo:
    """The hook must never commit/push into the project's own repo (#138).

    Since #127 a worktree session keeps PROJECT_DIR on the worktree while
    REMEMBER_DIR is redirected into the main checkout, so the plain
    ``REPO_ROOT == PROJECT_DIR`` legacy guard no longer matches and the hook
    used to treat the project repo as an external backup repo.
    """

    def test_no_op_for_legacy_memory_in_worktree_session(self, tmp_path):
        """Legacy mode + worktree session: project repo untouched, .gitignore kept."""
        main, worktree = _make_worktree_project(tmp_path)
        remember_dir = main / ".remember"
        remember_dir.mkdir()
        gitignore = remember_dir / ".gitignore"
        gitignore.write_text("*\n")
        (remember_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        commits_before = _commit_log(main)
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(remember_dir, worktree, tmp_path / "home", config_path=cfg)

        assert result.returncode == 0
        assert _commit_log(main) == commits_before, "project repo must not be committed to"
        assert gitignore.exists(), "protective .remember/.gitignore must survive"
        assert not (main / ".git-backup.lock").exists()
        assert not (main / ".last-git-backup-ts").exists()

    def test_no_op_when_repo_root_is_a_sibling_worktree_of_the_project(self, tmp_path):
        """Memory parked in another worktree of the same repo is still the project repo."""
        main, worktree = _make_worktree_project(tmp_path)
        remember_dir = worktree / ".remember"
        remember_dir.mkdir()
        (remember_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")

        commits_before = _commit_log(main)
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(remember_dir, main, tmp_path / "home", config_path=cfg)

        assert result.returncode == 0
        assert _commit_log(main) == commits_before

    def test_external_repo_still_activates_for_a_worktree_session(self, tmp_path):
        """Guard must not over-fire: a dedicated memory repo still backs up normally."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        _main, worktree = _make_worktree_project(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(slug_dir, worktree, home, config_path=cfg)
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")

        commits = _commit_log(remember)
        assert len(commits) == 2, "external backup must still commit for worktree sessions"
        assert commits[0].split(" ", 1)[1].startswith(f"auto: {slug}")

    def test_leaked_git_dir_does_not_disable_the_guard(self, tmp_path):
        """A leaked GIT_DIR must not make every `git -C` resolve to the same repo.

        `git -C DIR rev-parse` honours an exported GIT_DIR over `-C`, so without
        sanitizing the environment first, both sides of the common-dir comparison
        collapse onto the leaked repo, compare equal, and the hook skips every
        backup — including legitimate external ones.
        """
        home, remember, _ = make_external_remember_repo(tmp_path)
        _main, worktree = _make_worktree_project(tmp_path)
        slug = "test-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")
        cfg = _make_config(tmp_path, cooldown=0)

        leaked = tmp_path / "unrelated"
        leaked.mkdir()
        _git(leaked, ["init", "-q"])

        result = _run_hook(
            slug_dir, worktree, home, config_path=cfg,
            extra_env={"GIT_DIR": str(leaked / ".git"), "GIT_WORK_TREE": str(leaked)},
        )
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")

        assert len(_commit_log(remember)) == 2, \
            "leaked GIT_DIR must not suppress a legitimate external backup"
        assert _commit_log(leaked) == [], "the leaked repo must never be committed to"


class TestConfigIsReadBeforeBackgrounding:
    """Every git_backup.* value must be read in the parent, not the subshell.

    lib-memory-dir.sh installs an EXIT trap that deletes $REMEMBER_CONFIG. The
    hook backgrounds its work and returns, so a read inside the subshell races
    that deletion — and once the merged config is gone, config() quietly hands
    back each caller's default (issue #135). A slow backup would push to the
    wrong remote, or to nowhere with remote and branch emptied, and nothing
    would say so: a missing config file is not itself reported.

    This is asserted structurally rather than by running the race. The window
    is a few microseconds wide, and the harness cannot even open it — it sets
    _LIB_MEMORY_DIR_LOADED=1, so the trap that deletes the file never installs.
    A timing test here would pass on the broken code most of the time, which is
    worse than no test. What can be pinned exactly is the ordering the fix
    depends on.
    """

    GIT_BACKUP_VARS = (
        "GIT_BACKUP_REMOTE", "GIT_BACKUP_BRANCH",
        "GIT_BACKUP_GPG_SIGN", "ALLOW_REMOTE_CHANGE",
        "REJECT_NOTICE_AFTER",
    )

    @staticmethod
    def _fork_line(lines):
        """Index of the line opening the background subshell."""
        forks = [i for i, line in enumerate(lines) if line.rstrip() == "("]
        assert len(forks) == 1, f"expected exactly one top-level subshell, found {forks}"
        return forks[0]

    def test_no_config_read_of_any_kind_inside_the_subshell(self):
        """Nothing may consult the config file after the fork.

        Checked as "no config() call at all past the fork" rather than by
        grepping for the git_backup keys: an earlier version of this test
        matched the literal string `config ".git_backup.`, and a review beat
        it in one move by putting the key in a variable — the race was back
        and the test still passed.
        """
        lines = HOOK.read_text().splitlines()
        fork = self._fork_line(lines)
        late = [
            (i + 1, line.strip())
            for i, line in enumerate(lines)
            if i > fork and re.search(r"(?<![\w-])config\s+[\"'$]", line)
        ]
        assert late == [], (
            "these config reads happen after the fork, so they race the parent's "
            f"EXIT trap deleting $REMEMBER_CONFIG (#135): {late}"
        )

    def test_every_git_backup_value_is_assigned_before_the_fork(self):
        """Each value must be READ in the parent, not merely mentioned there.

        Assignment sites, not substrings: the previous version was satisfied by
        the key appearing anywhere in the file, so a dead comment above the
        fork made it pass while the real read sat inside the subshell.
        """
        lines = HOOK.read_text().splitlines()
        fork = self._fork_line(lines)
        for var in self.GIT_BACKUP_VARS:
            assigned = [
                i for i, line in enumerate(lines)
                if re.match(rf"\s*{var}=\$\(\s*config\s", line)
            ]
            assert assigned, f"{var} is never assigned from config()"
            assert all(i < fork for i in assigned), (
                f"{var} is read after the fork (lines {[i + 1 for i in assigned]}, "
                f"fork at {fork + 1}) — it races the config file's deletion (#135)"
            )

    def test_the_subshell_still_uses_every_hoisted_value(self):
        """A hoist that nothing consumes would pass the checks above and do
        nothing — pin that what was read still crosses the fork.

        gpg_sign is consumed in the parent to pick GPG_SIGN_FLAG, so that flag
        is the value the subshell actually uses.
        """
        consumed_as = {
            "GIT_BACKUP_REMOTE": "GIT_BACKUP_REMOTE",
            "GIT_BACKUP_BRANCH": "GIT_BACKUP_BRANCH",
            "GIT_BACKUP_GPG_SIGN": "GPG_SIGN_FLAG",
            "ALLOW_REMOTE_CHANGE": "ALLOW_REMOTE_CHANGE",
            "REJECT_NOTICE_AFTER": "REJECT_NOTICE_AFTER",
        }
        lines = HOOK.read_text().splitlines()
        fork = self._fork_line(lines)
        body = "\n".join(lines[fork:])
        for var, used in consumed_as.items():
            assert f"${used}" in body or f"${{{used}" in body, (
                f"{var} is read in the parent but {used} never reaches the backup"
            )
