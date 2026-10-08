"""#974 -- the #719 untrack receipt lost its rotate-the-credential advice.

Commit 73c77f8 (#918) reworded `docs/git-backup-security.md` SS4's stale
`haiku.oauth_token` wording, and in the same commit the hook's own untrack
receipt (`hooks.d/after_save/50-git-backup.sh`, logged when a legacy
`$SLUG/config.json` is untracked) lost its "rotate that credential" sentence.
The doc still told a reader to rotate; the one place a user actually learns
their config.json was committed to history did not.

Paired with a positive control (an ordinary backup, no legacy config.json)
so a harness that always logs the rotate sentence -- or logs nothing at all
either way -- could not pass both (CLAUDE.md: a negative assertion needs a
positive control).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_backup_hook import (
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


def _memory_log_text(remember_slug_dir: Path) -> str:
    log_files = list((remember_slug_dir / "logs").glob("memory-*.log"))
    assert log_files, "no memory-*.log written at all"
    return "\n".join(f.read_text() for f in log_files)


class TestUntrackReceiptCarriesRotateAdvice:

    def test_legacy_config_json_untrack_logs_rotate_advice(self, tmp_path):
        """Untracking a legacy config.json must tell the user to rotate the
        credential it may have carried -- the same advice
        docs/git-backup-security.md SS4 gives, now missing from the one
        receipt a user actually sees."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "legacy-slug-974"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")
        (slug_dir / "config.json").write_text(
            '{"haiku": {"oauth_token": "sk-ant-oat-OLD-SECRET"}}'
        )
        # Simulate a pre-#719 backup: commit config.json alongside memory directly.
        _git(remember, ["add", "--", f"{slug}/"])
        _git(remember, ["commit", "-q", "-m", "auto: legacy commit with config.json"])

        # Write new memory so the next backup has something to commit.
        (slug_dir / "now.md").write_text("## 11:00 | test\nMore memory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")

        log_text = _memory_log_text(slug_dir)
        assert "untracked" in log_text and "config.json" in log_text, log_text
        assert "rotate" in log_text.lower(), (
            "untrack receipt must advise rotating a credential config.json "
            f"may have carried: {log_text}"
        )

    def test_ordinary_backup_does_not_log_rotate_advice(self, tmp_path):
        """Positive control: an ordinary backup with no legacy config.json
        must NOT trip the rotate-advice sentence -- pairing the "must fire"
        case above with a "must not fire" one, since a harness that always
        logs the sentence would otherwise pass the assertion above too."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "ordinary-slug-974"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nSome memory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")

        log_text = _memory_log_text(slug_dir)
        assert "rotate" not in log_text.lower(), log_text
