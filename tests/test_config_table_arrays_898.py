"""#898 round 8: log.sh's flattened config table is two parallel arrays
(slot name -> value) read by _remember_cfg_table_get_into, instead of one
shell variable per slot read back with an indirect `${!...}` expansion (a
shape the plugin directory's scanner reads as "reads an environment
variable named at run time").

What must not change: a later set of the same slot wins (the old form
overwrote the variable), an absent slot answers the caller's default, and
an absent slot does not end a caller running under `set -e`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash harness (#79)")

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"


def _run(tmp_path: Path, lines: list) -> subprocess.CompletedProcess:
    remember_dir = tmp_path / "project" / ".remember"
    remember_dir.mkdir(parents=True, exist_ok=True)
    script = "\n".join([
        "set -eu",
        f'export REMEMBER_DIR="{remember_dir.as_posix()}"',
        f'source "{LOG_SH.as_posix()}" >/dev/null 2>&1',
        *lines,
    ]) + "\n"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=20, check=False,
        env={**os.environ, "PROJECT_DIR": str(tmp_path), "TMPDIR": str(tmp_path)},
    )


def test_a_later_set_of_the_same_slot_wins(tmp_path):
    r = _run(tmp_path, [
        "_remember_cfg_table_set _RCFG_a one",
        "_remember_cfg_table_set _RCFG_b other",
        "_remember_cfg_table_set _RCFG_a two",
        "_remember_cfg_table_get_into v _RCFG_a",
        'echo "A=$v"',
        "_remember_cfg_table_get_into v _RCFG_b",
        'echo "B=$v"',
    ])
    assert r.returncode == 0, r.stderr
    assert "A=two" in r.stdout and "B=other" in r.stdout, r.stdout


def test_an_absent_slot_is_empty_and_returns_1(tmp_path):
    r = _run(tmp_path, [
        "_remember_cfg_table_set _RCFG_a one",
        "v=stale",
        "if _remember_cfg_table_get_into v _RCFG_missing; then echo HIT; else echo MISS; fi",
        'echo "V=[$v]"',
    ])
    assert r.returncode == 0, r.stderr
    assert "MISS" in r.stdout and "V=[]" in r.stdout, r.stdout


def test_config_into_answers_from_the_table_and_defaults_under_set_e(tmp_path):
    """Must-fire and must-not-fire together: a slot present in an "ok"
    table answers its value, an absent one answers the default, and the
    absent lookup does not abort the `set -eu` script."""
    r = _run(tmp_path, [
        "_REMEMBER_CFG_STATE=ok",
        '_REMEMBER_CFG_LOADED_FROM="${REMEMBER_CONFIG:-}"',
        "_remember_cfg_table_set _RCFG_cooldowns_save_seconds 42",
        "config_into got .cooldowns.save_seconds 7",
        'echo "HIT=$got"',
        "config_into got .cooldowns.nope 7",
        'echo "DEFAULT=$got"',
        "echo END",
    ])
    assert r.returncode == 0, r.stderr
    assert "HIT=42" in r.stdout, r.stdout
    assert "DEFAULT=7" in r.stdout and "END" in r.stdout, r.stdout
