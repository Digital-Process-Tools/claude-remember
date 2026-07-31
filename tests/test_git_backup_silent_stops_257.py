"""Four ways the git backup stops without saying so (#257, #258, #260, #261).

Every test here pins a state the hooks currently cannot express. The shape is
the one this repo keeps filing: an absence produced by the tool, read as an
absence in the world.

  * **#260** — a store reached through a symlink is compared textually against
    a resolved `rev-parse --show-toplevel`, so both hooks exit 0 above every
    `log` call. Backup never runs, restore never runs, and the store looks
    exactly like a healthy one.
  * **#257** — a failed commit is `log … ; exit 0`, so memory is committed
    nowhere while the hook reports success; and a store whose push target is
    reachable only through the branch's upstream is told it has "no remote
    configured", forever.
  * **#258** — the cooldown marker is unvalidated file content fed into `$(( ))`
    under `set -u`, and it is written only when the commit succeeds.
  * **#261** — hook state files accumulate untracked at the store root, where a
    name collision makes `merge --ff-only` refuse.

Real repositories throughout, real symlinks, real bare remotes. The failures
these pin are all invisible to a caller, so a test asserting "the hook exited 0"
would pass against every one of them — each assertion below is on the
post-condition (a commit exists, the remote tip moved, the store root is clean)
rather than on the hook's own report of itself.
"""

import json
import os
import subprocess
import shutil
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics — not portable to Windows runners (#79)",
)

from .test_git_backup_hook import (  # noqa: E402
    REPO_ROOT,
    _git,
    make_external_remember_repo,
)
from .test_git_backup_push_rejected_253 import (  # noqa: E402
    _advance_remote,
    _log_text,
    _remote_head,
)

BACKUP_HOOK = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"
RESTORE_HOOK = REPO_ROOT / "hooks.d" / "before_session_start" / "50-git-restore.sh"

FLOCK_AVAILABLE = shutil.which("flock") is not None

# Every file either hook has ever written at the store root. #261 is about all
# of them, not only the two the issue happened to observe in a live run.
ROOT_STATE_NAMES = (
    ".git-backup.lock",
    ".last-git-backup-ts",
    ".git-backup-remote",
    ".git-backup-rejected",
    ".git-restore-fetch",
    ".git-restore-diverged",
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _env(slug_dir: Path, project: Path, home: Path, cfg: Path = None, **extra) -> dict:
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
    return env


def _run_backup(slug_dir, project, home, cfg=None, **extra):
    return subprocess.run(
        ["bash", str(BACKUP_HOOK)],
        env=_env(slug_dir, project, home, cfg, **extra),
        capture_output=True, text=True, timeout=120,
    )


def _run_restore(slug_dir, project, home, cfg=None, **extra):
    return subprocess.run(
        ["bash", str(RESTORE_HOOK)],
        env=_env(slug_dir, project, home, cfg, **extra),
        capture_output=True, text=True, timeout=120,
    )


def _backup_config(tmp_path: Path, name: str = "remember-config.json", **git_backup) -> Path:
    cfg = tmp_path / name
    body: dict = {"cooldowns": {"git_backup_seconds": 0}}
    if git_backup:
        body["git_backup"] = git_backup
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def _restore_config(tmp_path: Path, name: str = "remember-config.json", **git_restore) -> Path:
    cfg = tmp_path / name
    body: dict = {"git_restore": {"enabled": True, **git_restore}}
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def _log_or_empty(slug_dir: Path) -> str:
    """`_log_text` asserts when no log file exists yet, which is the right
    behaviour for a final assertion and the wrong one inside a poll loop — it
    raises out of the wait instead of meaning "not yet"."""
    files = list((slug_dir / "logs").glob("memory-*.log"))
    return "\\n".join(f.read_text(encoding="utf-8") for f in files)


def _wait_until(predicate, timeout: float = 30, interval: float = 0.1):
    """Poll a post-condition. The backup hook does its work in a detached
    subshell, so returning from the subprocess proves nothing about it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _slug_is_committed(remember: Path, slug: str = "test-slug") -> bool:
    r = subprocess.run(
        ["git", "-C", str(remember), "ls-files", "--", f"{slug}/"],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def _head(repo: Path) -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def _notice_path(slug_dir: Path) -> Path:
    return slug_dir / "tmp" / "git-backup-notice"


def _break_commits(remember: Path, marker: str = "PRECOMMIT-REFUSED-XYZ") -> None:
    """A pre-commit hook that always refuses. This is #257's own list of durable
    causes (a pre-commit hook installed on the backup repo) and it is the one
    that is deterministic on every runner."""
    hooks = remember / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    pc = hooks / "pre-commit"
    pc.write_text(f"#!/bin/sh\necho '{marker}' >&2\nexit 1\n", encoding="utf-8")
    pc.chmod(0o755)


def _store(tmp_path: Path, *, root: Path = None):
    """A memory store with one unsaved slug, a bare remote, and a project dir."""
    base = root if root is not None else tmp_path
    home, remember, remote = make_external_remember_repo(base)
    slug_dir = remember / "test-slug"
    slug_dir.mkdir()
    (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return home, remember, remote, slug_dir, project


# ═════════════════════════════════════════════════════════════════════════════
# #260 — a store reached through a symlink
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def symlinked_store(tmp_path):
    """The store exists at `real/`, and every env var names it through `link/`.

    `REPO_ROOT` is then spelled with the symlink while `rev-parse
    --show-toplevel` answers with the resolved path, which is the whole of
    #260. macOS makes this ordinary rather than exotic — `/var` is a symlink to
    `/private/var`, so any store under `TMPDIR` is already in this state.
    """
    real = tmp_path / "real"
    real.mkdir()
    home, remember, remote, _, project = _store(tmp_path, root=real)

    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    linked_home = link / "home"
    linked_remember = linked_home / ".remember"
    linked_slug = linked_remember / "test-slug"

    # The fixture is only meaningful if the two spellings actually differ.
    toplevel = subprocess.run(
        ["git", "-C", str(linked_remember), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert toplevel != str(linked_remember), (
        "fixture did not reproduce #260: the symlinked spelling and "
        f"--show-toplevel agree ({toplevel!r})"
    )
    return linked_home, linked_remember, remote, linked_slug, project, remember


def test_a_symlinked_store_is_backed_up(symlinked_store, tmp_path):
    """#260, write half. Currently: `exit 0` at line 46, above every log call."""
    home, remember, remote, slug_dir, project, real_remember = symlinked_store
    cfg = _backup_config(tmp_path)

    _run_backup(slug_dir, project, home, cfg)

    assert _wait_until(lambda: _slug_is_committed(real_remember)), (
        "the slug was never committed — a store reached through a symlink gets "
        "no backup at all, and nothing anywhere says so"
    )


def test_a_symlinked_store_is_restored(symlinked_store, tmp_path):
    """#260, read half. The same textual comparison at 50-git-restore.sh:74."""
    home, remember, remote, slug_dir, project, real_remember = symlinked_store
    _git(real_remember, ["add", "-A"])
    _git(real_remember, ["commit", "-q", "-m", "local"])
    _git(real_remember, ["push", "-q", "origin", "main"])

    _advance_remote(tmp_path, remote)
    _git(real_remember, ["fetch", "-q", "origin"])
    before = _head(real_remember)

    _run_restore(slug_dir, project, home, _restore_config(tmp_path))

    assert _wait_until(lambda: _head(real_remember) != before), (
        "HEAD never moved — a store reached through a symlink is never "
        "fast-forwarded, and hook-errors.log stays empty"
    )


def test_a_subdirectory_of_a_larger_repo_is_still_refused_and_says_so(tmp_path):
    """The property #260's fix must not trade away, plus its second half.

    The guard exists to stop the hooks operating on a user's project repo. A
    resolved comparison must still refuse a store that is a *subdirectory* of
    some larger repo — and a store the hooks deliberately decline to touch is
    currently indistinguishable from one they are managing correctly.
    """
    outer = tmp_path / "outer"
    outer.mkdir()
    _git_init = subprocess.run(["git", "init", "-q", "-b", "main", str(outer)],
                               capture_output=True, check=True)
    _git(outer, ["config", "user.email", "test@test"])
    _git(outer, ["config", "user.name", "Test"])
    (outer / "README").write_text("outer\n", encoding="utf-8")
    _git(outer, ["add", "-A"])
    _git(outer, ["commit", "-q", "-m", "init"])

    inner = outer / "nested" / ".remember"
    slug_dir = inner / "test-slug"
    slug_dir.mkdir(parents=True)
    (slug_dir / "now.md").write_text("memory\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    _run_backup(slug_dir, project, home, _backup_config(tmp_path))

    # The property: nothing from the nested store reaches the outer repo.
    assert not _wait_until(lambda: _slug_is_committed(outer), timeout=3), (
        "the hook committed into a repo it does not own"
    )

    # The disclosure: silence must mean "ran, nothing to do", not "declined".
    text = _log_text(slug_dir)
    assert "not the toplevel" in text or "not a repository root" in text, (
        "the hook declined to manage this store and said nothing — a declined "
        f"store reads exactly like a healthy one. Log was:\n{text}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# #257 case 1 — a failed commit is log-only
# ═════════════════════════════════════════════════════════════════════════════


def test_a_failed_commit_reports_gits_own_reason(tmp_path):
    """`ERROR: commit failed for $SLUG` names no cause, because git's stderr is
    thrown away by `>/dev/null 2>&1`. A user reading that line learns only that
    something is wrong, which is the half they already knew."""
    home, remember, remote, slug_dir, project = _store(tmp_path)
    _break_commits(remember)

    _run_backup(slug_dir, project, home, _backup_config(tmp_path))
    _wait_until(lambda: "commit FAILED" in _log_or_empty(slug_dir), timeout=20)

    text = _log_text(slug_dir)
    assert "PRECOMMIT-REFUSED-XYZ" in text, (
        "the commit failed and git's own explanation was discarded — the log "
        f"says something is broken and not what. Log was:\n{text}"
    )


def test_repeated_commit_failure_escalates_to_the_human(tmp_path):
    """A commit that fails for a durable reason fails identically forever, so
    the escalation #253 built is reachable without inventing anything. Nothing
    currently counts, so nothing ever escalates."""
    home, remember, remote, slug_dir, project = _store(tmp_path)
    _break_commits(remember)
    cfg = _backup_config(tmp_path, commit_notice_after=2)

    for i in range(3):
        (slug_dir / "now.md").write_text(f"memory {i}\n", encoding="utf-8")
        _run_backup(slug_dir, project, home, cfg)
        _wait_until(lambda: _log_or_empty(slug_dir).count("commit FAILED") > i,
                    timeout=20)
        time.sleep(0.3)

    assert _wait_until(lambda: _notice_path(slug_dir).exists(), timeout=15), (
        "three consecutive failed commits and the human was never told — "
        "memory is being committed nowhere and the hook reports success"
    )
    assert "STOPPED" in _notice_path(slug_dir).read_text(encoding="utf-8")


# ═════════════════════════════════════════════════════════════════════════════
# #257 case 2 — a store that backs up nowhere
# ═════════════════════════════════════════════════════════════════════════════


def test_push_follows_the_branchs_upstream_when_there_is_no_origin(tmp_path):
    """Not in the issue, found reading the call site.

    `_push` with `git_backup.remote` unset runs a bare `git push`, which follows
    the branch's upstream. The validation immediately above it asks
    `git remote get-url origin`. A store whose upstream is any other remote
    therefore gets `no remote configured, skipping push` forever while holding a
    perfectly good push target — and the URL the security check records is not
    the URL the push goes to.
    """
    home, remember, remote, slug_dir, project = _store(tmp_path)
    _git(remember, ["remote", "rename", "origin", "backup"])
    _git(remember, ["branch", "--set-upstream-to=backup/main", "main"])
    before = _remote_head(remote)

    _run_backup(slug_dir, project, home, _backup_config(tmp_path))

    assert _wait_until(lambda: _remote_head(remote) != before), (
        "nothing reached the remote — the hook looked for `origin`, did not "
        "find it, and declared the store remoteless while `git push` would "
        f"have worked. Log was:\n{_log_text(slug_dir)}"
    )


def test_a_store_with_no_remote_at_all_eventually_says_so_exactly_once(tmp_path):
    """One info-level line per save, forever, is how a half-finished setup
    becomes a permanent absence of off-machine backup.

    Local-only is a legitimate choice, so this must not nag: the escalation
    fires once for the lifetime of the store and never again.
    """
    home, remember, remote, slug_dir, project = _store(tmp_path)
    _git(remember, ["remote", "remove", "origin"])
    cfg = _backup_config(tmp_path, no_remote_notice_after=2)

    for i in range(3):
        (slug_dir / "now.md").write_text(f"memory {i}\n", encoding="utf-8")
        _run_backup(slug_dir, project, home, cfg)
        _wait_until(lambda: _log_or_empty(slug_dir).count("no remote configured") > i,
                    timeout=20)
        time.sleep(0.3)

    notice = _notice_path(slug_dir)
    assert _wait_until(lambda: notice.exists(), timeout=15), (
        "a store with no remote backed up nowhere and never said so once — "
        f"log was:\n{_log_text(slug_dir)}"
    )
    body = notice.read_text(encoding="utf-8")
    assert "remote" in body.lower()

    # And then it stops. A channel that fires on every save is a channel nobody
    # reads, which would cost #253's escalation its meaning.
    notice.unlink()
    for i in range(3):
        (slug_dir / "now.md").write_text(f"more {i}\n", encoding="utf-8")
        _run_backup(slug_dir, project, home, cfg)
        time.sleep(0.5)
    assert not notice.exists(), (
        "the no-remote notice fired again — a deliberately local-only store "
        "would be nagged on every save"
    )


# ═════════════════════════════════════════════════════════════════════════════
# #258 — the cooldown marker
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("corrupt", ["garbage-text", "12x34", "17\x00", "  1785512249  "])
def test_a_corrupt_cooldown_marker_does_not_kill_the_hook(tmp_path, corrupt):
    """#258 item 1, and its direction is wrong in the issue.

    `LAST_MOD` is unvalidated file content expanded inside `$(( ))` under
    `set -u`. A non-numeric marker is not "the cooldown is skipped": bash
    evaluates it as an *arithmetic expression*, hits an unbound variable or an
    invalid base, and the hook DIES at that line — above the lock, above the
    add, above the commit. The marker is only rewritten after a successful
    commit, which is now unreachable, so one bad byte stops the backup
    permanently and the only trace was dispatch's nameless `hook failed` —
    nameless until #277 gave it the exit status and bash's own diagnostic.
    """
    home, remember, remote, slug_dir, project = _store(tmp_path)
    (remember / ".last-git-backup-ts").write_text(corrupt, encoding="utf-8", errors="ignore")

    proc = _run_backup(slug_dir, project, home, _backup_config(tmp_path))

    assert proc.returncode == 0, (
        f"the hook died on a corrupt cooldown marker (rc={proc.returncode}): "
        f"{proc.stderr.strip()}"
    )
    assert _wait_until(lambda: _slug_is_committed(remember)), (
        "a corrupt cooldown marker stopped the backup entirely — and because "
        "the marker is rewritten only on a successful commit, it stays corrupt "
        "and the store never backs up again"
    )


def test_the_cooldown_marker_is_written_even_when_the_commit_fails(tmp_path):
    """#258 item 2. The two defects compound: a store whose commits fail never
    engages the throttle, so it does the full add/rm/commit on every single
    save — the opposite of what a cooldown is for, while backing up nothing."""
    home, remember, remote, slug_dir, project = _store(tmp_path)
    _break_commits(remember)

    _run_backup(slug_dir, project, home, _backup_config(tmp_path))
    _wait_until(lambda: "commit FAILED" in _log_or_empty(slug_dir), timeout=20)

    stamps = list(remember.glob(".last-git-backup-ts")) + \
        list((remember / ".git").rglob("*/last-git-backup-ts"))
    assert stamps, (
        "no cooldown marker was written after a failed commit — the throttle "
        "never engages on exactly the store that is doing the most useless work"
    )
    assert stamps[0].read_text(encoding="utf-8").strip().isdigit()


@pytest.mark.skipif(not FLOCK_AVAILABLE, reason="flock(1) not available")
def test_the_flock_path_does_not_unlink_its_own_lock_file(tmp_path):
    """#258 item 3, re-derived rather than taken on trust.

    flock's ownership is fd-based. Unlinking the path while holding it means a
    second instance can open the file, a third can create a fresh inode at the
    same path, and the two then flock *different inodes* — two concurrent
    backups on one store. The restore half already gets this right: `_take_lock`
    installs the `rm` trap only on the noclobber branch.
    """
    home, remember, remote, slug_dir, project = _store(tmp_path)

    _run_backup(slug_dir, project, home, _backup_config(tmp_path))
    assert _wait_until(lambda: _slug_is_committed(remember))
    time.sleep(0.5)

    locks = list(remember.glob(".git-backup.lock")) + \
        list((remember / ".git").rglob("*/git-backup.lock"))
    assert locks, (
        "the flock path unlinked its own lock file on exit — a concurrent "
        "instance can hold a lock on the unlinked inode while another creates "
        "a fresh one at the same path"
    )


# ═════════════════════════════════════════════════════════════════════════════
# #261 — state files at the store root
# ═════════════════════════════════════════════════════════════════════════════


def test_no_hook_state_is_left_at_the_store_root(tmp_path):
    """`git merge --ff-only` refuses when an untracked file would be
    overwritten, so the day a remote carries any of these names the restore
    stops with a message that reads like a dirty worktree. The mechanism the
    feature depends on is the mechanism that breaks."""
    home, remember, remote, slug_dir, project = _store(tmp_path)

    _run_backup(slug_dir, project, home, _backup_config(tmp_path))
    assert _wait_until(lambda: _slug_is_committed(remember))
    _run_restore(slug_dir, project, home, _restore_config(tmp_path))
    time.sleep(1.0)

    left = [n for n in ROOT_STATE_NAMES if (remember / n).exists()]
    assert not left, f"hook state left at the store root: {left}"

    status = subprocess.run(
        ["git", "-C", str(remember), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    noise = [l for l in status.splitlines()
             if ".git-backup" in l or ".git-restore" in l or "last-git-backup" in l]
    assert not noise, (
        "the plugin's own files show as untracked in the user's backup repo, "
        f"which trains them to ignore git status:\n{status}"
    )


def test_an_existing_root_remote_state_file_is_not_forgotten(tmp_path):
    """The compatibility question, asked where it actually bites.

    `.git-backup-remote` is a security control: it records the URL trusted on
    the first push and aborts if it later changes. Relocating the state without
    carrying that file forward would re-record whatever URL is configured now —
    silently forgiving exactly the change the check exists to catch.
    """
    home, remember, remote, slug_dir, project = _store(tmp_path)
    (remember / ".git-backup-remote").write_text(
        "git@example.invalid:someone-else/not-your-repo.git\n", encoding="utf-8")
    before = _remote_head(remote)

    _run_backup(slug_dir, project, home, _backup_config(tmp_path))
    _wait_until(lambda: "remote URL" in _log_or_empty(slug_dir), timeout=20)
    time.sleep(1.0)

    assert _remote_head(remote) == before, (
        "the push went through despite a recorded remote URL that does not "
        "match the configured one — the pre-existing state file was ignored, "
        f"so the security check was silently re-armed. Log was:\n{_log_text(slug_dir)}"
    )
    assert "remote URL changed" in _log_text(slug_dir)
