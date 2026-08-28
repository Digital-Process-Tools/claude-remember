"""#407 -- resolve-paths.sh's hand-mirrored plugin-root variable list must
agree with pipeline/host.PLUGIN_ROOT_VARS, the same way lib-slug.sh mirrors
pipeline/slug.py (tests/test_slug_parity.py). ``pipeline/host.py``'s own
module docstring names this test explicitly, so a fourth host added to the
Python registry and never mirrored into the shell script fails loudly here
instead of silently reading a variable nothing checks.

A second test pins the derive-from-script-location branch at
resolve-paths.sh:107 (now ~117): the one reached only by local installs
today, and the only route left once a host sets no plugin-root variable at
all.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess assertions -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"

from pipeline.host import PLUGIN_ROOT_VARS


def _shell_mirrored_vars() -> set[str]:
    """The plugin-root variable names resolve-paths.sh actually reads.

    Read off the ``_REMEMBER_PLUGIN_ROOT="${VAR:-$OTHER}"`` line rather than
    hand-copied here a second time -- a hand-copied list is exactly the kind
    of second copy that drifts unnoticed, which is the failure this test
    exists to catch in the *shell* side; re-typing it in the *test* would
    only move the drift one file over.
    """
    text = RESOLVE_PATHS.read_text(encoding="utf-8")
    match = re.search(
        r'_REMEMBER_PLUGIN_ROOT="\$\{(\w+):-\$\{(\w+):-\}\}"', text,
    )
    assert match, "resolve-paths.sh no longer has the expected plugin-root read"
    return {match.group(1), match.group(2)}


def test_host_shell_parity():
    """The set of names must agree. Order is deliberately not compared: the
    shell script always prefers the vendor-neutral ``PLUGIN_ROOT`` over
    ``CLAUDE_PLUGIN_ROOT`` regardless of host, while ``PLUGIN_ROOT_VARS`` is
    built in *registry* order (Claude Code before Codex) rather than
    per-variable precedence -- the two orderings answer different questions
    and pinning them equal would make this test fail for a reason that has
    nothing to do with drift.
    """
    assert _shell_mirrored_vars() == set(PLUGIN_ROOT_VARS)


def test_plugin_root_wins_over_claude_plugin_root(tmp_path):
    """The vendor-neutral name wins when both are set (#407)."""
    marker = tmp_path / "native"
    marker.mkdir()
    (marker / "pipeline").mkdir()
    (marker / "pipeline" / "haiku.py").write_text("", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.mkdir()

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "PLUGIN_ROOT": str(marker),
        "CLAUDE_PLUGIN_ROOT": str(alias),
    }
    script = f'source "{RESOLVE_PATHS}"; echo "PIPELINE_DIR=$PIPELINE_DIR"'
    result = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"PIPELINE_DIR={marker}" in result.stdout


def test_claude_plugin_root_still_works_when_plugin_root_is_unset(tmp_path):
    """The alias must keep working alone -- nothing here should regress a
    Claude Code install that has never heard of ``PLUGIN_ROOT``."""
    alias = tmp_path / "alias"
    alias.mkdir()

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(alias),
    }
    env.pop("PLUGIN_ROOT", None)
    script = f'source "{RESOLVE_PATHS}"; echo "PIPELINE_DIR=$PIPELINE_DIR"'
    result = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"PIPELINE_DIR={alias}" in result.stdout


def test_derive_from_script_location_when_no_plugin_root_var_is_set(tmp_path):
    """resolve-paths.sh:~117 -- the branch reached only by local installs
    today, and the ONLY route left once a host sets neither ``PLUGIN_ROOT``
    nor ``CLAUDE_PLUGIN_ROOT`` (Gemini CLI documents no such variable at
    all, per #407's own comparison table). Unpinned before this change: a
    regression here would surface only on an install this repo's own test
    matrix cannot reach.

    Builds a local install layout -- $install/scripts/resolve-paths.sh with
    $install/pipeline/haiku.py as the marker resolve-paths.sh looks for --
    by symlinking scripts/ and pipeline/ from the real repo into a fresh
    directory, so the "walk up from this script's real location" branch has
    somewhere real to land without depending on this checkout's own
    position in the filesystem (which is a marketplace cache path here, not
    a local install).
    """
    install = tmp_path / "install"
    install.mkdir()
    os.symlink(REPO_ROOT / "scripts", install / "scripts")
    os.symlink(REPO_ROOT / "pipeline", install / "pipeline")

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
    }
    env.pop("PLUGIN_ROOT", None)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    script = f"""
    source "{install / "scripts" / "resolve-paths.sh"}"
    echo "PIPELINE_DIR=$PIPELINE_DIR"
    """
    result = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"PIPELINE_DIR={install}" in result.stdout, (
        "the derive-from-script-location branch did not fire, or resolved "
        "somewhere other than the local install root: "
        + result.stdout + result.stderr
    )
