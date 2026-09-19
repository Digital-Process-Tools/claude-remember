"""#719 -- git-backup must never stage <slug>/config.json.

<REMEMBER_DIR>/config.json (i.e. <slug>/config.json under the store root) is
a documented home for haiku.oauth_token -- a live claude.ai OAuth credential
(docs/configuration.md). The backup hook staged the whole slug subtree with
`git add -- "$SLUG/"` and the only exclude rules maintained in
`$GIT_COMMON_DIR/info/exclude` were logs/ and tmp/ -- config.json was
committed and pushed right alongside memory, landing the credential in the
remote's history and in every clone of the backup store.

These tests assert the OBSERVABLE: what actually lands in the commit/remote
tree, paired with a positive control (ordinary memory files still make it
in) so a harness that silently staged NOTHING could not pass either case.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_backup_hook import (
    _commit_log,
    _files_in_commit,
    _git,
    _make_config,
    _run_hook,
    make_external_remember_repo,
    wait_for_lock_release,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics - not portable to Windows runners (#79)",
)


class TestConfigJsonNeverStaged:

    def test_config_json_with_oauth_token_never_staged(self, tmp_path):
        """A slug config.json carrying haiku.oauth_token must not be
        committed, while an ordinary memory file in the same slug still is
        (positive control)."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "test-project-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nSome memory.\n")
        (slug_dir / "config.json").write_text(
            "{\"haiku\": {\"oauth_token\": \"sk-ant-oat-SECRET-VALUE\"}}"
        )

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0

        wait_for_lock_release(remember / ".git-backup.lock")

        commits = _commit_log(remember)
        assert len(commits) == 2  # init + auto commit

        changed = _files_in_commit(remember)
        assert f"{slug}/config.json" not in changed, (
            f"config.json must never be staged/committed by git-backup: {changed}"
        )
        # Positive control: the commit is not simply empty/no-op.
        assert f"{slug}/now.md" in changed

        exclude_file = remember / ".git" / "info" / "exclude"
        assert exclude_file.exists()
        assert f"/{slug}/config.json" in exclude_file.read_text()

    def test_legacy_tracked_config_json_gets_untracked(self, tmp_path):
        """A store that committed config.json before this fix existed stops
        tracking it on the next backup -- without touching existing memory."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "legacy-slug"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")
        (slug_dir / "config.json").write_text(
            "{\"haiku\": {\"oauth_token\": \"sk-ant-oat-OLD-SECRET\"}}"
        )
        # Simulate a pre-#719 backup: commit config.json alongside memory directly.
        _git(remember, ["add", "--", f"{slug}/"])
        _git(remember, ["commit", "-q", "-m", "auto: legacy commit with config.json"])
        tracked_before = subprocess.run(
            ["git", "-C", str(remember), "ls-files", "--", f"{slug}/"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert f"{slug}/config.json" in tracked_before

        # Write new memory so the next backup has something to commit.
        (slug_dir / "now.md").write_text("## 11:00 | test\nMore memory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")

        tracked_now = subprocess.run(
            ["git", "-C", str(remember), "ls-files", "--", f"{slug}/"],
            capture_output=True, text=True, check=True,
        ).stdout
        # config.json must no longer be tracked going forward.
        assert f"{slug}/config.json" not in tracked_now
        # now.md is still tracked -- the untracking commit did not sweep memory away.
        assert f"{slug}/now.md" in tracked_now
