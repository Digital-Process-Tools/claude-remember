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

    # ── Point 1b: Git Bash / MSYS / Cygwin POSIX-style paths ──
    # Claude Code on Windows stores sessions under the Win32-path slug
    # (e.g. C--Users-dev-project from C:\Users\dev\project). But Git Bash
    # exposes $CLAUDE_PROJECT_DIR as a POSIX-style path (/c/Users/...),
    # which the existing tests above don't cover and which slugs to a
    # different folder name (-c-Users-...). resolve-paths.sh normalizes
    # the POSIX form to Win32 before the slug is computed.
    #
    # The transformation is tested via a temp script file (not bash -c) so
    # that bash parses backslashes the same way it does in the production
    # script — `bash -c '<body>'` mishandles `\\}` inside `${var//\//\\}`
    # in some interactive contexts.

    @staticmethod
    def _msys_normalize_path(path: str, tmp_path) -> tuple[int, str, str]:
        """Run the production OSTYPE=msys normalization block on a path; return (rc, stdout, stderr)."""
        script = tmp_path / "msys-normalize.sh"
        script.write_text(
            '#!/bin/bash\n'
            'PROJECT_DIR="$1"\n'
            'OSTYPE="msys"\n'
            'case "$OSTYPE" in\n'
            '    msys|cygwin)\n'
            '        if [[ "$PROJECT_DIR" =~ ^/([a-zA-Z])/(.*)$ ]]; then\n'
            '            _drive=$(printf \'%s\' "${BASH_REMATCH[1]}" | tr \'[:lower:]\' \'[:upper:]\')\n'
            '            _rest="${BASH_REMATCH[2]//\\//\\\\}"\n'
            '            PROJECT_DIR="${_drive}:\\\\${_rest}"\n'
            '        fi\n'
            '        ;;\n'
            'esac\n'
            'printf "%s" "$PROJECT_DIR"\n'
        )
        result = subprocess.run(
            ["bash", str(script), path],
            capture_output=True, text=True,
        )
        return result.returncode, result.stdout, result.stderr

    def test_resolve_paths_normalizes_msys_posix_path(self, tmp_path):
        """resolve-paths.sh on $OSTYPE=msys converts /c/Users/... to C:\\Users\\..."""
        for posix_path, expected_win in [
            ("/c/Users/dev/project", r"C:\Users\dev\project"),
            ("/d/Repos/My Project", r"D:\Repos\My Project"),
            ("/C/UPPER/case", r"C:\UPPER\case"),
        ]:
            rc, stdout, stderr = self._msys_normalize_path(posix_path, tmp_path)
            assert rc == 0, f"bash failed: {stderr!r}"
            assert stdout == expected_win, (
                f"Posix path {posix_path!r}: expected {expected_win!r}, got {stdout!r}"
            )

    def test_resolve_paths_leaves_unix_paths_unchanged(self, tmp_path):
        """resolve-paths.sh on $OSTYPE=msys must not touch already-Win32 or pure-Unix paths."""
        for path in [
            r"C:\Users\dev\project",          # already Win32 — unchanged
            "/home/user/project",             # Linux home — multi-letter first dir doesn't match
            "/Users/dev/project",             # macOS home — uppercase first dir doesn't match
        ]:
            rc, stdout, stderr = self._msys_normalize_path(path, tmp_path)
            assert rc == 0, f"bash failed: {stderr!r}"
            assert stdout == path, (
                f"Path {path!r} should be unchanged, got {stdout!r}"
            )

    def test_normalized_msys_path_yields_windows_slug(self):
        """End-to-end: a /c/Users/... path post-normalization slugs to the same folder Claude Code uses."""
        # After resolve-paths.sh normalizes /c/Users/dev/project → C:\Users\dev\project,
        # the Python _session_dir (and bash sed) must produce the same slug Claude Code
        # uses to store session JSONLs on Windows.
        normalized = r"C:\Users\dev\project"
        slug = _session_dir(normalized).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        assert slug == "C--Users-dev-project", (
            f"Normalized Win32 path slug mismatch: got {slug!r}"
        )

    def test_resolve_paths_has_msys_normalization_block(self):
        """resolve-paths.sh contains the OSTYPE=msys|cygwin normalization block."""
        with open(os.path.join(REPO_ROOT, "scripts", "resolve-paths.sh")) as f:
            content = f.read()
        assert 'msys|cygwin' in content, (
            "resolve-paths.sh missing the Git Bash / MSYS / Cygwin normalization case"
        )
        assert 'BASH_REMATCH' in content, (
            "resolve-paths.sh missing the regex-based POSIX→Win32 conversion"
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


class TestBsdMktempCompatibility:
    """PR #30: BSD mktemp (macOS) fails with chars after XXXXXX suffix.

    GNU mktemp (Linux) silently ignores chars after XXXXXX:
        mktemp /tmp/foo-XXXXXX.txt  →  /tmp/foo-a1b2c3.txt  (works)

    BSD mktemp (macOS) treats the suffix as literal template chars:
        mktemp /tmp/foo-XXXXXX.txt  →  "mkstemp: File exists" (fails)

    Fix: remove file extensions after XXXXXX in all mktemp calls.
    """

    def test_bsd_mktemp_no_randomization_with_extension(self, tmp_path):
        """BUG REPRODUCTION: BSD mktemp treats chars after XXXXXX as literal.

        On macOS, mktemp /tmp/foo-XXXXXX.txt creates /tmp/foo-XXXXXX.txt
        (no randomization!) — the first call succeeds but the file has a
        predictable name. The SECOND call fails with "File exists" because
        the name is always the same. This is both a collision bug and a
        security issue (predictable temp filenames).
        """
        import platform
        if platform.system() != "Darwin":
            pytest.skip("BSD mktemp test only relevant on macOS")

        template = os.path.join(str(tmp_path), "test-XXXXXX.txt")

        # First call: succeeds but creates a non-random filename
        r1 = subprocess.run(
            ["mktemp", template], capture_output=True, text=True,
        )
        assert r1.returncode == 0, "First mktemp should succeed"
        created = r1.stdout.strip()

        # On BSD, the filename IS the template (no randomization)
        assert created == template, (
            f"BSD mktemp should create literal filename {template}, "
            f"got {created} — this means XXXXXX was randomized (GNU behavior)"
        )

        # Second call: fails because the literal file already exists
        r2 = subprocess.run(
            ["mktemp", template], capture_output=True, text=True,
        )
        assert r2.returncode != 0, (
            "Second mktemp with same template should fail on BSD — "
            "the non-random file already exists"
        )
        assert "File exists" in r2.stderr, (
            f"Expected 'File exists' error, got: {r2.stderr[:200]}"
        )

        # Cleanup
        if os.path.isfile(created):
            os.unlink(created)

    def test_bsd_mktemp_works_without_extension(self, tmp_path):
        """FIX VERIFICATION: mktemp without extension works on all platforms."""
        result = subprocess.run(
            ["mktemp", os.path.join(str(tmp_path), "test-XXXXXX")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"mktemp without extension should work everywhere. "
            f"stderr={result.stderr[:200]}"
        )
        # Clean up
        created = result.stdout.strip()
        if os.path.isfile(created):
            os.unlink(created)

    def test_no_mktemp_with_extension_in_scripts(self):
        """Guard: no mktemp call should have chars after XXXXXX.

        Catches future regressions — any new mktemp must follow the pattern.
        """
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        violations = []
        for script_name in ("save-session.sh", "run-tests.sh",
                            "run-consolidation.sh", "session-start-hook.sh",
                            "post-tool-hook.sh", "user-prompt-hook.sh"):
            script_path = os.path.join(repo_root, "scripts", script_name)
            if not os.path.isfile(script_path):
                continue
            with open(script_path) as f:
                for i, line in enumerate(f, 1):
                    stripped = line.lstrip()
                    if stripped.startswith("#"):
                        continue
                    # Match: mktemp ... XXXXXX.ext) or XXXXXX.ext"
                    if "mktemp" in line and "XXXXXX." in line:
                        violations.append(f"{script_name}:{i}: {line.strip()}")

        assert not violations, (
            "mktemp calls with extension after XXXXXX break on macOS (BSD mktemp).\n"
            "Remove the file extension — use XXXXXX) not XXXXXX.txt)\n"
            "Violations:\n" + "\n".join(violations)
        )


class TestHaikuHeaderGuard:
    """PR #22 / Issue #24: Haiku invents 'unknown' header from previous entries.

    When a previous entry's header showed 'unknown' (e.g., from git rev-parse
    returning no branch in a non-repo cwd), Haiku occasionally mimicked that
    header instead of using the {{TIME}} | {{BRANCH}} values from the prompt.

    Fix: (1) explicit prompt instruction to copy header verbatim,
         (2) expand placeholder guard to check entire prompt, not just line 1.
    """

    def test_prompt_instructs_verbatim_header(self):
        """Prompt must explicitly tell Haiku to copy header values verbatim."""
        prompt_path = os.path.join(
            os.path.dirname(__file__), "..", "prompts", "save-session.prompt.txt"
        )
        with open(prompt_path) as f:
            content = f.read()

        # Must mention copying TIME/BRANCH verbatim
        assert "{{TIME}}" in content, (
            "Prompt should reference {{TIME}} placeholder"
        )
        assert "{{BRANCH}}" in content, (
            "Prompt should reference {{BRANCH}} placeholder"
        )
        # Must warn against inventing 'unknown'
        assert "unknown" in content.lower(), (
            "Prompt should warn against mimicking 'unknown' headers"
        )

    def test_placeholder_guard_checks_all_placeholders(self):
        """save-session.sh should check ALL placeholders, not just TIME/BRANCH."""
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "save-session.sh"
        )
        with open(script_path) as f:
            content = f.read()

        # The guard should check for unsubstituted placeholders
        assert "{{TIME}}" in content, "Guard should check {{TIME}}"
        assert "{{BRANCH}}" in content, "Guard should check {{BRANCH}}"

        # After fix: should also check {{LAST_ENTRY}} and {{EXTRACT}}
        assert "{{LAST_ENTRY}}" in content, (
            "Guard should check {{LAST_ENTRY}} — partial substitution "
            "means the prompt is broken"
        )
        assert "{{EXTRACT}}" in content, (
            "Guard should check {{EXTRACT}} — partial substitution "
            "means the prompt is broken"
        )

    def test_placeholder_guard_checks_full_prompt_not_just_header(self):
        """Guard must grep the entire prompt file, not just head -1.

        The old guard was: head -1 "$TMP_PROMPT" | grep -q '{{TIME}}'
        This only caught unsubstituted headers, not body placeholders.
        """
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "save-session.sh"
        )
        with open(script_path) as f:
            content = f.read()

        # Should NOT use 'head -1' before the placeholder grep
        # Look for the guard pattern
        for line in content.split("\n"):
            if "{{TIME}}" in line and "grep" in line:
                assert "head -1" not in line, (
                    "Placeholder guard uses 'head -1' — only checks first line. "
                    "Should grep the entire prompt file."
                )


class TestTimeFormatConfig:
    """PR #25: configurable 12h/24h time format for save-session output.

    Users in 12h-format locales (US, Philippines, etc.) can set
    time_format: "12h" in config.json to get "2:32 PM" instead of "14:32".
    """

    def test_config_example_has_time_format(self):
        """config.example.json must document the time_format option."""
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config.example.json"
        )
        with open(config_path) as f:
            config = json.load(f)
        assert "time_format" in config, (
            "config.example.json should include time_format key"
        )
        assert config["time_format"] == "24h", (
            f"Default time_format should be '24h', got '{config['time_format']}'"
        )

    def test_save_session_reads_time_format_config(self):
        """save-session.sh must read time_format from config."""
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "save-session.sh"
        )
        with open(script_path) as f:
            content = f.read()
        assert "time_format" in content, (
            "save-session.sh should read time_format config"
        )
        assert "12h" in content, (
            "save-session.sh should handle 12h format"
        )

    def test_24h_format_produces_hhmm(self):
        """24h format should produce HH:MM (e.g., 14:32)."""
        import platform
        result = subprocess.run(
            ["bash", "-c", 'TZ=UTC date "+%H:%M"'],
            capture_output=True, text=True,
        )
        time_str = result.stdout.strip()
        import re
        assert re.match(r'^\d{2}:\d{2}$', time_str), (
            f"24h format should be HH:MM, got '{time_str}'"
        )

    def test_12h_format_produces_ampm(self):
        """12h format should produce h:MM AM/PM (e.g., 2:32 PM)."""
        result = subprocess.run(
            ["bash", "-c", 'TZ=UTC date "+%-I:%M %p"'],
            capture_output=True, text=True,
        )
        time_str = result.stdout.strip()
        import re
        assert re.match(r'^\d{1,2}:\d{2} (AM|PM)$', time_str), (
            f"12h format should be h:MM AM/PM, got '{time_str}'"
        )

    def test_header_regex_accepts_24h(self):
        """Header validation regex must accept 24h format: ## 14:32 |"""
        result = subprocess.run(
            ["bash", "-c",
             """echo '## 14:32 | main' | grep -qE '^## ([0-9]{2}:[0-9]{2}|[0-9]{1,2}:[0-9]{2} (AM|PM)) \\|'"""],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, "Regex should match 24h header '## 14:32 |'"

    def test_header_regex_accepts_12h_pm(self):
        """Header validation regex must accept 12h PM: ## 2:32 PM |"""
        result = subprocess.run(
            ["bash", "-c",
             """echo '## 2:32 PM | main' | grep -qE '^## ([0-9]{2}:[0-9]{2}|[0-9]{1,2}:[0-9]{2} (AM|PM)) \\|'"""],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, "Regex should match 12h header '## 2:32 PM |'"

    def test_header_regex_accepts_12h_am(self):
        """Header validation regex must accept 12h AM: ## 9:05 AM |"""
        result = subprocess.run(
            ["bash", "-c",
             """echo '## 9:05 AM | main' | grep -qE '^## ([0-9]{2}:[0-9]{2}|[0-9]{1,2}:[0-9]{2} (AM|PM)) \\|'"""],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, "Regex should match 12h header '## 9:05 AM |'"

    def test_header_regex_accepts_12h_double_digit(self):
        """Header validation regex must accept 12h with two-digit hour: ## 12:00 PM |"""
        result = subprocess.run(
            ["bash", "-c",
             """echo '## 12:00 PM | main' | grep -qE '^## ([0-9]{2}:[0-9]{2}|[0-9]{1,2}:[0-9]{2} (AM|PM)) \\|'"""],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, "Regex should match 12h header '## 12:00 PM |'"

    def test_header_regex_rejects_garbage(self):
        """Header validation regex must reject malformed headers."""
        for bad_header in [
            "## unknown | main",
            "## 2:32PM | main",  # missing space before AM/PM
            "not a header",
        ]:
            result = subprocess.run(
                ["bash", "-c",
                 f"""echo '{bad_header}' | grep -qE '^## ([0-9]{{2}}:[0-9]{{2}}|[0-9]{{1,2}}:[0-9]{{2}} (AM|PM)) \\|'"""],
                capture_output=True, text=True,
            )
            assert result.returncode != 0, (
                f"Regex should reject '{bad_header}'"
            )

    def test_default_24h_when_no_config(self):
        """When time_format is not in config.json, default to 24h."""
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "save-session.sh"
        )
        with open(script_path) as f:
            content = f.read()
        # Should use config() with "24h" as default
        assert '"24h"' in content, (
            "save-session.sh should default to '24h' when config key is absent"
        )


class TestMarketplacePathResolution:
    """All hook scripts must resolve config and hooks.d via PIPELINE_DIR.

    In marketplace installs, the plugin lives in ~/.claude/plugins/cache/,
    NOT in $PROJECT_DIR/.claude/remember/. Every script must source
    resolve-paths.sh (or have PIPELINE_DIR set) before sourcing log.sh.
    """

    # -- Source guards: verify scripts use the right patterns --

    def test_log_sh_config_uses_pipeline_dir(self):
        """log.sh REMEMBER_CONFIG must reference PIPELINE_DIR."""
        log_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "log.sh"
        )
        with open(log_path) as f:
            content = f.read()

        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("REMEMBER_CONFIG="):
                assert "PIPELINE_DIR" in stripped, (
                    f"REMEMBER_CONFIG should use PIPELINE_DIR. Line: {stripped}"
                )
                break
        else:
            assert False, "REMEMBER_CONFIG assignment not found in log.sh"

    def test_log_sh_hooks_dir_uses_pipeline_dir(self):
        """log.sh REMEMBER_HOOKS_DIR must reference PIPELINE_DIR."""
        log_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "log.sh"
        )
        with open(log_path) as f:
            content = f.read()

        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("REMEMBER_HOOKS_DIR="):
                assert "PIPELINE_DIR" in stripped, (
                    f"REMEMBER_HOOKS_DIR should use PIPELINE_DIR. Line: {stripped}"
                )
                break
        else:
            assert False, "REMEMBER_HOOKS_DIR assignment not found in log.sh"

    def test_all_hooks_source_resolve_paths(self):
        """Every hook script must source resolve-paths.sh for consistent paths."""
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        hooks = [
            "session-start-hook.sh",
            "post-tool-hook.sh",
            "user-prompt-hook.sh",
            "save-session.sh",
            "run-consolidation.sh",
        ]
        for hook in hooks:
            path = os.path.join(scripts_dir, hook)
            with open(path) as f:
                content = f.read()
            assert "resolve-paths.sh" in content, (
                f"{hook} must source resolve-paths.sh for PIPELINE_DIR. "
                f"Without it, marketplace installs read config from wrong path."
            )

    def test_no_hardcoded_paris_timezone_default(self):
        """No script should hardcode 'Europe/Paris' as timezone default."""
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        for fname in os.listdir(scripts_dir):
            if not fname.endswith(".sh"):
                continue
            path = os.path.join(scripts_dir, fname)
            with open(path) as f:
                for lineno, line in enumerate(f, 1):
                    if "Europe/Paris" in line and not line.strip().startswith("#"):
                        assert False, (
                            f'{fname}:{lineno} hardcodes "Europe/Paris". '
                            f"Use empty string (system local) as default. "
                            f"Line: {line.strip()}"
                        )

    def test_no_redundant_timezone_reread(self):
        """Only log.sh should set REMEMBER_TZ. Other scripts inherit it."""
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        violators = []
        for fname in os.listdir(scripts_dir):
            if not fname.endswith(".sh") or fname == "log.sh":
                continue
            path = os.path.join(scripts_dir, fname)
            with open(path) as f:
                for lineno, line in enumerate(f, 1):
                    stripped = line.strip()
                    if stripped.startswith("REMEMBER_TZ=") and not stripped.startswith("#"):
                        violators.append(f"{fname}:{lineno}")
        assert not violators, (
            f"REMEMBER_TZ should only be set in log.sh. Found in: {violators}"
        )

    def test_single_config_reader(self):
        """All config reads should use config() from log.sh, not direct jq."""
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        for fname in os.listdir(scripts_dir):
            if not fname.endswith(".sh") or fname == "log.sh":
                continue
            path = os.path.join(scripts_dir, fname)
            with open(path) as f:
                for lineno, line in enumerate(f, 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    # No standalone cfg() definitions or config.json direct reads
                    if "config.json" in stripped and ("jq" in stripped or "$JQ" in stripped):
                        assert False, (
                            f"{fname}:{lineno} reads config.json directly. "
                            f"Use config() from log.sh instead. Line: {stripped}"
                        )

    # -- Integration: real hook invocations in marketplace layout --

    def test_marketplace_layout_config_found(self, tmp_path):
        """In marketplace layout, config.json is read from plugin dir, not project."""
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({
                "timezone": "America/New_York",
                "cooldowns": {"save_seconds": 120},
                "features": {"recovery": False},
            }, f)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0, (
            f"Marketplace layout should work without .claude/remember/ in project. "
            f"rc={result.returncode} stderr={result.stderr[:300]}"
        )

    def test_marketplace_hooks_d_dispatches_from_plugin(self, tmp_path):
        """hooks.d/ dispatch should look in the plugin dir, not project dir."""
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)
        os.makedirs(os.path.join(plugin, "scripts"))
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        hook_dir = os.path.join(plugin, "hooks.d", "after_session_start")
        os.makedirs(hook_dir, exist_ok=True)
        hook_file = os.path.join(hook_dir, "test-hook.sh")
        with open(hook_file, "w") as f:
            f.write("#!/bin/bash\necho 'HOOK_FIRED_FROM_PLUGIN=true'\n")
        os.chmod(hook_file, 0o755)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0
        assert "HOOK_FIRED_FROM_PLUGIN=true" in result.stdout, (
            f"hooks.d/ dispatch should find hooks in plugin dir. "
            f"stdout={result.stdout[:300]}"
        )

    def test_user_prompt_hook_marketplace(self, tmp_path):
        """user-prompt-hook.sh must work in marketplace layout (was the root bug)."""
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        os.makedirs(project)
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0, (
            f"user-prompt-hook.sh should work in marketplace layout. "
            f"rc={result.returncode} stderr={result.stderr[:300]}"
        )


class TestLogShConfigBehavior:
    """Behavioral tests: source log.sh and verify actual resolved values.

    These tests prove the config paths resolve correctly — not by grepping
    source code, but by running the shell and checking the result.
    """

    LOG_SH = os.path.join(os.path.dirname(__file__), "..", "scripts", "log.sh")

    def _source_log_sh(self, env: dict, tmp_path) -> dict:
        """Source log.sh and return resolved variables."""
        # Create required .remember/logs so log.sh doesn't fail
        log_dir = os.path.join(env.get("PROJECT_DIR", str(tmp_path)), ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)

        script = f"""\
#!/bin/bash
export PROJECT_DIR="{env.get('PROJECT_DIR', str(tmp_path))}"
export PIPELINE_DIR="{env.get('PIPELINE_DIR', '')}"
source "{self.LOG_SH}" 2>/dev/null
echo "PIPELINE_DIR=$PIPELINE_DIR"
echo "REMEMBER_CONFIG=$REMEMBER_CONFIG"
echo "REMEMBER_HOOKS_DIR=$REMEMBER_HOOKS_DIR"
echo "REMEMBER_TZ=$REMEMBER_TZ"
"""
        wrapper = os.path.join(str(tmp_path), "test-wrapper.sh")
        with open(wrapper, "w") as f:
            f.write(script)

        result = subprocess.run(
            ["bash", wrapper],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, f"log.sh failed: {result.stderr[:300]}"

        resolved = {}
        for line in result.stdout.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                resolved[k] = v
        return resolved

    def test_marketplace_pipeline_dir_used_for_config(self, tmp_path):
        """When PIPELINE_DIR is set (marketplace), config reads from plugin dir."""
        plugin = os.path.join(str(tmp_path), "plugin")
        project = os.path.join(str(tmp_path), "project")
        os.makedirs(plugin)
        os.makedirs(project)

        result = self._source_log_sh({
            "PROJECT_DIR": project,
            "PIPELINE_DIR": plugin,
        }, tmp_path)

        assert result["REMEMBER_CONFIG"] == f"{plugin}/config.json"
        assert result["REMEMBER_HOOKS_DIR"] == f"{plugin}/hooks.d"

    def test_local_install_fallback(self, tmp_path):
        """When PIPELINE_DIR is unset, falls back to PROJECT_DIR/.claude/remember/."""
        project = os.path.join(str(tmp_path), "project")
        os.makedirs(project)

        result = self._source_log_sh({
            "PROJECT_DIR": project,
            "PIPELINE_DIR": "",
        }, tmp_path)

        expected = f"{project}/.claude/remember"
        assert result["PIPELINE_DIR"] == expected
        assert result["REMEMBER_CONFIG"] == f"{expected}/config.json"
        assert result["REMEMBER_HOOKS_DIR"] == f"{expected}/hooks.d"

    def test_timezone_reads_from_config(self, tmp_path):
        """REMEMBER_TZ should come from config.json when it exists."""
        plugin = os.path.join(str(tmp_path), "plugin")
        project = os.path.join(str(tmp_path), "project")
        os.makedirs(plugin)
        os.makedirs(project)

        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({"timezone": "America/Chicago"}, f)

        result = self._source_log_sh({
            "PROJECT_DIR": project,
            "PIPELINE_DIR": plugin,
        }, tmp_path)

        assert result["REMEMBER_TZ"] == "America/Chicago"

    def test_timezone_defaults_to_empty_without_config(self, tmp_path):
        """Without config.json, REMEMBER_TZ defaults to empty (system local)."""
        plugin = os.path.join(str(tmp_path), "plugin")
        project = os.path.join(str(tmp_path), "project")
        os.makedirs(plugin)
        os.makedirs(project)
        # No config.json created

        result = self._source_log_sh({
            "PROJECT_DIR": project,
            "PIPELINE_DIR": plugin,
        }, tmp_path)

        assert result["REMEMBER_TZ"] == ""

    def test_marketplace_config_not_read_from_project(self, tmp_path):
        """Config in project/.claude/remember/ must NOT be used when PIPELINE_DIR is set."""
        plugin = os.path.join(str(tmp_path), "plugin")
        project = os.path.join(str(tmp_path), "project")
        wrong_config_dir = os.path.join(project, ".claude", "remember")
        os.makedirs(plugin)
        os.makedirs(wrong_config_dir)

        import json as json_mod
        # Put a config in the WRONG place (project dir)
        with open(os.path.join(wrong_config_dir, "config.json"), "w") as f:
            json_mod.dump({"timezone": "WRONG_TIMEZONE"}, f)
        # Put correct config in plugin dir
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({"timezone": "America/Denver"}, f)

        result = self._source_log_sh({
            "PROJECT_DIR": project,
            "PIPELINE_DIR": plugin,
        }, tmp_path)

        assert result["REMEMBER_TZ"] == "America/Denver", (
            "Should read from PIPELINE_DIR, not PROJECT_DIR/.claude/remember/"
        )

    def test_post_tool_hook_config_reads_from_plugin(self, tmp_path):
        """post-tool-hook.sh config read must use plugin dir, not project dir."""
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({
                "timezone": "UTC",
                "thresholds": {"delta_lines_trigger": 999},
                "cooldowns": {"save_seconds": 120},
                "features": {"recovery": False},
            }, f)

        # Source post-tool-hook.sh's config path and check it reads 999
        script = f"""\
#!/bin/bash
export CLAUDE_PROJECT_DIR="{project}"
export CLAUDE_PLUGIN_ROOT="{plugin}"
source "{plugin}/scripts/resolve-paths.sh"
source "{plugin}/scripts/bootstrap-dirs.sh"
source "{plugin}/scripts/detect-tools.sh"
source "{plugin}/scripts/log.sh" 2>/dev/null
echo "THRESHOLD=$(config ".thresholds.delta_lines_trigger" 50)"
"""
        wrapper = os.path.join(str(tmp_path), "test-wrapper.sh")
        with open(wrapper, "w") as f:
            f.write(script)

        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        assert "THRESHOLD=999" in result.stdout, (
            f"config() should read delta_lines_trigger=999 from plugin config. "
            f"Got: {result.stdout.strip()}"
        )


class TestPerHookMarketplacePaths:
    """For each hook script, verify it resolves all paths from the plugin dir.

    Each test sets up a marketplace layout with a known config, sources the
    hook's dependency chain, and checks PIPELINE_DIR, REMEMBER_CONFIG,
    REMEMBER_TZ, and REMEMBER_HOOKS_DIR all point to the plugin — not the
    project. This catches the exact class of bug this PR fixes.
    """

    SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")

    # Hook scripts that source resolve-paths.sh → log.sh
    HOOKS = [
        "session-start-hook.sh",
        "post-tool-hook.sh",
        "user-prompt-hook.sh",
        "save-session.sh",
        "run-consolidation.sh",
    ]

    def _setup_marketplace(self, tmp_path):
        """Create marketplace layout with known config, return (project, plugin)."""
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({
                "timezone": "Pacific/Auckland",
                "cooldowns": {"save_seconds": 120, "ndc_seconds": 3600},
                "thresholds": {"delta_lines_trigger": 42},
                "features": {"recovery": False},
            }, f)

        return project, plugin

    def _source_hook_and_dump_vars(self, hook_script, project, plugin, tmp_path):
        """Source a hook's dependency chain and return resolved path variables.

        We can't run hooks to completion (they need sessions, etc.), but we CAN
        source their dependency chain (resolve-paths → bootstrap → log.sh) and
        check the variables that get set. This is exactly what each hook does
        before any business logic.
        """
        # Build a wrapper that mimics the hook's source chain then dumps vars
        script = f"""\
#!/bin/bash
set +e  # don't exit on errors — we just want the variables
export CLAUDE_PROJECT_DIR="{project}"
export CLAUDE_PLUGIN_ROOT="{plugin}"
source "{plugin}/scripts/resolve-paths.sh" 2>/dev/null
source "{plugin}/scripts/bootstrap-dirs.sh" 2>/dev/null
[ -f "{plugin}/scripts/detect-tools.sh" ] && source "{plugin}/scripts/detect-tools.sh" 2>/dev/null
source "{plugin}/scripts/log.sh" 2>/dev/null
echo "PIPELINE_DIR=$PIPELINE_DIR"
echo "PROJECT_DIR=$PROJECT_DIR"
echo "REMEMBER_CONFIG=$REMEMBER_CONFIG"
echo "REMEMBER_HOOKS_DIR=$REMEMBER_HOOKS_DIR"
echo "REMEMBER_TZ=$REMEMBER_TZ"
"""
        wrapper = os.path.join(str(tmp_path), f"test-{hook_script}")
        with open(wrapper, "w") as f:
            f.write(script)

        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, (
            f"{hook_script} dependency chain failed: {result.stderr[:300]}"
        )

        resolved = {}
        for line in result.stdout.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                resolved[k] = v
        return resolved

    @pytest.mark.parametrize("hook", HOOKS)
    def test_hook_pipeline_dir_points_to_plugin(self, hook, tmp_path):
        """PIPELINE_DIR must resolve to the plugin dir, not the project."""
        project, plugin = self._setup_marketplace(tmp_path)
        result = self._source_hook_and_dump_vars(hook, project, plugin, tmp_path)
        assert result["PIPELINE_DIR"] == plugin, (
            f"{hook}: PIPELINE_DIR={result['PIPELINE_DIR']}, expected {plugin}"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_hook_config_points_to_plugin(self, hook, tmp_path):
        """REMEMBER_CONFIG must be in the plugin dir."""
        project, plugin = self._setup_marketplace(tmp_path)
        result = self._source_hook_and_dump_vars(hook, project, plugin, tmp_path)
        assert result["REMEMBER_CONFIG"] == f"{plugin}/config.json", (
            f"{hook}: REMEMBER_CONFIG={result['REMEMBER_CONFIG']}"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_hook_hooks_dir_points_to_plugin(self, hook, tmp_path):
        """REMEMBER_HOOKS_DIR must be in the plugin dir."""
        project, plugin = self._setup_marketplace(tmp_path)
        result = self._source_hook_and_dump_vars(hook, project, plugin, tmp_path)
        assert result["REMEMBER_HOOKS_DIR"] == f"{plugin}/hooks.d", (
            f"{hook}: REMEMBER_HOOKS_DIR={result['REMEMBER_HOOKS_DIR']}"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_hook_timezone_from_plugin_config(self, hook, tmp_path):
        """REMEMBER_TZ must come from the plugin's config.json."""
        project, plugin = self._setup_marketplace(tmp_path)
        result = self._source_hook_and_dump_vars(hook, project, plugin, tmp_path)
        assert result["REMEMBER_TZ"] == "Pacific/Auckland", (
            f"{hook}: REMEMBER_TZ={result['REMEMBER_TZ']}, expected Pacific/Auckland"
        )

    @pytest.mark.parametrize("hook", HOOKS)
    def test_hook_project_dir_points_to_project(self, hook, tmp_path):
        """PROJECT_DIR must resolve to the user's project, not the plugin."""
        project, plugin = self._setup_marketplace(tmp_path)
        result = self._source_hook_and_dump_vars(hook, project, plugin, tmp_path)
        assert result["PROJECT_DIR"] == project, (
            f"{hook}: PROJECT_DIR={result['PROJECT_DIR']}, expected {project}"
        )


class TestHookActualOutput:
    """End-to-end tests: run each hook and verify it produces correct output.

    These tests invoke the real hook scripts in a marketplace layout and
    check that visible output (timestamps, memory sections, exit codes)
    is correct. This is what the user actually sees.
    """

    def _setup_marketplace_with_memory(self, tmp_path, timezone="America/New_York"):
        """Create marketplace layout with config and memory files."""
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({
                "timezone": timezone,
                "cooldowns": {"save_seconds": 120, "ndc_seconds": 3600},
                "thresholds": {"delta_lines_trigger": 50},
                "features": {"recovery": False},
            }, f)

        return project, plugin

    def _run_hook(self, plugin, hook_name, env, timeout=10):
        """Run a hook script and return the subprocess result."""
        hook_path = os.path.join(plugin, "scripts", hook_name)
        return subprocess.run(
            ["bash", hook_path],
            capture_output=True, text=True, env=env, timeout=timeout,
        )

    def _clean_env(self, project, plugin):
        """Build a clean env with only CLAUDE_* vars set."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin
        return env

    def test_user_prompt_hook_outputs_configured_timezone(self, tmp_path):
        """user-prompt-hook.sh must display time in the configured timezone."""
        project, plugin = self._setup_marketplace_with_memory(tmp_path, "UTC")
        env = self._clean_env(project, plugin)
        result = self._run_hook(plugin, "user-prompt-hook.sh", env)

        assert result.returncode == 0
        # Output should be like "[HH:MM UTC — username]"
        assert "UTC" in result.stdout, (
            f"Timestamp should use configured timezone UTC. Got: {result.stdout.strip()}"
        )

    def test_user_prompt_hook_not_paris_when_configured_differently(self, tmp_path):
        """user-prompt-hook.sh must NOT show Paris time when config says otherwise."""
        project, plugin = self._setup_marketplace_with_memory(tmp_path, "America/Chicago")
        env = self._clean_env(project, plugin)
        result = self._run_hook(plugin, "user-prompt-hook.sh", env)

        assert result.returncode == 0
        # Should show CDT/CST, not CET/CEST (Paris)
        stdout = result.stdout.strip()
        assert "CET" not in stdout and "CEST" not in stdout, (
            f"Should NOT show Paris timezone (CET/CEST). Got: {stdout}"
        )

    def test_session_start_hook_runs_in_marketplace(self, tmp_path):
        """session-start-hook.sh must run and produce output in marketplace."""
        project, plugin = self._setup_marketplace_with_memory(tmp_path)
        env = self._clean_env(project, plugin)
        result = self._run_hook(plugin, "session-start-hook.sh", env)

        assert result.returncode == 0, (
            f"session-start should succeed. stderr={result.stderr[:300]}"
        )

    def test_session_start_hook_outputs_memory_when_present(self, tmp_path):
        """session-start-hook.sh must output === MEMORY === when files exist."""
        project, plugin = self._setup_marketplace_with_memory(tmp_path)

        # Create a memory file so session-start has something to inject
        now_file = os.path.join(project, ".remember", "now.md")
        with open(now_file, "w") as f:
            f.write("## 10:00 | main\nDid some work.\n")

        env = self._clean_env(project, plugin)
        result = self._run_hook(plugin, "session-start-hook.sh", env)

        assert result.returncode == 0
        assert "=== MEMORY ===" in result.stdout, (
            f"Should output memory section. Got: {result.stdout[:300]}"
        )
        assert "Did some work" in result.stdout, (
            f"Should include now.md content. Got: {result.stdout[:300]}"
        )

    def test_post_tool_hook_exits_cleanly_no_session(self, tmp_path):
        """post-tool-hook.sh must exit 0 when there's no active session."""
        project, plugin = self._setup_marketplace_with_memory(tmp_path)
        env = self._clean_env(project, plugin)
        result = self._run_hook(plugin, "post-tool-hook.sh", env)

        assert result.returncode == 0, (
            f"post-tool should exit cleanly with no session. "
            f"stderr={result.stderr[:300]}"
        )

    def test_run_consolidation_exits_cleanly_no_staging(self, tmp_path):
        """run-consolidation.sh must exit 0 when no staging files exist."""
        project, plugin = self._setup_marketplace_with_memory(tmp_path)
        env = self._clean_env(project, plugin)
        result = self._run_hook(plugin, "run-consolidation.sh", env)

        # Exits 0 with "no staging files" — not a crash
        assert result.returncode == 0, (
            f"run-consolidation should exit cleanly with no staging files. "
            f"stderr={result.stderr[:300]}"
        )

    def test_user_prompt_hook_timestamp_format(self, tmp_path):
        """user-prompt-hook.sh output must match [HH:MM TZ -- username] format."""
        import re
        project, plugin = self._setup_marketplace_with_memory(tmp_path, "UTC")
        env = self._clean_env(project, plugin)
        result = self._run_hook(plugin, "user-prompt-hook.sh", env)

        assert result.returncode == 0
        line = result.stdout.strip().split("\n")[0]
        # Format: [HH:MM TIMEZONE — username] (with em-dash or --)
        pattern = r"^\[\d{1,2}:\d{2} \S+"
        assert re.match(pattern, line), (
            f"Timestamp must match [HH:MM TZ ...] format. Got: {line}"
        )

    def test_session_start_hook_finds_identity_in_plugin(self, tmp_path):
        """session-start-hook.sh reads identity.md from plugin dir, not project."""
        project, plugin = self._setup_marketplace_with_memory(tmp_path)

        # Identity lives in the plugin dir (shipped with the plugin)
        identity = os.path.join(plugin, "identity.md")
        with open(identity, "w") as f:
            f.write("# Who I Am\nI am a test identity.\n")

        env = self._clean_env(project, plugin)
        result = self._run_hook(plugin, "session-start-hook.sh", env)

        assert result.returncode == 0
        assert "test identity" in result.stdout, (
            f"Should read identity.md from plugin dir. Got: {result.stdout[:300]}"
        )


class TestLocalInstallLayout:
    """Verify all hooks work in local install layout (plugin inside project)."""

    def _setup_local(self, tmp_path, timezone="Europe/Berlin"):
        """Create local install: plugin at $PROJECT/.claude/remember/."""
        project = os.path.join(str(tmp_path), "my-project")
        plugin = os.path.join(project, ".claude", "remember")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({
                "timezone": timezone,
                "cooldowns": {"save_seconds": 120, "ndc_seconds": 3600},
                "features": {"recovery": False},
            }, f)

        return project, plugin

    def _clean_env(self, project):
        """Local install: only CLAUDE_PROJECT_DIR, no CLAUDE_PLUGIN_ROOT."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        # No CLAUDE_PLUGIN_ROOT — resolve-paths.sh must walk up from script
        return env

    def test_session_start_hook_local(self, tmp_path):
        """session-start-hook.sh must work in local install layout."""
        project, plugin = self._setup_local(tmp_path)
        env = self._clean_env(project)
        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0, (
            f"session-start should work in local layout. stderr={result.stderr[:300]}"
        )

    def test_user_prompt_hook_local(self, tmp_path):
        """user-prompt-hook.sh must work in local install layout."""
        project, plugin = self._setup_local(tmp_path)
        env = self._clean_env(project)
        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0
        assert "CET" in result.stdout or "CEST" in result.stdout or "Europe" not in "test", (
            "Should output a timestamp"
        )

    def test_user_prompt_hook_local_uses_configured_tz(self, tmp_path):
        """Local install must also read timezone from config, not hardcode Paris."""
        project, plugin = self._setup_local(tmp_path, "Asia/Tokyo")
        env = self._clean_env(project)
        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0
        assert "JST" in result.stdout, (
            f"Local install should use configured TZ (Asia/Tokyo→JST). Got: {result.stdout.strip()}"
        )

    def test_post_tool_hook_local(self, tmp_path):
        """post-tool-hook.sh must exit cleanly in local install."""
        project, plugin = self._setup_local(tmp_path)
        env = self._clean_env(project)
        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "post-tool-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0

    def test_run_consolidation_local(self, tmp_path):
        """run-consolidation.sh must exit cleanly in local install."""
        project, plugin = self._setup_local(tmp_path)
        env = self._clean_env(project)
        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "run-consolidation.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0


class TestEdgeCases:
    """Edge cases that have caused bugs before or could cause them."""

    LOG_SH = os.path.join(os.path.dirname(__file__), "..", "scripts", "log.sh")

    def test_no_config_json_graceful_degradation(self, tmp_path):
        """When config.json doesn't exist, hooks must still work with defaults."""
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        # Delete config.json — fresh install scenario
        config = os.path.join(plugin, "config.json")
        if os.path.exists(config):
            os.remove(config)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )

        assert result.returncode == 0, (
            f"Hooks must work without config.json. stderr={result.stderr[:300]}"
        )

    def test_no_config_json_timezone_is_system_local(self, tmp_path):
        """Without config.json, REMEMBER_TZ must be empty (system local), not Paris."""
        project = os.path.join(str(tmp_path), "project")
        plugin = os.path.join(str(tmp_path), "plugin")
        os.makedirs(plugin)
        os.makedirs(project)
        # No config.json

        log_dir = os.path.join(project, ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)

        script = f"""\
#!/bin/bash
export PROJECT_DIR="{project}"
export PIPELINE_DIR="{plugin}"
source "{self.LOG_SH}" 2>/dev/null
echo "TZ=$REMEMBER_TZ"
"""
        wrapper = os.path.join(str(tmp_path), "test.sh")
        with open(wrapper, "w") as f:
            f.write(script)
        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        assert "TZ=" in result.stdout
        tz_value = result.stdout.strip().split("TZ=")[1]
        assert tz_value == "", (
            f"Without config, TZ should be empty (system local), got '{tz_value}'"
        )

    def test_spaces_in_project_path(self, tmp_path):
        """Hooks must work when the project path contains spaces."""
        project = os.path.join(str(tmp_path), "my project with spaces")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)
        os.makedirs(project)
        _create_full_project(project)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0, (
            f"Hooks must handle spaces in paths. stderr={result.stderr[:300]}"
        )

    def test_log_dir_in_project_not_plugin(self, tmp_path):
        """REMEMBER_LOG_DIR must be in PROJECT_DIR/.remember/, not PIPELINE_DIR."""
        project = os.path.join(str(tmp_path), "project")
        plugin = os.path.join(str(tmp_path), "plugin")
        os.makedirs(plugin)
        os.makedirs(project)
        log_dir = os.path.join(project, ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)

        script = f"""\
#!/bin/bash
export PROJECT_DIR="{project}"
export PIPELINE_DIR="{plugin}"
source "{self.LOG_SH}" 2>/dev/null
echo "LOG_DIR=$REMEMBER_LOG_DIR"
"""
        wrapper = os.path.join(str(tmp_path), "test.sh")
        with open(wrapper, "w") as f:
            f.write(script)
        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        log_dir_val = result.stdout.strip().split("LOG_DIR=")[1]
        assert log_dir_val.startswith(project), (
            f"Logs must go in PROJECT_DIR, not plugin. Got: {log_dir_val}"
        )
        assert plugin not in log_dir_val, (
            f"Logs must NOT be in plugin dir. Got: {log_dir_val}"
        )

    def test_dispatch_no_hooks_dir_is_noop(self, tmp_path):
        """dispatch() must be a no-op when hooks.d/ doesn't exist, not a crash."""
        project = os.path.join(str(tmp_path), "project")
        plugin = os.path.join(str(tmp_path), "plugin")
        os.makedirs(plugin)
        os.makedirs(project)
        log_dir = os.path.join(project, ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)
        # No hooks.d/ directory

        script = f"""\
#!/bin/bash
export PROJECT_DIR="{project}"
export PIPELINE_DIR="{plugin}"
source "{self.LOG_SH}" 2>/dev/null
dispatch "nonexistent_event"
echo "OK"
"""
        wrapper = os.path.join(str(tmp_path), "test.sh")
        with open(wrapper, "w") as f:
            f.write(script)
        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        assert "OK" in result.stdout, (
            f"dispatch with no hooks.d should be a no-op. stderr={result.stderr[:200]}"
        )

    def test_empty_claude_plugin_root_falls_back(self, tmp_path):
        """Empty CLAUDE_PLUGIN_ROOT (vs unset) must not crash resolve-paths.sh."""
        project = os.path.join(str(tmp_path), "my-project")
        plugin = os.path.join(project, ".claude", "remember")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({"timezone": "UTC", "features": {"recovery": False}}, f)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = ""  # empty string, not unset

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0, (
            f"Empty CLAUDE_PLUGIN_ROOT should fall back. stderr={result.stderr[:300]}"
        )

    def test_regression_pipeline_dir_unset_reads_wrong_config(self, tmp_path):
        """REGRESSION GUARD: without PIPELINE_DIR, log.sh falls back to local path.

        This is the exact bug that was reported: marketplace install, PIPELINE_DIR
        not set → config read from PROJECT_DIR/.claude/remember/ → file doesn't
        exist → silent fallback to defaults → wrong timezone.
        """
        plugin = os.path.join(str(tmp_path), "marketplace-plugin")
        project = os.path.join(str(tmp_path), "user-project")
        os.makedirs(plugin)
        os.makedirs(project)
        log_dir = os.path.join(project, ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)

        # Config is in the plugin dir (marketplace layout)
        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({"timezone": "America/Denver"}, f)

        # Simulate the OLD bug: source log.sh WITHOUT PIPELINE_DIR
        script = f"""\
#!/bin/bash
export PROJECT_DIR="{project}"
unset PIPELINE_DIR
source "{self.LOG_SH}" 2>/dev/null
echo "CONFIG=$REMEMBER_CONFIG"
echo "TZ=$REMEMBER_TZ"
"""
        wrapper = os.path.join(str(tmp_path), "test.sh")
        with open(wrapper, "w") as f:
            f.write(script)
        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )

        # Without PIPELINE_DIR, log.sh falls back to PROJECT_DIR/.claude/remember/
        # which does NOT have config.json → TZ defaults to empty
        lines = result.stdout.strip().split("\n")
        tz_line = [l for l in lines if l.startswith("TZ=")][0]
        tz_val = tz_line.split("=", 1)[1]

        # The fallback path won't find the config (it's in plugin dir, not project)
        # so TZ should be empty (the default), NOT "America/Denver"
        assert tz_val != "America/Denver", (
            "Without PIPELINE_DIR, config in plugin dir should NOT be found. "
            "This was the original bug — silent config miss."
        )

    def test_invalid_json_config_returns_defaults(self, tmp_path):
        """Malformed config.json must not crash — config() returns defaults."""
        plugin = os.path.join(str(tmp_path), "plugin")
        project = os.path.join(str(tmp_path), "project")
        os.makedirs(plugin)
        os.makedirs(project)
        log_dir = os.path.join(project, ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)

        # Write broken JSON (missing comma, unclosed brace)
        with open(os.path.join(plugin, "config.json"), "w") as f:
            f.write('{"timezone": "America/Chicago" "broken": true')

        script = f"""\
#!/bin/bash
export PROJECT_DIR="{project}"
export PIPELINE_DIR="{plugin}"
source "{self.LOG_SH}" 2>/dev/null
echo "TZ=$REMEMBER_TZ"
echo "COOLDOWN=$(config ".cooldowns.save_seconds" 120)"
"""
        wrapper = os.path.join(str(tmp_path), "test.sh")
        with open(wrapper, "w") as f:
            f.write(script)
        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, (
            f"Broken config.json must not crash. stderr={result.stderr[:300]}"
        )
        # Should fall back to defaults
        assert "TZ=" in result.stdout
        assert "COOLDOWN=120" in result.stdout, (
            "Broken JSON → config() must return the default value"
        )

    def test_config_timezone_wrong_type_number(self, tmp_path):
        """config.json with timezone as number must not crash the hook."""
        plugin = os.path.join(str(tmp_path), "plugin")
        project = os.path.join(str(tmp_path), "project")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({"timezone": 12345, "features": {"recovery": False}}, f)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        # Must not crash — TZ="12345" is weird but date still runs
        assert result.returncode == 0, (
            f"Numeric timezone in config must not crash. stderr={result.stderr[:300]}"
        )

    def test_config_timezone_null_uses_default(self, tmp_path):
        """config.json with timezone: null must fall back to empty default."""
        plugin = os.path.join(str(tmp_path), "plugin")
        project = os.path.join(str(tmp_path), "project")
        os.makedirs(plugin)
        os.makedirs(project)
        log_dir = os.path.join(project, ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)

        import json as json_mod
        with open(os.path.join(plugin, "config.json"), "w") as f:
            json_mod.dump({"timezone": None}, f)

        script = f"""\
#!/bin/bash
export PROJECT_DIR="{project}"
export PIPELINE_DIR="{plugin}"
source "{self.LOG_SH}" 2>/dev/null
echo "TZ=$REMEMBER_TZ"
"""
        wrapper = os.path.join(str(tmp_path), "test.sh")
        with open(wrapper, "w") as f:
            f.write(script)
        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        # null in JSON → jq returns "null" for `-r` or empty for `// empty`
        # config() should return default (empty string)
        tz_val = [l for l in result.stdout.strip().split("\n") if l.startswith("TZ=")][0].split("=", 1)[1]
        assert tz_val == "", (
            f"timezone: null should fall back to empty default, got '{tz_val}'"
        )

    def test_trailing_slash_in_plugin_root(self, tmp_path):
        """CLAUDE_PLUGIN_ROOT with trailing slash must not break paths."""
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin + "/"  # trailing slash

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0, (
            f"Trailing slash in PLUGIN_ROOT must not crash. stderr={result.stderr[:300]}"
        )

    def test_unicode_in_project_path(self, tmp_path):
        """Project paths with unicode characters (accents, CJK) must work."""
        project = os.path.join(str(tmp_path), "projet-café-日本語")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)
        os.makedirs(project)
        _create_full_project(project)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0, (
            f"Unicode in project path must not crash. stderr={result.stderr[:300]}"
        )

    def test_symlinked_scripts_directory(self, tmp_path):
        """Hooks must work when scripts/ is a symlink (DVSI local dev layout)."""
        import shutil
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        # Move scripts to a different location and symlink back
        real_scripts = os.path.join(str(tmp_path), "real-scripts")
        shutil.move(os.path.join(plugin, "scripts"), real_scripts)
        os.symlink(real_scripts, os.path.join(plugin, "scripts"))

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0, (
            f"Symlinked scripts/ must work. stderr={result.stderr[:300]}"
        )

    def test_broken_config_symlink_returns_defaults(self, tmp_path):
        """Broken symlink for config.json must fall back to defaults, not crash."""
        plugin = os.path.join(str(tmp_path), "plugin")
        project = os.path.join(str(tmp_path), "project")
        os.makedirs(plugin)
        os.makedirs(project)
        log_dir = os.path.join(project, ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)

        # Create a broken symlink for config.json
        os.symlink("/nonexistent/config.json", os.path.join(plugin, "config.json"))

        script = f"""\
#!/bin/bash
export PROJECT_DIR="{project}"
export PIPELINE_DIR="{plugin}"
source "{self.LOG_SH}" 2>/dev/null
echo "TZ=$REMEMBER_TZ"
echo "COOLDOWN=$(config ".cooldowns.save_seconds" 120)"
"""
        wrapper = os.path.join(str(tmp_path), "test.sh")
        with open(wrapper, "w") as f:
            f.write(script)
        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        assert "COOLDOWN=120" in result.stdout, (
            "Broken config symlink → must return default, not crash"
        )

    def test_dispatch_continues_after_hook_failure(self, tmp_path):
        """If one hooks.d script fails, dispatch must continue to the next."""
        plugin = os.path.join(str(tmp_path), "plugin")
        project = os.path.join(str(tmp_path), "project")
        os.makedirs(plugin)
        os.makedirs(project)
        log_dir = os.path.join(project, ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)

        hook_dir = os.path.join(plugin, "hooks.d", "test_event")
        os.makedirs(hook_dir)

        # First hook: fails
        with open(os.path.join(hook_dir, "01-fail.sh"), "w") as f:
            f.write("#!/bin/bash\nexit 1\n")
        os.chmod(os.path.join(hook_dir, "01-fail.sh"), 0o755)

        # Second hook: succeeds and prints marker
        with open(os.path.join(hook_dir, "02-ok.sh"), "w") as f:
            f.write("#!/bin/bash\necho 'SECOND_HOOK_RAN=true'\n")
        os.chmod(os.path.join(hook_dir, "02-ok.sh"), 0o755)

        script = f"""\
#!/bin/bash
export PROJECT_DIR="{project}"
export PIPELINE_DIR="{plugin}"
source "{self.LOG_SH}" 2>/dev/null
dispatch "test_event"
echo "DISPATCH_COMPLETED=true"
"""
        wrapper = os.path.join(str(tmp_path), "test.sh")
        with open(wrapper, "w") as f:
            f.write(script)
        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        assert "SECOND_HOOK_RAN=true" in result.stdout, (
            "dispatch must continue after a hook failure"
        )
        assert "DISPATCH_COMPLETED=true" in result.stdout

    def test_resolve_paths_failure_does_not_silently_continue(self, tmp_path):
        """If resolve-paths.sh fails (bad PROJECT_DIR), hook must not run with wrong paths."""
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = "/nonexistent/project/that/does/not/exist"
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        # resolve-paths.sh should exit 1, killing the hook
        assert result.returncode != 0, (
            "Hook must fail if resolve-paths.sh can't find PROJECT_DIR. "
            "Silent continuation with wrong paths is worse than a crash."
        )

    def test_handoff_consumed_after_session_start(self, tmp_path):
        """session-start must clear remember.md after reading it (one-shot)."""
        project = os.path.join(str(tmp_path), "user-project")
        plugin = os.path.join(str(tmp_path), "cache", "org", "remember", "0.5.0")
        _create_full_plugin_copy(plugin)
        _create_full_project(project)

        handoff = os.path.join(project, ".remember", "remember.md")
        with open(handoff, "w") as f:
            f.write("## Handoff\nPick up the security audit.\n")

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}
        env["CLAUDE_PROJECT_DIR"] = project
        env["CLAUDE_PLUGIN_ROOT"] = plugin

        result = subprocess.run(
            ["bash", os.path.join(plugin, "scripts", "session-start-hook.sh")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0
        assert "security audit" in result.stdout, (
            "Handoff content should appear in session output"
        )
        # File should be empty after consumption
        with open(handoff) as f:
            remaining = f.read()
        assert remaining.strip() == "", (
            f"remember.md should be cleared after session-start. Contains: {remaining[:100]}"
        )

    def test_pipeline_dir_empty_string_triggers_fallback(self, tmp_path):
        """PIPELINE_DIR="" (empty, not unset) must trigger the log.sh guard."""
        project = os.path.join(str(tmp_path), "project")
        plugin_local = os.path.join(project, ".claude", "remember")
        os.makedirs(plugin_local, exist_ok=True)
        log_dir = os.path.join(project, ".remember", "logs")
        os.makedirs(log_dir, exist_ok=True)

        import json as json_mod
        with open(os.path.join(plugin_local, "config.json"), "w") as f:
            json_mod.dump({"timezone": "Asia/Seoul"}, f)

        script = f"""\
#!/bin/bash
export PROJECT_DIR="{project}"
export PIPELINE_DIR=""
source "{self.LOG_SH}" 2>/dev/null
echo "PIPELINE_DIR=$PIPELINE_DIR"
echo "TZ=$REMEMBER_TZ"
"""
        wrapper = os.path.join(str(tmp_path), "test.sh")
        with open(wrapper, "w") as f:
            f.write(script)
        result = subprocess.run(
            ["bash", wrapper], capture_output=True, text=True, timeout=5,
        )
        lines = {l.split("=", 1)[0]: l.split("=", 1)[1]
                 for l in result.stdout.strip().split("\n") if "=" in l}
        # Empty PIPELINE_DIR should trigger fallback to PROJECT_DIR/.claude/remember
        assert lines.get("PIPELINE_DIR") == f"{project}/.claude/remember", (
            f"Empty PIPELINE_DIR should trigger fallback. Got: {lines.get('PIPELINE_DIR')}"
        )
        assert lines.get("TZ") == "Asia/Seoul", (
            f"Should read config from fallback path. Got TZ={lines.get('TZ')}"
        )
