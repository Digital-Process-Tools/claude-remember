"""#1134: the #138 guard in 50-git-backup.sh must fail CLOSED.

The after_save hook runs detached. By the time it runs, an orchestrator may have
removed the linked worktree PROJECT_DIR names, or something may have re-created
the path as a bare stub. A live "which repository is the project?" lookup then
answers "none", and the hook used to read that as "so REPO_ROOT is not the project"
and committed the memory into it.

The fix: save-session.sh asks while the project is live and exports the answer
(REMEMBER_PROJECT_GIT_STATE = repo | none | unknown, REMEMBER_PROJECT_COMMON_DIR);
the hook compares against that and declines when it cannot prove separation.

Every "must not commit" case here is paired with a "must commit" case on the same
branch of the hook, so a hook that simply never commits cannot pass.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import test_save_session_gates as gates  # noqa: E402
from test_git_backup_hook import (  # noqa: E402
    REPO_ROOT,
    _commit_log,
    _git,
    _make_config,
    _make_worktree_project,
    _run_hook,
    make_external_remember_repo,
    wait_for_lock_release,
)

LIB = REPO_ROOT / "scripts" / "lib-git-common-dir.sh"
TRANSCRIPT_LINE = json.dumps({"type": "user"}) + "\n"


def _common_dir(repo: Path) -> str:
    """The real common dir of a repo, resolved the way the helper resolves it."""
    return str((repo / ".git").resolve())


def _state_env(state: str, common: str = "") -> dict:
    return {"REMEMBER_PROJECT_GIT_STATE": state, "REMEMBER_PROJECT_COMMON_DIR": common}


def _drop_worktree(main: Path, worktree: Path) -> None:
    _git(main, ["worktree", "remove", "--force", str(worktree)])


def _project_memory(tmp_path: Path):
    """Main checkout + linked worktree OUTSIDE it, memory redirected into main (#127)."""
    main, worktree = _make_worktree_project(tmp_path)
    remember = main / ".remember"
    remember.mkdir()
    (remember / ".gitignore").write_text("*\n")
    (remember / "now.md").write_text("## 10:00 | test\nMemory.\n")
    return main, worktree, remember


def _external_backup(tmp_path: Path):
    home, remember, _ = make_external_remember_repo(tmp_path)
    slug_dir = remember / "test-slug"
    slug_dir.mkdir()
    (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n")
    return home, remember, slug_dir


class TestCommonDirHelper:
    """_remember_git_common_dir: three answers, never an empty string for two."""

    @staticmethod
    def _ask(path, extra_env=None):
        env = {**os.environ, "_LIB_MEMORY_DIR_LOADED": "1", **(extra_env or {})}
        script = f'source "{LIB}"; _remember_git_common_dir "$1"; echo "rc=$?"'
        r = subprocess.run(
            ["bash", "-c", script, "x", str(path)],
            env=env, capture_output=True, text=True,
        )
        out = r.stdout.strip().splitlines()
        return out[:-1], out[-1]

    def test_repo_and_linked_worktree_answer_the_same_common_dir(self, tmp_path):
        main, worktree = _make_worktree_project(tmp_path)
        want = _common_dir(main)
        assert self._ask(main) == ([want], "rc=0")
        assert self._ask(worktree) == ([want], "rc=0")

    def test_plain_directory_is_not_a_repository_rc3(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert self._ask(plain) == ([], "rc=3")

    def test_missing_directory_is_could_not_tell_rc1(self, tmp_path):
        assert self._ask(tmp_path / "gone") == ([], "rc=1")

    def test_leaked_git_env_does_not_change_the_answer(self, tmp_path):
        main, _worktree = _make_worktree_project(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        _git(other, ["init", "-q"])
        leaked = {
            "GIT_DIR": str(other / ".git"),
            "GIT_COMMON_DIR": str(other / ".git"),
            "GIT_WORK_TREE": str(other),
        }
        assert self._ask(main, leaked) == ([_common_dir(main)], "rc=0")


class TestSaveSessionCapturesBeforeDetaching:
    """End to end: save-session.sh -> dispatch after_save -> what the hook receives."""

    def _save(self, tmp_path, project, with_lib=True):
        env, _default_project, plugin, _calls, sid = gates._make_env(
            tmp_path, exchanges=5, humans=5, position=10,
        )
        if with_lib:
            # The shared stub plugin copies a fixed script list that predates the helper.
            (plugin / "scripts" / LIB.name).write_text(LIB.read_text())
        hooks = plugin / "hooks.d" / "after_save"
        hooks.mkdir(parents=True)
        dump = tmp_path / "after_save.env"
        hook = hooks / "10-dump.sh"
        hook.write_text(
            "#!/bin/bash\n"
            f'echo "$REMEMBER_PROJECT_GIT_STATE|$REMEMBER_PROJECT_COMMON_DIR" > {dump}\n'
        )
        hook.chmod(0o755)
        slug = str(project).replace("/", "-").replace(".", "-").replace("_", "-")
        sdir = tmp_path / "home" / ".claude" / "projects" / slug
        sdir.mkdir(parents=True)
        (sdir / f"{sid}.jsonl").write_text(TRANSCRIPT_LINE * 10)
        env = {**env, "CLAUDE_PROJECT_DIR": str(project), "STUB_HAIKU_SKIP": "0"}
        return gates._run(plugin, env, sid), dump

    def test_a_linked_worktree_project_exports_its_common_dir(self, tmp_path):
        main, worktree = _make_worktree_project(tmp_path)
        result, dump = self._save(tmp_path, worktree)
        assert dump.is_file(), (result.stdout, result.stderr)
        assert dump.read_text().strip() == f"repo|{_common_dir(main)}"

    def test_a_non_git_project_exports_state_none(self, tmp_path):
        """Control: the exported state really varies with the project."""
        plain = tmp_path / "plain-project"
        plain.mkdir()
        result, dump = self._save(tmp_path, plain)
        assert dump.is_file(), (result.stdout, result.stderr)
        assert dump.read_text().strip() == "none|"

    def test_a_failed_lookup_exports_state_unknown_not_an_empty_answer(self, tmp_path):
        """Control for the two above: when the lookup cannot run, the hook is told so."""
        main, worktree = _make_worktree_project(tmp_path)
        result, dump = self._save(tmp_path, worktree, with_lib=False)
        assert dump.is_file(), (result.stdout, result.stderr)
        assert dump.read_text().strip() == "unknown|"


class TestRealResolutionPathWorktreeVanishesMidRun:
    """Without the _LIB_MEMORY_DIR_LOADED pin: the layout the reporter had.

    Main clone, linked worktree OUTSIDE it, REMEMBER_DIR re-derived by the real
    lib-memory-dir.sh to <main>/.remember (#127). A shim removes the worktree the
    moment the hook runs its first toplevel check, i.e. after REMEMBER_DIR was
    resolved from the live worktree and before the #138 guard reads the project.
    """

    @staticmethod
    def _run(tmp_path, worktree, state_env, gone_at):
        shimdir = tmp_path / "shim"
        shimdir.mkdir()
        real = shutil.which("git")
        shim = shimdir / "git"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import os, shutil, sys\n"
            "if '--show-toplevel' in sys.argv:\n"
            "    shutil.rmtree(os.environ['VANISH_DIR'], ignore_errors=True)\n"
            f"os.execv({real!r}, [{real!r}] + sys.argv[1:])\n"
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        env = {
            **os.environ,
            "HOME": str(home),
            "PROJECT_DIR": str(worktree),
            "PIPELINE_DIR": str(REPO_ROOT),
            "REMEMBER_PROJECT": str(worktree),
            "PATH": f"{shimdir}{os.pathsep}{os.environ['PATH']}",
            "VANISH_DIR": str(gone_at),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@t",
            **state_env,
        }
        env.pop("_LIB_MEMORY_DIR_LOADED", None)
        env.pop("REMEMBER_DIR", None)
        hook = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"
        return subprocess.run(["bash", str(hook)], env=env, capture_output=True, text=True)

    def test_worktree_removed_after_remember_dir_resolved_does_not_commit(self, tmp_path):
        main, worktree, _remember = _project_memory(tmp_path)
        before = _commit_log(main)

        result = self._run(tmp_path, worktree, _state_env("repo", _common_dir(main)), worktree)
        wait_for_lock_release(main / ".git-backup.lock")

        assert not worktree.exists(), "harness check: the shim must have removed the worktree"
        assert _commit_log(main) == before, (result.stdout, result.stderr)

    def test_same_race_with_a_decoy_leaves_the_harness_working(self, tmp_path):
        """Control: the shim fires but removes a decoy, the project stays live."""
        main, worktree, _remember = _project_memory(tmp_path)
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        before = _commit_log(main)

        self._run(tmp_path, worktree, _state_env("repo", _common_dir(main)), decoy)
        wait_for_lock_release(main / ".git-backup.lock")

        assert not decoy.exists(), "harness check: the shim must have fired"
        assert worktree.exists()
        assert _commit_log(main) == before


class TestCommonDirHelperPortability:
    """The helper must not depend on the language of the VCS or on it being new (#1134)."""

    @staticmethod
    def _ask_from(cwd, path, extra_env=None, drop=()):
        env = {**os.environ, "_LIB_MEMORY_DIR_LOADED": "1", **(extra_env or {})}
        for name in drop:
            env.pop(name, None)
        script = f'source "{LIB}"; _remember_git_common_dir "$1"; echo "rc=$?"'
        r = subprocess.run(
            ["bash", "-c", script, "x", str(path)],
            env=env, cwd=str(cwd), capture_output=True, text=True,
        )
        out = r.stdout.strip().splitlines()
        return out[:-1], out[-1]

    def test_a_non_english_git_still_reports_not_a_repository(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        french = {"LANG": "fr_FR.UTF-8", "LANGUAGE": "fr"}
        env = {k: v for k, v in os.environ.items() if k != "LC_ALL"}
        argv = [shutil.which("git"), "-C", str(plain)] + "rev-parse --git-common-dir".split()
        probe = subprocess.run(argv, env={**env, **french}, capture_output=True, text=True)
        if "not a git repository" in probe.stderr:
            pytest.skip("this git has no translated messages for fr_FR here")
        # Positive control: the harness really does get a non-English message.
        assert probe.returncode != 0 and probe.stderr.strip()
        assert self._ask_from(tmp_path, plain, french, drop=("LC_ALL",)) == ([], "rc=3")

    def test_english_git_still_reports_not_a_repository(self, tmp_path):
        """Control for the test above: the same call on a plain environment."""
        plain = tmp_path / "plain"
        plain.mkdir()
        assert self._ask_from(tmp_path, plain, {"LC_ALL": "C"}) == ([], "rc=3")

    @staticmethod
    def _old_vcs(tmp_path):
        """A git older than 2.31: echoes --path-format=... back and exits 0."""
        shimdir = tmp_path / "oldgit"
        shimdir.mkdir()
        real = shutil.which("git")
        shim = shimdir / "git"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "keep = []\n"
            "for a in sys.argv[1:]:\n"
            "    if a.startswith('--path-format='):\n"
            "        print(a, flush=True)\n"
            "    else:\n"
            "        keep.append(a)\n"
            f"os.execv({real!r}, [{real!r}] + keep)\n"
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        return shimdir

    def test_old_git_relative_answer_is_resolved_against_the_asked_directory(self, tmp_path):
        main, worktree = _make_worktree_project(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        path = f"{self._old_vcs(tmp_path)}{os.pathsep}{os.environ['PATH']}"
        want = _common_dir(main)
        # Positive control: the shim really does behave like an old git.
        argv = ["git", "-C", str(main)]
        argv += ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        echoed = subprocess.run(
            argv, env={**os.environ, "PATH": path}, capture_output=True, text=True,
        )
        assert echoed.stdout.splitlines()[0] == "--path-format=absolute"
        assert self._ask_from(elsewhere, main, {"PATH": path}) == ([want], "rc=0")
        assert self._ask_from(elsewhere, worktree, {"PATH": path}) == ([want], "rc=0")


class TestHookUsesCapturedProjectState:
    def test_gone_project_and_captured_repo_equal_to_backup_does_not_commit(self, tmp_path):
        main, worktree, remember = _project_memory(tmp_path)
        captured = _common_dir(main)
        _drop_worktree(main, worktree)
        before = _commit_log(main)

        result = _run_hook(
            remember, worktree, tmp_path / "home", config_path=_make_config(tmp_path, cooldown=0),
            extra_env=_state_env("repo", captured),
        )
        assert result.returncode == 0
        wait_for_lock_release(main / ".git-backup.lock")
        assert _commit_log(main) == before, "project repo must not be committed to"
        assert (remember / ".gitignore").exists()

    def test_recreated_stub_project_dir_and_captured_repo_does_not_commit(self, tmp_path):
        main, worktree, remember = _project_memory(tmp_path)
        captured = _common_dir(main)
        _drop_worktree(main, worktree)
        (worktree / ".remember" / "logs").mkdir(parents=True)
        before = _commit_log(main)

        result = _run_hook(
            remember, worktree, tmp_path / "home", config_path=_make_config(tmp_path, cooldown=0),
            extra_env=_state_env("repo", captured),
        )
        assert result.returncode == 0
        wait_for_lock_release(main / ".git-backup.lock")
        assert _commit_log(main) == before, "project repo must not be committed to"

    def test_gone_project_with_provably_separate_backup_repo_still_commits(self, tmp_path):
        """Control for the two above, same branch: captured repo differs from the backup."""
        home, remember, slug_dir = _external_backup(tmp_path)
        main, worktree = _make_worktree_project(tmp_path)
        captured = _common_dir(main)
        _drop_worktree(main, worktree)

        result = _run_hook(
            slug_dir, worktree, home, config_path=_make_config(tmp_path, cooldown=0),
            extra_env=_state_env("repo", captured),
        )
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")
        assert len(_commit_log(remember)) == 2, "a separate backup repo must still be committed to"

    def test_unknown_state_declines_and_says_so(self, tmp_path):
        home, remember, slug_dir = _external_backup(tmp_path)
        project = tmp_path / "plain-project"
        project.mkdir()

        result = _run_hook(
            slug_dir, project, home, config_path=_make_config(tmp_path, cooldown=0),
            extra_env=_state_env("unknown"),
        )
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")
        assert len(_commit_log(remember)) == 1, "cannot prove separation: must not commit"
        logs = list((slug_dir / "logs").glob("*.log")) if (slug_dir / "logs").is_dir() else []
        assert any("declined" in p.read_text() for p in logs), "the decline must be logged"

    def test_none_state_with_external_backup_commits(self, tmp_path):
        """Control for the unknown case: a non-git project with its own backup repo."""
        home, remember, slug_dir = _external_backup(tmp_path)
        project = tmp_path / "plain-project"
        project.mkdir()

        result = _run_hook(
            slug_dir, project, home, config_path=_make_config(tmp_path, cooldown=0),
            extra_env=_state_env("none"),
        )
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")
        assert len(_commit_log(remember)) == 2

    def test_manual_run_without_state_and_vanished_project_declines(self, tmp_path):
        """No state from save-session (manual run): the live lookup must fail closed."""
        main, worktree, remember = _project_memory(tmp_path)
        _drop_worktree(main, worktree)
        before = _commit_log(main)

        result = _run_hook(
            remember, worktree, tmp_path / "home", config_path=_make_config(tmp_path, cooldown=0),
        )
        assert result.returncode == 0
        wait_for_lock_release(main / ".git-backup.lock")
        assert _commit_log(main) == before

    def test_leaked_git_common_dir_does_not_make_the_sides_differ(self, tmp_path):
        """A leaked GIT_COMMON_DIR must not turn a project-repo store into 'separate'."""
        main, worktree, remember = _project_memory(tmp_path)
        other = tmp_path / "unrelated"
        other.mkdir()
        _git(other, ["init", "-q"])
        before = _commit_log(main)

        result = _run_hook(
            remember, worktree, tmp_path / "home", config_path=_make_config(tmp_path, cooldown=0),
            extra_env={**_state_env("repo", _common_dir(main)), "GIT_COMMON_DIR": str(other / ".git")},
        )
        assert result.returncode == 0
        wait_for_lock_release(main / ".git-backup.lock")
        assert _commit_log(main) == before, "project repo must not be committed to"

    def test_leaked_git_common_dir_does_not_block_a_separate_backup(self, tmp_path):
        """Control: same leak, genuinely separate backup repo, still commits."""
        home, remember, slug_dir = _external_backup(tmp_path)
        main, worktree = _make_worktree_project(tmp_path)
        other = tmp_path / "unrelated"
        other.mkdir()
        _git(other, ["init", "-q"])

        result = _run_hook(
            slug_dir, worktree, home, config_path=_make_config(tmp_path, cooldown=0),
            extra_env={**_state_env("repo", _common_dir(main)), "GIT_COMMON_DIR": str(other / ".git")},
        )
        assert result.returncode == 0
        wait_for_lock_release(remember / ".git-backup.lock")
        assert len(_commit_log(remember)) == 2
