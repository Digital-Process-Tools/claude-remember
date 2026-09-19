"""#723 -- git-backup's push call has the identical unvalidated-argv shape
as the git-restore fetch call it mirrors: `git_backup.remote`/
`git_backup.branch` are read from `config.json` (git-tracked, and
attacker-controlled on a shared store or by anyone with push access) and
reached `git push`'s argv as leading positional operands with no `--`
separator and no validation.

These tests assert the OBSERVABLE: an injected remote/branch value never
reaches `git push` unvalidated -- the hook logs a WARNING and falls back to
the branch's own push target (a bare `git push`) rather than passing the
poisoned value through -- paired with a positive control (a legitimate
remote/branch still produces a working push) so a backup hook that silently
stopped pushing altogether could not pass either case.
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics - not portable to Windows runners (#79)",
)

from .test_git_backup_hook import make_external_remember_repo
from .test_git_backup_push_rejected_253 import _config, _log_text, _remote_head, _run


def _store_not_diverged(tmp_path):
    """A memory store with a bare remote that has NOT diverged, so a
    legitimate push (or a validated fallback) actually succeeds."""
    home, remember, remote = make_external_remember_repo(tmp_path)
    slug_dir = remember / "test-slug"
    slug_dir.mkdir()
    (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    return home, remember, remote, slug_dir, project


class TestPushRejectsInjectedRemote:

    def test_dash_leading_remote_is_rejected_and_falls_back(self, tmp_path):
        home, remember, remote, slug_dir, project = _store_not_diverged(tmp_path)
        cfg = _config(tmp_path, remote="--upload-pack=touch /tmp/pwned-723-push")
        before = _remote_head(remote)

        result = _run(slug_dir, project, home, remember, cfg)
        assert result.returncode == 0

        log = _log_text(slug_dir)
        assert "is not a plain remote name" in log
        assert "--upload-pack=touch /tmp/pwned-723-push" in log

        after = _remote_head(remote)
        assert after != before, (
            "the fallback push never reached the remote at all -- the hook "
            "did not merely reject the poisoned value, it stopped backing up"
        )

    def test_dash_leading_branch_is_rejected_and_falls_back(self, tmp_path):
        home, remember, remote, slug_dir, project = _store_not_diverged(tmp_path)
        cfg = _config(tmp_path, branch="--upload-pack=touch /tmp/pwned-723-push-branch")
        before = _remote_head(remote)

        result = _run(slug_dir, project, home, remember, cfg)
        assert result.returncode == 0

        log = _log_text(slug_dir)
        assert "starts with '-' or contains ':'" in log
        assert "--upload-pack=touch /tmp/pwned-723-push-branch" in log

        after = _remote_head(remote)
        assert after != before, "the fallback push never reached the remote"

    def test_colon_branch_refspec_is_rejected(self, tmp_path):
        """A colon makes a branch value a src:dst REFSPEC rather than a plain
        branch name -- `--` does not neutralize this, only `-`-leading options.

        Checks BOTH halves, not just the absence: the poisoned ref must never
        appear on the remote (negative), AND the fallback bare push must still
        have reached the remote at all (positive control) -- otherwise an
        unrelated total push failure (the exact silent-mkdir-failure shape
        logged in trap.d/719.silent-exclude-mkdir-failure.md, or any other
        breakage) would make "the poisoned ref does not exist" trivially true
        for the wrong reason (second-pass review's finding: this test did not
        originally check that the push itself still succeeded).
        """
        home, remember, remote, slug_dir, project = _store_not_diverged(tmp_path)
        cfg = _config(tmp_path, branch="main:refs/heads/some-other-branch")
        before = _remote_head(remote)

        result = _run(slug_dir, project, home, remember, cfg)
        assert result.returncode == 0

        log = _log_text(slug_dir)
        assert "starts with '-' or contains ':'" in log

        other_branch = subprocess.run(
            ["git", "-C", str(remote), "rev-parse", "--verify", "--quiet",
             "refs/heads/some-other-branch"],
            capture_output=True, text=True, check=False,
        )
        assert other_branch.returncode != 0, (
            "the poisoned refspec created/overwrote an arbitrary branch on "
            "the remote: " + other_branch.stdout
        )

        after = _remote_head(remote)
        assert after != before, (
            "the fallback push never reached the remote at all -- 'the "
            "poisoned ref does not exist' would then be trivially true for "
            "the wrong reason"
        )

    def test_legitimate_remote_and_branch_still_push(self, tmp_path):
        """Positive control: a normal, valid remote/branch configuration is
        untouched by the validation and still produces a working push."""
        home, remember, remote, slug_dir, project = _store_not_diverged(tmp_path)
        cfg = _config(tmp_path, remote="origin", branch="main")
        before = _remote_head(remote)

        result = _run(slug_dir, project, home, remember, cfg)
        assert result.returncode == 0

        log = _log_text(slug_dir)
        assert "is not a plain remote name" not in log
        assert "starts with '-' or contains ':'" not in log

        after = _remote_head(remote)
        assert after != before, "legitimate push failed to reach the remote"
