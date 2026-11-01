"""#920 self-review, round 3: lib-memory-dir.sh, post-tool-hook.sh and log.sh
each carry their own, separately-named copy of the REMEMBER_DIR-unsafe
predicate -- necessarily duplicated because post-tool-hook.sh's fast path
never sources lib-memory-dir.sh (it replays a cached REMEMBER_DIR instead),
and because the #900 compiled-hook build inlines every sourced file's top
level into one script, so two top-level functions sharing a name there is a
build failure, not a harmless shadow (tests/test_compile_hooks_900.py caught
exactly that in an earlier round of this fix).

lib-memory-dir.sh's own copy is ALSO deliberately written more compactly
than the other two -- every byte it adds is paid by every hook that sources
it (unconditionally, at top level, regardless of whether that hook's own
code ever reads REMEMBER_DIR_UNSAFE), and session-start-hook.sh's compiled
size was already within 37 bytes of its 120 KiB release budget before this
fix -- so a BODY-TEXT comparison across the three copies is the wrong
invariant now: the compact form legitimately reads differently from the
other two while agreeing on every input. This file pins the invariant that
actually matters: all three predicates classify the SAME set of values the
SAME way.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_log_sh import _bash_path, _find_bash
from test_remember_dir_guard_920 import (
    HOOK,
    LOG_SH,
    UNC_VALUE,
    UNC_WITH_NEWLINE,
    _extract_function,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_MEMORY_DIR = REPO_ROOT / "scripts" / "lib-memory-dir.sh"

_BASH = _find_bash()

# value -> expected exit code when run through the predicate directly
# (0 = bash truthy = "fires" = unsafe; 1 = safe). Covers every shape the
# three #920 gaps care about: plain absolute, both Windows drive forms, the
# MSYS/cygwin spelling, UNC, a bare two-backslash value with nothing after
# it (the auditor's below-bar edge case -- all three must agree on it even
# though none of them accept it), relative, and both control-char cases.
CASES = {
    "/abs/safe/path": 1,
    "C:/windows/safe": 1,
    "C:\\windows\\safe": 1,
    "/c/msys/safe": 1,
    UNC_VALUE: 1,
    "\\\\x": 1,
    "\\\\": 0,
    "relative/path": 0,
    "/abs/with\nnewline": 0,
    UNC_WITH_NEWLINE: 0,
}


def _run_all_three(value: str) -> dict[str, int]:
    body = (
        _extract_function(LIB_MEMORY_DIR, "_remember_lmd_set_dir_unsafe")
        + _extract_function(HOOK, "_remember_dir_is_unsafe")
        + _extract_function(LOG_SH, "_remember_log_dir_unsafe")
        # Travels through an environment variable, not argv (#920 CI round
        # 2): a bare, unquoted argv element carrying a raw embedded newline
        # byte does not survive to the child process on windows-latest --
        # observed identically on every windows-latest Python leg for both
        # control-char CASES entries, never on ubuntu/macos, and never for
        # any of the other (control-char-free) CASES values. An environment
        # variable's value is not re-tokenized the way a win32 command line
        # is, so it survives intact.
        + 'REMEMBER_DIR="$REMEMBER_DIR_PROBE_VALUE"\n'
        # lib-memory-dir.sh's own copy sets a FLAG rather than returning a
        # status (it is called unconditionally, never in an `if`, so its own
        # exit status is never checked in the real file either) -- read the
        # flag it set, inverted to the same 0=unsafe/1=safe convention
        # _remember_dir_is_unsafe's own exit status already uses, so all
        # three lines below are directly comparable.
        + '_remember_lmd_set_dir_unsafe; echo "lib=$(( 1 - REMEMBER_DIR_UNSAFE ))"\n'
        + '_remember_dir_is_unsafe; echo "hook=$?"\n'
        + '_remember_log_dir_unsafe; echo "log=$?"\n'
    )
    tmp = Path(__file__).parent / f"_sync_probe_{abs(hash(value))}.sh"
    tmp.write_text(body, encoding="utf-8")
    env = dict(os.environ, REMEMBER_DIR_PROBE_VALUE=value)
    try:
        result = subprocess.run(
            [_BASH, _bash_path(tmp)], capture_output=True, text=True, timeout=30, check=False, env=env,
        )
    finally:
        tmp.unlink(missing_ok=True)
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = int(v)
    return out


def test_must_fire_all_three_predicates_classify_every_case_the_same_way():
    mismatches = []
    for value, expected in CASES.items():
        got = _run_all_three(value)
        for name in ("lib", "hook", "log"):
            if got.get(name) != expected:
                mismatches.append(f"{value!r}: expected {name}={expected}, got {got.get(name)!r} (full: {got})")
    assert not mismatches, "the three REMEMBER_DIR-unsafe predicates disagree:\n" + "\n".join(mismatches)


def test_must_not_fire_an_absolute_value_is_never_classified_unsafe_by_anyone():
    """Positive control: every predicate must still accept the ordinary case
    -- a table that happened to assert only on disagreement would pass even
    if all three predicates were simultaneously, identically broken."""
    got = _run_all_three("/an/ordinary/absolute/path")
    assert got == {"lib": 1, "hook": 1, "log": 1}, got
