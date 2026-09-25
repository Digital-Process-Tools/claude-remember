"""#741: a cross-machine fast-forward can silently delete
`.remember/$SLUG/config.json` on a machine where the file is locally
unmodified, after another machine committed a `git rm --cached` of it
during a storage-mode upgrade (#719's untracking commit in
`hooks.d/after_save/50-git-backup.sh`).

The mechanism: `git rm --cached` only removes the path from the INDEX --
the machine that runs it keeps its own working-tree copy. Every OTHER
machine sharing the store never runs that removal itself; it only ever
fast-forwards onto the commit that recorded it
(`hooks.d/before_session_start/50-git-restore.sh`, `git merge --ff-only`).
A fast-forward that removes a path from the tracked tree deletes that
path from the working tree too, as long as the working copy is identical
to what was tracked before -- exactly the "locally unmodified" case this
issue names. There was no signal: the file simply stopped being there,
and haiku.oauth_token / per-project settings went with it.

This is a real two-clone reproduction (a real bare remote, a real second
clone doing the untracking, a real fast-forward on the first clone) --
not a unit test against a diff computed in Python -- because the defect
is specifically in what `git merge --ff-only` does to the *working tree*,
which no diff-only simulation can exercise.

Positive control included, per CLAUDE.md: a fast-forward that does NOT
touch config.json must leave it alone, byte for byte -- otherwise a
"config.json survived" assertion could pass merely because nothing ran.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .test_git_backup_hook import _git, make_external_remember_repo

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks.d" / "before_session_start" / "50-git-restore.sh"


def _config(tmp_path: Path, **git_restore) -> Path:
    cfg = tmp_path / "remember-config.json"
    body: dict = {}
    if git_restore:
        body["git_restore"] = git_restore
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def _run(slug_dir: Path, project: Path, home: Path, cfg: Path):
    env = {
        **os.environ,
        "HOME": str(home),
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(REPO_ROOT),
        "REMEMBER_DIR": str(slug_dir),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_PROJECT": str(project),
        "REMEMBER_CONFIG": str(cfg),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    return subprocess.run(["bash", str(HOOK)], env=env, capture_output=True,
                           text=True, timeout=120, check=False)


def _store_with_committed_config(tmp_path: Path):
    """A memory store with `$SLUG/config.json` already committed and pushed --
    the pre-#719 shape every existing store that upgrades from is in."""
    home, remember, remote = make_external_remember_repo(tmp_path)
    slug_dir = remember / "test-slug"
    slug_dir.mkdir()
    (slug_dir / "now.md").write_text("## 10:00 | test\nlocal memory\n", encoding="utf-8")
    config = slug_dir / "config.json"
    config.write_text(json.dumps({"haiku": {"oauth_token": "secret-token-abc"}}), encoding="utf-8")
    _git(remember, ["add", "-A"])
    _git(remember, ["commit", "-q", "-m", "local, with config.json tracked"])
    _git(remember, ["push", "-q", "origin", "main"])
    project = tmp_path / "project"
    project.mkdir()
    return home, remember, remote, slug_dir, project, config


def _other_machine_untracks_config(tmp_path: Path, remote: Path) -> str:
    """A second clone runs the #719 upgrade: `git rm --cached` on
    config.json (index-only -- its own working copy is untouched), commits,
    pushes. Returns the new remote tip."""
    other = tmp_path / "other-machine"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)],
                    check=True, capture_output=True)
    _git(other, ["config", "user.email", "other@test"])
    _git(other, ["config", "user.name", "Other"])
    _git(other, ["rm", "-r", "-q", "--cached", "--ignore-unmatch",
                 "--", "test-slug/logs/", "test-slug/tmp/", "test-slug/config.json"])
    _git(other, ["commit", "-q", "-m",
                 "auto: stop tracking test-slug/logs, test-slug/tmp and test-slug/config.json"])
    _git(other, ["push", "-q", "origin", "main"])
    result = subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True)
    tip = result.stdout.strip()
    # config.json must still exist on the OTHER machine's own disk -- `rm
    # --cached` never deletes it there. If the fixture itself deleted it,
    # every assertion below about the FIRST machine would be vacuous.
    assert (other / "test-slug" / "config.json").exists(), (
        "fixture: git rm --cached deleted the other machine's own working "
        "copy -- it should not have"
    )
    return tip


class TestFastForwardPreservesUntrackedConfig:
    """The defect and its fix."""

    def test_unmodified_config_survives_the_fast_forward(self, tmp_path):
        home, remember, remote, slug_dir, project, config = _store_with_committed_config(tmp_path)
        original_bytes = config.read_bytes()

        _other_machine_untracks_config(tmp_path, remote)
        _git(remember, ["fetch", "-q", "origin"])

        result = _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        assert result.returncode == 0, result.stderr
        assert config.exists(), (
            "config.json was deleted by the fast-forward that adopted another "
            "machine's git-rm-cached commit (#741) -- haiku.oauth_token and "
            "per-project settings are gone with no signal\n--- stderr ---\n"
            + result.stderr
        )
        assert config.read_bytes() == original_bytes, (
            "config.json survived but its content changed -- it must be "
            "restored byte-for-byte from the pre-merge commit, not "
            "regenerated"
        )

    def test_it_stays_untracked_after_being_restored(self, tmp_path):
        """The restore must not re-stage the file -- the whole point of the
        #719 upgrade this fast-forward is adopting is that config.json stops
        being pushed from now on."""
        home, remember, remote, slug_dir, project, _config_path = _store_with_committed_config(tmp_path)
        _other_machine_untracks_config(tmp_path, remote)
        _git(remember, ["fetch", "-q", "origin"])

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        tracked = subprocess.run(
            ["git", "-C", str(remember), "ls-files", "--", "test-slug/config.json"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert tracked == "", (
            "config.json was restored back into the INDEX -- it should stay "
            "on disk but untracked, matching the new HEAD, or the next "
            "backup re-commits it and undoes the #719 upgrade"
        )

    def test_it_is_reported_out_loud(self, tmp_path):
        home, remember, remote, slug_dir, project, _config_path = _store_with_committed_config(tmp_path)
        _other_machine_untracks_config(tmp_path, remote)
        _git(remember, ["fetch", "-q", "origin"])

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        log_files = list((remember / "test-slug" / "logs").glob("memory-*.log"))
        assert log_files, "hook wrote no log file at all"
        text = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
        assert "config.json" in text and "741" in text, (
            "the recovery from a config.json untracked out from under this "
            "machine was silent -- a machine that lost its haiku token would "
            "have no signal anything happened\n--- log ---\n" + text
        )

    def test_positive_control_a_plain_ff_leaves_config_untouched(self, tmp_path):
        """Must-fire pair for the assertions above: an ordinary fast-forward
        that does NOT touch config.json must not disturb it either -- proves
        the harness can detect a change if one occurred."""
        home, remember, remote, slug_dir, project, config = _store_with_committed_config(tmp_path)
        original_bytes = config.read_bytes()

        other = tmp_path / "other-machine-plain"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)],
                        check=True, capture_output=True)
        _git(other, ["config", "user.email", "other@test"])
        _git(other, ["config", "user.name", "Other"])
        (other / "test-slug" / "unrelated.md").write_text("elsewhere\n", encoding="utf-8")
        _git(other, ["add", "-A"])
        _git(other, ["commit", "-q", "-m", "unrelated change"])
        _git(other, ["push", "-q", "origin", "main"])
        _git(remember, ["fetch", "-q", "origin"])

        result = _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        assert result.returncode == 0, result.stderr
        assert config.exists() and config.read_bytes() == original_bytes, (
            "an unrelated fast-forward touched config.json -- it should be "
            "left completely alone"
        )
        assert (remember / "test-slug" / "unrelated.md").exists(), (
            "fixture: the fast-forward did not actually happen"
        )
