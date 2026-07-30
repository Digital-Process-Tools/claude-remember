"""Shared test environment guards.

Tests build sandboxes by pointing HOME at a temp directory. That is only half
a sandbox: CLAUDE_CONFIG_DIR relocates Claude Code's whole config tree, so a
developer who exports it (per-project accounts, direnv) had the real variable
follow the code straight out of the fixture and into their actual store.

It stayed invisible while the plugin hardcoded ~/.claude and simply ignored the
variable — the leak and the bug cancelled out. Honouring it (#166) exposed both
at once: five existing tests started reading the developer's real config tree.
Cleared here rather than in each fixture, so the next sandbox is hermetic
without anyone having to remember why.
"""

from __future__ import annotations

import os
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_claude_config_dir(monkeypatch):
    """Keep the ambient CLAUDE_CONFIG_DIR out of every test.

    A test that wants it set does so explicitly, which then means what it says
    rather than inheriting whatever the developer happens to run.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _isolate_spawn_guard_runtime_dir(monkeypatch, tmp_path):
    """Give every test its own summarizer spawn records (#204).

    `pipeline/spawn_guard.py` keeps them under HOME on purpose — a child that
    inherited no plugin environment has to find the same directory — but a test
    that does not relocate HOME would otherwise count against the developer's
    real records, and the rate cap would then fail tests depending on the order
    they ran in. A test that wants the HOME-derived path (the recursion test in
    test_spawn_containment.py does, because its child is given nothing else)
    clears this explicitly.
    """
    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "remember-run"))


# Git repo-selection variables. Any one of these, if exported in the shell
# that launches pytest, overrides `-C <dir>` and `cwd=<dir>` alike — that is
# documented git precedence, not a bug — so a test that builds its own repo
# under tmp_path and passes it explicitly via -C/cwd is STILL not isolated
# from an ambient GIT_DIR. This bit us for real (2026-07): running the suite
# from inside a git worktree, with a leaked GIT_DIR in the launching shell,
# flipped core.bare on the real repo, moved HEAD via stray commits, deleted
# a tracked file, and registered a pytest tmp dir as a live worktree of it.
# Every git subprocess call in tests/ already used -C or cwd= correctly; the
# leak happened anyway. The fix belongs here, once, for the whole suite —
# matching the same unset the plugin's own git-backup hook applies to itself
# (hooks.d/after_save/50-git-backup.sh: "Prevent outer git env vars from
# overriding git -C behaviour").
_GIT_REPO_SELECTION_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_NAMESPACE",
)


@pytest.fixture(autouse=True)
def _sanitize_ambient_git_env(monkeypatch):
    """Strip leaked GIT_* repo-selection vars before every test.

    A test that wants one of these set does so explicitly (as
    test_leaked_git_dir_does_not_disable_the_guard does, via its own env
    dict passed straight to subprocess.run) — never by inheriting whatever
    happened to be exported in the shell pytest was launched from.
    """
    for _var in _GIT_REPO_SELECTION_ENV_VARS:
        monkeypatch.delenv(_var, raising=False)


def _clean_git_env() -> dict:
    """A copy of the ambient environment with repo-selection vars stripped.

    Used only by the regression guard below, which must sanitize its own
    fingerprinting calls independently of the autouse fixture above (the
    guard's session-scoped snapshot runs outside any per-test monkeypatch).
    """
    return {k: v for k, v in os.environ.items() if k not in _GIT_REPO_SELECTION_ENV_VARS}


VERDICT_OK = "ok"
VERDICT_CONCURRENT = "concurrent"
VERDICT_MUTATED = "mutated"
VERDICT_UNATTRIBUTABLE = "unattributable"


@dataclass(frozen=True)
class RepoFingerprint:
    """A cheap, sensitive snapshot of repo state.

    Kept structured rather than as one blob of text because two of these
    fields are worktree-local (`head`, `status`) and one is repo-wide
    (`worktrees`): a sibling worktree of the same clone moving its HEAD
    changes the repo-wide field without this worktree doing anything.
    Comparing blobs cannot tell those apart; comparing fields can (#219).
    """

    bare: str
    head: str
    status: str
    toplevel: str
    worktrees: dict  # real path -> "HEAD <sha> branch <ref>"

    def render(self) -> str:
        lines = [
            f"bare={self.bare}",
            f"head={self.head}",
            f"toplevel={self.toplevel}",
            f"status={self.status}",
        ]
        lines += [f"worktree {path} {state}" for path, state in sorted(self.worktrees.items())]
        return "\n".join(lines)


@dataclass(frozen=True)
class RepoChange:
    """The guard's verdict on a before/after pair.

    Three answers, not two — `ok`, a finding, and *cannot tell*. A checker
    that cannot attribute a change must say so rather than pick the
    explanation that happens to be most alarming.
    """

    verdict: str
    detail: str
    message: str

    @property
    def should_fail(self) -> bool:
        return self.verdict not in (VERDICT_OK, VERDICT_CONCURRENT)


def _parse_worktree_list(porcelain: str) -> dict:
    """Map each linked worktree's real path to its HEAD/branch state."""
    worktrees: dict = {}
    path = None
    state: list = []
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            if path is not None:
                worktrees[path] = " ".join(state)
            path = os.path.realpath(line[len("worktree "):])
            state = []
        elif line.strip():
            state.append(line.strip())
    if path is not None:
        worktrees[path] = " ".join(state)
    return worktrees


def _repo_fingerprint(repo_root: Path) -> RepoFingerprint:
    """Snapshot bareness, HEAD, worktrees and working-tree status.

    Any test-suite escape that mutates the repo pytest is running from will
    change at least one of these.
    """
    env = _clean_git_env()

    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            env=env, capture_output=True, text=True,
        )
        return result.stdout.strip()

    toplevel = _git("rev-parse", "--show-toplevel")
    return RepoFingerprint(
        bare=_git("config", "--get", "core.bare"),
        head=_git("rev-parse", "HEAD"),
        status=_git("status", "--porcelain"),
        toplevel=os.path.realpath(toplevel) if toplevel else "",
        worktrees=_parse_worktree_list(_git("worktree", "list", "--porcelain")),
    )


_MUTATION_MESSAGE = (
    "Test suite mutated the repo it was launched from — this is the "
    "exact GIT_DIR-leak class of bug tracked by "
    "_sanitize_ambient_git_env. DO NOT ignore this."
)


def _classify_repo_change(before: RepoFingerprint, after: RepoFingerprint) -> RepoChange:
    """Attribute a change in the clone to this worktree, a sibling, or neither.

    - Bareness, this worktree's HEAD/toplevel/working-tree status, or this
      worktree's own entry in `git worktree list` moving is the thing the
      guard exists to catch: this suite mutated the repo it runs from.
    - A change confined to *other* worktrees' HEADs is another agent working
      in a sibling worktree of the same clone — normal here, and not this
      suite's doing.
    - The worktree set itself gaining or losing an entry could be either an
      escaped test registering a tmp dir as a live worktree (the 2026-07
      incident did exactly that) or a human running `git worktree add`. The
      guard cannot tell, so it says that, and still fails.
    """
    local_fields = [
        name for name in ("bare", "head", "status", "toplevel")
        if getattr(before, name) != getattr(after, name)
    ]
    self_path = before.toplevel
    self_entry_moved = (
        self_path
        and self_path in before.worktrees
        and self_path in after.worktrees
        and before.worktrees[self_path] != after.worktrees[self_path]
    )
    if self_entry_moved:
        local_fields.append(f"worktree entry for {self_path}")

    if local_fields:
        detail = "changed: " + ", ".join(local_fields)
        return RepoChange(
            verdict=VERDICT_MUTATED,
            detail=detail,
            message=(
                f"{_MUTATION_MESSAGE}\n{detail}\n"
                f"--- before ---\n{before.render()}\n--- after ---\n{after.render()}"
            ),
        )

    appeared = sorted(set(after.worktrees) - set(before.worktrees))
    disappeared = sorted(set(before.worktrees) - set(after.worktrees))
    if appeared or disappeared:
        detail = (
            "the clone's worktree set changed during this run and the guard "
            "cannot attribute it: it is either an escaped test registering a "
            "worktree of the launching repo, or someone running `git worktree "
            "add`/`remove` on the same clone.\n"
            f"appeared: {appeared or 'none'}\ndisappeared: {disappeared or 'none'}"
        )
        return RepoChange(
            verdict=VERDICT_UNATTRIBUTABLE,
            detail=detail,
            message=(
                f"{detail}\n"
                f"--- before ---\n{before.render()}\n--- after ---\n{after.render()}"
            ),
        )

    moved_siblings = sorted(
        path for path, state in after.worktrees.items()
        if before.worktrees.get(path) != state
    )
    if moved_siblings:
        detail = (
            "another worktree of this clone moved while the suite ran — "
            "concurrent activity, not a mutation of the launching worktree: "
            + ", ".join(moved_siblings)
        )
        return RepoChange(verdict=VERDICT_CONCURRENT, detail=detail, message=detail)

    return RepoChange(verdict=VERDICT_OK, detail="", message="")


@pytest.fixture(scope="session", autouse=True)
def _guard_launching_repo_is_not_mutated():
    """Fail the run if the suite mutates the repo pytest was launched from.

    Session-scoped so it brackets the whole run: snapshot before any test,
    compare after the last one. This is the regression guard for the exact
    incident described above — if a future test (or a reverted fix)
    reopens the leak, this fails loudly instead of silently corrupting
    whatever repo the developer happened to be sitting in.

    Sibling worktrees of the same clone are the normal working mode here, so
    the comparison is classified rather than taken as a blob: a sibling
    moving its HEAD is reported as concurrent activity and does not fail the
    run, and a change the guard cannot attribute is reported as exactly that
    instead of being dressed up as a GIT_DIR leak (#219).
    """
    repo_root = Path(__file__).resolve().parent.parent
    before = _repo_fingerprint(repo_root)
    yield
    after = _repo_fingerprint(repo_root)
    change = _classify_repo_change(before, after)
    if change.verdict == VERDICT_CONCURRENT:
        warnings.warn(f"repo-mutation guard: {change.detail}", stacklevel=1)
        return
    assert not change.should_fail, change.message
