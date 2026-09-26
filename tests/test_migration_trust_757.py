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
import os
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


def _path_with_broken_git(tmp_path: Path) -> str:
    """A PATH where every OTHER real binary resolves normally but `git` is
    a shim that always exits 128 with an unrelated fatal error -- never
    "not a git repository", never a confirmed tracked-status answer -- so
    `_remember_config_tracked_status` cannot read it as `untracked` and
    must report `could-not-tell`."""
    fake_bin = tmp_path / "broken-git-bin"
    fake_bin.mkdir()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name == "git":
                continue
            target = fake_bin / name
            if target.exists() or target.is_symlink():
                continue
            try:
                os.symlink(os.path.join(d, name), target)
            except OSError:
                pass
    shim = fake_bin / "git"
    shim.write_text("#!/bin/sh\necho 'fatal: simulated unrelated git failure' >&2\nexit 128\n")
    shim.chmod(0o755)
    return str(fake_bin)


def _source_bootstrap_with_env(project_dir: str, pipeline_dir: str, home_dir: str,
                                path_override: str | None = None) -> subprocess.CompletedProcess:
    """Same as tests.test_migration._source_bootstrap, but lets a caller
    override PATH -- needed to hand bootstrap-dirs.sh a broken `git` shim
    without breaking `bash`/`mktemp`/etc themselves."""
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project_dir)}"
    export PIPELINE_DIR="{_bash_path(pipeline_dir)}"
    export HOME="{_bash_path(home_dir)}"
    source "{_bash_path(DETECT_SCRIPT)}"
    source "{_bash_path(BOOTSTRAP_SCRIPT)}"
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    """
    env = {**os.environ}
    if path_override is not None:
        env["PATH"] = path_override
    return subprocess.run([_BASH, "-c", script], capture_output=True, text=True, env=env, check=False)


class TestSubdirectoryOfARepoIsStillSeenAsTracked:
    """#754: `[ -e "$_mem_proj/.git" ]` (the pre-review check) only looks in
    the project dir itself -- a project STARTED FROM A SUBDIRECTORY of a
    normal (non-worktree) git repo has no `.git` there at all, even though
    the enclosing repository can still have committed
    `<subdir>/.remember/config.json`. `_remember_config_tracked_status`
    walks up via `git rev-parse --is-inside-work-tree`, which finds the
    enclosing repo correctly."""

    def test_a_tracked_config_two_levels_under_the_repo_root_is_still_left_behind(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        project = repo_root / "subdir"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "sub-token"}}))

        # The repo lives at repo_root, NOT at project (= repo_root/subdir):
        # `[ -e "project/.git" ]` would miss this entirely.
        _init_git(repo_root)
        _git(repo_root, ["add", "subdir/.remember/config.json"])
        _git(repo_root, ["commit", "-q", "-m", "seed"])

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        result = _source_bootstrap_with_env(str(project), str(pipeline), str(home))
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
        remember_dir = result.stdout.strip().split("REMEMBER_DIR=")[-1].strip()

        assert (legacy / "config.json").exists(), (
            "a config.json tracked TWO LEVELS UP (repo root, not the project "
            "dir itself) was migrated anyway -- the tracked check only saw "
            "the project directory's own .git"
        )
        assert not (Path(remember_dir) / "config.json").exists()

    def test_an_untracked_config_two_levels_under_the_repo_root_still_migrates(self, tmp_path):
        """Positive control: same enclosing-repo shape, file never added --
        must still migrate normally."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        project = repo_root / "subdir"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "sub-token"}}))

        _init_git(repo_root)
        # Deliberately never `git add`ed.

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        result = _source_bootstrap_with_env(str(project), str(pipeline), str(home))
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
        remember_dir = result.stdout.strip().split("REMEMBER_DIR=")[-1].strip()

        assert not (legacy / "config.json").exists()
        assert (Path(remember_dir) / "config.json").exists()


class TestUncertainGitStatusFailsClosed:
    """#760: a git spawn that fails for a reason OTHER than "no repository
    here" or "this path is not tracked" must never be read the same as a
    confirmed absence -- a PATH shim that makes every git invocation fail
    with an unrelated fatal error must still leave the config.json behind,
    the same as a confirmed-tracked one."""

    def test_a_git_that_always_fails_leaves_the_config_behind(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "sub-token"}}))
        # A real repo exists (so a naive check would find a work tree) --
        # the broken shim below is what must be hit and fail closed.
        _init_git(project)
        _git(project, ["add", ".remember/config.json"])
        _git(project, ["commit", "-q", "-m", "seed"])

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        broken_git_path = _path_with_broken_git(tmp_path)
        result = _source_bootstrap_with_env(str(project), str(pipeline), str(home),
                                             path_override=broken_git_path)
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
        remember_dir = result.stdout.strip().split("REMEMBER_DIR=")[-1].strip()

        assert (legacy / "config.json").exists(), (
            "an UNDETERMINED git status (a spawn failing for an unrelated "
            "reason) let the config.json migrate as trusted -- must fail "
            "CLOSED, the same as a confirmed-tracked file"
        )
        assert not (Path(remember_dir) / "config.json").exists()


class TestSymlinkedGitDirFailsClosed:
    """#761: `_remember_config_tracked_status` resolves `.git` the ordinary
    way -- a pre-planted SYMLINK swapping it for a DIFFERENT repository's
    git directory (its own, independent objects and index) must not let
    the tracked-check answer against that unrelated repo. It must fail
    CLOSED (could-not-tell), the same as #760's broken-git-shim case."""

    def test_a_symlinked_git_dir_to_an_unrelated_repo_leaves_the_config_behind(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "sub-token"}}))

        # No real repo for `project` at all. Instead, project/.git is a
        # SYMLINK to an UNRELATED repository's git dir that never tracked
        # (and cannot track) project/.remember/config.json -- the shape a
        # pre-planted swap produces.
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        _init_git(unrelated)
        (unrelated / "readme.txt").write_text("hi\n")
        _git(unrelated, ["add", "readme.txt"])
        _git(unrelated, ["commit", "-q", "-m", "seed"])
        (project / ".git").symlink_to(unrelated / ".git")

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        result = _source_bootstrap_with_env(str(project), str(pipeline), str(home))
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
        remember_dir = result.stdout.strip().split("REMEMBER_DIR=")[-1].strip()

        assert (legacy / "config.json").exists(), (
            "a project/.git SYMLINK to an unrelated repository answered the "
            "tracked-check against that repo's own index instead of failing "
            "closed"
        )
        assert not (Path(remember_dir) / "config.json").exists()

    def test_an_ordinary_untracked_git_dir_still_migrates(self, tmp_path):
        """Positive control (#602): same shape minus the symlink -- an
        ordinary, untracked project repo must still migrate normally,
        proving the fix above does not fail closed unconditionally."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "sub-token"}}))
        _init_git(project)
        # Deliberately never `git add`ed.

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        result = _source_bootstrap_with_env(str(project), str(pipeline), str(home))
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
        remember_dir = result.stdout.strip().split("REMEMBER_DIR=")[-1].strip()

        assert not (legacy / "config.json").exists()
        assert (Path(remember_dir) / "config.json").exists()
