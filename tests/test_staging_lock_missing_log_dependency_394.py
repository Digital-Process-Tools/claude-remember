"""staging_append must not crash when log.sh is absent or returned early (#394).

`staging_append` (scripts/lib-staging-lock.sh) calls `config()` and
`report_error()` unconditionally -- added in #349 for the growth warning --
but both are defined only in scripts/log.sh, which is *not* declared as a
requirement in the file's own USAGE block. A caller that sources
lib-staging-lock.sh without log.sh, or with a log.sh that `return`ed early
(the #361/#372 case: no writable logs/ directory), hits an undefined
function inside the append path.

There is no live impact today -- the sole production call site,
save-session.sh, sources log.sh and calls `log` extensively before reaching
staging_append -- which is exactly why the existing #349 test
(tests/test_staging_growth_warning_349.py) cannot catch this: its driver
sources log.sh too, satisfying the undeclared dependency by accident. This
file supplies the missing half: a fixture that does NOT source log.sh,
paired against the driver above (in the sibling test file) that does.
"""

from __future__ import annotations

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

# Deliberately omits `source .../log.sh` -- the one line the #349 driver has
# that satisfies the undeclared dependency by accident. Everything else
# mirrors that driver so the only variable between the two fixtures is
# whether log.sh was sourced.
DRIVER_NO_LOG = """#!/bin/bash
set -e
source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/bootstrap-dirs.sh"
source "$(dirname "$0")/lib-lock.sh"
source "$(dirname "$0")/lib-staging-lock.sh"

TODAY="$1"
TEXT="$2"
staging_append "$TODAY" "$TEXT"
"""

# Sources nothing but lib-staging-lock.sh itself -- no REMEMBER_DIR, no
# config.json, nothing staging_append needs. Isolates the fallback guard
# functions (log/report_error/config) from the rest of the append path, so
# the assertion is exactly "the fallback is installed and is not silent",
# not "the whole pipeline still works".
DRIVER_FALLBACK_DIRECT = """#!/bin/bash
set -e
source "$(dirname "$0")/lib-staging-lock.sh"
report_error "staging" "TEST-MARKER-394"
config ".thresholds.staging_warn_bytes" "2000000"
"""


def _make_env(tmp_path: Path):
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (remember / "logs").mkdir(parents=True)

    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    for script in ("resolve-paths.sh", "detect-tools.sh", "bootstrap-dirs.sh",
                   "log.sh", "lib-memory-dir.sh", "lib-lock.sh",
                   "lib-staging-lock.sh", "lib-slug.sh", "lib-clock.sh"):
        (plugin / "scripts" / script).write_text((REPO_ROOT / "scripts" / script).read_text())
    (plugin / "scripts" / "driver-no-log.sh").write_text(DRIVER_NO_LOG)
    (plugin / "scripts" / "driver-fallback.sh").write_text(DRIVER_FALLBACK_DIRECT)

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    return env, project, plugin, remember


def test_staging_append_without_log_sourced_does_not_crash(tmp_path):
    """The 'must fire' half of the pair: staging_append must complete and
    actually append when the caller never sourced log.sh at all -- the
    undeclared-dependency case named in #394. Before the fix this dies with
    'config: command not found' (exit 127), because config() is called
    unconditionally on the very first line of the growth-warning check."""
    env, _project, plugin, _remember = _make_env(tmp_path)
    today = tmp_path / "today-2026-08-27.md"
    text_file = tmp_path / "text.txt"
    text_file.write_text("an ordinary note\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(plugin / "scripts" / "driver-no-log.sh"), str(today), str(text_file)],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )
    assert result.returncode == 0, (
        f"staging_append crashed without log.sh sourced: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert today.read_text(encoding="utf-8") == "an ordinary note\n"


def test_staging_append_with_log_sourced_is_unaffected(tmp_path):
    """The positive control this pair needs (per CLAUDE.md's own rule): the
    ordinary, log.sh-sourced path must keep working unchanged. Pairs with
    the test above so a fallback that swallowed every call -- silently
    returning success without actually appending -- could not pass both."""
    env, _project, plugin, _remember = _make_env(tmp_path)
    today = tmp_path / "today-2026-08-27.md"
    text_file = tmp_path / "text.txt"
    text_file.write_text("an ordinary note\n", encoding="utf-8")

    driver_with_log = plugin / "scripts" / "driver-with-log.sh"
    driver_with_log.write_text(DRIVER_NO_LOG.replace(
        'source "$(dirname "$0")/lib-lock.sh"',
        'source "$(dirname "$0")/log.sh"\nsource "$(dirname "$0")/lib-lock.sh"',
    ))

    result = subprocess.run(
        ["bash", str(driver_with_log), str(today), str(text_file)],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )
    assert result.returncode == 0, (
        f"staging_append failed with log.sh sourced: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert today.read_text(encoding="utf-8") == "an ordinary note\n"


def test_fallback_report_error_is_not_silent(tmp_path):
    """#394's hidden judgment call, made concrete: the fallback must behave
    like session-end-hook.sh's guard (log() to stderr), not like
    user-prompt-hook.sh's `dispatch() { :; }` no-op stub. A no-op
    report_error() here would turn #349's growth warning into exactly the
    silent failure #349 exists to end, on the broken stores where it
    matters most. Before the fix this driver fails outright (report_error
    and config are undefined); a fix that installed a silent no-op instead
    of the sibling-hooks stderr fallback would pass with returncode 0 but
    an empty stderr -- so this checks the message content, not just the
    exit code."""
    env, _project, plugin, _remember = _make_env(tmp_path)

    result = subprocess.run(
        ["bash", str(plugin / "scripts" / "driver-fallback.sh")],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )
    assert result.returncode == 0, (
        f"fallback guard driver failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "TEST-MARKER-394" in result.stderr, (
        f"report_error's fallback did not report to stderr — a silent "
        f"no-op would defeat #349's growth warning: stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "2000000", (
        f"config()'s fallback did not return the caller-supplied default: "
        f"stdout={result.stdout!r}"
    )
