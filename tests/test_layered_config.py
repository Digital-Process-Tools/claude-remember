"""Tests for the lib-memory-dir.sh layered config merge.

Verifies that per-project > user-global > plugin-bundled precedence is
honoured, that missing layers are silently skipped, and that REMEMBER_DIR
resolves correctly for both legacy and external data_dir values.
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
    reason="bash subprocess + POSIX lib-memory-dir.sh — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SCRIPT = REPO_ROOT / "scripts" / "lib-memory-dir.sh"
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"
BUNDLED_CONFIG = REPO_ROOT / "config.example.json"


def _run_lib(project_dir: str, pipeline_dir: str, home_dir: str, env_extra: "dict | None" = None) -> dict:
    """Source lib-memory-dir.sh and return the exported variables."""
    script = f"""
    set -e
    export PROJECT_DIR={project_dir}
    export PIPELINE_DIR={pipeline_dir}
    export HOME={home_dir}
    source {DETECT_SCRIPT}
    source {LIB_SCRIPT}
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    echo "REMEMBER_CONFIG=$REMEMBER_CONFIG"
    # Dump a key from the merged config to verify merge
    if [ -f "$REMEMBER_CONFIG" ] && command -v jq >/dev/null 2>&1; then
        SAVE_SEC=$(jq -r '.cooldowns.save_seconds // "absent"' "$REMEMBER_CONFIG")
        DATA_DIR=$(jq -r '.data_dir // "absent"' "$REMEMBER_CONFIG")
        UNDERSCORE_KEYS=$(jq -r '[keys[] | select(startswith("_"))] | length' "$REMEMBER_CONFIG")
        echo "MERGED_SAVE_SECONDS=$SAVE_SEC"
        echo "MERGED_DATA_DIR=$DATA_DIR"
        echo "MERGED_UNDERSCORE_KEYS=$UNDERSCORE_KEYS"
    fi
    """
    env = {**os.environ, **(env_extra or {})}
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"lib-memory-dir.sh failed:\n{result.stderr}"
    parsed: dict = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k] = v
    return parsed


class TestRememberDirResolution:

    def test_legacy_default_no_config_override(self, tmp_path):
        """No data_dir override → legacy ${PROJECT_DIR}/.remember."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        home.mkdir()

        # Write a minimal bundled config with no data_dir override
        (pipeline / "config.json").write_text(json.dumps({"cooldowns": {"save_seconds": 120}}))

        result = _run_lib(str(project), str(pipeline), str(home))
        assert result["REMEMBER_DIR"] == str(project / ".remember")

    def test_user_global_sets_external_data_dir(self, tmp_path):
        """User-global config sets data_dir → REMEMBER_DIR uses external path."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        (pipeline / "config.json").write_text(json.dumps({"cooldowns": {"save_seconds": 120}}))
        (home / ".remember" / "config.json").write_text(
            json.dumps({"data_dir": str(home / "ext-mem" / "{slug}")})
        )

        result = _run_lib(str(project), str(pipeline), str(home))
        assert result["REMEMBER_DIR"].startswith(str(home / "ext-mem" / ""))
        assert "{slug}" not in result["REMEMBER_DIR"]

    def test_legacy_relative_data_dir(self, tmp_path):
        """Relative data_dir (no leading / or ~) is resolved against PROJECT_DIR."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        home.mkdir()

        (pipeline / "config.json").write_text(json.dumps({"data_dir": "my-memory"}))

        result = _run_lib(str(project), str(pipeline), str(home))
        assert result["REMEMBER_DIR"] == str(project / "my-memory")


class TestLayeredConfigMerge:

    def test_bundled_only(self, tmp_path):
        """Only bundled config → its values are used."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        home.mkdir()

        (pipeline / "config.json").write_text(json.dumps({"cooldowns": {"save_seconds": 99}}))

        result = _run_lib(str(project), str(pipeline), str(home))
        assert result.get("MERGED_SAVE_SECONDS") == "99"

    def test_user_global_overrides_bundled(self, tmp_path):
        """User-global config overrides bundled cooldown."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        (pipeline / "config.json").write_text(json.dumps({"cooldowns": {"save_seconds": 99}}))
        (home / ".remember" / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 200}})
        )

        result = _run_lib(str(project), str(pipeline), str(home))
        assert result.get("MERGED_SAVE_SECONDS") == "200"

    def test_per_project_overrides_user_global(self, tmp_path):
        """Per-project config wins over user-global."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        # REMEMBER_DIR is project-relative (default), so per-project cfg lives there
        remember = project / ".remember"
        remember.mkdir()

        (pipeline / "config.json").write_text(json.dumps({"cooldowns": {"save_seconds": 99}}))
        (home / ".remember" / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 200}})
        )
        (remember / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 999}})
        )

        result = _run_lib(str(project), str(pipeline), str(home))
        assert result.get("MERGED_SAVE_SECONDS") == "999"

    def test_underscore_keys_stripped(self, tmp_path):
        """`_`-prefixed doc keys (e.g. _comments, _purpose) never reach the merged config."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        (pipeline / "config.json").write_text(
            json.dumps({"_comments": {"a": "doc"}, "cooldowns": {"save_seconds": 99}})
        )
        (home / ".remember" / "config.json").write_text(
            json.dumps({"_purpose": "doc", "_notes": ["x"], "cooldowns": {"save_seconds": 200}})
        )

        result = _run_lib(str(project), str(pipeline), str(home))
        assert result.get("MERGED_UNDERSCORE_KEYS") == "0"
        assert result.get("MERGED_SAVE_SECONDS") == "200"

    def test_missing_user_global_skipped(self, tmp_path):
        """Missing user-global file does not cause an error; bundled values are used."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        # No ~/.remember/config.json

        (pipeline / "config.json").write_text(json.dumps({"cooldowns": {"save_seconds": 55}}))

        result = _run_lib(str(project), str(pipeline), str(home))
        assert result.get("MERGED_SAVE_SECONDS") == "55"


def _run_log_env(project_dir: str, pipeline_dir: str, home_dir: str, env_extra: "dict | None" = None) -> dict:
    """Source log.sh (via detect-tools + lib-memory-dir) and return the model
    knobs it exports. REMEMBER_MODEL / REMEMBER_REJECT_PATTERN are stripped from
    the base env so the config-vs-default resolution is deterministic; pass
    env_extra to simulate an explicit shell override."""
    script = f"""
    set -e
    export PROJECT_DIR={project_dir}
    export PIPELINE_DIR={pipeline_dir}
    export HOME={home_dir}
    source {DETECT_SCRIPT}
    source {LIB_SCRIPT}
    source {REPO_ROOT / "scripts" / "log.sh"}
    echo "REMEMBER_MODEL=$REMEMBER_MODEL"
    echo "REMEMBER_REJECT_PATTERN=$REMEMBER_REJECT_PATTERN"
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("REMEMBER_MODEL", "REMEMBER_REJECT_PATTERN")}
    env.update(env_extra or {})
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"log.sh failed:\n{result.stderr}"
    parsed: dict = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k] = v
    return parsed


class TestModelConfigBridge:
    """log.sh bridges config.json model/reject_pattern keys to the env vars
    pipeline/haiku.py reads, with explicit shell env taking precedence."""

    def _dirs(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        return project, pipeline, home

    def test_model_defaults_to_haiku(self, tmp_path):
        project, pipeline, home = self._dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        result = _run_log_env(str(project), str(pipeline), str(home))
        assert result.get("REMEMBER_MODEL") == "haiku"
        assert result.get("REMEMBER_REJECT_PATTERN") == ""

    def test_model_from_config(self, tmp_path):
        project, pipeline, home = self._dirs(tmp_path)
        (pipeline / "config.json").write_text(
            json.dumps({"model": "sonnet", "reject_pattern": "none"})
        )
        result = _run_log_env(str(project), str(pipeline), str(home))
        assert result.get("REMEMBER_MODEL") == "sonnet"
        assert result.get("REMEMBER_REJECT_PATTERN") == "none"

    def test_env_overrides_config(self, tmp_path):
        project, pipeline, home = self._dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({"model": "sonnet"}))
        result = _run_log_env(
            str(project), str(pipeline), str(home),
            env_extra={"REMEMBER_MODEL": "opus"},
        )
        assert result.get("REMEMBER_MODEL") == "opus"
