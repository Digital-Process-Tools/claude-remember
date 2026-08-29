"""scripts/doctor.sh --json builds its stderr-capture temp path from ${TMPDIR:-/tmp}
and the shell's own $$ instead of mktemp (#427).

The shell opens a redirection target following symlinks and truncates it on
open (`>`), before a single byte is written -- so a pre-seeded symlink at the
predictable name `remember-doctor-json-resolve-<pid>` truncates an
attacker-chosen file as the invoking user the moment doctor.sh reaches that
line. The `rm -f` immediately after removes only the symlink, never the
truncated victim.

Two tests:

  * `test_predictable_temp_path_lets_a_symlink_truncate_an_arbitrary_file`
    wins the race deterministically (the PID is known to the caller the
    instant `Popen` returns, since ["bash", DOCTOR, ...] makes DOCTOR's own
    top-level shell -- not a subshell -- so `proc.pid` equals `$$` inside the
    script) and asserts the victim survives untouched. Before the fix this
    fails because the victim IS truncated; after the fix (`mktemp`) the
    symlink's name no longer matches what doctor.sh actually opens, so the
    victim is never touched.

  * `test_no_script_builds_a_shared_tmpdir_path_from_pid` pins the class
    (#427's own suggestion) rather than only this instance: no script under
    scripts/ may build a path under a shared, non-mktemp'd `${TMPDIR:-/tmp}`
    location keyed on `$$` or `$BASHPID`. `lib-lock.sh`'s own `_LOCK_SELF="$$"`
    (a lock-token *value*, not a path component) and every `$$`-suffixed path
    that lives under `$REMEMBER_DIR/tmp` (a directory doctor.sh's own install
    already treats as private, not the shared, world-writable `/tmp`) are
    legitimate and must not trip this guard -- see the allowlist below.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Only the race test below drives a bash subprocess and POSIX symlink
# semantics -- the static class-pin test is a pure text/regex scan with
# nothing platform-specific in it, so it is deliberately NOT under this
# skip and must keep running (and reporting) on the Windows leg too.
_needs_posix_bash = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX symlink semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    return home, project, remember


@_needs_posix_bash
def test_predictable_temp_path_lets_a_symlink_truncate_an_arbitrary_file(tmp_path):
    home, project, remember = _project(tmp_path)
    shared_tmp = tmp_path / "shared-tmp"
    shared_tmp.mkdir()

    victim = tmp_path / "victim.txt"
    victim.write_text("attacker did not touch this\n")

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "CLAUDE_PROJECT_DIR": str(project),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "TMPDIR": str(shared_tmp),
    }

    proc = subprocess.Popen(
        ["bash", str(DOCTOR), "--json"], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # `["bash", DOCTOR, ...]` makes DOCTOR's own interpreter the top-level
    # shell of this process -- not a subshell -- so proc.pid IS $$ inside the
    # script. That is exactly what the attacker needs to predict, and here it
    # costs nothing: it is handed to us the instant Popen returns.
    predicted_path = shared_tmp / f"remember-doctor-json-resolve-{proc.pid}"

    deadline = time.monotonic() + 5
    while not predicted_path.parent.exists() and time.monotonic() < deadline:
        pass  # shared_tmp already exists; this loop never actually spins

    # Win the TOCTOU race: plant the symlink before doctor.sh's own `source
    # ... 2>"$_JSON_RESOLVE_ERR_FILE"` opens (and, per `>`, immediately
    # truncates) that path. `os.symlink` costs microseconds; doctor.sh has
    # several `source`s and directory computations to run first.
    os.symlink(victim, predicted_path)

    try:
        proc.wait(timeout=30)
    finally:
        if predicted_path.is_symlink():
            predicted_path.unlink()

    assert victim.read_text() == "attacker did not touch this\n", (
        "the victim file was truncated -- doctor.sh followed a predictable, "
        "attacker-plantable symlink at " + str(predicted_path)
    )


# Directories under here are private to one already-resolved remember
# install (created with a real mkdir, not raced from the outside) rather
# than the shared, world-writable tmp root this issue is about.
_PRIVATE_TMP_ROOTS = re.compile(r"REMEMBER_DIR[}\"]?/tmp|_dir\}/|MEMORY_FILE\}")

_PID_TEMP_PATH = re.compile(
    r'\$\{TMPDIR:-/tmp\}[^\n"]*\$\$'      # ${TMPDIR:-/tmp}/...-$$
    r'|/tmp[^\n"]*\$\$'                    # a bare /tmp literal + $$, same shape
)


def test_no_script_builds_a_shared_tmpdir_path_from_pid():
    offenders = []
    for path in sorted(glob.glob(str(SCRIPTS_DIR / "*.sh"))):
        text = Path(path).read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _PID_TEMP_PATH.search(line) and not _PRIVATE_TMP_ROOTS.search(line):
                offenders.append(f"{Path(path).name}:{lineno}: {line.strip()}")

    assert offenders == [], (
        "a script under scripts/ builds a shared-tmpdir path from $$ instead "
        "of mktemp -- the exact predictable-name TOCTOU this issue is about:\n"
        + "\n".join(offenders)
    )
