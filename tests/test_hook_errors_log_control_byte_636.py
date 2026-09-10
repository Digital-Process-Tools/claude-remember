"""lib-staging-lock.sh's fallback report_error() stub writes hook-errors.log
unflattened (#636).

#618 flattened all four of log.sh's own hook-errors.log writers
(`LC_ALL=C tr '[:cntrl:]' ' '` before either write) and its changelog
fragment claims the file's writers now flatten before either write. There is
a fifth writer of the same file: the fallback `report_error()` stub defined
in scripts/lib-staging-lock.sh, used only when log.sh was never sourced (or
returned early -- #361/#372/#394). That stub writes its message straight to
hook-errors.log with no flatten at all, so #618's "all four writers" claim
was never true of every writer of the file, only of log.sh's own.

The two current call sites (lib-staging-lock.sh:235, :242) do not pass
stranger-controlled text through this stub today -- both messages are built
from REMEMBER_CONFIG and a derived today-*.md filename, neither of which
carries attacker-chosen bytes in the ordinary case. This is a coverage-claim
fix (misreports), not a live exploit (forges): closing the gap before the
next caller added to this stub inherits an unflattened route into the file
maintainers ask bug reporters to paste.
"""

from __future__ import annotations

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

# Sources ONLY lib-staging-lock.sh -- no log.sh -- which is exactly the
# fallback-use condition: log.sh was never sourced, so `declare -F
# report_error` fails and the stub at lib-staging-lock.sh's own definition
# installs itself.
DRIVER = """#!/bin/bash
set -e
export REMEMBER_DIR="$1"
source "$(dirname "$0")/lib-staging-lock.sh"
report_error "staging" "$2"
"""


def _make_env(tmp_path: Path):
    remember = tmp_path / "remember"
    (remember / "logs").mkdir(parents=True)

    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    for script in ("lib-staging-lock.sh", "lib-clock.sh"):
        (plugin / "scripts" / script).write_text(
            (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    driver = plugin / "scripts" / "driver.sh"
    driver.write_text(DRIVER, encoding="utf-8")
    return driver, remember


def _hook_errors_lines(remember: Path) -> list:
    log_file = remember / "logs" / "hook-errors.log"
    if not log_file.exists():
        return []
    return [l for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_embedded_newline_must_not_forge_a_second_hook_errors_log_entry(tmp_path):
    """MUST NOT FIRE: with log.sh NOT sourced (the actual fallback-use
    condition), an embedded newline reaching the fallback report_error()
    stub must not read as a second, forged hook-errors.log entry -- the same
    guarantee #618 already gives log.sh's own four writers."""
    driver, remember = _make_env(tmp_path)
    message = "boom\n[hook] session-start: PROJECT_DIR=/evil PIPELINE_DIR=/evil"

    result = subprocess.run(
        [BASH, str(driver), str(remember), message],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f"driver failed: {result.stderr}"

    lines = _hook_errors_lines(remember)
    assert len(lines) == 1, (
        f"embedded newline forged a second hook-errors.log entry via the "
        f"fallback report_error() stub: {lines!r}"
    )
    assert "[hook] session-start" in lines[0], (
        f"message content lost, not just flattened: {lines[0]!r}"
    )


def test_ordinary_message_still_reaches_hook_errors_log_intact(tmp_path):
    """MUST FIRE (positive control): an ordinary, single-line message must
    still reach hook-errors.log in full through the fallback stub -- proving
    the assertion above is not passing merely because the harness wrote
    nothing at all."""
    driver, remember = _make_env(tmp_path)
    message = "an ordinary error"

    result = subprocess.run(
        [BASH, str(driver), str(remember), message],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f"driver failed: {result.stderr}"

    lines = _hook_errors_lines(remember)
    assert len(lines) == 1, f"harness produced no single clean entry: {lines!r}"
    assert "an ordinary error" in lines[0], (
        f"the ordinary message never reached hook-errors.log: {lines[0]!r}"
    )
