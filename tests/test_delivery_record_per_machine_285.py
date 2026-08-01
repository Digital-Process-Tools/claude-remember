"""The handoff delivery record is per-machine state and must not travel (#285).

`remember.delivered` records that THIS clone has already injected the current
handoff into a session, so a note that is pending replacement is labelled
instead of re-read as news. The fingerprint in it is a `cksum` of the handoff
CONTENT, so it matches on every machine that has the same handoff — which is
what made sharing the record actively harmful rather than merely noisy:

  * machine A delivers the handoff eight times and records that;
  * the backup commits the record and pushes it;
  * machine B pulls, opens its FIRST session with that handoff, and is told
    *already delivered 8 times since <machine A's clock> — this is pending
    replacement, not news. You may already have acted on it.*

The record exists to stop a stale handoff reading as fresh. Shared, it makes a
fresh handoff read as stale — the same silent lie, inverted, and in the
direction that suppresses action rather than duplicating it.

Two of its three fields are per-clone by construction (`first_delivered` is one
machine's wall clock, `deliveries` is one machine's session count); only
`fingerprint` is globally meaningful, and it is the field that carries the harm.

So the record is local, and the tests below assert the BEHAVIOUR rather than
the absence of a path from a commit: a delivery recorded on one machine must
not make a first delivery on another report as already-seen — while a repeat
delivery on the SAME machine must still be labelled, because a fix that simply
deleted the feature would satisfy the first half on its own.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX git semantics — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
BACKUP_HOOK = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"

HANDOFF_TEXT = "## Handoff\nFinish the delivery-record audit.\n"
ALREADY = "already delivered"


def _git(repo: Path, args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo)] + args, check=True, capture_output=True, text=True
    )


def _env(slug_dir: Path, project: Path, home: Path) -> dict:
    """The environment save-session.sh / Claude Code would provide.

    `_LIB_MEMORY_DIR_LOADED=1` is what lets a test name REMEMBER_DIR outright
    instead of reproducing external-storage resolution; the git-backup suite
    drives both hooks the same way.
    """
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


def _session_start(slug_dir: Path, project: Path, home: Path) -> str:
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        env=_env(slug_dir, project, home),
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, (
        "session-start-hook.sh is documented EXIT CODES: 0 Always. "
        f"stderr={result.stderr[:400]}"
    )
    return result.stdout


def _backup(slug_dir: Path, project: Path, home: Path) -> None:
    """Run the backup hook and wait for the PUSH, not the commit.

    The hook does its work in a detached subshell and returns immediately, and
    the commit lands well before the push does. Waiting on the local log is a
    race that passes on a fast machine and fails on a loaded one; the remote
    ref is the observable that actually means "the other machine can see this".
    """
    import time

    repo = slug_dir.parent
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
        "about what the other machine sees.\nlocal log:\n"
        + _git(repo, ["log", "--oneline"]).stdout
    )


def _make_machine(root: Path, name: str, remote: Path, slug: str):
    """A machine: its own clone of the shared backup repo, plus a project dir."""
    home = root / name
    home.mkdir(parents=True)
    store = home / ".remember"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(store)], check=True, capture_output=True
    )
    _git(store, ["config", "user.email", "test@test"])
    _git(store, ["config", "user.name", "Test"])
    slug_dir = store / slug
    (slug_dir / "tmp").mkdir(parents=True, exist_ok=True)
    (slug_dir / "logs").mkdir(parents=True, exist_ok=True)
    project = root / f"{name}-project"
    project.mkdir()
    return home, store, slug_dir, project


@pytest.fixture
def two_machines(tmp_path):
    """One shared backup remote, two clones of it, one shared project slug.

    This is the reported topology: `data_dir` with `{slug}`, a private backup
    repo, two machines writing the same store.
    """
    remote = tmp_path / "backup.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    _git_init = subprocess.run(
        ["git", "init", "-q", "-b", "main", str(seed)], check=True, capture_output=True
    )
    _git(seed, ["config", "user.email", "test@test"])
    _git(seed, ["config", "user.name", "Test"])
    (seed / "README").write_text("backup\n")
    _git(seed, ["add", "-A"])
    _git(seed, ["commit", "-qm", "init"])
    _git(seed, ["remote", "add", "origin", str(remote)])
    _git(seed, ["push", "-q", "-u", "origin", "main"])

    slug = "c--projects-shared"
    a = _make_machine(tmp_path, "machine-a", remote, slug)
    b = _make_machine(tmp_path, "machine-b", remote, slug)
    return a, b


class TestDeliveryRecordIsPerMachine:

    def test_first_delivery_on_second_machine_does_not_read_as_already_seen(
        self, two_machines
    ):
        """The reported bug, end to end and in the terms the reader sees.

        Machine A delivers the handoff twice and backs the store up. Machine B
        pulls that backup and opens its first session with the same handoff.
        The handoff must arrive, and it must arrive as NEWS.
        """
        (_, a_store, a_slug, a_project) = two_machines[0]
        (_, b_store, b_slug, b_project) = two_machines[1]
        home_a = a_store.parent
        home_b = b_store.parent

        (a_slug / "remember.md").write_text(HANDOFF_TEXT)

        first = _session_start(a_slug, a_project, home_a)
        assert ALREADY not in first, "machine A's own first delivery is news"
        second = _session_start(a_slug, a_project, home_a)
        assert ALREADY in second, (
            "a repeat delivery on the SAME machine must still be labelled — "
            "otherwise this suite would pass with the feature deleted"
        )

        _backup(a_slug, a_project, home_a)
        _git(b_store, ["pull", "-q", "--ff-only", "origin", "main"])

        # Machine B has the same handoff content — the fingerprint matches by
        # construction, which is exactly why sharing the record misleads.
        assert (b_slug / "remember.md").read_text() == HANDOFF_TEXT, (
            "the handoff itself is memory and MUST travel; only the record of "
            "having delivered it is local"
        )

        out = _session_start(b_slug, b_project, home_b)

        assert "delivery-record audit" in out, "the handoff must still be delivered"
        assert ALREADY not in out, (
            "machine B has never delivered this handoff. Being told it was "
            "already delivered N times since another machine's clock is the "
            "inversion #285 reports: the record that exists to stop a stale "
            "note reading as fresh instead makes a fresh note read as stale.\n"
            f"stdout was:\n{out[:1200]}"
        )

    def test_backup_does_not_carry_the_delivery_record(self, two_machines):
        """The mechanism behind the behaviour above: nothing git tracks in the
        store may hold this clone's delivery state."""
        (_, a_store, a_slug, a_project) = two_machines[0]
        home_a = a_store.parent

        (a_slug / "remember.md").write_text(HANDOFF_TEXT)
        _session_start(a_slug, a_project, home_a)
        _backup(a_slug, a_project, home_a)

        tracked = _git(a_store, ["ls-files"]).stdout.splitlines()
        offenders = [p for p in tracked if "delivered" in p]
        assert offenders == [], (
            f"delivery state is tracked by the backup: {offenders}. An ignore "
            "rule alone does nothing to a tracked file — it has to not be there."
        )
        assert any(p.endswith("remember.md") for p in tracked), (
            "the handoff itself must still be backed up — this test must fail "
            "for the right reason, not because nothing is committed at all"
        )

    def test_a_store_that_already_committed_the_record_is_cleaned_up(
        self, two_machines
    ):
        """Every store that has run the backup already has this file committed.

        Leaving it tracked is not harmless: nothing writes it any more, so it
        sits in the history describing a machine that may no longer exist, and
        the local copy stays permanently modified against it — noise in the
        user's own `git status`, and a name a later `merge --ff-only` can
        refuse over.
        """
        (_, a_store, a_slug, a_project) = two_machines[0]
        home_a = a_store.parent

        # A store as it exists today: the record committed at the store path.
        legacy = a_slug / "remember.delivered"
        legacy.write_text("fingerprint=2325240425-2162\nfirst_delivered=2026-07-31 23:40\ndeliveries=7\n")
        (a_slug / "remember.md").write_text(HANDOFF_TEXT)
        _git(a_store, ["add", "-A"])
        _git(a_store, ["commit", "-qm", "legacy state"])
        assert "remember.delivered" in _git(a_store, ["ls-files"]).stdout

        _session_start(a_slug, a_project, home_a)
        _backup(a_slug, a_project, home_a)

        tracked = _git(a_store, ["ls-files"]).stdout
        assert "remember.delivered" not in tracked, (
            "the legacy tracked copy must be removed from the backup, not left "
            f"behind as a stale file nothing maintains. tracked:\n{tracked}"
        )
        status = _git(a_store, ["status", "--porcelain"]).stdout
        assert "remember.delivered" not in status, (
            "and the store must come to rest clean — a permanently dirty "
            f"working tree is the noise this fix exists to remove. status:\n{status}"
        )
