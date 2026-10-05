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

    Read off the two ``_REMEMBER_PLUGIN_ROOT=`` assignments rather than
    hand-copied here a second time -- a hand-copied list is exactly the kind
    of second copy that drifts unnoticed, which is the failure this test
    exists to catch in the *shell* side; re-typing it in the *test* would
    only move the drift one file over.

    #898, round 4 converted the original single nested-default-expansion
    line (``_REMEMBER_PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"``)
    into an explicit if/else -- a ``${X:-$Y}`` the directory's portal scanner
    reads as "a command assembled at run time" is not a hold trigger here
    (there is no command, only a variable default), but the sweep rewrote it
    at the same source location; the regex below now matches the if/else
    shape that replaced it, same anti-drift property, same two names.
    """
    text = RESOLVE_PATHS.read_text(encoding="utf-8")
    first = re.search(r'_REMEMBER_PLUGIN_ROOT="\$(\w+)"', text)
    second = re.search(r'_REMEMBER_PLUGIN_ROOT="\$\{(\w+):-\}"', text)
    assert first and second, (
        "resolve-paths.sh no longer has the expected plugin-root assignments"
    )
    return {first.group(1), second.group(1)}


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
    # #898, round 5: resolve-paths.sh's marker moved from pipeline/haiku.py
    # to .claude-plugin/plugin.json (install manifest, not a script path).
    (marker / ".claude-plugin").mkdir()
    (marker / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
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
    Claude Code install that has never heard of ``PLUGIN_ROOT``.

    Deliberately updated for #471: this used to accept an EMPTY ``alias``
    directory (``alias.mkdir()`` with nothing inside) as a valid
    ``PIPELINE_DIR``, which was exactly the missing validation #471 is
    about -- ``[ -d ]`` and nothing else. Pinning that as "still works"
    would re-encode the bug this lane exists to fix. ``alias`` now has to
    look like an actual plugin install (``.claude-plugin/plugin.json``
    present -- #898 round 5 moved this marker from pipeline/haiku.py to the
    install manifest -- the same marker the local-install branch already
    required) for the assertion below to mean what it says.
    """
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / ".claude-plugin").mkdir()
    (alias / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")

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
    $install/.claude-plugin/plugin.json as the marker resolve-paths.sh looks
    for (#898, round 5: moved from pipeline/haiku.py to the install
    manifest) -- by symlinking scripts/ and .claude-plugin/ from the real
    repo into a fresh directory, so the "walk up from this script's real
    location" branch has somewhere real to land without depending on this
    checkout's own position in the filesystem (which is a marketplace cache
    path here, not a local install).
    """
    install = tmp_path / "install"
    install.mkdir()
    os.symlink(REPO_ROOT / "scripts", install / "scripts")
    os.symlink(REPO_ROOT / ".claude-plugin", install / ".claude-plugin")
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
