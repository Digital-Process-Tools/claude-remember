"""staging_append warns once when an append-only file crosses a growth line (#349).

`staging_append` (scripts/lib-staging-lock.sh) is a bare ``>>`` with no size
check, and five branches append a span and never roll it back on a
*persistent* cause (sustained lock contention, a full disk, a stalled
consolidation): every one of those leaves the same kind of span landing here
every round, forever, in a file that is genuinely append-only. Nothing in the
tree noticed. This does not cap or truncate anything -- it makes the
crossing visible once, via report_error() (daily log + hook-errors.log,
surfaced by /remember:doctor).

Driven directly against a small standalone bash script that sources the real
scripts/lib-staging-lock.sh (and its own dependencies) rather than through
the full save-session.sh pipeline -- staging_append has no dependency on the
rest of that pipeline, and a unit-level driver keeps the two calls under
test (below the line, then crossing it) explicit instead of buried in a
Haiku-summary round trip.
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

DRIVER = """#!/bin/bash
set -e
source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/bootstrap-dirs.sh"
source "$(dirname "$0")/log.sh"
source "$(dirname "$0")/lib-lock.sh"
source "$(dirname "$0")/lib-staging-lock.sh"

TODAY="$1"
TEXT="$2"
staging_append "$TODAY" "$TEXT"
"""


def _make_env(tmp_path: Path, warn_bytes=None):
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
    (plugin / "scripts" / "driver.sh").write_text(DRIVER)

    thresholds = {}
    if warn_bytes is not None:
        thresholds["staging_warn_bytes"] = warn_bytes
    cfg = {"cooldowns": {}, "thresholds": thresholds}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(__import__("json").dumps(cfg))
    (plugin / "config.json").write_text(__import__("json").dumps(cfg))

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "REMEMBER_DIR": str(remember),
        "REMEMBER_CONFIG": str(cfg_path),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    return env, project, plugin, remember


def _append(plugin: Path, env: dict, today: Path, text: str, tmp_path: Path):
    # bootstrap-dirs.sh MOVES $REMEMBER_CONFIG into $REMEMBER_DIR/tmp on every
    # run (its own leak-sweep relocation, see scripts/bootstrap-dirs.sh) — so
    # the file at env["REMEMBER_CONFIG"] is consumed by the first call and
    # gone for the second. Recreate it from the template before every call
    # rather than once in _make_env.
    from shutil import copyfile
    copyfile(plugin / "config.json", env["REMEMBER_CONFIG"])

    text_file = tmp_path / f"text-{today.name}-{len(text)}.txt"
    text_file.write_text(text, encoding="utf-8")
    result = subprocess.run(
        ["bash", str(plugin / "scripts" / "driver.sh"), str(today), str(text_file)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, (
        f"staging_append failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result


def test_crossing_the_line_logs_a_warning_and_touches_nothing(tmp_path):
    """The 'must fire' half: crossing the threshold logs once, and the file
    still holds exactly what was appended -- nothing dropped, nothing
    truncated."""
    env, _project, plugin, remember = _make_env(tmp_path, warn_bytes=50)
    today = remember / "today-2026-08-27.md"

    # First append stays under the 50-byte line.
    _append(plugin, env, today, "short\n", tmp_path)
    hook_errors = remember / "logs" / "hook-errors.log"
    assert not hook_errors.exists() or "staging" not in hook_errors.read_text(), (
        "warned before the file ever crossed the threshold — a false alarm"
    )

    # Second append pushes it over 50 bytes.
    _append(plugin, env, today, "a" * 60 + "\n", tmp_path)

    assert hook_errors.exists(), "no warning was logged after crossing the line"
    log_text = hook_errors.read_text()
    assert "staging" in log_text and "50" in log_text, (
        f"hook-errors.log does not name the staging warning: {log_text!r}"
    )

    # Nothing was dropped: both appended spans are still in the file.
    content = today.read_text(encoding="utf-8")
    assert "short" in content
    assert "a" * 60 in content


def test_staying_under_the_line_never_warns(tmp_path):
    """The positive control: a healthy, small staging file must not warn.
    Pairs with the test above so a broken harness that always logs cannot
    pass either test."""
    env, _project, plugin, remember = _make_env(tmp_path, warn_bytes=2_000_000)
    today = remember / "today-2026-08-27.md"

    _append(plugin, env, today, "## 10:00 | main\n\n- ordinary day's work\n", tmp_path)
    _append(plugin, env, today, "## 14:00 | main\n\n- more ordinary work\n", tmp_path)

    hook_errors = remember / "logs" / "hook-errors.log"
    assert not hook_errors.exists() or "staging" not in hook_errors.read_text(), (
        "a healthy, small staging file triggered the growth warning"
    )
