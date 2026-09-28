"""thresholds.consolidate_max_bytes lacked #816's malformed-value guard (#834).

#816/#821 gave `CONSOLIDATE_TIMEOUT_SECONDS` (two lines below this key in
`run-consolidation.sh`) a `case ''|*[!0-9]*) log WARNING ...; VALUE=default ;;
esac` guard so a typo'd config value is logged rather than silently swapped
for the default. `CONSOLIDATE_MAX_BYTES`, read immediately above it, never
got the same treatment -- an operator who typos `thresholds.consolidate_max_bytes`
sees exactly the behaviour of a deliberate 600000 default, with nothing in the
daily log distinguishing the two.

Note #360: `consolidate_max_bytes: 0` is a valid, meaningful value (disables
the cap) -- so unlike the timeout guard, this one must only reject empty/
non-digit strings, never 0 itself.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash

BASH = resolve_bash()

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

STUB_SHELL = """\
import os, sys

CALLS = os.environ["STUB_CALLS_LOG"]
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
with open(CALLS, "a") as f:
    f.write(" ".join([cmd] + sys.argv[2:]) + chr(10))
if cmd == "consolidate":
    print("STAGING_COUNT=0")
    print("CONSOLIDATION_STATUS=ok")
"""


def _make_env(tmp_path: Path, *, consolidate_max_bytes):
    """A project with an empty staging dir -- the guard fires long before
    staging count is even checked, so nothing else needs to be real here."""
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (remember / "logs").mkdir(parents=True)

    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "pipeline").mkdir(parents=True)
    (plugin / "pipeline" / "__init__.py").write_text("")
    (plugin / "pipeline" / "haiku.py").write_text("# marker\n")
    (plugin / "pipeline" / "shell.py").write_text(STUB_SHELL)
    for script in ("run-consolidation.sh", "resolve-paths.sh", "detect-tools.sh",
                   "bootstrap-dirs.sh", "log.sh", "lib-memory-dir.sh",
                   "lib-lock.sh", "lib-staging-lock.sh", "lib-slug.sh",
                   "lib-clock.sh"):
        (plugin / "scripts" / script).write_text((REPO_ROOT / "scripts" / script).read_text())

    cfg = {"cooldowns": {}, "thresholds": {"consolidate_max_bytes": consolidate_max_bytes}}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))

    calls_log = tmp_path / "calls.log"
    calls_log.write_text("")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "REMEMBER_CONFIG": str(cfg_path),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "STUB_CALLS_LOG": str(calls_log),
    }
    return env, project, remember, calls_log


def _run(plugin: Path, env: dict):
    return subprocess.run([BASH, str(plugin / "scripts" / "run-consolidation.sh")],
                          capture_output=True, text=True, env=env, timeout=90, check=False)


def _daily_log_text(remember: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in (remember / "logs").glob("*.log"))


def _consolidate_max_bytes_arg(calls_log: Path) -> str:
    """argv: consolidate <staging> <recent> <archive> <max_bytes> <snapshot> <timeout>."""
    lines = [line for line in calls_log.read_text().splitlines() if line.startswith("consolidate")]
    assert lines, f"no consolidate invocation found in the calls log: {calls_log.read_text()!r}"
    return lines[-1].split(" ")[4]


class TestConsolidateMaxBytesMalformedIsLogged:
    """#834: the fallback branch must name what it discarded, not stay silent."""

    def test_malformed_value_is_logged(self, tmp_path):
        env, _project, remember, calls = _make_env(tmp_path, consolidate_max_bytes="6OOOOO")
        plugin = Path(env["CLAUDE_PLUGIN_ROOT"])

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        logs = _daily_log_text(remember)
        assert "6OOOOO" in logs, (
            f"the malformed thresholds.consolidate_max_bytes value was "
            f"discarded with no trace of what it was: {logs!r}"
        )
        assert "consolidate_max_bytes" in logs, (
            f"the log line does not even name the config key that failed to parse: {logs!r}"
        )
        assert _consolidate_max_bytes_arg(calls) == "600000", (
            "a malformed value did not fall back to the 600000 default"
        )

    def test_valid_value_is_not_logged_as_malformed(self, tmp_path):
        """Positive control: a genuinely valid override must not trip the
        new warning."""
        env, _project, remember, calls = _make_env(tmp_path, consolidate_max_bytes=12345)
        plugin = Path(env["CLAUDE_PLUGIN_ROOT"])

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        logs = _daily_log_text(remember)
        assert "consolidate_max_bytes" not in logs, (
            f"a valid configured cap was reported as malformed: {logs!r}"
        )
        assert _consolidate_max_bytes_arg(calls) == "12345"

    def test_zero_is_not_rejected_it_means_disabled(self, tmp_path):
        """#360: 0 is a valid, meaningful value (disables the cap) -- the
        malformed-value guard must not treat it as malformed."""
        env, _project, remember, calls = _make_env(tmp_path, consolidate_max_bytes=0)
        plugin = Path(env["CLAUDE_PLUGIN_ROOT"])

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        logs = _daily_log_text(remember)
        assert "consolidate_max_bytes" not in logs, (
            f"a deliberate 0 (disables the cap) was reported as malformed: {logs!r}"
        )
        assert _consolidate_max_bytes_arg(calls) == "0", (
            "a deliberate 0 was replaced with the default instead of passed through"
        )
