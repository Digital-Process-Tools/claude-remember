"""Tests for worktree-aware REMEMBER_DIR resolution (issue #56).

When a Claude Code session runs inside a git worktree, Claude Code sets
CLAUDE_PROJECT_DIR to the *worktree* path. Without special handling the plugin
resolves REMEMBER_DIR relative to that worktree path, so:

  - Legacy mode: memory lands in ``<worktree>/.remember`` — physically inside
    the worktree. ``git worktree remove`` deletes it (silently, since the plugin
    gitignores it with ``*``), and it is never migrated to the main checkout.
  - External mode: the ``{slug}`` is computed from the worktree path, producing
    ``~/.remember/<slug-of-worktree>`` — a separate subtree the main checkout's
    session never loads.

The fix routes REMEMBER_DIR resolution through git's *common dir* so worktree
sessions share the main checkout's memory root, while PROJECT_DIR itself is left
untouched (session recovery relies on the worktree slug).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX git worktree layout — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"
SESSION_START_SCRIPT = REPO_ROOT / "scripts" / "session-start-hook.sh"


# The real implementation, not a reproduction of it. Three test files each
# carried their own copy of the naive rule; they agreed only because no fixture
# used a non-ASCII or over-200-character path, which is the same "true today"
# that let the shipped copies drift (#177).
from pipeline.slug import session_dir_slug as _slug  # noqa: E402


def _make_plugin_dir(tmp_path: Path, data_dir_value: str) -> Path:
    """Create a minimal plugin directory with a config.json carrying data_dir."""
    plugin = tmp_path / "plugin"
    plugin.mkdir(parents=True)
    (plugin / "scripts").mkdir()
    (plugin / "pipeline").mkdir()
    (plugin / "pipeline" / "haiku.py").write_text("# marker\n")
    (plugin / "config.json").write_text(json.dumps({
        "data_dir": data_dir_value,
        "cooldowns": {"save_seconds": 120, "ndc_seconds": 3600},
        "thresholds": {"min_human_messages": 3, "delta_lines_trigger": 50},
        "features": {"ndc_compression": True, "recovery": False},
    }))
    return plugin


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "t")
    (path / "file.txt").write_text("hello\n")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-qm", "init")


def _add_worktree(main: Path, wt: Path, branch: str) -> None:
    _git(main, "worktree", "add", "-q", "-b", branch, str(wt))


def _run_bootstrap(project_dir: str, pipeline_dir: str, home_dir: str) -> dict:
    """Source detect-tools.sh + bootstrap-dirs.sh and capture REMEMBER_DIR."""
    script = f"""
    set -e
    export PROJECT_DIR="{project_dir}"
    export PIPELINE_DIR="{pipeline_dir}"
    export HOME="{home_dir}"
    source "{DETECT_SCRIPT}"
    source "{BOOTSTRAP_SCRIPT}"
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    """
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
    parsed: dict = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k] = v
    return parsed


# ─── Legacy mode ─────────────────────────────────────────────────────────────

class TestLegacyModeWorktree:

    def test_worktree_resolves_to_main_checkout_remember(self, tmp_path):
        """A worktree session's REMEMBER_DIR points at the MAIN checkout's .remember."""
        main = tmp_path / "main"
        _init_repo(main)
        wt = tmp_path / "wt-feature"
        _add_worktree(main, wt, "feature")

        home = tmp_path / "home"
        home.mkdir()
        plugin = _make_plugin_dir(tmp_path, ".remember")  # legacy default

        result = _run_bootstrap(str(wt), str(plugin), str(home))
        remember_dir = Path(result["REMEMBER_DIR"]).resolve()

        assert remember_dir == (main / ".remember").resolve(), (
            f"worktree memory should live in main checkout, got {remember_dir}"
        )
        # And crucially NOT inside the worktree (which gets deleted on cleanup).
        assert not str(remember_dir).startswith(str(wt.resolve())), (
            "REMEMBER_DIR must not live inside the worktree"
        )

    def test_worktree_remember_is_gitignored(self, tmp_path):
        """The main-checkout .remember reached via a worktree still gets .gitignore."""
        main = tmp_path / "main"
        _init_repo(main)
        wt = tmp_path / "wt-feature"
        _add_worktree(main, wt, "feature")

        home = tmp_path / "home"
        home.mkdir()
        plugin = _make_plugin_dir(tmp_path, ".remember")

        result = _run_bootstrap(str(wt), str(plugin), str(home))
        remember_dir = Path(result["REMEMBER_DIR"])

        assert (remember_dir / ".gitignore").exists(), (
            ".gitignore should still be written for the main-checkout .remember"
        )

    def test_non_worktree_repo_unchanged(self, tmp_path):
        """Regression: an ordinary (non-worktree) checkout resolves to its own .remember."""
        main = tmp_path / "main"
        _init_repo(main)

        home = tmp_path / "home"
        home.mkdir()
        plugin = _make_plugin_dir(tmp_path, ".remember")

        result = _run_bootstrap(str(main), str(plugin), str(home))
        remember_dir = Path(result["REMEMBER_DIR"]).resolve()

        assert remember_dir == (main / ".remember").resolve()

    def test_non_git_dir_unchanged(self, tmp_path):
        """Regression: a plain (non-git) project resolves to PROJECT_DIR/.remember."""
        project = tmp_path / "plain-proj"
        project.mkdir()

        home = tmp_path / "home"
        home.mkdir()
        plugin = _make_plugin_dir(tmp_path, ".remember")

        result = _run_bootstrap(str(project), str(plugin), str(home))
        remember_dir = Path(result["REMEMBER_DIR"]).resolve()

        assert remember_dir == (project / ".remember").resolve()


# ─── External mode ───────────────────────────────────────────────────────────

class TestExternalModeWorktree:

    def test_worktree_slug_matches_main_checkout(self, tmp_path):
        """External mode: {slug} is derived from the MAIN checkout, not the worktree."""
        main = tmp_path / "main"
        _init_repo(main)
        wt = tmp_path / "wt-feature"
        _add_worktree(main, wt, "feature")

        home = tmp_path / "home"
        home.mkdir()
        ext_base = tmp_path / "ext-mem"
        plugin = _make_plugin_dir(tmp_path, str(ext_base) + "/{slug}")

        result = _run_bootstrap(str(wt), str(plugin), str(home))
        remember_dir = result["REMEMBER_DIR"]

        expected_slug = _slug(str(main.resolve()))
        assert remember_dir.endswith(expected_slug), (
            f"expected slug of main checkout '{expected_slug}', got {remember_dir}"
        )
        wt_slug = _slug(str(wt.resolve()))
        assert not remember_dir.endswith(wt_slug), (
            "external REMEMBER_DIR must not be keyed by the worktree slug"
        )

    def test_worktree_and_main_share_external_dir(self, tmp_path):
        """A worktree session and a main-checkout session resolve to the SAME external dir."""
        main = tmp_path / "main"
        _init_repo(main)
        wt = tmp_path / "wt-feature"
        _add_worktree(main, wt, "feature")

        home = tmp_path / "home"
        home.mkdir()
        ext_base = tmp_path / "ext-mem"
        plugin = _make_plugin_dir(tmp_path, str(ext_base) + "/{slug}")

        from_wt = _run_bootstrap(str(wt), str(plugin), str(home))["REMEMBER_DIR"]
        from_main = _run_bootstrap(str(main), str(plugin), str(home))["REMEMBER_DIR"]

        assert from_wt == from_main, (
            f"worktree and main should share memory dir; got {from_wt} vs {from_main}"
        )


# ─── Handoff path in worktree sessions ───────────────────────────────────────

class TestWorktreeHandoff:

    def test_legacy_worktree_emits_handoff_to_main_checkout(self, tmp_path):
        """In a worktree, session-start must emit === HANDOFF === pointing at the
        MAIN checkout's remember.md — even in legacy mode.

        Legacy mode normally suppresses the hint (#118) because the /remember
        skill's fallback ``{project}/.remember/remember.md`` already matches. But
        inside a worktree that fallback would resolve to the *worktree* path,
        which is wrong (and deleted on cleanup). The hint must fire so the skill
        writes to the main checkout instead. This is covered by session-start's
        existing ``REMEMBER_ROOT != PROJECT_DIR`` guard once REMEMBER_DIR is
        redirected — this test locks that interaction in.
        """
        main = tmp_path / "main"
        _init_repo(main)
        wt = tmp_path / "wt-feature"
        _add_worktree(main, wt, "feature")

        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)
        # Legacy mode via user-global config.
        (home / ".remember" / "config.json").write_text(
            json.dumps({"data_dir": ".remember", "features": {"recovery": False}})
        )
        # session-start reads ~/.claude/projects/<worktree-slug>/ — create it.
        sessions_dir = home / ".claude" / "projects" / _slug(str(wt.resolve()))
        sessions_dir.mkdir(parents=True)

        env = {
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(wt),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "HOME": str(home),
        }
        result = subprocess.run(
            ["bash", str(SESSION_START_SCRIPT)], env=env,
            capture_output=True, text=True,
        )
        output = result.stdout

        assert "=== HANDOFF ===" in output, (
            f"HANDOFF block must be emitted for worktree sessions.\n"
            f"stdout: {output[:400]}\nstderr: {result.stderr[:400]}"
        )
        expected = str((main / ".remember" / "remember.md").resolve())
        assert expected in output, (
            f"handoff should point at main checkout {expected}.\noutput: {output[:500]}"
        )
        assert str(wt.resolve()) + "/.remember" not in output, (
            "handoff must not point inside the worktree"
        )
