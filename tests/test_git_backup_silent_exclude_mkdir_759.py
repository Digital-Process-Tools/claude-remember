"""#759: a failed `mkdir -p "$BACKUP_COMMON_DIR/info"` silently skipped the
config.json exclusion rule -- filed while curating `trap.d/719.silent-exclude
-mkdir-failure.md`, an auditor finding from the #719/#723 self-review that was
argued down to non-blocking and logged rather than fixed at the time.

Every OTHER failure path in this hook (commit failure, push rejection, untrack
failure) logs a WARNING/ERROR when it can't do its job (tests/test_git_backup
_silent_stops_257.py pins that class). This one didn't: when `mkdir -p` fails
(read-only filesystem, permission issue, `BACKUP_COMMON_DIR` empty), the
`config.json` exclusion in `$GIT_COMMON_DIR/info/exclude` was silently never
written, and the subsequent `git add -- "$SLUG/"` could then stage
config.json -- which can carry a live `haiku.oauth_token` -- exactly as
before the #719 fix. The caller saw the ordinary "committed $SLUG" success
line either way, indistinguishable from the exclusion having worked.

Paired with a positive control (an ordinary backup, no mkdir failure) so a
harness that always writes the WARNING -- or one that writes nothing at all
either way -- could not pass both (CLAUDE.md: a negative assertion needs a
positive control).
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_backup_hook import (
    _commit_log,
    _make_config,
    _run_hook,
    make_external_remember_repo,
    wait_for_lock_release,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics - not portable to Windows runners (#79)",
)


class TestSilentExcludeMkdirFailureIsLogged:

    def test_mkdir_failure_on_info_dir_logs_a_warning(self, tmp_path):
        """Pre-create a REGULAR FILE at "$GIT_COMMON_DIR/info" so `mkdir -p`
        fails with ENOTDIR -- the same failure shape a read-only filesystem
        or a permissions error produces, constructible without root. The
        exclude rule must not silently vanish: a WARNING naming the failure
        must land in hook-errors.log."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        common_dir = remember / ".git"
        # "git init" already creates ".git/info" as a directory -- replace
        # it with a REGULAR FILE so `mkdir -p "$common_dir/info"` fails with
        # ENOTDIR, the same failure shape a read-only filesystem or a
        # permissions error produces, constructible without root.
        shutil.rmtree(common_dir / "info")
        (common_dir / "info").write_text("not a directory")

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

        hook_errors = slug_dir / "logs" / "hook-errors.log"
        assert hook_errors.exists(), "no hook-errors.log written at all"
        log_text = hook_errors.read_text()
        assert "git-backup" in log_text, log_text
        assert "info" in log_text, log_text
        assert "config.json" in log_text, log_text

    def test_ordinary_backup_does_not_log_the_mkdir_warning(self, tmp_path):
        """Positive control: an ordinary backup with a healthy git common
        dir must NOT trip the mkdir-failure warning -- pairing the "must
        fire" case above with a "must not fire" one, since a harness that
        always writes the WARNING (or one whose hook-errors.log write is
        itself broken) would otherwise pass the assertions above too."""
        home, remember, _ = make_external_remember_repo(tmp_path)
        slug = "test-project-slug-2"
        slug_dir = remember / slug
        slug_dir.mkdir()
        (slug_dir / "now.md").write_text("## 10:00 | test\nSome memory.\n")

        project = tmp_path / "project"
        project.mkdir()
        cfg = _make_config(tmp_path, cooldown=0)

        result = _run_hook(slug_dir, project, home, config_path=cfg)
        assert result.returncode == 0

        wait_for_lock_release(remember / ".git-backup.lock")

        # Positive control that the backup actually ran.
        commits = _commit_log(remember)
        assert len(commits) == 2  # init + auto commit

        exclude_file = remember / ".git" / "info" / "exclude"
        assert exclude_file.exists()
        assert f"/{slug}/config.json" in exclude_file.read_text()

        hook_errors = slug_dir / "logs" / "hook-errors.log"
        assert not hook_errors.exists() or "info" not in hook_errors.read_text(), (
            hook_errors.read_text() if hook_errors.exists() else ""
        )
