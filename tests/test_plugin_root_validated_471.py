"""#471 -- PLUGIN_ROOT is the highest-precedence source for the directory the
plugin executes from (`scripts/resolve-paths.sh:119-121`, from #407), and
until this fix was checked only with ``[ -d ]`` at the `PIPELINE_DIR`
validation four lines above it -- not for a specific file, unlike the
sibling `_PLUGIN_ROOT_CANDIDATE` branch right beside it, which already
checks `[ -f "$_PLUGIN_ROOT_CANDIDATE/pipeline/haiku.py" ]`.

An unrelated directory that merely happens to be named by `PLUGIN_ROOT` (a
generic, unnamespaced name) became the directory this plugin loads and
executes its own code from -- `PIPELINE_DIR` is then the
`python3 -m pipeline.shell` import root, the source of
`$PIPELINE_DIR/scripts/log.sh`, the executable
`$PIPELINE_DIR/scripts/save-session.sh`, and the dispatch root
`$PIPELINE_DIR/hooks.d`.

The precedence itself (`PLUGIN_ROOT` before `CLAUDE_PLUGIN_ROOT`) is correct
and deliberate (#407) and nothing here reverses it -- these tests exercise
the missing validation, and the fallback to `CLAUDE_PLUGIN_ROOT` when
`PLUGIN_ROOT` collides with something that is not this plugin.
"""

from __future__ import annotations

import os
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


def _run(env, cwd):
    script = f'source "{RESOLVE_PATHS}"; echo "PIPELINE_DIR=$PIPELINE_DIR"'
    return subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(cwd),
        capture_output=True, text=True, timeout=30, check=False,
    )


def _make_real_plugin(path: Path) -> Path:
    (path / "pipeline").mkdir(parents=True)
    (path / "pipeline" / "haiku.py").write_text("", encoding="utf-8")
    return path


def test_plugin_root_naming_an_unrelated_directory_is_rejected(tmp_path):
    """The bug itself: PLUGIN_ROOT names a real directory that is simply not
    this plugin (no pipeline/haiku.py inside) -- an accidental collision with
    some other tool's generic env var. Must NOT be accepted as PIPELINE_DIR."""
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    real_alias = _make_real_plugin(tmp_path / "real_via_alias")

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "PLUGIN_ROOT": str(unrelated),
        "CLAUDE_PLUGIN_ROOT": str(real_alias),
    }
    result = _run(env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"PIPELINE_DIR={unrelated}" not in result.stdout, (
        "an unrelated directory that merely happens to be named by "
        "PLUGIN_ROOT must never become PIPELINE_DIR: " + result.stdout
    )
    assert f"PIPELINE_DIR={real_alias}" in result.stdout, (
        "PLUGIN_ROOT's collision should fall through to the valid "
        "CLAUDE_PLUGIN_ROOT alias, not fail outright when a real "
        "install is available through it: " + result.stdout + result.stderr
    )


def test_plugin_root_collision_with_no_valid_fallback_fails_loudly(tmp_path):
    """When PLUGIN_ROOT collides AND there is no valid CLAUDE_PLUGIN_ROOT AND
    no local-install layout either, resolution must fail loudly (FATAL, non-
    zero without REMEMBER_PATHS_SOFT_FAIL) rather than silently adopt the
    wrong directory.

    Sourcing resolve-paths.sh straight out of THIS repo always makes the
    local-install candidate (_PLUGIN_ROOT_CANDIDATE, derived from the
    script's own location) resolve successfully -- this checkout IS a valid
    plugin root. So this test builds an independent install directory that
    symlinks in only scripts/ (never pipeline/), the same technique
    test_derive_from_script_location_when_no_plugin_root_var_is_set uses,
    to get a script location with no local install standing behind it."""
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    os.symlink(REPO_ROOT / "scripts", install / "scripts")

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "PLUGIN_ROOT": str(unrelated),
    }
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    script = f'source "{install / "scripts" / "resolve-paths.sh"}"; echo "PIPELINE_DIR=$PIPELINE_DIR"'
    result = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode != 0, (
        "no valid plugin root anywhere -- must exit non-zero, not silently "
        "adopt the unrelated directory: " + result.stdout + result.stderr
    )
    assert "FATAL" in result.stderr, result.stdout + result.stderr


def test_valid_plugin_root_still_wins_over_claude_plugin_root(tmp_path):
    """Precedence is not reversed by adding validation: a PLUGIN_ROOT that
    genuinely IS this plugin still wins over CLAUDE_PLUGIN_ROOT (#407)."""
    native = _make_real_plugin(tmp_path / "native")
    alias = _make_real_plugin(tmp_path / "alias")

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "PLUGIN_ROOT": str(native),
        "CLAUDE_PLUGIN_ROOT": str(alias),
    }
    result = _run(env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"PIPELINE_DIR={native}" in result.stdout, result.stdout + result.stderr
