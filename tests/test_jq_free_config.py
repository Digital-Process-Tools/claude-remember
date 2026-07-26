"""``config()`` (scripts/log.sh) and the 3-layer merge (scripts/lib-memory-dir.sh)
must actually use the jq-free fallback that scripts/detect-tools.sh already
ships, on any machine without ``jq`` on PATH.

Before this fix, both branched on ``command -v jq`` and fell straight through
to ``echo "$default"`` / a bundled-only copy whenever it was absent — even
though detect-tools.sh defines ``_jq_fallback``, a Python-based reader, for
exactly this case. It was simply never wired into either caller. Consequence:
on any jq-less install, *every* user config override (time_format, model,
cooldowns.*, thresholds.*, git_backup.*) was silently ignored — config()
always returned its hardcoded default, and the merge always returned the
plugin-bundled config.json untouched.

A second, narrower bug rides along: the fallback's Python reader printed
``str(True)`` / ``str(False)`` ("True"/"False") for boolean config values
instead of jq's JSON textual form ("true"/"false"). Every ``[ "$x" = "true" ]``
caller against a boolean key (git_backup.gpg_sign, allow_remote_change) read
it as false no matter what the config said.

These tests force jq off PATH by construction (a filtered PATH with every
real binary symlinked in *except* jq), so they fail deterministically on
pristine 0.8.6 whether or not the machine running them happens to have jq
installed, and pass once the fallback is actually wired in.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"
LIB_SCRIPT = REPO_ROOT / "scripts" / "lib-memory-dir.sh"
LOG_SH = REPO_ROOT / "scripts" / "log.sh"


def _path_without_jq(tmp_path: Path) -> str:
    """Build a PATH that resolves every real binary except ``jq``.

    Symlinking the entire real PATH minus jq (rather than e.g. an empty PATH)
    keeps bash, python3, coreutils etc. all working exactly as on the real
    machine — only jq disappears, deterministically, regardless of whether
    this test happens to run on a box that has it installed.
    """
    fake_bin = tmp_path / "no-jq-bin"
    fake_bin.mkdir()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name == "jq":
                continue
            target = fake_bin / name
            if target.exists() or target.is_symlink():
                continue
            try:
                os.symlink(os.path.join(d, name), target)
            except OSError:
                pass
    return str(fake_bin)


def _dirs(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    pipeline = tmp_path / "plugin"
    pipeline.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return project, pipeline, home


def _run_config(tmp_path: Path, key: str, default: str, config_json: dict) -> str:
    """Source detect-tools + lib-memory-dir + log.sh (no jq on PATH) against a
    per-project config override, and return what config() prints for `key`."""
    project, pipeline, home = _dirs(tmp_path)
    (pipeline / "config.json").write_text(json.dumps({}))
    remember = project / ".remember"
    remember.mkdir()
    (remember / "config.json").write_text(json.dumps(config_json))

    script = f"""
    set -e
    export PROJECT_DIR={project}
    export PIPELINE_DIR={pipeline}
    export HOME={home}
    source {DETECT_SCRIPT}
    source {LIB_SCRIPT}
    source {LOG_SH}
    config '{key}' '{default}'
    """
    env = {**os.environ, "PATH": _path_without_jq(tmp_path)}
    result = subprocess.run(["bash", "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"config() sourcing failed:\n{result.stderr}"
    return result.stdout.strip()


class TestConfigUsesJqFreeFallback:

    def test_string_override_honoured_without_jq(self, tmp_path):
        """A per-project config override must not be silently dropped just
        because jq is absent — config() must fall through to the Python
        fallback detect-tools.sh already provides."""
        out = _run_config(tmp_path, ".model", "haiku", {"model": "sonnet"})
        assert out == "sonnet", (
            "config() ignored the project override and returned the "
            "hardcoded default — the jq-free fallback was never wired in"
        )

    def test_nested_cooldown_override_honoured_without_jq(self, tmp_path):
        out = _run_config(tmp_path, ".cooldowns.save_seconds", "120",
                          {"cooldowns": {"save_seconds": 45}})
        assert out == "45"

    def test_boolean_true_reads_as_lowercase_true_without_jq(self, tmp_path):
        """git_backup.gpg_sign / allow_remote_change are booleans compared with
        `[ "$x" = "true" ]` — Python's str(True) ("True") must never leak
        through; jq's own textual form ("true") must."""
        out = _run_config(tmp_path, ".git_backup.gpg_sign", "false",
                          {"git_backup": {"gpg_sign": True}})
        assert out == "true", (
            f"expected jq-style lowercase 'true', got {out!r} — a bash "
            '`[ "$x" = "true" ]` comparison against this would always be false'
        )

    def test_boolean_false_reads_as_lowercase_false_without_jq(self, tmp_path):
        out = _run_config(tmp_path, ".git_backup.gpg_sign", "true",
                          {"git_backup": {"gpg_sign": False}})
        assert out == "false"


class TestLayeredMergeUsesJqFreeFallback:

    def test_per_project_override_survives_merge_without_jq(self, tmp_path):
        """lib-memory-dir.sh's 3-layer merge must not collapse to the bundled
        config alone just because jq is absent."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 99}})
        )
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 999}})
        )

        script = f"""
        set -e
        export PROJECT_DIR={project}
        export PIPELINE_DIR={pipeline}
        export HOME={home}
        source {DETECT_SCRIPT}
        source {LIB_SCRIPT}
        source {LOG_SH}
        config '.cooldowns.save_seconds' '120'
        """
        env = {**os.environ, "PATH": _path_without_jq(tmp_path)}
        result = subprocess.run(["bash", "-c", script], env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "999", (
            "per-project override was dropped by the no-jq merge fallback — "
            "it copied only the bundled config"
        )

    def test_underscore_keys_stripped_without_jq(self, tmp_path):
        """The `_`-prefixed doc-key convention must hold in the Python merge
        path too, not just the jq path."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(
            json.dumps({"_comments": {"a": "doc"}, "cooldowns": {"save_seconds": 99}})
        )

        script = f"""
        set -e
        export PROJECT_DIR={project}
        export PIPELINE_DIR={pipeline}
        export HOME={home}
        source {DETECT_SCRIPT}
        source {LIB_SCRIPT}
        python3 -c "import json,sys; d=json.load(open('$REMEMBER_CONFIG')); print('_comments' in d)"
        """
        env = {**os.environ, "PATH": _path_without_jq(tmp_path)}
        result = subprocess.run(["bash", "-c", script], env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"
