"""#757: a legacy `.remember/config.json` a REPOSITORY committed must not be
carried, still trusted, across the one-shot external-storage migration in
scripts/bootstrap-dirs.sh.

Once `REMEMBER_DIR` is external, `lib-memory-dir.sh` (~293-300) trusts
`${REMEMBER_DIR}/config.json` unconditionally -- correct for a config an
OPERATOR wrote there, wrong for one a cloned repository shipped in the old
in-project `.remember/` and that the migration `mv` would otherwise carry
across unchanged. This module drives the two scripts together (bootstrap
then a "second session" merge) and checks the credential a real second
session would resolve, not just the file's location.

Every "must not migrate/leak" case is paired with a "must still
migrate/leak" positive control in the same fixture shape, per CLAUDE.md: a
negative assertion needs a positive control.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_migration import (
    _BASH,
    BOOTSTRAP_SCRIPT,
    DETECT_SCRIPT,
    _bash_path,
    _make_legacy_dir,
)

pytestmark = pytest.mark.skipif(_BASH is None, reason="Git Bash not found (Windows without Git for Windows)")


def _git(repo: Path, args: list) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_git(project: Path) -> None:
    _git(project, ["init", "-q"])
    _git(project, ["config", "user.email", "t@t"])
    _git(project, ["config", "user.name", "T"])


def _run_bootstrap_and_dump_merged_config(project_dir: str, pipeline_dir: str, home_dir: str):
    """Source detect-tools.sh + bootstrap-dirs.sh (which sources
    lib-memory-dir.sh itself) and cat the merged REMEMBER_CONFIG back
    INSIDE the same process -- it is a mktemp file removed by an EXIT trap
    the instant the subprocess ends, so it must be read before that.
    """
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project_dir)}"
    export PIPELINE_DIR="{_bash_path(pipeline_dir)}"
    export HOME="{_bash_path(home_dir)}"
    source "{_bash_path(DETECT_SCRIPT)}"
    source "{_bash_path(BOOTSTRAP_SCRIPT)}"
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    echo "---MERGED---"
    if [ -f "$REMEMBER_CONFIG" ]; then
        cat "$REMEMBER_CONFIG"
    fi
    """
    result = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
    dir_line, _, merged_json = result.stdout.partition("---MERGED---\n")
    remember_dir = dir_line.strip().split("REMEMBER_DIR=")[-1].strip()
    merged = json.loads(merged_json) if merged_json.strip() else {}
    return merged, remember_dir, result.stderr


class TestSecondSessionCredentialAfterMigration:
    """The claim #757 is actually about: after the first-session migration
    moves a tracked legacy config out of the way, a SECOND session's merged
    config (what pipeline/haiku.py's `_configured_oauth_token()` reads) must
    not resolve the repository's credential."""

    def test_git_tracked_legacy_haiku_oauth_token_does_not_survive_migration(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "repo-token-0000000000000000000"}})
        )
        _init_git(project)
        _git(project, ["add", ".remember/config.json"])
        _git(project, ["commit", "-q", "-m", "seed"])

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        # First session: migration happens.
        merged1, remember_dir, stderr1 = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )
        assert "757" in stderr1, "the skipped migration was not logged"

        # Second session: REMEMBER_DIR already exists, migration is a no-op,
        # this is the ordinary merge every later session performs.
        merged2, remember_dir2, _ = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )
        assert remember_dir2 == remember_dir

        for merged in (merged1, merged2):
            assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {}), (
                "the repository-committed haiku.oauth_token reached the merged config "
                "after migration into external storage"
            )

    def test_untracked_legacy_haiku_oauth_token_still_survives_migration(self, tmp_path):
        """Positive control: the SAME key, in a legacy config the user
        never committed, must still resolve after migration -- a fix that
        stopped trusting every migrated config, tracked or not, cannot
        pass."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "my-own-token-000000000000000"}})
        )
        _init_git(project)
        # Deliberately never `git add`ed -- the user's own untracked file.

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        # Session 1: performs the migration. lib-memory-dir.sh runs BEFORE
        # the `mv` in bootstrap-dirs.sh, so config.json is not yet at
        # REMEMBER_DIR when THIS session's own merge runs -- the file only
        # appears there once this call returns. Session 2 is the first one
        # that can see it, exactly like a real second session would.
        _run_bootstrap_and_dump_merged_config(str(project), str(pipeline), str(home))
        merged, _, _ = _run_bootstrap_and_dump_merged_config(str(project), str(pipeline), str(home))
        assert merged["haiku"]["oauth_token"] == "my-own-token-000000000000000"
