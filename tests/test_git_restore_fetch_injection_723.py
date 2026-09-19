"""#723 -- git-restore's detached fetch lacks a `--` separator, and
`git_restore.remote`/`git_restore.branch` (fast-forwarded from the very
remote they name) reach `git fetch`'s argv unvalidated.

`config.json` is git-tracked and is exactly what this file's own
`git merge --ff-only` fast-forwards FROM the remote, so a party with push
access to the store -- or a second machine sharing it -- controls
`git_restore.remote`/`git_restore.branch`. A `-`-leading value is parsed as
an OPTION rather than an operand (`--upload-pack=...` against a
local-transport target is local command execution); a value containing `:`
or `/` is a transport URL/spec rather than a name naming a remote this repo
already trusts.

These tests assert the OBSERVABLE: the hook logs a WARNING and falls back to
a safe value (`origin` for the remote, no branch operand for the branch)
rather than passing the poisoned value through -- paired with a positive
control (a legitimate remote/branch still produces a working fetch) so a
restore hook that silently stopped fetching altogether could not pass either
case.
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics - not portable to Windows runners (#79)",
)

from .test_git_backup_push_rejected_253 import _log_text
from .test_git_restore_hook_253 import (
    _config,
    _flock_env,
    _run,
    _store,
    _wait_for_fetch,
)


class TestFetchRejectsInjectedRemote:

    def test_dash_leading_remote_is_rejected_and_falls_back_to_origin(self, tmp_path):
        """A `-`-leading git_restore.remote never reaches git fetch as an
        operand -- the hook logs a WARNING and falls back to 'origin', which
        is a real, already-configured remote, so the fetch still succeeds."""
        home, remember, _remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path, enabled=True, remote="--upload-pack=touch /tmp/pwned-723")

        result = _run(slug_dir, project, home, cfg=cfg, **_flock_env(tmp_path))
        assert result.returncode == 0

        state = _wait_for_fetch(remember, timeout=60)
        assert state.get("rc") == "0", (
            f"fetch did not succeed against the origin fallback: {state}"
        )

        log_text = _log_text(slug_dir)
        assert "is not a plain remote name" in log_text
        assert "--upload-pack=touch /tmp/pwned-723" in log_text
        assert "falling back to 'origin'" in log_text

    def test_dash_leading_branch_is_rejected_and_cleared(self, tmp_path):
        """A `-`-leading git_restore.branch never reaches git fetch as an
        operand -- the hook logs a WARNING and clears it, falling back to no
        branch operand at all (git fetch then follows the remote's HEAD)."""
        home, remember, _remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path, enabled=True, branch="--upload-pack=touch /tmp/pwned-723-branch")

        result = _run(slug_dir, project, home, cfg=cfg, **_flock_env(tmp_path))
        assert result.returncode == 0

        state = _wait_for_fetch(remember, timeout=60)
        assert state.get("rc") == "0", (
            f"fetch did not succeed once the poisoned branch was cleared: {state}"
        )

        log_text = _log_text(slug_dir)
        assert "starts with '-'" in log_text
        assert "--upload-pack=touch /tmp/pwned-723-branch" in log_text

    def test_colon_branch_refspec_is_rejected(self, tmp_path):
        """A colon makes a branch value a src:dst REFSPEC rather than a plain
        branch name -- `--` does not neutralize this, only `-`-leading
        options, so it needs its own check alongside the leading-dash one."""
        home, remember, _remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path, enabled=True, branch="main:refs/heads/some-other-branch")

        result = _run(slug_dir, project, home, cfg=cfg, **_flock_env(tmp_path))
        assert result.returncode == 0

        state = _wait_for_fetch(remember, timeout=60)
        assert state.get("rc") == "0", (
            f"fetch did not succeed once the poisoned refspec was cleared: {state}"
        )

        log_text = _log_text(slug_dir)
        assert "starts with '-' or contains ':'" in log_text

        # `git fetch -- origin "main:refs/heads/some-other-branch"` writes
        # directly to that LOCAL destination ref -- never to anything under
        # refs/remotes/origin/, which is what an ordinary fetch populates. The
        # ref this attack actually creates is refs/heads/some-other-branch, so
        # that is the one this assertion has to check for the test to be able
        # to fail at all (second-pass review caught the fetch-side test
        # checking refs/remotes/origin/... instead, which the attack never
        # touches, making it pass regardless of whether the guard held).
        other_ref = subprocess.run(
            ["git", "-C", str(remember), "rev-parse", "--verify", "--quiet",
             "refs/heads/some-other-branch"],
            capture_output=True, text=True, check=False,
        )
        assert other_ref.returncode != 0, (
            "the poisoned refspec fetched into an arbitrary local ref: "
            + other_ref.stdout
        )

    def test_legitimate_remote_and_branch_still_fetch(self, tmp_path):
        """Positive control: a normal, valid remote/branch configuration is
        untouched by the validation and still produces a working fetch."""
        home, remember, _remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path, enabled=True, remote="origin", branch="main")

        result = _run(slug_dir, project, home, cfg=cfg, **_flock_env(tmp_path))
        assert result.returncode == 0

        state = _wait_for_fetch(remember, timeout=60)
        assert state.get("rc") == "0", f"legitimate fetch failed: {state}"

        log_text = _log_text(slug_dir)
        assert "is not a plain remote name" not in log_text
        assert "starts with '-'" not in log_text
