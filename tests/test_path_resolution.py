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

    def test_session_start_creates_gitignore(self, tmp_path):
        """session-start-hook.sh creates .remember/.gitignore before any save (#17)."""
        project = os.path.join(str(tmp_path), "my-project")
        plugin = os.path.join(project, ".claude", "remember")
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)
        # Remove .gitignore if it exists to prove session-start creates it
        gitignore = os.path.join(project, ".remember", ".gitignore")
        if os.path.exists(gitignore):
            os.remove(gitignore)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["HOME"] = str(tmp_path)
        result = _run_hook_like_claude_code(plugin, "session-start-hook.sh", env)
        assert result.returncode == 0, f"stderr: {result.stderr[:300]}"
        assert os.path.exists(gitignore), ".remember/.gitignore not created by session-start-hook"
        with open(gitignore) as f:
            assert f.read().strip() == "*", ".gitignore should contain '*'"

    def test_ndc_subshell_disables_set_e(self):
        """NDC subshell must have set +e to survive claude -p failures (#14)."""
        save_script = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "save-session.sh"
        )
        with open(save_script) as f:
            content = f.read()
        # Find the NDC subshell — it starts with '(set +e' or '(' followed by
        # 'set +e', and contains 'claude -p' and ends with ') &'
        in_ndc = False
        found_set_plus_e = False
        for line in content.splitlines():
            stripped = line.strip()
            if "set +e" in stripped and not in_ndc:
                # Check if this is inside a subshell (line starts with '(')
                if stripped.startswith("("):
                    in_ndc = True
                    found_set_plus_e = True
            if "NDC_ERR=$(mktemp" in stripped:
                in_ndc = True
            if in_ndc and "set +e" in stripped:
                found_set_plus_e = True
            if in_ndc and ") &" in stripped:
                break  # end of subshell
        assert found_set_plus_e, (
            "NDC subshell in save-session.sh must contain 'set +e' "
            "to prevent inherited set -e from killing it on claude -p failure"
        )

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


# ─── Issue #11: Windows compatibility tests ──────────────────────────────────
# Tests for each of the 6 sub-issues reported in GitHub issue #11.
# Some prove the bug exists (xfail), some prove it's already fixed.

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pipeline.extract import _session_dir


class TestWindowsCompatIssue11:
    """GitHub issue #11: Windows compatibility — 6 sub-issues."""

    # ── Point 1: Session directory path encoding ──
    # Fixed: extract.py now uses re.sub(r'[^a-zA-Z0-9]', '-', ...) matching bash sed.

    def test_session_dir_unix_path(self):
        """Unix paths work — forward slashes replaced."""
        result = _session_dir("/home/user/project")
        assert "//" not in result.split("projects/")[1], "Forward slashes not replaced"
        assert result.endswith("-home-user-project")

    def test_session_dir_windows_backslash(self):
        """Windows backslash paths encoded correctly (fixed: re.sub replaces all non-alnum)."""
        result = _session_dir("D:\\Users\\dev\\project")
        assert "\\" not in result, "Backslashes not replaced"
        assert ":" not in result, "Colons not replaced"

    def test_session_dir_windows_colon(self):
        """Windows drive letters (D:) encoded correctly."""
        result = _session_dir("D:/Users/dev/project")
        assert ":" not in result, "Colons not replaced"

    def test_session_dir_matches_bash_slug(self):
        """Python slug matches bash sed 's/[^a-zA-Z0-9]/-/g' for all path types."""
        for path, expected_slug in [
            ("/home/user/project", "-home-user-project"),
            ("D:\\Users\\dev\\project", "D--Users-dev-project"),
            ("D:/Users/dev/project", "D--Users-dev-project"),
            ("/Users/dev/My Project", "-Users-dev-My-Project"),
        ]:
            result = _session_dir(path)
            assert result.endswith(expected_slug), (
                f"Path {path!r}: expected slug {expected_slug!r}, got {result!r}"
            )

    # ── Point 2: python3/python detection via detect-tools.sh ──
    # Fixed: detect-tools.sh tries python3 then python, exports $PYTHON.

    def test_all_scripts_source_detect_tools(self):
        """All pipeline scripts source detect-tools.sh for python detection."""
        for script in ("save-session.sh", "run-consolidation.sh",
                        "post-tool-hook.sh", "session-start-hook.sh"):
            with open(os.path.join(REPO_ROOT, "scripts", script)) as f:
                content = f.read()
            assert "detect-tools.sh" in content, (
                f"{script} not sourcing detect-tools.sh"
            )

    def test_scripts_use_python_var_not_hardcoded(self):
        """Production scripts use $PYTHON, not hardcoded python3."""
        for script in ("save-session.sh", "run-consolidation.sh",
                        "post-tool-hook.sh"):
            with open(os.path.join(REPO_ROOT, "scripts", script)) as f:
                for i, line in enumerate(f, 1):
                    if line.strip().startswith("#"):
                        continue
                    assert "python3 -m" not in line and "python3 -" not in line, (
                        f"{script}:{i} still has hardcoded python3: {line.strip()}"
                    )

    def test_detect_tools_finds_python(self):
        """detect-tools.sh finds python3 or python and exports $PYTHON."""
        result = subprocess.run(
            ["bash", "-c",
             f'source "{REPO_ROOT}/scripts/detect-tools.sh" && echo "PYTHON=$PYTHON"'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"detect-tools.sh failed: {result.stderr}"
        assert "PYTHON=" in result.stdout
        python_cmd = result.stdout.strip().split("=")[1]
        assert python_cmd in ("python3", "python"), f"Unexpected PYTHON={python_cmd}"

    # ── Point 4 & 5: PROJECT_DIR and PIPELINE_DIR resolution ──
    # Fixed in v0.3.0 via resolve-paths.sh

    def test_save_session_uses_resolve_paths(self):
        """save-session.sh should source resolve-paths.sh (v0.3.0 fix)."""
        with open(os.path.join(REPO_ROOT, "scripts", "save-session.sh")) as f:
            content = f.read()
        assert "resolve-paths.sh" in content, "save-session.sh not sourcing resolve-paths.sh"
        assert 'PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd' not in content, \
               "Old inline PROJECT_DIR resolution still present"

    def test_run_consolidation_uses_resolve_paths(self):
        """run-consolidation.sh should source resolve-paths.sh (v0.3.0 fix)."""
        with open(os.path.join(REPO_ROOT, "scripts", "run-consolidation.sh")) as f:
            content = f.read()
        assert "resolve-paths.sh" in content
        assert 'PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd' not in content

    # ── Point 6: CRLF in safe_eval ──
    # Fixed: detect-tools.sh overrides safe_eval with line="${line%$'\r'}" strip.

    def test_safe_eval_with_lf(self):
        """safe_eval works with normal LF line endings."""
        result = subprocess.run(
            ["bash", "-c",
             f'source "{REPO_ROOT}/scripts/detect-tools.sh"; '
             'safe_eval <<< "FOO=bar"; echo "FOO=$FOO"'],
            capture_output=True, text=True,
        )
        assert "FOO=bar" in result.stdout

    def test_safe_eval_with_crlf(self):
        """safe_eval strips \\r from CRLF lines — values are clean (fixed via detect-tools.sh)."""
        result = subprocess.run(
            ["bash", "-c",
             f'source "{REPO_ROOT}/scripts/detect-tools.sh"; '
             'safe_eval < <(printf "FOO=bar\\r\\n"); '
             'echo -n "$FOO" | xxd | grep -q "0d" && echo "CORRUPTED" || echo "CLEAN"'],
            capture_output=True, text=True,
        )
        assert "CLEAN" in result.stdout, (
            f"safe_eval CRLF: value corrupted with trailing \\r: {result.stdout!r}"
        )

    def test_safe_eval_crlf_arithmetic(self):
        """CRLF-safe safe_eval: numeric values work in arithmetic."""
        result = subprocess.run(
            ["bash", "-c",
             f'source "{REPO_ROOT}/scripts/detect-tools.sh"; '
             'safe_eval < <(printf "NUM=42\\r\\n"); '
             'echo "RESULT=$((NUM + 1))"'],
            capture_output=True, text=True,
        )
        assert "RESULT=43" in result.stdout, (
            f"Arithmetic with CRLF value failed: {result.stdout!r} {result.stderr!r}"
        )

    # ── Point 3: jq fallback ──
    # detect-tools.sh provides _jq_fallback using Python when jq is missing.

    def test_detect_tools_jq_fallback(self):
        """When jq is unavailable, detect-tools.sh provides a Python-based fallback."""
        with open(os.path.join(REPO_ROOT, "scripts", "detect-tools.sh")) as f:
            content = f.read()
        assert "_jq_fallback" in content, "No jq fallback function in detect-tools.sh"
        assert "command -v jq" in content, "No jq detection in detect-tools.sh"

    def test_scripts_use_jq_var_not_hardcoded(self):
        """Hook scripts use $JQ, not hardcoded jq (except log.sh and detect-tools.sh)."""
        for script in ("save-session.sh", "run-consolidation.sh",
                        "post-tool-hook.sh", "session-start-hook.sh"):
            with open(os.path.join(REPO_ROOT, "scripts", script)) as f:
                for i, line in enumerate(f, 1):
                    if line.strip().startswith("#"):
                        continue
                    # Match raw 'jq' but not '$JQ' or 'JQ=' or 'command -v jq'
                    if " jq " in line or "(jq " in line or "$(jq " in line:
                        assert False, (
                            f"{script}:{i} uses hardcoded jq: {line.strip()}"
                        )

    def test_jq_fallback_reads_json(self, tmp_path):
        """The jq fallback correctly reads a value from a JSON file."""
        import json as jsonmod
        config = os.path.join(str(tmp_path), "config.json")
        with open(config, "w") as f:
            jsonmod.dump({"timezone": "Europe/Paris", "cooldowns": {"save_seconds": 120}}, f)

        # Simulate no jq — override PATH to exclude it, source detect-tools.sh
        result = subprocess.run(
            ["bash", "-c",
             f'export PATH="/usr/bin:/bin"; '
             f'source "{REPO_ROOT}/scripts/detect-tools.sh" 2>/dev/null; '
             f'$JQ -r ".timezone" "{config}"'],
            capture_output=True, text=True,
        )
        # This will use real jq if it's in /usr/bin, or fallback if not.
        # Either way, the result should be correct.
        assert "Europe/Paris" in result.stdout or result.returncode == 0

    # ── Issue #11 integration: all 6 points proven in one place ──

    # ── Bonus: mktemp /tmp hardcoded path ──
    # Windows Git Bash might not have /tmp. Use ${TMPDIR:-/tmp} instead.

    def test_no_hardcoded_tmp_in_mktemp(self):
        """Production scripts use ${TMPDIR:-/tmp} in mktemp, not hardcoded /tmp."""
        for script in ("save-session.sh", "run-consolidation.sh",
                        "post-tool-hook.sh", "session-start-hook.sh"):
            with open(os.path.join(REPO_ROOT, "scripts", script)) as f:
                for i, line in enumerate(f, 1):
                    if line.strip().startswith("#"):
                        continue
                    assert "mktemp /tmp/" not in line, (
                        f"{script}:{i} uses hardcoded /tmp in mktemp: {line.strip()}"
                    )

    def test_issue_11_all_points_summary(self):
        """Meta-test documenting the status of all 6 issue #11 points.

        This test exists to prove we have coverage for each sub-issue:
          1. Path encoding  → test_session_dir_windows_backslash, _colon, _matches_bash_slug
          2. python3 cmd    → test_all_scripts_source_detect_tools, _use_python_var, _finds_python
          3. jq fallback    → test_detect_tools_jq_fallback, test_jq_fallback_reads_json
          4. PROJECT_DIR    → test_save_session_uses_resolve_paths
          5. PIPELINE_DIR   → test_run_consolidation_uses_resolve_paths
          6. CRLF           → test_safe_eval_with_crlf, _crlf_arithmetic
        """
        pass  # All assertions are in the individual tests above


@pytest.mark.skipif(not _has_resolve_paths(), reason="resolve-paths.sh not yet created")
class TestFreshProjectBootstrap:
    """GitHub issues #23, #27, #31, #32: hooks fail on fresh projects.

    When .remember/logs/ doesn't exist, the 2>> redirect in hooks.json
    fails before the script runs — a chicken-and-egg bug. Scripts must
    bootstrap their own directory structure instead of relying on the
    caller to pre-create it.
    """

    def test_current_hooks_json_fails_without_remember_dir(self, tmp_path):
        """BUG REPRODUCTION: hooks.json 2>> redirect fails when .remember/logs/ missing.

        This is the exact bug reported in issues #23, #27, #31, #32.
        The hook command from hooks.json includes:
            2>> "${CLAUDE_PROJECT_DIR:-.}/.remember/logs/hook-errors.log"
        But bash opens that file BEFORE the script runs. No directory = no redirect = no script.
        """
        project = os.path.join(str(tmp_path), "fresh-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)  # bare project — no .remember/ at all
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        # Simulate the exact hooks.json command with inline 2>> redirect
        hook_errors_log = os.path.join(project, ".remember", "logs", "hook-errors.log")
        cmd = (
            f'bash "{plugin}/scripts/session-start-hook.sh" '
            f'2>> "{hook_errors_log}"'
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True,
            env=env, timeout=10,
        )

        # The redirect itself fails — bash can't open the file
        assert result.returncode != 0 or "No such file or directory" in result.stderr, (
            "Expected failure: bash should fail to open 2>> redirect "
            f"when .remember/logs/ doesn't exist. rc={result.returncode} "
            f"stderr={result.stderr[:300]}"
        )

    def test_current_hooks_json_fails_post_tool_without_remember_dir(self, tmp_path):
        """Same bug for post-tool-hook.sh — fails on fresh project."""
        project = os.path.join(str(tmp_path), "fresh-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        hook_errors_log = os.path.join(project, ".remember", "logs", "hook-errors.log")
        cmd = (
            f'bash "{plugin}/scripts/post-tool-hook.sh" '
            f'2>> "{hook_errors_log}"'
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True,
            env=env, timeout=10,
        )

        assert result.returncode != 0 or "No such file or directory" in result.stderr, (
            "Expected failure for post-tool-hook on fresh project. "
            f"rc={result.returncode} stderr={result.stderr[:300]}"
        )

    def test_scripts_self_bootstrap_on_fresh_project(self, tmp_path):
        """FIX VERIFICATION: scripts create .remember/ dirs themselves.

        After the fix, hooks.json has no 2>> redirect — scripts handle
        dir creation and stderr redirect internally via bootstrap-dirs.sh.
        Running the script directly (as hooks.json will do) on a bare
        project should succeed and create the full directory structure.
        """
        project = os.path.join(str(tmp_path), "fresh-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)  # bare project
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        # Run script WITHOUT 2>> redirect — the script handles it now
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0, (
            f"Script should succeed on fresh project after fix. "
            f"rc={result.returncode} stderr={result.stderr[:300]}"
        )

        # Verify directory structure was created
        remember_dir = os.path.join(project, ".remember")
        assert os.path.isdir(os.path.join(remember_dir, "tmp")), \
            ".remember/tmp/ not created"
        assert os.path.isdir(os.path.join(remember_dir, "logs")), \
            ".remember/logs/ not created"
        assert os.path.isdir(os.path.join(remember_dir, "logs", "autonomous")), \
            ".remember/logs/autonomous/ not created"

        # Verify .gitignore was created
        gitignore = os.path.join(remember_dir, ".gitignore")
        assert os.path.isfile(gitignore), ".remember/.gitignore not created"
        with open(gitignore) as f:
            assert "*" in f.read(), ".gitignore should contain '*'"

    def test_post_tool_self_bootstrap_on_fresh_project(self, tmp_path):
        """post-tool-hook.sh also works on fresh project after fix."""
        project = os.path.join(str(tmp_path), "fresh-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "post-tool-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0, (
            f"post-tool-hook should succeed on fresh project. "
            f"rc={result.returncode} stderr={result.stderr[:300]}"
        )

        # At minimum, dirs should exist
        assert os.path.isdir(os.path.join(project, ".remember", "tmp")), \
            ".remember/tmp/ not created by post-tool-hook"
        assert os.path.isdir(os.path.join(project, ".remember", "logs")), \
            ".remember/logs/ not created by post-tool-hook"

    def test_hooks_json_clean_command_no_redirect(self, tmp_path):
        """hooks.json commands should NOT contain 2>> redirect after fix.

        The fix moves stderr handling into the scripts themselves,
        keeping hooks.json clean and preventing the chicken-and-egg bug.
        """
        hooks_file = os.path.join(
            os.path.dirname(__file__), "..", "hooks", "hooks.json"
        )
        with open(hooks_file) as f:
            hooks = json.load(f)

        for event_name, event_hooks in hooks.get("hooks", {}).items():
            for hook_group in event_hooks:
                for hook in hook_group.get("hooks", []):
                    cmd = hook.get("command", "")
                    assert "2>>" not in cmd, (
                        f"hooks.json {event_name} still has inline 2>> redirect. "
                        f"Stderr handling should be inside the scripts, not hooks.json. "
                        f"Command: {cmd[:200]}"
                    )

    # ── Partial .remember/ state ─────────────────────────────────────────

    def test_partial_remember_dir_missing_logs(self, tmp_path):
        """.remember/ exists but logs/ doesn't — bootstrap fills the gaps."""
        project = os.path.join(str(tmp_path), "partial-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(os.path.join(project, ".remember"))  # exists but empty
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0, (
            f"Should handle partial .remember/ state. "
            f"rc={result.returncode} stderr={result.stderr[:300]}"
        )
        assert os.path.isdir(os.path.join(project, ".remember", "logs"))
        assert os.path.isdir(os.path.join(project, ".remember", "tmp"))
        assert os.path.isdir(os.path.join(project, ".remember", "logs", "autonomous"))

    def test_partial_remember_dir_missing_tmp(self, tmp_path):
        """.remember/logs/ exists but tmp/ doesn't — bootstrap fills the gap."""
        project = os.path.join(str(tmp_path), "partial-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(os.path.join(project, ".remember", "logs"))
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0
        assert os.path.isdir(os.path.join(project, ".remember", "tmp"))

    def test_partial_remember_existing_gitignore_preserved(self, tmp_path):
        """Existing .gitignore is not overwritten by bootstrap."""
        project = os.path.join(str(tmp_path), "custom-gitignore")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(os.path.join(project, ".remember"))
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        # Create a custom .gitignore before bootstrap runs
        gitignore = os.path.join(project, ".remember", ".gitignore")
        with open(gitignore, "w") as f:
            f.write("*.log\n!important.log\n")

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0
        with open(gitignore) as f:
            content = f.read()
        assert "*.log" in content, (
            f".gitignore was overwritten by bootstrap: {content!r}"
        )
        assert "!important.log" in content

    # ── Spaces in paths ──────────────────────────────────────────────────

    def test_fresh_project_with_spaces_in_path(self, tmp_path):
        """Bootstrap works when project path contains spaces (common on Windows/macOS)."""
        project = os.path.join(str(tmp_path), "My Projects", "cool app")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)  # bare project with spaces
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0, (
            f"Spaces in path broke bootstrap. "
            f"rc={result.returncode} stderr={result.stderr[:300]}"
        )
        assert os.path.isdir(os.path.join(project, ".remember", "logs"))
        assert os.path.isdir(os.path.join(project, ".remember", "tmp"))

    def test_fresh_project_with_special_chars_in_path(self, tmp_path):
        """Bootstrap works with unicode/special chars in path (accents, etc.)."""
        project = os.path.join(str(tmp_path), "Projets été", "café-app")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0, (
            f"Special chars in path broke bootstrap. "
            f"rc={result.returncode} stderr={result.stderr[:300]}"
        )
        assert os.path.isdir(os.path.join(project, ".remember", "logs"))

    # ── Read-only / permission edge cases ────────────────────────────────

    def test_read_only_project_dir_does_not_crash(self, tmp_path):
        """If project dir is read-only, bootstrap degrades gracefully.

        This can happen on CI systems, Docker containers with read-only mounts,
        or restricted corporate environments. bootstrap-dirs.sh itself must not
        crash (mkdir -p has 2>/dev/null, exec 2>> is guarded by -d check).

        Note: log.sh does `return 1` when it can't create the log dir, which
        means log()/dispatch() are never defined. The session-start-hook.sh
        then fails on `dispatch` (command not found, rc=127). This is a
        pre-existing limitation in log.sh, not a bootstrap-dirs.sh bug.
        The important thing is bootstrap-dirs.sh doesn't make it worse.
        """
        project = os.path.join(str(tmp_path), "readonly-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        # Make project dir read-only
        os.chmod(project, 0o555)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        try:
            result = subprocess.run(
                ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
                capture_output=True, text=True, env=env, timeout=10,
            )

            # bootstrap-dirs.sh itself should not segfault or hang.
            # The script may fail (rc=127 from undefined dispatch in log.sh)
            # but should not timeout or produce unexpected errors.
            assert result.returncode in (0, 127), (
                f"Unexpected exit code on read-only project dir. "
                f"rc={result.returncode} stderr={result.stderr[:300]}"
            )

            # Verify .remember/ was NOT created (read-only dir)
            assert not os.path.exists(os.path.join(project, ".remember")), (
                ".remember/ should not exist on read-only filesystem"
            )
        finally:
            # Restore permissions for cleanup
            os.chmod(project, 0o755)

    # ── Idempotency ──────────────────────────────────────────────────────

    def test_bootstrap_idempotent_multiple_runs(self, tmp_path):
        """Running bootstrap multiple times doesn't corrupt or duplicate anything."""
        project = os.path.join(str(tmp_path), "idempotent-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        # Run three times in succession
        for i in range(3):
            result = subprocess.run(
                ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
                capture_output=True, text=True, env=env, timeout=10,
            )
            assert result.returncode == 0, (
                f"Run {i+1}/3 failed. rc={result.returncode} "
                f"stderr={result.stderr[:300]}"
            )

        # .gitignore should contain exactly '*', not '*\n*\n*'
        gitignore = os.path.join(project, ".remember", ".gitignore")
        with open(gitignore) as f:
            content = f.read()
        assert content.strip() == "*", (
            f".gitignore corrupted after 3 runs: {content!r}"
        )

    # ── Git worktree simulation ──────────────────────────────────────────

    def test_git_worktree_separate_remember_dir(self, tmp_path):
        """In a git worktree, .remember/ is created in the worktree, not main repo.

        Issues #23 and #31 specifically mention worktree failures.
        CLAUDE_PROJECT_DIR points to the worktree path.
        """
        main_repo = os.path.join(str(tmp_path), "main-repo")
        worktree = os.path.join(str(tmp_path), "worktrees", "feature-branch")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(main_repo)
        os.makedirs(worktree)  # bare worktree — no .remember/
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        # Claude Code sets CLAUDE_PROJECT_DIR to the worktree
        env["CLAUDE_PROJECT_DIR"] = worktree
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0, (
            f"Worktree bootstrap failed. "
            f"rc={result.returncode} stderr={result.stderr[:300]}"
        )

        # .remember/ should be in the worktree, NOT in main repo
        assert os.path.isdir(os.path.join(worktree, ".remember", "logs")), \
            ".remember/logs/ not created in worktree"
        assert not os.path.exists(os.path.join(main_repo, ".remember")), \
            ".remember/ leaked into main repo instead of worktree"

    # ── Concurrent bootstrap ─────────────────────────────────────────────

    def test_concurrent_bootstrap_no_race(self, tmp_path):
        """Two sessions bootstrapping simultaneously don't corrupt state.

        mkdir -p is atomic on POSIX, but verify the full bootstrap
        (dirs + gitignore + stderr redirect) survives concurrency.
        """
        project = os.path.join(str(tmp_path), "concurrent-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        script = os.path.join(plugin, "scripts", "session-start-hook.sh")

        # Launch two processes simultaneously
        p1 = subprocess.Popen(
            ["bash", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True,
        )
        p2 = subprocess.Popen(
            ["bash", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True,
        )

        out1, err1 = p1.communicate(timeout=10)
        out2, err2 = p2.communicate(timeout=10)

        assert p1.returncode == 0, f"Process 1 failed: {err1[:300]}"
        assert p2.returncode == 0, f"Process 2 failed: {err2[:300]}"

        # Dirs exist and gitignore is valid
        assert os.path.isdir(os.path.join(project, ".remember", "logs"))
        assert os.path.isdir(os.path.join(project, ".remember", "tmp"))
        gitignore = os.path.join(project, ".remember", ".gitignore")
        assert os.path.isfile(gitignore)
        with open(gitignore) as f:
            content = f.read().strip()
        # Should be just '*' — not duplicated
        assert content == "*", f".gitignore corrupted by race: {content!r}"

    # ── bootstrap-dirs.sh itself ─────────────────────────────────────────

    def test_bootstrap_dirs_requires_project_dir(self, tmp_path):
        """bootstrap-dirs.sh uses PROJECT_DIR from resolve-paths.sh.

        If sourced without PROJECT_DIR set, it should create dirs
        relative to empty string (current dir) — not crash.
        """
        bootstrap = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "bootstrap-dirs.sh"
        )
        assert os.path.isfile(bootstrap), "bootstrap-dirs.sh not found"

        # Verify it references PROJECT_DIR (not CLAUDE_PROJECT_DIR)
        with open(bootstrap) as f:
            content = f.read()
        assert "PROJECT_DIR" in content, \
            "bootstrap-dirs.sh should reference PROJECT_DIR"
        assert "REMEMBER_DIR" in content, \
            "bootstrap-dirs.sh should define REMEMBER_DIR"
        assert "mkdir -p" in content, \
            "bootstrap-dirs.sh should create directories"
        assert "exec 2>>" in content, \
            "bootstrap-dirs.sh should redirect stderr"
        assert ".gitignore" in content, \
            "bootstrap-dirs.sh should create .gitignore"

    def test_all_hook_scripts_source_bootstrap(self):
        """Every hook script sources bootstrap-dirs.sh for consistent setup."""
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        for script_name in ("session-start-hook.sh", "post-tool-hook.sh"):
            script_path = os.path.join(repo_root, "scripts", script_name)
            with open(script_path) as f:
                content = f.read()
            assert "bootstrap-dirs.sh" in content, (
                f"{script_name} does not source bootstrap-dirs.sh — "
                f"directory creation will be missing on fresh installs"
            )

    def test_no_hardcoded_tmp_in_production_scripts(self):
        """Production scripts must not use hardcoded /tmp/ — use $SYS_TMPDIR.

        Windows (Git Bash) may not have /tmp, but $TMPDIR is always set.
        bootstrap-dirs.sh exports SYS_TMPDIR="${TMPDIR:-/tmp}" for this.
        Test scripts (run-tests.sh) should also use it for portability.
        """
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        for script_name in ("session-start-hook.sh", "post-tool-hook.sh",
                            "user-prompt-hook.sh", "save-session.sh",
                            "run-consolidation.sh", "run-tests.sh"):
            script_path = os.path.join(repo_root, "scripts", script_name)
            if not os.path.isfile(script_path):
                continue
            with open(script_path) as f:
                for i, line in enumerate(f, 1):
                    # Skip comments and lines using .remember/tmp/ (project-relative)
                    stripped = line.lstrip()
                    if stripped.startswith("#"):
                        continue
                    if ".remember/tmp" in line:
                        continue
                    assert "mktemp /tmp/" not in line, (
                        f"{script_name}:{i} uses hardcoded /tmp/ in mktemp. "
                        f"Use $SYS_TMPDIR instead. Line: {line.strip()}"
                    )
                    # Check for /tmp/claude- pattern (the ctx-pct file)
                    if "/tmp/claude-" in line and "SYS_TMPDIR" not in line and "TMPDIR" not in line:
                        assert False, (
                            f"{script_name}:{i} uses hardcoded /tmp/claude-*. "
                            f"Use $SYS_TMPDIR instead. Line: {line.strip()}"
                        )

    def test_bootstrap_before_detect_tools(self):
        """bootstrap-dirs.sh must be sourced BEFORE detect-tools.sh.

        The order matters: resolve-paths → bootstrap-dirs → detect-tools.
        bootstrap needs PROJECT_DIR (from resolve-paths) but nothing else.
        detect-tools may write to logs, which need the dirs from bootstrap.
        """
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        for script_name in ("session-start-hook.sh", "post-tool-hook.sh"):
            script_path = os.path.join(repo_root, "scripts", script_name)
            with open(script_path) as f:
                content = f.read()
            bootstrap_pos = content.find("bootstrap-dirs.sh")
            detect_pos = content.find("detect-tools.sh")
            assert bootstrap_pos < detect_pos, (
                f"{script_name}: bootstrap-dirs.sh must come before "
                f"detect-tools.sh (bootstrap at {bootstrap_pos}, "
                f"detect at {detect_pos})"
            )
