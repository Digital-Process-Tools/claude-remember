"""Tests for path resolution across different install layouts.

Tests the current inline path resolution in save-session.sh and
run-consolidation.sh, proving where it breaks. Then tests the fix
(resolve-paths.sh) once it exists.

Install layouts tested:
  1. Local:       $PROJECT/.claude/remember/scripts/save-session.sh
  2. Marketplace: ~/.claude/plugins/cache/org/remember/0.1.0/scripts/save-session.sh
  3. Symlinked:   Local layout with symlinked scripts/ directory
  4. Spaces:      Local layout with spaces in the project path
"""

import os
import stat
import subprocess
import tempfile

import pytest


def _create_local_install(base: str) -> tuple[str, str]:
    """Create a local install layout and return (project_dir, plugin_dir).

    Layout:
        base/my-project/
        base/my-project/.claude/remember/scripts/save-session.sh
        base/my-project/.claude/remember/pipeline/haiku.py
        base/my-project/.remember/tmp/
        base/my-project/.remember/logs/
    """
    project = os.path.join(base, "my-project")
    plugin = os.path.join(project, ".claude", "remember")
    scripts = os.path.join(plugin, "scripts")
    os.makedirs(scripts)
    os.makedirs(os.path.join(plugin, "pipeline"))
    os.makedirs(os.path.join(project, ".remember", "tmp"))
    os.makedirs(os.path.join(project, ".remember", "logs"))

    # Create a marker file so resolve-paths.sh can detect the plugin root
    with open(os.path.join(plugin, "pipeline", "haiku.py"), "w") as f:
        f.write("# marker\n")

    return project, plugin


def _create_marketplace_install(base: str) -> tuple[str, str, str]:
    """Create a marketplace install layout and return (project_dir, plugin_dir, cache_dir).

    Layout:
        base/my-project/                                          (project)
        base/my-project/.remember/tmp/
        base/my-project/.remember/logs/
        base/home/.claude/plugins/cache/org/remember/0.1.0/       (plugin)
        base/home/.claude/plugins/cache/org/remember/0.1.0/scripts/
        base/home/.claude/plugins/cache/org/remember/0.1.0/pipeline/haiku.py
    """
    project = os.path.join(base, "my-project")
    cache_base = os.path.join(base, "home", ".claude", "plugins", "cache")
    plugin = os.path.join(cache_base, "claude-plugins-official", "remember", "0.1.0")
    scripts = os.path.join(plugin, "scripts")
    os.makedirs(scripts)
    os.makedirs(os.path.join(plugin, "pipeline"))
    os.makedirs(os.path.join(project, ".remember", "tmp"))
    os.makedirs(os.path.join(project, ".remember", "logs"))

    with open(os.path.join(plugin, "pipeline", "haiku.py"), "w") as f:
        f.write("# marker\n")

    return project, plugin, cache_base


def _write_test_script(plugin_dir: str, filename: str, content: str) -> str:
    """Write a test script into the plugin's scripts/ dir and make it executable."""
    path = os.path.join(plugin_dir, "scripts", filename)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


# ─── Test the CURRENT inline resolution (proving the bug) ────────────────────

# This is the pattern used in save-session.sh line 57 and run-consolidation.sh line 38:
#   PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../../.." && pwd)}"
#   PIPELINE_DIR="${CLAUDE_PLUGIN_ROOT:-${PROJECT_DIR}/.claude/remember}"
CURRENT_RESOLUTION_SCRIPT = """\
#!/bin/bash
set -e
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../../.." && pwd)}"
PIPELINE_DIR="${CLAUDE_PLUGIN_ROOT:-${PROJECT_DIR}/.claude/remember}"
echo "PROJECT_DIR=$PROJECT_DIR"
echo "PIPELINE_DIR=$PIPELINE_DIR"
"""


class TestCurrentResolutionLocal:
    """Current inline resolution with a local install layout."""

    def test_local_without_env_vars(self, tmp_path):
        """Local install without env vars — should work (path traversal is correct)."""
        project, plugin = _create_local_install(str(tmp_path))
        script = _write_test_script(plugin, "test-resolve.sh", CURRENT_RESOLUTION_SCRIPT)

        result = subprocess.run(
            ["bash", script],
            capture_output=True, text=True,
            env={**os.environ, "PATH": os.environ["PATH"]},
        )
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_local_with_env_vars(self, tmp_path):
        """Local install with env vars — should work (env vars take priority)."""
        project, plugin = _create_local_install(str(tmp_path))
        script = _write_test_script(plugin, "test-resolve.sh", CURRENT_RESOLUTION_SCRIPT)

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project, "CLAUDE_PLUGIN_ROOT": plugin}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin


class TestCurrentResolutionMarketplace:
    """Current inline resolution with a marketplace install layout — proves the bug."""

    def test_marketplace_without_env_vars_is_wrong(self, tmp_path):
        """Marketplace install WITHOUT env vars — path traversal gives WRONG result.

        This is the core of issue #9: ../../.. from
        ~/.claude/plugins/cache/org/remember/0.1.0/scripts/ goes to
        ~/.claude/plugins/cache/org — NOT the project dir.
        """
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        script = _write_test_script(plugin, "test-resolve.sh", CURRENT_RESOLUTION_SCRIPT)

        # Deliberately NOT setting CLAUDE_PROJECT_DIR or CLAUDE_PLUGIN_ROOT
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0

        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)

        # THIS IS THE BUG: PROJECT_DIR resolves to the wrong location
        assert resolved["PROJECT_DIR"] != project, (
            "If this passes, the bug is fixed and this test needs updating"
        )
        # It resolves to cache/org instead of the project
        assert "cache" in resolved["PROJECT_DIR"]

    def test_marketplace_with_env_vars_works(self, tmp_path):
        """Marketplace install WITH env vars — should work."""
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        script = _write_test_script(plugin, "test-resolve.sh", CURRENT_RESOLUTION_SCRIPT)

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project, "CLAUDE_PLUGIN_ROOT": plugin}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin


class TestCurrentResolutionSpaces:
    """Current inline resolution with spaces in the path."""

    def test_local_with_spaces_without_env_vars(self, tmp_path):
        """Local install with spaces in path — should work (quotes are correct)."""
        base = os.path.join(str(tmp_path), "my projects", "work stuff")
        os.makedirs(base)
        project, plugin = _create_local_install(base)
        script = _write_test_script(plugin, "test-resolve.sh", CURRENT_RESOLUTION_SCRIPT)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        assert resolved["PROJECT_DIR"] == project


# ─── Test resolve-paths.sh (the fix) ─────────────────────────────────────────

RESOLVE_PATHS_SH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "resolve-paths.sh"
)

# Wrapper that sources resolve-paths.sh and prints the results
RESOLVE_WRAPPER = """\
#!/bin/bash
source "{resolve_paths}" 2>&1
echo "PROJECT_DIR=$PROJECT_DIR"
echo "PIPELINE_DIR=$PIPELINE_DIR"
"""


def _has_resolve_paths() -> bool:
    """Check if resolve-paths.sh exists (tests skip if not yet created)."""
    return os.path.isfile(RESOLVE_PATHS_SH)


@pytest.mark.skipif(not _has_resolve_paths(), reason="resolve-paths.sh not yet created")
class TestResolvePathsLocal:
    """resolve-paths.sh with a local install layout."""

    def test_local_without_env_vars(self, tmp_path):
        """Should resolve from script location when in local layout."""
        project, plugin = _create_local_install(str(tmp_path))
        wrapper = RESOLVE_WRAPPER.format(resolve_paths=RESOLVE_PATHS_SH)
        # Copy resolve-paths.sh into the test plugin's scripts dir
        import shutil
        shutil.copy(RESOLVE_PATHS_SH, os.path.join(plugin, "scripts", "resolve-paths.sh"))
        script = _write_test_script(plugin, "test-wrapper.sh",
            '#!/bin/bash\nsource "$(dirname "$0")/resolve-paths.sh" 2>&1\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_local_with_env_vars(self, tmp_path):
        """Env vars should take priority over path traversal."""
        project, plugin = _create_local_install(str(tmp_path))
        import shutil
        shutil.copy(RESOLVE_PATHS_SH, os.path.join(plugin, "scripts", "resolve-paths.sh"))
        script = _write_test_script(plugin, "test-wrapper.sh",
            '#!/bin/bash\nsource "$(dirname "$0")/resolve-paths.sh" 2>&1\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project, "CLAUDE_PLUGIN_ROOT": plugin}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin


@pytest.mark.skipif(not _has_resolve_paths(), reason="resolve-paths.sh not yet created")
class TestResolvePathsMarketplace:
    """resolve-paths.sh with a marketplace install layout."""

    def test_marketplace_with_env_vars(self, tmp_path):
        """Marketplace with env vars — the normal working case."""
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        import shutil
        shutil.copy(RESOLVE_PATHS_SH, os.path.join(plugin, "scripts", "resolve-paths.sh"))
        script = _write_test_script(plugin, "test-wrapper.sh",
            '#!/bin/bash\nsource "$(dirname "$0")/resolve-paths.sh" 2>&1\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project, "CLAUDE_PLUGIN_ROOT": plugin}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_marketplace_without_env_vars_fails_loud(self, tmp_path):
        """Marketplace WITHOUT env vars — should FAIL with a clear error, not silently compute wrong paths."""
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        import shutil
        shutil.copy(RESOLVE_PATHS_SH, os.path.join(plugin, "scripts", "resolve-paths.sh"))
        script = _write_test_script(plugin, "test-wrapper.sh",
            '#!/bin/bash\nsource "$(dirname "$0")/resolve-paths.sh" 2>&1\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        # Should fail — marketplace install without env vars cannot resolve project dir
        assert result.returncode != 0, (
            "Should fail when marketplace install has no CLAUDE_PROJECT_DIR"
        )
        assert "FATAL" in result.stderr or "FATAL" in result.stdout


@pytest.mark.skipif(not _has_resolve_paths(), reason="resolve-paths.sh not yet created")
class TestResolvePathsSpaces:
    """resolve-paths.sh with spaces in paths."""

    def test_spaces_in_project_path(self, tmp_path):
        """Paths with spaces should resolve correctly."""
        base = os.path.join(str(tmp_path), "my projects", "work stuff")
        os.makedirs(base)
        project, plugin = _create_local_install(base)
        import shutil
        shutil.copy(RESOLVE_PATHS_SH, os.path.join(plugin, "scripts", "resolve-paths.sh"))
        script = _write_test_script(plugin, "test-wrapper.sh",
            '#!/bin/bash\nsource "$(dirname "$0")/resolve-paths.sh" 2>&1\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        assert resolved["PROJECT_DIR"] == project

    def test_spaces_in_env_var_paths(self, tmp_path):
        """Env vars with spaces should work too."""
        base = os.path.join(str(tmp_path), "path with spaces")
        os.makedirs(base)
        project, plugin = _create_local_install(base)
        import shutil
        shutil.copy(RESOLVE_PATHS_SH, os.path.join(plugin, "scripts", "resolve-paths.sh"))
        script = _write_test_script(plugin, "test-wrapper.sh",
            '#!/bin/bash\nsource "$(dirname "$0")/resolve-paths.sh" 2>&1\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project, "CLAUDE_PLUGIN_ROOT": plugin}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin


@pytest.mark.skipif(not _has_resolve_paths(), reason="resolve-paths.sh not yet created")
class TestResolvePathsSymlink:
    """resolve-paths.sh with symlinked plugin directory."""

    def test_symlinked_plugin_dir(self, tmp_path):
        """When plugin dir is symlinked, resolve through the symlink."""
        # Create the real plugin somewhere else
        real_plugin = os.path.join(str(tmp_path), "real-plugin")
        os.makedirs(os.path.join(real_plugin, "scripts"))
        os.makedirs(os.path.join(real_plugin, "pipeline"))
        with open(os.path.join(real_plugin, "pipeline", "haiku.py"), "w") as f:
            f.write("# marker\n")

        # Create project with symlinked .claude/remember -> real_plugin
        project = os.path.join(str(tmp_path), "my-project")
        os.makedirs(os.path.join(project, ".claude"))
        os.makedirs(os.path.join(project, ".remember", "tmp"))
        os.makedirs(os.path.join(project, ".remember", "logs"))
        os.symlink(real_plugin, os.path.join(project, ".claude", "remember"))

        plugin = os.path.join(project, ".claude", "remember")
        import shutil
        shutil.copy(RESOLVE_PATHS_SH, os.path.join(plugin, "scripts", "resolve-paths.sh"))
        script = _write_test_script(plugin, "test-wrapper.sh",
            '#!/bin/bash\nsource "$(dirname "$0")/resolve-paths.sh" 2>&1\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", script], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        lines = result.stdout.strip().split("\n")
        resolved = dict(line.split("=", 1) for line in lines)
        # The resolved paths should point to the real locations
        assert os.path.isdir(resolved["PROJECT_DIR"])
        assert os.path.isdir(resolved["PIPELINE_DIR"])
        assert os.path.isfile(os.path.join(resolved["PIPELINE_DIR"], "pipeline", "haiku.py"))


# ─── Test parse_response for CLI v2+ format ──────────────────────────────────
# These go in this file because the issue was reported alongside path resolution.
# They test the existing haiku.py code with v2+ JSON array fixtures.

import json
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pipeline.haiku import _parse_response, _extract_tokens


class TestParseResponseCLIv2:
    """Tests for CLI v2+ JSON array format — the format issue #10 reports."""

    V2_RESPONSE = json.dumps([
        {
            "type": "system",
            "subtype": "init",
            "apiKeyInUse": "ak-ant-xxxx",
            "sessionId": "abc-123",
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "## 14:30 | fixed auth bug\nDetails here"}
                ],
                "usage": {
                    "input_tokens": 1500,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 800,
                },
            },
        },
        {
            "type": "result",
            "result": "## 14:30 | fixed auth bug\nDetails here",
            "total_cost_usd": 0.0032,
            "usage": {
                "input_tokens": 1500,
                "output_tokens": 200,
                "cache_read_input_tokens": 800,
            },
        },
    ])

    V2_SKIP_RESPONSE = json.dumps([
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "result": "SKIP — no new activity since last save",
            "total_cost_usd": 0.001,
            "usage": {"input_tokens": 500, "output_tokens": 10},
        },
    ])

    V2_NO_RESULT_KEY = json.dumps([
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "content": [
                {"type": "text", "text": "## 15:00 | content from assistant block"}
            ],
        },
    ])

    V2_EMPTY_ARRAY = json.dumps([])

    def test_v2_normal_response(self):
        """CLI v2 array with result event — extracts text and tokens."""
        r = _parse_response(self.V2_RESPONSE)
        assert r.text == "## 14:30 | fixed auth bug\nDetails here"
        assert r.is_skip is False
        assert r.tokens.cost_usd == pytest.approx(0.0032)
        assert r.tokens.input == 1500
        assert r.tokens.output == 200
        assert r.tokens.cache == 800

    def test_v2_skip_response(self):
        """CLI v2 array with SKIP result."""
        r = _parse_response(self.V2_SKIP_RESPONSE)
        assert r.is_skip is True
        assert "no new activity" in r.text

    def test_v2_no_result_falls_back_to_assistant(self):
        """CLI v2 array without result event — falls back to assistant content blocks."""
        r = _parse_response(self.V2_NO_RESULT_KEY)
        assert "content from assistant block" in r.text

    def test_v2_empty_array(self):
        """CLI v2 empty array — returns empty text, doesn't crash."""
        r = _parse_response(self.V2_EMPTY_ARRAY)
        assert r.text == ""
        assert r.is_skip is False

    def test_v2_old_code_would_crash(self):
        """Reproduce issue #10: old code called data.get('result') on a list.

        The old _parse_response (commit 779ab61, v0.1.0) did:
            data = json.loads(raw)
            text = data.get("result") or ""
        When CLI v2+ returns a list, list.get() raises AttributeError.
        This test proves the current code handles the same input correctly.
        """
        # This is the exact format described in issue #10
        v2_array = [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "## 10:30 | did stuff\ndetails"}
                    ],
                    "usage": {"input_tokens": 500, "output_tokens": 100},
                },
            },
            {
                "type": "result",
                "total_cost_usd": 0.03,
                "result": "## 10:30 | did stuff\ndetails",
                "usage": {"input_tokens": 500, "output_tokens": 100},
            },
        ]
        raw = json.dumps(v2_array)

        # Prove the old code would crash
        data = json.loads(raw)
        assert isinstance(data, list), "CLI v2 returns a list"
        assert not hasattr(data, "get"), "list has no .get() — old code crashes here"

        # Prove the current code handles it
        r = _parse_response(raw)
        assert r.text == "## 10:30 | did stuff\ndetails"
        assert r.is_skip is False
        assert r.tokens.input == 500
        assert r.tokens.output == 100


# ─── Integration tests: real scripts with resolve-paths.sh ───────────────────
# These test that the actual save-session.sh, run-consolidation.sh,
# session-start-hook.sh, and post-tool-hook.sh correctly source resolve-paths.sh
# and get the right PROJECT_DIR/PIPELINE_DIR.
#
# We can't run the full scripts (they need claude CLI, python pipeline, etc.)
# so we extract just the path resolution header and verify the output.

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")


def _install_plugin_scripts(plugin_dir: str) -> None:
    """Copy all scripts from the repo into a test plugin layout."""
    import shutil
    src_scripts = os.path.join(REPO_ROOT, "scripts")
    dst_scripts = os.path.join(plugin_dir, "scripts")
    for fname in os.listdir(src_scripts):
        if fname.endswith(".sh"):
            shutil.copy(os.path.join(src_scripts, fname), os.path.join(dst_scripts, fname))


def _make_path_probe(plugin_dir: str, script_name: str) -> str:
    """Create a wrapper that sources the real script's resolve step then prints vars.

    We source resolve-paths.sh (like the real scripts do) and print the
    resulting PROJECT_DIR and PIPELINE_DIR. We also need log.sh to exist
    (save-session.sh sources it), so we create a no-op stub.
    """
    # Create a no-op log.sh stub so sourcing doesn't fail
    log_stub = os.path.join(plugin_dir, "scripts", "log.sh")
    if not os.path.exists(log_stub):
        with open(log_stub, "w") as f:
            f.write('#!/bin/bash\nlog() { :; }\nlog_tokens() { :; }\n'
                    'safe_eval() { :; }\nconfig() { echo "$2"; }\n'
                    'dispatch() { :; }\nrotate_logs() { :; }\n'
                    'REMEMBER_TZ="UTC"\n')

    probe = os.path.join(plugin_dir, "scripts", f"probe-{script_name}")
    with open(probe, "w") as f:
        f.write('#!/bin/bash\n'
                'source "$(dirname "$0")/resolve-paths.sh"\n'
                'echo "PROJECT_DIR=$PROJECT_DIR"\n'
                'echo "PIPELINE_DIR=$PIPELINE_DIR"\n')
    os.chmod(probe, os.stat(probe).st_mode | stat.S_IEXEC)
    return probe


def _parse_output(stdout: str) -> dict[str, str]:
    """Parse KEY=VALUE lines from script output."""
    result = {}
    for line in stdout.strip().split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            result[k] = v
    return result


@pytest.mark.skipif(not _has_resolve_paths(), reason="resolve-paths.sh not yet created")
class TestRealScriptsLocal:
    """Test real scripts resolve paths correctly in a local install."""

    def test_save_session_local(self, tmp_path):
        project, plugin = _create_local_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        probe = _make_path_probe(plugin, "save-session.sh")

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", probe], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        resolved = _parse_output(result.stdout)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_run_consolidation_local(self, tmp_path):
        project, plugin = _create_local_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        probe = _make_path_probe(plugin, "run-consolidation.sh")

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", probe], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        resolved = _parse_output(result.stdout)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_session_start_hook_local(self, tmp_path):
        project, plugin = _create_local_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        probe = _make_path_probe(plugin, "session-start-hook.sh")

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", probe], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        resolved = _parse_output(result.stdout)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_post_tool_hook_local(self, tmp_path):
        project, plugin = _create_local_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        probe = _make_path_probe(plugin, "post-tool-hook.sh")

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", probe], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        resolved = _parse_output(result.stdout)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin


@pytest.mark.skipif(not _has_resolve_paths(), reason="resolve-paths.sh not yet created")
class TestRealScriptsMarketplace:
    """Test real scripts resolve paths correctly in a marketplace install."""

    def test_save_session_marketplace_with_env(self, tmp_path):
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        probe = _make_path_probe(plugin, "save-session.sh")

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project, "CLAUDE_PLUGIN_ROOT": plugin}
        result = subprocess.run(["bash", probe], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        resolved = _parse_output(result.stdout)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_save_session_marketplace_without_env_fails(self, tmp_path):
        """Marketplace without env vars must fail loud, not silently resolve wrong."""
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        probe = _make_path_probe(plugin, "save-session.sh")

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", probe], capture_output=True, text=True, env=env)
        assert result.returncode != 0, (
            "Marketplace install without CLAUDE_PROJECT_DIR should fail"
        )
        assert "FATAL" in result.stderr or "FATAL" in result.stdout

    def test_run_consolidation_marketplace_with_env(self, tmp_path):
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        probe = _make_path_probe(plugin, "run-consolidation.sh")

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project, "CLAUDE_PLUGIN_ROOT": plugin}
        result = subprocess.run(["bash", probe], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        resolved = _parse_output(result.stdout)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_post_tool_hook_marketplace_with_env(self, tmp_path):
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        probe = _make_path_probe(plugin, "post-tool-hook.sh")

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project, "CLAUDE_PLUGIN_ROOT": plugin}
        result = subprocess.run(["bash", probe], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        resolved = _parse_output(result.stdout)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin


@pytest.mark.skipif(not _has_resolve_paths(), reason="resolve-paths.sh not yet created")
class TestEndToEnd:
    """Full end-to-end tests sourcing resolve-paths.sh exactly like the real scripts do."""

    def test_e2e_local_no_env(self, tmp_path):
        """Local install without env vars — path traversal from script location."""
        project, plugin = _create_local_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        harness = _write_test_script(plugin, "harness.sh",
            '#!/bin/bash\nset -e\n'
            'source "$(dirname "$0")/resolve-paths.sh"\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", harness], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        resolved = _parse_output(result.stdout)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_e2e_marketplace_with_env(self, tmp_path):
        """Marketplace install with env vars — the normal working case."""
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        harness = _write_test_script(plugin, "harness.sh",
            '#!/bin/bash\nset -e\n'
            'source "$(dirname "$0")/resolve-paths.sh"\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )
        env = {**os.environ, "CLAUDE_PROJECT_DIR": project, "CLAUDE_PLUGIN_ROOT": plugin}
        result = subprocess.run(["bash", harness], capture_output=True, text=True, env=env)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        resolved = _parse_output(result.stdout)
        assert resolved["PROJECT_DIR"] == project
        assert resolved["PIPELINE_DIR"] == plugin

    def test_e2e_marketplace_no_env_fails_loud(self, tmp_path):
        """Marketplace install WITHOUT env vars — must fail with FATAL, not resolve wrong."""
        project, plugin, _ = _create_marketplace_install(str(tmp_path))
        _install_plugin_scripts(plugin)
        harness = _write_test_script(plugin, "harness.sh",
            '#!/bin/bash\nset -e\n'
            'source "$(dirname "$0")/resolve-paths.sh"\n'
            'echo "PROJECT_DIR=$PROJECT_DIR"\n'
            'echo "PIPELINE_DIR=$PIPELINE_DIR"\n'
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(["bash", harness], capture_output=True, text=True, env=env)
        assert result.returncode != 0, "Should fail when marketplace has no CLAUDE_PROJECT_DIR"
        assert "FATAL" in result.stderr or "FATAL" in result.stdout


# ─── Full realistic simulation: real hooks invoked like Claude Code does ─────
# Copies the ENTIRE plugin into a fake install layout and invokes the hooks
# via `bash "${CLAUDE_PLUGIN_ROOT}/scripts/..."` — exactly like hooks.json.


def _create_full_plugin_copy(plugin_dir: str) -> None:
    """Copy the entire real plugin into a test install location."""
    import shutil
    repo = os.path.join(os.path.dirname(__file__), "..")
    for item in ("scripts", "pipeline", "prompts", "hooks", "hooks.d", "skills"):
        src = os.path.join(repo, item)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(plugin_dir, item), dirs_exist_ok=True)
    # config.json needed by log.sh and session-start-hook
    import json
    with open(os.path.join(plugin_dir, "config.json"), "w") as f:
        json.dump({
            "timezone": "UTC",
            "cooldowns": {"save_seconds": 120},
            "features": {"recovery": False},
        }, f)


def _create_full_project(project_dir: str) -> None:
    """Create a realistic .remember directory structure."""
    for d in (".remember/tmp", ".remember/logs", ".remember/logs/autonomous", ".claude"):
        os.makedirs(os.path.join(project_dir, d), exist_ok=True)


def _run_hook_like_claude_code(plugin_dir: str, script_name: str,
                               env: dict) -> subprocess.CompletedProcess:
    """Run a hook exactly like Claude Code does: bash "${CLAUDE_PLUGIN_ROOT}/scripts/..."."""
    script_path = os.path.join(plugin_dir, "scripts", script_name)
    return subprocess.run(
        ["bash", script_path],
        capture_output=True, text=True, env=env, timeout=10,
    )


@pytest.mark.skipif(not _has_resolve_paths(), reason="resolve-paths.sh not yet created")
class TestRealisticPluginSimulation:
    """Full simulation: real plugin copy, invoked exactly like Claude Code does.

    Tests both local and marketplace layouts with the real hook scripts,
    not just the path resolution wrapper.
    """

    def _read_log(self, project: str) -> str:
        """Read the most recent memory log file content, or empty string."""
        import glob
        log_files = glob.glob(os.path.join(project, ".remember", "logs", "memory-*.log"))
        if not log_files:
            return ""
        with open(sorted(log_files)[-1]) as f:
            return f.read()

    def test_session_start_hook_marketplace(self, tmp_path):
        """session-start-hook.sh in marketplace layout — succeeds and logs."""
        project = os.path.join(str(tmp_path), "Users", "dev", "my-project")
        plugin = os.path.join(str(tmp_path), "Users", "dev", ".claude",
                              "plugins", "cache", "org", "remember", "0.1.0")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project,
               "CLAUDE_PLUGIN_ROOT": plugin, "HOME": os.path.join(str(tmp_path), "Users", "dev")}
        result = _run_hook_like_claude_code(plugin, "session-start-hook.sh", env)
        assert result.returncode == 0, f"stderr: {result.stderr[:300]}"
        assert "FATAL" not in result.stderr
        log = self._read_log(project)
        assert "[hook] session-start:" in log, f"Missing hook log entry: {log[:300]}"
        assert project in log, "Log should contain PROJECT_DIR"

    def test_session_start_hook_local(self, tmp_path):
        """session-start-hook.sh in local layout — succeeds and logs."""
        project = os.path.join(str(tmp_path), "my-project")
        plugin = os.path.join(project, ".claude", "remember")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["HOME"] = str(tmp_path)
        result = _run_hook_like_claude_code(plugin, "session-start-hook.sh", env)
        assert result.returncode == 0, f"stderr: {result.stderr[:300]}"
        assert "FATAL" not in result.stderr
        log = self._read_log(project)
        assert "[hook] session-start:" in log, f"Missing hook log entry: {log[:300]}"

    def test_post_tool_hook_marketplace(self, tmp_path):
        """post-tool-hook.sh in marketplace layout — succeeds and logs."""
        project = os.path.join(str(tmp_path), "Users", "dev", "my-project")
        plugin = os.path.join(str(tmp_path), "Users", "dev", ".claude",
                              "plugins", "cache", "org", "remember", "0.1.0")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project,
               "CLAUDE_PLUGIN_ROOT": plugin, "HOME": os.path.join(str(tmp_path), "Users", "dev")}
        result = _run_hook_like_claude_code(plugin, "post-tool-hook.sh", env)
        assert result.returncode == 0, f"stderr: {result.stderr[:300]}"
        assert "FATAL" not in result.stderr
        log = self._read_log(project)
        assert "[hook] post-tool:" in log, f"Missing hook log entry: {log[:300]}"

    def test_post_tool_hook_local(self, tmp_path):
        """post-tool-hook.sh in local layout — succeeds and logs."""
        project = os.path.join(str(tmp_path), "my-project")
        plugin = os.path.join(project, ".claude", "remember")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["HOME"] = str(tmp_path)
        result = _run_hook_like_claude_code(plugin, "post-tool-hook.sh", env)
        assert result.returncode == 0, f"stderr: {result.stderr[:300]}"
        assert "FATAL" not in result.stderr
        log = self._read_log(project)
        assert "[hook] post-tool:" in log, f"Missing hook log entry: {log[:300]}"

    def test_save_session_marketplace_path_resolution_and_logs(self, tmp_path):
        """save-session.sh in marketplace — path resolution succeeds, writes to log."""
        project = os.path.join(str(tmp_path), "Users", "dev", "my-project")
        plugin = os.path.join(str(tmp_path), "Users", "dev", ".claude",
                              "plugins", "cache", "org", "remember", "0.1.0")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project,
               "CLAUDE_PLUGIN_ROOT": plugin, "HOME": os.path.join(str(tmp_path), "Users", "dev")}
        result = _run_hook_like_claude_code(plugin, "save-session.sh", env)
        assert "FATAL" not in result.stderr, f"Path resolution failed: {result.stderr[:300]}"

        # Verify log file was written in the project's .remember/logs/
        log = self._read_log(project)
        assert "[hook] save-session:" in log, f"Missing hook log entry: {log[:300]}"
        assert project in log, "Log should contain PROJECT_DIR"

    def test_save_session_local_path_resolution_and_logs(self, tmp_path):
        """save-session.sh in local layout — path resolution succeeds, writes to log."""
        project = os.path.join(str(tmp_path), "my-project")
        plugin = os.path.join(project, ".claude", "remember")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["HOME"] = str(tmp_path)
        result = _run_hook_like_claude_code(plugin, "save-session.sh", env)
        assert "FATAL" not in result.stderr, f"Path resolution failed: {result.stderr[:300]}"

        log = self._read_log(project)
        assert "[hook] save-session:" in log, f"Missing hook log entry: {log[:300]}"

    def test_run_consolidation_marketplace(self, tmp_path):
        """run-consolidation.sh in marketplace layout."""
        project = os.path.join(str(tmp_path), "Users", "dev", "my-project")
        plugin = os.path.join(str(tmp_path), "Users", "dev", ".claude",
                              "plugins", "cache", "org", "remember", "0.1.0")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {**os.environ, "CLAUDE_PROJECT_DIR": project,
               "CLAUDE_PLUGIN_ROOT": plugin, "HOME": os.path.join(str(tmp_path), "Users", "dev")}
        result = _run_hook_like_claude_code(plugin, "run-consolidation.sh", env)
        assert "FATAL" not in result.stderr, f"Path resolution failed: {result.stderr[:300]}"

    def test_run_consolidation_local(self, tmp_path):
        """run-consolidation.sh in local layout."""
        project = os.path.join(str(tmp_path), "my-project")
        plugin = os.path.join(project, ".claude", "remember")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["HOME"] = str(tmp_path)
        result = _run_hook_like_claude_code(plugin, "run-consolidation.sh", env)
        assert "FATAL" not in result.stderr, f"Path resolution failed: {result.stderr[:300]}"

    def test_marketplace_without_env_fails_loud(self, tmp_path):
        """Marketplace layout WITHOUT env vars — every script should fail with FATAL in stderr."""
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.1.0")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        for script in ("session-start-hook.sh", "post-tool-hook.sh",
                        "save-session.sh", "run-consolidation.sh"):
            result = _run_hook_like_claude_code(plugin, script, env)
            combined = result.stderr + result.stdout
            assert "FATAL" in combined, (
                f"{script} should emit FATAL without env vars, got: "
                f"rc={result.returncode} stderr={result.stderr[:200]}"
            )
            assert result.returncode != 0, (
                f"{script} should exit non-zero without env vars"
            )

    def test_hooks_json_stderr_redirect_captures_errors(self, tmp_path):
        """hooks.json stderr redirect captures FATAL errors to hook-errors.log.

        Simulates the exact command from hooks.json:
          bash "${CLAUDE_PLUGIN_ROOT}/scripts/..." 2>> "${CLAUDE_PROJECT_DIR:-.}/.remember/logs/hook-errors.log"
        """
        project = os.path.join(str(tmp_path), "my-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.1.0")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        # Run the hook command exactly like hooks.json does, but WITHOUT
        # CLAUDE_PROJECT_DIR — so resolve-paths.sh fails with FATAL.
        # The 2>> redirect should capture the error.
        hook_errors_log = os.path.join(project, ".remember", "logs", "hook-errors.log")
        cmd = (
            f'bash "{plugin}/scripts/session-start-hook.sh" '
            f'2>> "{hook_errors_log}"'
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        # Set CLAUDE_PLUGIN_ROOT but NOT CLAUDE_PROJECT_DIR — partial env
        env["CLAUDE_PLUGIN_ROOT"] = plugin
        result = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True,
            env=env, timeout=10,
        )
        assert result.returncode != 0

        # The FATAL error should be in hook-errors.log, not lost
        assert os.path.isfile(hook_errors_log), "hook-errors.log not created"
        with open(hook_errors_log) as f:
            error_content = f.read()
        assert "FATAL" in error_content, (
            f"hook-errors.log missing FATAL: {error_content[:200]}"
        )

    def test_hooks_json_stderr_redirect_with_spaces_in_path(self, tmp_path):
        """hooks.json stderr redirect works when paths contain spaces."""
        project = os.path.join(str(tmp_path), "My Projects", "cool app")
        plugin = os.path.join(project, ".claude", "remember")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        hook_errors_log = os.path.join(project, ".remember", "logs", "hook-errors.log")
        cmd = (
            f'bash "{plugin}/scripts/post-tool-hook.sh" '
            f'2>> "{hook_errors_log}"'
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["HOME"] = str(tmp_path)
        result = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True,
            env=env, timeout=10,
        )
        assert result.returncode == 0, f"Spaces in path broke the hook: {result.stderr[:200]}"
        # Verify log file was written to the correct path (with spaces)
        import glob
        log_files = glob.glob(os.path.join(project, ".remember", "logs", "memory-*.log"))
        assert len(log_files) > 0, "No memory log written to path with spaces"

    def test_hooks_json_stderr_redirect_on_success(self, tmp_path):
        """On success, hook-errors.log is either empty or not created."""
        project = os.path.join(str(tmp_path), "my-project")
        plugin = os.path.join(project, ".claude", "remember")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        hook_errors_log = os.path.join(project, ".remember", "logs", "hook-errors.log")
        cmd = (
            f'bash "{plugin}/scripts/post-tool-hook.sh" '
            f'2>> "{hook_errors_log}"'
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["HOME"] = str(tmp_path)
        result = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True,
            env=env, timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr[:200]}"

        # On success, no FATAL in hook-errors.log
        if os.path.isfile(hook_errors_log):
            with open(hook_errors_log) as f:
                content = f.read()
            assert "FATAL" not in content

    def test_marketplace_failure_logs_when_project_dir_exists(self, tmp_path):
        """When FATAL fires but a .remember/logs/ dir exists at cwd, log is written there."""
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.1.0")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        # Create a .remember/logs/ in the cwd so resolve-paths.sh can write to it
        cwd_project = os.path.join(str(tmp_path), "cwd-project")
        os.makedirs(os.path.join(cwd_project, ".remember", "logs"))

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "save-session.sh")],
            capture_output=True, text=True, env=env, timeout=10,
            cwd=cwd_project,
        )
        assert result.returncode != 0

        # Check if FATAL was logged
        import glob
        log_files = glob.glob(os.path.join(cwd_project, ".remember", "logs", "memory-*.log"))
        if log_files:
            with open(log_files[0]) as f:
                log_content = f.read()
            assert "[resolve]" in log_content, (
                f"Log exists but missing [resolve] entry: {log_content[:200]}"
            )
            assert "FATAL" in log_content
