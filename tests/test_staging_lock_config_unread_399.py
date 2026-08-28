"""lib-staging-lock.sh's fallback config() cannot tell "no config" from
"log.sh returned before it could be read" (#399).

Filed as a follow-up to #394/#398: when log.sh was never sourced, or was
sourced but returned early (the #361/#372 case -- a store whose
$REMEMBER_DIR/logs cannot be created), staging_append (scripts/lib-staging-lock.sh)
falls back to a stub config() that answers every key with the caller-supplied
default and no marker. That is the right answer when REMEMBER_CONFIG is
genuinely absent -- but it is also the answer when REMEMBER_CONFIG names a
real, readable file holding a configured, non-default
`.thresholds.staging_warn_bytes`, because nothing in the fallback ever looked
at the file. A user who configured that threshold gets the built-in default
silently applied on exactly the broken store where the growth warning it
gates matters most.

Route 2 from the issue: the fallback distinguishes the two states without
parsing REMEMBER_CONFIG -- `[ -r "$REMEMBER_CONFIG" ]` is knowable with no jq
and no Python fallback -- and staging_append (the sole caller reachable
through this file) reports the uncertainty via report_error() rather than
silently trusting the default.

Every "must warn" case here is paired with a "must not warn" case in the same
fixture (REMEMBER_CONFIG entirely unset): those two currently render alike,
which is the whole defect, and a test exercising only one of them could not
show it.
"""

from __future__ import annotations

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

# Mirrors #394's DRIVER_FALLBACK_DIRECT, not its DRIVER_NO_LOG: sourcing
# bootstrap-dirs.sh (as DRIVER_NO_LOG does) triggers its own
# `exec 2>> "$REMEMBER_DIR/logs/hook-errors.log"` once logs/ exists (see
# scripts/bootstrap-dirs.sh, the "This replaces the 2>> that was in
# hooks.json" block) -- so a driver that sources it would move every later
# line's stderr, report_error()'s included, out of this test's reach and
# into a file it never reads. Sourcing only lib-staging-lock.sh keeps
# config() and report_error() on the fallback (log.sh -- their only real
# definitions -- was never sourced either way) with stderr staying stderr.
DRIVER = """#!/bin/bash
set -e
source "$(dirname "$0")/lib-staging-lock.sh"

TODAY="$1"
TEXT="$2"
staging_append "$TODAY" "$TEXT"
"""


def _make_env(tmp_path: Path, configured: bool, unreadable: bool = False):
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    for script in ("lib-staging-lock.sh", "lib-clock.sh"):
        (plugin / "scripts" / script).write_text((REPO_ROOT / "scripts" / script).read_text())
    (plugin / "scripts" / "driver.sh").write_text(DRIVER)

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
    }
    env.pop("REMEMBER_DIR", None)
    env.pop("REMEMBER_CONFIG", None)

    if configured:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"thresholds": {"staging_warn_bytes": 999}}))
        if unreadable:
            cfg_path.chmod(0o000)
        env["REMEMBER_CONFIG"] = str(cfg_path)

    return env, plugin


def _run(plugin, env: dict, tmp_path: Path):
    today = tmp_path / "today-2026-08-27.md"
    text_file = tmp_path / "text.txt"
    text_file.write_text("an ordinary note\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(plugin / "scripts" / "driver.sh"), str(today), str(text_file)],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )
    return result, today


def test_readable_unparsed_config_is_reported_not_silently_defaulted(tmp_path):
    """The 'must fire' half: REMEMBER_CONFIG exists, is readable, and holds a
    non-default staging_warn_bytes -- but log.sh never had the chance to read
    it. Before the fix, config()'s fallback silently returns the built-in
    default with no trace; this asserts the caller (staging_append) is told
    the default may not be the real value."""
    env, plugin = _make_env(tmp_path, configured=True)
    result, today = _run(plugin, env, tmp_path)

    assert result.returncode == 0, (
        f"staging_append crashed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert today.read_text(encoding="utf-8") == "an ordinary note\n"
    assert ".thresholds.staging_warn_bytes" in result.stderr, (
        "the fallback did not report that a configured, non-default "
        f"threshold could not be confirmed: stderr={result.stderr!r}"
    )
    assert "REMEMBER_CONFIG" in result.stderr or "config.json" in result.stderr, (
        f"the report did not name which file went unread: stderr={result.stderr!r}"
    )


def test_absent_config_is_not_reported(tmp_path):
    """The positive control this pair needs: REMEMBER_CONFIG entirely unset
    is the genuinely-default case, and must stay silent. Without this half,
    a fallback that warned unconditionally -- on every call, config present
    or not -- would also pass the test above."""
    env, plugin = _make_env(tmp_path, configured=False)
    result, today = _run(plugin, env, tmp_path)

    assert result.returncode == 0, (
        f"staging_append crashed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert today.read_text(encoding="utf-8") == "an ordinary note\n"
    assert ".thresholds.staging_warn_bytes" not in result.stderr, (
        "the fallback warned even though REMEMBER_CONFIG was never set -- "
        f"the genuinely-default case must stay silent: stderr={result.stderr!r}"
    )


def test_existing_but_unreadable_config_is_also_reported(tmp_path):
    """Auditor finding on this issue's own self-review: `[ -r ... ]` alone
    renders "REMEMBER_CONFIG exists but this process cannot read it" (a
    permission change, a mount hiccup) exactly as silently as "REMEMBER_CONFIG
    was never set" -- the same defect class the fix exists to close, one
    layer further out. scripts/log.sh's own real config() checks `-f`
    (existence) and reports explicitly on a read failure rather than falling
    silent; the fallback must not be narrower than the function it stands in
    for. Root is skipped: chmod 000 does not stop root from reading a file it
    owns, so the assertion cannot hold under a root test runner."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permission bits -- chmod-based "
                    "unreadability does not reproduce as this user")

    env, plugin = _make_env(tmp_path, configured=True, unreadable=True)
    try:
        result, today = _run(plugin, env, tmp_path)
    finally:
        (Path(env["REMEMBER_CONFIG"])).chmod(0o644)

    assert result.returncode == 0, (
        f"staging_append crashed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert today.read_text(encoding="utf-8") == "an ordinary note\n"
    assert ".thresholds.staging_warn_bytes" in result.stderr, (
        "an existing-but-unreadable REMEMBER_CONFIG rendered exactly like an "
        f"absent one -- the fallback stayed silent: stderr={result.stderr!r}"
    )
