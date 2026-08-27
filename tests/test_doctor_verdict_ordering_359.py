"""doctor.sh's VERDICT ladder names causes before effects (#359).

The ladder's own header (scripts/doctor.sh, above the "Verdict" section)
states the rule: "Specific causes are named before the general one," added
after a generic "restart Claude Code" line was reached first for both a
missing Python and a mismatched slug, fixing neither. The
`_STORE_NEEDS_A_HUMAN` arm #348 added tested staging over the cap -- the
EFFECT of consolidation not running -- above the no-usable-Python arm, one
of the CAUSES of that. A user whose Python broke on an already-large store
(the Microsoft Store `python3` alias this repo documents) got told to look
at oversized files instead of the Tools section that actually explains it.

Constructing a store genuinely over the cap AND a genuinely broken Python is
awkward through doctor.sh's real detect-tools.sh probe, so this runs doctor.sh
from a scratch copy of scripts/ with detect-tools.sh swapped for one that
always fails -- the one script doctor.sh already treats specially (probed in
a subshell first; see scripts/doctor.sh's own "3. Detected tools" section).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_doctor_oversized_store_348 import (
    CAP,
    DOCTOR,
    REPO_ROOT,
    _fill,
    _project,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)


def _run_with_broken_python(home: Path, project: Path, remember: Path,
                             scratch: Path) -> subprocess.CompletedProcess:
    """Run doctor.sh from a scratch scripts/ copy with detect-tools.sh broken.

    SCRIPT_DIR inside doctor.sh is derived from its own BASH_SOURCE, so
    every sibling script it sources (resolve-paths.sh, lib-memory-dir.sh,
    lib-slug.sh, detect-tools.sh) comes from wherever doctor.sh itself
    lives -- copying the directory with one file swapped is enough to make
    it source the broken one, without touching the real scripts/.
    """
    scratch_scripts = scratch / "scripts"
    scratch_scripts.mkdir(parents=True)
    for item in (REPO_ROOT / "scripts").iterdir():
        if item.name == "detect-tools.sh" or not item.is_file():
            continue
        (scratch_scripts / item.name).write_bytes(item.read_bytes())
        (scratch_scripts / item.name).chmod(0o755)
    (scratch_scripts / "detect-tools.sh").write_text(
        "#!/bin/bash\n"
        "echo 'no usable python found (simulated for #359)' >&2\n"
        "exit 1\n"
    )
    (scratch_scripts / "detect-tools.sh").chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    return subprocess.run(
        ["bash", str(scratch_scripts / "doctor.sh")], env=env,
        capture_output=True, text=True, timeout=180, check=False,
    )


def _verdict(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("VERDICT:"):
            return line
    raise AssertionError("no VERDICT line in output:\n" + stdout)


def test_no_usable_python_outranks_an_oversized_store_in_the_verdict(tmp_path):
    """The specific cause (no Python) must be named, not the general effect.

    Past-day staging alone over the cap AND no usable Python: before the
    fix, `_STORE_NEEDS_A_HUMAN` was checked first and the verdict pointed at
    the oversized files, even though nothing will ever consolidate them
    until Python is fixed -- the Tools section explains that, but the
    verdict line is the one commands/doctor.md tells the operator to trust
    without scrolling.
    """
    home, project, remember = _project(tmp_path)
    _fill(remember / "today-2020-01-01.md", CAP + 1000)
    scratch = tmp_path / "scratch-plugin"

    result = _run_with_broken_python(home, project, remember, scratch)

    assert result.returncode == 0, result.stderr
    assert "no usable Python" in _verdict(result.stdout), (
        "the verdict does not name the cause a broken Python is:\n"
        + result.stdout
    )
    assert "staging files are over the prompt cap" not in _verdict(result.stdout), (
        "the verdict named the oversized-store effect instead of the "
        "no-usable-Python cause:\n" + result.stdout
    )


def test_an_oversized_store_alone_still_reaches_the_verdict(tmp_path):
    """Positive control: with Python fine, the #348 arm still has to fire.

    Without this, a fix that simply deleted the `_STORE_NEEDS_A_HUMAN` arm
    would satisfy the test above and silently regress #348 -- the store
    shape nothing in the pipeline will clear on its own would stop reaching
    the verdict at all.
    """
    home, project, remember = _project(tmp_path)
    _fill(remember / "today-2020-01-01.md", CAP + 1000)
    (remember / "tmp" / "capture-alive").write_text("sess-1")
    (remember / "tmp" / "last-save.json").write_text(
        '{"session": "sess-1", "line": 500}', encoding="utf-8")

    result = subprocess.run(
        ["bash", str(DOCTOR)],
        env={
            **os.environ,
            "HOME": str(home),
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "REMEMBER_DIR": str(remember),
            "_LIB_MEMORY_DIR_LOADED": "1",
        },
        capture_output=True, text=True, timeout=180, check=False,
    )

    assert "staging files are over the prompt cap" in _verdict(result.stdout), (
        "an oversized, non-self-healing store with Python fine did not "
        "reach the verdict at all:\n" + result.stdout
    )
