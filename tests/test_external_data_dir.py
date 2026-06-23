"""End-to-end tests for external data_dir mode.

Verifies that when data_dir points outside the project:
  - REMEMBER_DIR is created at the expected path
  - .gitignore is NOT written inside REMEMBER_DIR
  - Per-project identity.md overrides the plugin-bundled one
  - === HANDOFF === with the correct absolute path appears in session-start output
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX session-start hook — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"
SESSION_START_SCRIPT = REPO_ROOT / "scripts" / "session-start-hook.sh"
BUNDLED_IDENTITY = REPO_ROOT / "identity.example.md"


def _slug(path: str) -> str:
    """Reproduce the session_dir_slug logic in Python for assertion."""
    import re
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def _make_plugin_dir(tmp_path: Path, data_dir_value: str) -> Path:
    """Create a minimal plugin directory with a config.json."""
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
    (plugin / "identity.md").write_text("Bundled identity.\n")
    (plugin / "prompts").mkdir()
    (plugin / "prompts" / "session-history-hint.txt").write_text("")
    (plugin / "hooks.d").mkdir()
    return plugin


def _run_bootstrap(project_dir: str, pipeline_dir: str, home_dir: str) -> dict:
    script = f"""
    set -e
    export PROJECT_DIR={project_dir}
    export PIPELINE_DIR={pipeline_dir}
    export HOME={home_dir}
    source {DETECT_SCRIPT}
    source {BOOTSTRAP_SCRIPT}
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


class TestExternalMode:

    def test_remember_dir_created_at_external_path(self, tmp_path):
        """REMEMBER_DIR is created at the external path, not inside PROJECT_DIR."""
        project = tmp_path / "proj"
        project.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        ext_base = tmp_path / "ext-mem"
        plugin = _make_plugin_dir(tmp_path, str(ext_base) + "/{slug}")

        result = _run_bootstrap(str(project), str(plugin), str(home))
        remember_dir = Path(result["REMEMBER_DIR"])

        assert remember_dir.exists(), "REMEMBER_DIR not created"
        assert str(remember_dir).startswith(str(ext_base)), "REMEMBER_DIR not under ext_base"
        assert not str(remember_dir).startswith(str(project)), "REMEMBER_DIR is inside project"

    def test_no_gitignore_in_external_mode(self, tmp_path):
        """No .gitignore is written when REMEMBER_DIR is outside PROJECT_DIR."""
        project = tmp_path / "proj"
        project.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        ext_base = tmp_path / "ext-mem"
        plugin = _make_plugin_dir(tmp_path, str(ext_base) + "/{slug}")

        result = _run_bootstrap(str(project), str(plugin), str(home))
        remember_dir = Path(result["REMEMBER_DIR"])

        assert not (remember_dir / ".gitignore").exists(), ".gitignore must not be written in external mode"

    def test_gitignore_written_in_legacy_mode(self, tmp_path):
        """Sanity: .gitignore IS written when REMEMBER_DIR is inside PROJECT_DIR."""
        project = tmp_path / "proj"
        project.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        plugin = _make_plugin_dir(tmp_path, ".remember")  # legacy default

        result = _run_bootstrap(str(project), str(plugin), str(home))
        remember_dir = Path(result["REMEMBER_DIR"])

        assert (remember_dir / ".gitignore").exists(), ".gitignore must be written in legacy mode"

    def test_slug_matches_project_path(self, tmp_path):
        """The slug in REMEMBER_DIR matches the session_dir_slug of PROJECT_DIR."""
        project = tmp_path / "proj"
        project.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        ext_base = tmp_path / "ext-mem"
        plugin = _make_plugin_dir(tmp_path, str(ext_base) + "/{slug}")

        result = _run_bootstrap(str(project), str(plugin), str(home))
        remember_dir = result["REMEMBER_DIR"]

        expected_slug = _slug(str(project))
        assert remember_dir.endswith(expected_slug), (
            f"Expected REMEMBER_DIR to end with slug '{expected_slug}', got: {remember_dir}"
        )


class TestHandoffEmission:

    def test_session_start_emits_handoff_block(self, tmp_path):
        """session-start-hook.sh emits === HANDOFF === with the absolute REMEMBER_DIR path.

        Uses the real plugin scripts from REPO_ROOT; sets external mode via
        user-global ~/.remember/config.json so no fake plugin dir is needed.
        """
        project = tmp_path / "proj"
        project.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)
        ext_base = tmp_path / "ext-mem"

        # Activate external mode via user-global config.
        (home / ".remember" / "config.json").write_text(
            json.dumps({"data_dir": str(ext_base) + "/{slug}", "features": {"recovery": False}})
        )

        # session-start reads from ~/.claude/projects/<slug>/ — create an empty one.
        slug = _slug(str(project))
        sessions_dir = home / ".claude" / "projects" / slug
        sessions_dir.mkdir(parents=True)

        # Run as a standalone script (not sourced) so $0 is set correctly.
        env = {
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "HOME": str(home),
        }
        result = subprocess.run(
            ["bash", str(SESSION_START_SCRIPT)], env=env, capture_output=True, text=True
        )
        output = result.stdout

        assert "=== HANDOFF ===" in output, (
            f"=== HANDOFF === block missing from session-start output.\nstderr: {result.stderr[:500]}"
        )
        assert "remember.md" in output, "handoff path missing remember.md"
        assert str(ext_base) in output, (
            f"handoff path does not reference external base.\noutput: {output}\nstderr: {result.stderr[:300]}"
        )

    def test_session_start_suppresses_handoff_block_in_legacy_mode(self, tmp_path):
        """In legacy mode the === HANDOFF === hint is noise — the /remember skill
        falls back to {project}/.remember/remember.md, the exact path the hint
        would carry. Suppress it. The === LAST HANDOFF === block (file-gated) is
        unaffected and not asserted here.
        """
        project = tmp_path / "proj"
        project.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        # Legacy mode: relative data_dir resolves to {project}/.remember,
        # so REMEMBER_ROOT == PROJECT_DIR.
        (home / ".remember" / "config.json").write_text(
            json.dumps({"data_dir": ".remember", "features": {"recovery": False}})
        )

        slug = _slug(str(project))
        sessions_dir = home / ".claude" / "projects" / slug
        sessions_dir.mkdir(parents=True)

        env = {
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "HOME": str(home),
        }
        result = subprocess.run(
            ["bash", str(SESSION_START_SCRIPT)], env=env, capture_output=True, text=True
        )
        output = result.stdout

        assert "=== HANDOFF ===" not in output, (
            f"=== HANDOFF === hint should be suppressed in legacy mode.\noutput: {output}\nstderr: {result.stderr[:300]}"
        )


class TestIdentityFallback:

    def test_project_identity_overrides_bundled(self, tmp_path):
        """Per-project identity.md in REMEMBER_DIR takes precedence over bundled."""
        project = tmp_path / "proj"
        project.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)
        ext_base = tmp_path / "ext-mem"

        # Activate external mode.
        (home / ".remember" / "config.json").write_text(
            json.dumps({"data_dir": str(ext_base) + "/{slug}", "features": {"recovery": False}})
        )

        # Bootstrap first to learn REMEMBER_DIR path.
        result = _run_bootstrap(
            str(project),
            str(REPO_ROOT),   # real plugin
            str(home),
        )
        remember_dir = Path(result["REMEMBER_DIR"])
        (remember_dir / "identity.md").write_text("Project-specific identity.\n")

        slug = _slug(str(project))
        sessions_dir = home / ".claude" / "projects" / slug
        sessions_dir.mkdir(parents=True)

        env = {
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "HOME": str(home),
        }
        result = subprocess.run(
            ["bash", str(SESSION_START_SCRIPT)], env=env, capture_output=True, text=True
        )
        output = result.stdout

        assert "Project-specific identity." in output, (
            f"project-specific identity not injected.\nstderr: {result.stderr[:500]}"
        )
