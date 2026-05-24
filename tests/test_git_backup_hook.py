"""Tests for hooks.d/after_save/50-git-backup.sh.

Each test sets up a temp home with ~/.remember/ as its own git repo backed by a
bare remote.  The hook is invoked via subprocess with the env vars that
save-session.sh would normally provide.  _LIB_MEMORY_DIR_LOADED=1 prevents
lib-memory-dir.sh from overriding the REMEMBER_DIR we set explicitly.
"""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

FLOCK_AVAILABLE = shutil.which("flock") is not None

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"


# ── Helpers ───────────────────────────────────────────────────────────────────


def wait_for_lock_release(lock_path: Path, timeout: float = 10, interval: float = 0.1) -> bool:
    """Poll until lock_path disappears or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not lock_path.exists():
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
    gitignore.write_text(".git-backup.lock\n.last-git-backup-ts\n*/logs/\n*/tmp/\n")
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
        assert not (remember / ".git-backup.lock").exists()
        assert not (remember / ".last-git-backup-ts").exists()

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

        assert (remember / ".last-git-backup-ts").exists()
        assert not (remember / ".git-backup.lock").exists()

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
        cooldown_marker = remember / ".last-git-backup-ts"
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

        cooldown_marker = remember / ".last-git-backup-ts"
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

        lock_file = remember / ".git-backup.lock"
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

        lock_file = remember / ".git-backup.lock"
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
