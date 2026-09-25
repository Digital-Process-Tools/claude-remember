"""`REMEMBER_EMIT_READ_MAX` had no numeric guard, unlike every sibling knob
in the same file (#758).

`_remember_emit_file` reads `${REMEMBER_EMIT_READ_MAX:-16384}` straight off the
environment and uses it as the right-hand side of `[ "$2" -gt "$_remember_emit_max" ]`.
Every other numeric knob in this file (`MEMORY_INJECT_MAX_BYTES`) and in
`log.sh` (`_budget`, `_grace`) is passed through a
`case (''|*[!0-9]*) ... ;; esac` guard before use. This one was not: a
non-numeric value in the environment makes the `[` comparison print
`integer expression expected` to stderr and, because the test sits in an
`if` condition (so `set -e` does not fire), falls through to the `read` arm
-- sending the file down the quadratic path the threshold exists to keep it
off.
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
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib-memory-context.sh"


def _emit(tmp_path: Path, env_extra: dict, size_arg: str) -> subprocess.CompletedProcess:
    target = tmp_path / "f.md"
    target.write_text("x" * 20000, encoding="utf-8")
    script = f"""
    set -u
    export PIPELINE_DIR={REPO_ROOT}
    export PROJECT_DIR={tmp_path}
    source {LIB} >/dev/null 2>&1
    _remember_emit_file "{target}" {size_arg} | wc -c
    """
    return subprocess.run(["bash", "-c", script], check=False,
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, **env_extra})


class TestGarbageThresholdDoesNotCrashOrLaunder:

    def test_a_non_numeric_threshold_does_not_print_to_stderr(self, tmp_path):
        """The bug: a garbage REMEMBER_EMIT_READ_MAX makes `[` complain on
        stderr instead of being caught before the comparison runs."""
        r = _emit(tmp_path, {"REMEMBER_EMIT_READ_MAX": "notanumber"}, "20000")
        assert r.returncode == 0, r.stderr
        # GNU bash says "integer expression expected"; macOS's older bash
        # says "integer expected" -- match either, not one literal spelling.
        assert not re.search(r"integer\b.*expected", r.stderr), (
            f"a non-numeric REMEMBER_EMIT_READ_MAX reached the `[ -gt ]` "
            f"comparison unguarded: {r.stderr!r}"
        )

    def test_a_valid_numeric_threshold_still_works(self, tmp_path):
        """Positive control: guarding the garbage case must not break the
        ordinary numeric case."""
        r = _emit(tmp_path, {"REMEMBER_EMIT_READ_MAX": "16384"}, "20000")
        assert r.returncode == 0, r.stderr
        assert r.stderr == ""
        assert r.stdout.strip().endswith("20000")

    def test_an_empty_threshold_does_not_print_to_stderr(self, tmp_path):
        """Empty string is the other member of the `case (''|*[!0-9]*)` shape."""
        r = _emit(tmp_path, {"REMEMBER_EMIT_READ_MAX": ""}, "20000")
        assert r.returncode == 0, r.stderr
        assert not re.search(r"integer\b.*expected", r.stderr)
