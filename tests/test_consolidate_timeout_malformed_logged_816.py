"""#816: `run-consolidation.sh`'s `CONSOLIDATE_TIMEOUT_SECONDS` guard silently
substituted the 180s default whenever `thresholds.consolidate_timeout_seconds`
was empty or contained a non-digit, with no log line anywhere near the
substitution -- an operator who typo'd the value (e.g. "18O") saw exactly the
same behaviour as one who deliberately configured 180, with nothing in the
daily log distinguishing the two.

The fix logs the malformed value it discarded, on the same daily-log channel
every other event in this script already uses -- so the sibling guard in
`save-session.sh` (`NDC_TIMEOUT_SECONDS`, tested in test_ndc_reject_gate.py)
gets the identical shape, per the issue's own "matching shape must move
together" argument.
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

STUB_SHELL = '''\
import os, sys

CALLS = os.environ["STUB_CALLS_LOG"]
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
with open(CALLS, "a") as f:
    f.write(" ".join([cmd] + sys.argv[2:]) + chr(10))
if cmd == "consolidate":
    print("STAGING_COUNT=0")
    print("CONSOLIDATION_STATUS=ok")
'''


def _make_env(tmp_path: Path, *, consolidate_timeout_seconds):
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
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (plugin / "pipeline" / "shell.py").write_text(STUB_SHELL)
    for script in ("run-consolidation.sh", "resolve-paths.sh", "detect-tools.sh",
                   "bootstrap-dirs.sh", "log.sh", "lib-memory-dir.sh",
                   "lib-lock.sh", "lib-staging-lock.sh", "lib-slug.sh",
                   "lib-clock.sh"):
        (plugin / "scripts" / script).write_text((REPO_ROOT / "scripts" / script).read_text())

    # Written as REMEMBER_CONFIG directly (the already-merged form config()
    # reads), same as test_save_session_gates.py's _make_env -- with
    # _LIB_MEMORY_DIR_LOADED=1 set below, lib-memory-dir.sh's real 3-layer
    # merge never runs, so a plugin/config.json alone would never be read at
    # all; REMEMBER_CONFIG is the only route a threshold value reaches config().
    cfg = {"cooldowns": {}, "thresholds": {"consolidate_timeout_seconds": consolidate_timeout_seconds}}
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


def _consolidate_call_line(calls_log: Path) -> str:
    """The one `consolidate ...` invocation this stub records -- its last
    argv slot is the CONSOLIDATE_TIMEOUT_SECONDS value actually forwarded,
    after the guard has run (#823 self-review finding)."""
    lines = [line for line in calls_log.read_text().splitlines() if line.startswith("consolidate")]
    assert lines, f"no consolidate invocation found in the calls log: {calls_log.read_text()!r}"
    return lines[-1]


class TestConsolidateTimeoutMalformedIsLogged:
    """#816: the fallback branch must name what it discarded, not stay silent."""

    def test_malformed_value_is_logged(self, tmp_path):
        env, _project, remember, _calls = _make_env(tmp_path, consolidate_timeout_seconds="18O")
        plugin = Path(env["CLAUDE_PLUGIN_ROOT"])

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        logs = _daily_log_text(remember)
        assert "18O" in logs, (
            f"the malformed thresholds.consolidate_timeout_seconds value was "
            f"discarded with no trace of what it was: {logs!r}"
        )
        assert "consolidate_timeout_seconds" in logs, (
            f"the log line does not even name the config key that failed to parse: {logs!r}"
        )

    def test_valid_value_is_not_logged_as_malformed(self, tmp_path):
        """Positive control: a genuinely valid override must not trip the
        new warning."""
        env, _project, remember, _calls = _make_env(tmp_path, consolidate_timeout_seconds=42)
        plugin = Path(env["CLAUDE_PLUGIN_ROOT"])

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        logs = _daily_log_text(remember)
        assert "consolidate_timeout_seconds" not in logs, (
            f"a valid configured timeout was reported as malformed: {logs!r}"
        )


class TestConsolidateTimeoutDestructiveValuesRejected:
    """#823: the #816 guard above only rejects empty/non-digit strings -- an
    all-digits value of 0 or one large enough to overflow the pipeline's own
    subprocess.run(timeout=...) call (observed: OverflowError inside PyTime_t
    at 9e9 and 1e10) sailed through with no warning at all, either timing out
    every run or crashing with a generic pipeline-failed error instead of
    this key's own malformed-value message."""

    def test_zero_is_rejected_and_logged(self, tmp_path):
        env, _project, remember, calls = _make_env(tmp_path, consolidate_timeout_seconds=0)
        plugin = Path(env["CLAUDE_PLUGIN_ROOT"])

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        logs = _daily_log_text(remember)
        assert "consolidate_timeout_seconds" in logs, (
            f"a configured timeout of 0 (times out every run immediately) "
            f"was not reported at all: {logs!r}"
        )

        line = _consolidate_call_line(calls)
        assert line.split(" ")[-1] == "180", (
            f"a configured timeout of 0 was not rejected and fell through to "
            f"the pipeline.shell consolidate invocation unchanged: {line!r}"
        )

    def test_huge_value_is_rejected_and_logged(self, tmp_path):
        """9000000000 is the smallest value this repo has reproduced
        crashing pipeline.shell's subprocess.run(timeout=...) with
        OverflowError -- the guard must catch it before it ever reaches
        there."""
        env, _project, remember, calls = _make_env(tmp_path, consolidate_timeout_seconds=9000000000)
        plugin = Path(env["CLAUDE_PLUGIN_ROOT"])

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        logs = _daily_log_text(remember)
        assert "consolidate_timeout_seconds" in logs, (
            f"a configured timeout of 9000000000s (crashes with OverflowError "
            f"downstream) was not reported at all: {logs!r}"
        )

        line = _consolidate_call_line(calls)
        assert line.split(" ")[-1] == "180", (
            f"a configured timeout of 9000000000 was not rejected and fell "
            f"through to the pipeline.shell consolidate invocation unchanged: {line!r}"
        )

    def test_moderately_large_value_is_not_rejected(self, tmp_path):
        """Positive control: a large but sane override (1 hour) must not
        trip either new guard."""
        env, _project, remember, calls = _make_env(tmp_path, consolidate_timeout_seconds=3600)
        plugin = Path(env["CLAUDE_PLUGIN_ROOT"])

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        logs = _daily_log_text(remember)
        assert "consolidate_timeout_seconds" not in logs, (
            f"a sane 3600s override was reported as malformed/too large: {logs!r}"
        )

        line = _consolidate_call_line(calls)
        assert line.split(" ")[-1] == "3600", (
            f"a sane 3600s override did not reach the pipeline.shell consolidate invocation: {line!r}"
        )
