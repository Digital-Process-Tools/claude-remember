"""The launching-repo mutation guard must distinguish three states.

The guard in conftest.py exists because of a real incident (2026-07): a leaked
GIT_DIR let the suite mutate the repo pytest was launched from. Its fingerprint
includes `git worktree list`, which is repo-wide — so a *sibling* worktree of
the same clone committing during the run also changed the fingerprint, and the
guard reported it as "this suite mutated the repo ... GIT_DIR-leak ... DO NOT
ignore this" (#219).

These tests pin the contract: concurrent sibling activity is reported as such
and does not fail the run; a genuine mutation of the launching worktree still
fails loudly with the original accusation; and a change the guard cannot
attribute to either is reported honestly as unattributable rather than guessed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import (
    VERDICT_CONCURRENT,
    VERDICT_MUTATED,
    VERDICT_OK,
    VERDICT_UNATTRIBUTABLE,
    _classify_repo_change,
    _repo_fingerprint,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


@pytest.fixture
def clone(tmp_path):
    """A clone with a linked sibling worktree, mirroring the real setup."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main.parent, "init", "-q", "-b", "main", str(main))
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    _commit(main, "seed")

    sibling = tmp_path / "sibling"
    _git(main, "worktree", "add", "-q", "-b", "sibling", str(sibling))
    return main, sibling


class TestConcurrentSiblingIsNotAMutation:
    """The false positive from #219: another worktree moving mid-run."""

    def test_sibling_commit_during_the_run_is_reported_as_concurrent(self, clone):
        main, sibling = clone
        before = _repo_fingerprint(main)

        _commit(sibling, "work-by-another-agent")

        after = _repo_fingerprint(main)
        change = _classify_repo_change(before, after)

        assert change.verdict == VERDICT_CONCURRENT, (
            "a sibling worktree committing during the run is concurrent "
            f"activity, not a mutation of the launching repo: {change.detail}"
        )
        assert "sibling" in change.detail
        assert "GIT_DIR" not in change.detail, \
            "must not accuse the suite of the GIT_DIR-leak class of bug"

    def test_sibling_checkout_during_the_run_is_reported_as_concurrent(self, clone):
        main, sibling = clone
        before = _repo_fingerprint(main)

        _git(sibling, "checkout", "-q", "-b", "another-branch")

        after = _repo_fingerprint(main)
        assert _classify_repo_change(before, after).verdict == VERDICT_CONCURRENT

    def test_quiet_run_is_ok(self, clone):
        main, _sibling = clone
        before = _repo_fingerprint(main)
        after = _repo_fingerprint(main)
        change = _classify_repo_change(before, after)
        assert change.verdict == VERDICT_OK
        assert change.detail == ""


class TestRealMutationsStillFailLoudly:
    """The regression that matters most — the guard keeps its teeth."""

    def test_head_moved_in_the_launching_worktree_is_a_mutation(self, clone):
        main, _sibling = clone
        before = _repo_fingerprint(main)

        _commit(main, "stray-commit-from-an-escaped-test")

        after = _repo_fingerprint(main)
        change = _classify_repo_change(before, after)
        assert change.verdict == VERDICT_MUTATED, change.detail
        assert "head" in change.detail

    def test_bareness_flip_is_a_mutation(self, clone):
        main, _sibling = clone
        before = _repo_fingerprint(main)

        _git(main, "config", "core.bare", "true")

        after = _repo_fingerprint(main)
        change = _classify_repo_change(before, after)
        assert change.verdict == VERDICT_MUTATED, change.detail
        assert "bare" in change.detail

    def test_dirty_working_tree_is_a_mutation(self, clone):
        main, _sibling = clone
        before = _repo_fingerprint(main)

        (main / "dropped-by-a-test.txt").write_text("oops\n")

        after = _repo_fingerprint(main)
        change = _classify_repo_change(before, after)
        assert change.verdict == VERDICT_MUTATED, change.detail
        assert "status" in change.detail

    def test_deleting_a_tracked_file_is_a_mutation(self, clone):
        main, _sibling = clone
        before = _repo_fingerprint(main)

        (main / "seed").unlink()

        after = _repo_fingerprint(main)
        assert _classify_repo_change(before, after).verdict == VERDICT_MUTATED

    def test_mutation_wins_over_concurrent_when_both_happen(self, clone):
        main, sibling = clone
        before = _repo_fingerprint(main)

        _commit(sibling, "work-by-another-agent")
        (main / "dropped-by-a-test.txt").write_text("oops\n")

        after = _repo_fingerprint(main)
        assert _classify_repo_change(before, after).verdict == VERDICT_MUTATED


class TestUnattributableChangesAreSaidToBeUnattributable:
    """Third state: the guard reports what it cannot tell instead of guessing."""

    def test_new_worktree_is_unattributable_not_silently_passed(self, clone):
        main, _sibling = clone
        before = _repo_fingerprint(main)

        _git(main, "worktree", "add", "-q", "-b", "third", str(main.parent / "third"))

        after = _repo_fingerprint(main)
        change = _classify_repo_change(before, after)
        assert change.verdict == VERDICT_UNATTRIBUTABLE, change.detail
        assert "third" in change.detail

    def test_removed_worktree_is_unattributable(self, clone):
        main, sibling = clone
        before = _repo_fingerprint(main)

        _git(main, "worktree", "remove", "--force", str(sibling))

        after = _repo_fingerprint(main)
        assert _classify_repo_change(before, after).verdict == VERDICT_UNATTRIBUTABLE

    def test_unattributable_is_still_a_failing_state(self, clone):
        """A change the guard cannot explain must never be waved through."""
        main, _sibling = clone
        before = _repo_fingerprint(main)
        _git(main, "worktree", "add", "-q", "-b", "third", str(main.parent / "third"))
        after = _repo_fingerprint(main)

        change = _classify_repo_change(before, after)
        assert change.should_fail is True
        assert change.verdict == VERDICT_UNATTRIBUTABLE
        assert "cannot attribute" in change.detail.lower()


class TestTheFixtureItself:
    """End-to-end: the session fixture, not just the classifier it calls."""

    def _make_inner_suite(self, tmp_path, test_body: str) -> Path:
        repo = tmp_path / "inner"
        repo.mkdir()
        _git(repo.parent, "init", "-q", "-b", "main", str(repo))
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "t")
        _commit(repo, "seed")

        inner_tests = repo / "tests"
        inner_tests.mkdir()
        (inner_tests / "conftest.py").write_text(
            (Path(__file__).parent / "conftest.py").read_text()
        )
        (inner_tests / "test_inner.py").write_text(test_body)
        return repo

    def _run_inner(self, repo: Path):
        return subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
            cwd=repo, capture_output=True, text=True,
        )

    def test_an_escaped_test_that_commits_still_fails_the_run(self, tmp_path):
        repo = self._make_inner_suite(tmp_path, """
import subprocess
from pathlib import Path

def test_escapes_and_mutates_the_launching_repo():
    root = Path(__file__).resolve().parent.parent
    (root / "escaped.txt").write_text("x\\n")
    subprocess.run(["git", "-C", str(root), "add", "escaped.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "escaped"], check=True)
""")
        result = self._run_inner(repo)
        assert result.returncode != 0, "a real mutation must still fail the run"
        assert "GIT_DIR-leak" in result.stdout + result.stderr
        assert "DO NOT ignore this" in result.stdout + result.stderr

    def test_a_sibling_worktree_moving_does_not_fail_the_run(self, tmp_path):
        repo = self._make_inner_suite(tmp_path, """
import subprocess
from pathlib import Path

def test_only_a_sibling_worktree_moves():
    root = Path(__file__).resolve().parent.parent
    sibling = root.parent / "inner-sibling"
    (sibling / "work.txt").write_text("y\\n")
    subprocess.run(["git", "-C", str(sibling), "add", "work.txt"], check=True)
    subprocess.run(["git", "-C", str(sibling), "commit", "-q", "-m", "sibling"], check=True)
""")
        sibling = repo.parent / "inner-sibling"
        _git(repo, "worktree", "add", "-q", "-b", "sibling", str(sibling))

        result = self._run_inner(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "GIT_DIR-leak" not in result.stdout + result.stderr


class TestVerdictToFailurePolicy:
    def test_only_concurrent_and_ok_pass(self, clone):
        main, sibling = clone
        before = _repo_fingerprint(main)
        _commit(sibling, "work-by-another-agent")
        after = _repo_fingerprint(main)

        assert _classify_repo_change(before, after).should_fail is False
        assert _classify_repo_change(before, before).should_fail is False

    def test_mutation_message_keeps_the_original_accusation(self, clone):
        main, _sibling = clone
        before = _repo_fingerprint(main)
        _commit(main, "stray-commit-from-an-escaped-test")
        after = _repo_fingerprint(main)

        message = _classify_repo_change(before, after).message
        assert "GIT_DIR" in message
        assert "DO NOT" in message
