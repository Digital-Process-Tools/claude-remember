"""save-session.sh's two cooldown markers are unvalidated data in `$(( ))` (#322).

#258 fixed this shape in `50-git-backup.sh`. `save-session.sh` reads two markers
the same way and neither was guarded: `tmp/last-save-ts` at the save cooldown and
`tmp/last-ndc.ts` at the NDC gate.

Bash evaluates a bare variable's VALUE inside `$(( ))` as an arithmetic
expression, so a stray byte is a syntax error. Bash then abandons the rest of the
current command list and resumes at the NEXT LINE. Measured with `-e`, without
it, and with `-u`: identical, so errexit plays no part in this. What separates
the two files is the absence of `set -u` here —

* `save-session.sh` (no `-u`) — `ELAPSED=$(( ... ))` is abandoned, `ELAPSED`
  stays unset, and the `[ "$ELAPSED" -lt ... ]` on the next line complains and is
  false. This run skips its cooldown.
* `50-git-backup.sh` (`-u`) — the next line's `$ELAPSED` is an unbound variable
  and the shell exits, which is the permanent stop #258 documented.

Scope it honestly: both gates rewrite their marker on exactly the path a corrupt
one allows, so each SELF-HEALS. The cost is one skipped cooldown per corruption
event, not a throttle stuck off. These tests therefore pin the diagnostic rather
than a missed save — falling back to 0 makes a corrupt marker read as "very old",
so corrupt and clean-but-ancient produce the same decision, and the stderr is the
only thing that separates broken from fixed.

`"08"`/`"09"` are here because a digits-only guard is not enough on its own: they
pass it and are then read as octal (`value too great for base`), failing
identically. That is why the guard carries `10#` — and why the same `10#` is
added to #258's existing guard in `50-git-backup.sh`, where that shape is not a
skipped cooldown but the unbound-variable exit above.

CONTROL: `  1785512249  ` is in the list on purpose and must stay GREEN against
the unfixed script. Arithmetic skips surrounding whitespace, so that marker works
today -- and the guard now rejects it and falls back to 0. That is a real
behaviour change, deliberate, and the same call #258's guard already makes; the
parameter documents it rather than hiding it.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "tests"))
from subprocess_helpers import subprocess_failure_detail  # noqa: E402
from test_save_session_gates import _make_env, _run  # noqa: E402

# Shapes that genuinely reach the evaluator as something other than a plain
# decimal integer. "08"/"09" are the octal pair a digits-only guard lets through.
TRIPS_THE_BUG = ["12x34", "1786158837;", "08", "09"]

# Controls — see the module docstring. These must be green against the UNFIXED
# script too, so they assert the fix is not doing something it was not asked to.
# `  1785512249  ` additionally documents a real behaviour change: arithmetic
# accepts padded digits today, and after the guard it reads as 0. Deliberate, and
# the same call #258's guard already makes.
# `garbage-text` is deliberately NOT here: it parses as `garbage - text`, two
# unset identifiers that are both 0 without `set -u`, so it produces no
# diagnostic and the fix does not change its behaviour at all. A parameter that
# cannot fail either way is not a control, it is decoration.
CONTROLS = ["  1785512249  "]

CORRUPT_MARKERS = TRIPS_THE_BUG + CONTROLS

# bash's own wording for the two ways this shows up, plus the knock-on `test`
# error the save gate emits once ELAPSED has been left unset.
ARITHMETIC_DIAGNOSTICS = (
    "syntax error",
    "invalid arithmetic operator",
    "value too great for base",
    # NOT "integer expression expected": the whole if/then body is abandoned, so
    # the `[ "$ELAPSED" -lt ... ]` that would print it is never reached. Listing
    # a phrase the code cannot emit reads as coverage and is not.
    #
    # "invalid integer constant" is what bash 5 says for `10#` on an empty
    # string, i.e. the failure mode if a later change keeps 10# but drops the
    # case guard in front of it.
    "invalid integer constant",
)


def _set_cooldowns(env: dict, plugin: Path, *, save_seconds: int,
                   ndc_seconds: int) -> None:
    """Rewrite the config the script reads. _make_env pins save_seconds to 0,
    which retires the very gate these tests need to enter.

    Both copies, deliberately: _make_env writes the same document to
    $REMEMBER_CONFIG and to <plugin>/config.json, and updating only the first
    leaves save_seconds at 0 with no visible error — the run just sails through
    the gate and the test reads as a product bug. Caught exactly that way.
    """
    cfg_path = Path(env["REMEMBER_CONFIG"])
    cfg = json.loads(cfg_path.read_text())
    cfg["cooldowns"] = {"save_seconds": save_seconds, "ndc_seconds": ndc_seconds}
    for target in (cfg_path, plugin / "config.json"):
        target.write_text(json.dumps(cfg))


def _assert_no_arithmetic_error(proc, project: Path, marker_name: str) -> None:
    log = project / ".remember" / "logs" / "hook-errors.log"
    haystack = proc.stderr + (log.read_text(encoding="utf-8", errors="replace")
                              if log.is_file() else "")
    for phrase in ARITHMETIC_DIAGNOSTICS:
        assert phrase not in haystack, (
            f"a corrupt {marker_name} reached $(( )) unvalidated: bash reported "
            f"{phrase!r}. The command list is abandoned at that point, so the "
            f"cooldown it guards is silently skipped.\n--- stderr/log ---\n"
            f"{haystack.strip()}"
        )


@pytest.mark.parametrize("corrupt", CORRUPT_MARKERS)
def test_a_corrupt_save_cooldown_marker_does_not_break_the_gate(tmp_path, corrupt):
    env, project, plugin, _calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    _set_cooldowns(env, plugin, save_seconds=3600, ndc_seconds=999999)
    (project / ".remember" / "tmp" / "last-save-ts").write_text(
        corrupt, encoding="utf-8", errors="ignore")

    proc = _run(plugin, env, session_id)

    assert proc.returncode == 0, subprocess_failure_detail(
        proc, project / ".remember")
    _assert_no_arithmetic_error(proc, project, "tmp/last-save-ts")


@pytest.mark.parametrize("corrupt", CORRUPT_MARKERS)
def test_a_corrupt_ndc_marker_does_not_break_the_gate(tmp_path, corrupt):
    env, project, plugin, _calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    # Without this the stub's call-haiku returns SKIP, the run exits at Step 6,
    # and Step 8 — the gate under test — is never reached. The test then passes
    # against the unfixed script and proves nothing. Verified by reverting the
    # fix and watching it stay green until this line was added.
    env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- an entry\n"
    _set_cooldowns(env, plugin, save_seconds=0, ndc_seconds=999999)
    (project / ".remember" / "tmp" / "last-ndc.ts").write_text(
        corrupt, encoding="utf-8", errors="ignore")

    proc = _run(plugin, env, session_id)

    assert proc.returncode == 0, subprocess_failure_detail(
        proc, project / ".remember")
    _assert_no_arithmetic_error(proc, project, "tmp/last-ndc.ts")


def test_a_valid_recent_marker_still_throttles(tmp_path):
    """The guard must not pass by turning the cooldown off for everyone.

    A marker written now, against a 3600s window, has to skip the run — so the
    fallback-to-0 path is reached only by markers that genuinely do not parse.
    """
    env, project, plugin, calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    _set_cooldowns(env, plugin, save_seconds=3600, ndc_seconds=999999)
    (project / ".remember" / "tmp" / "last-save-ts").write_text(str(int(time.time())))

    proc = _run(plugin, env, session_id)

    assert proc.returncode == 0, subprocess_failure_detail(
        proc, project / ".remember")
    ran = calls.read_text() if calls.is_file() else ""
    assert "extract" not in ran, (
        "a marker written seconds ago did not engage the save cooldown — the "
        f"guard has disabled the throttle rather than protected it. calls: {ran!r}"
    )


def test_a_leading_zero_marker_is_read_as_decimal(tmp_path):
    """`10#`, specifically. "08" passes a digits-only guard and is then octal.

    Pinned separately from the parametrized cases because it is the one shape
    that survives the fix #258 shipped, and a future simplification back to a
    bare `case` would leave every other case above green.
    """
    env, project, plugin, calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    _set_cooldowns(env, plugin, save_seconds=3600, ndc_seconds=999999)
    # Interpreted as octal this is an error; as decimal it is 8 seconds past the
    # epoch, i.e. far outside the window, so the run proceeds.
    (project / ".remember" / "tmp" / "last-save-ts").write_text("08")

    proc = _run(plugin, env, session_id)

    assert proc.returncode == 0, subprocess_failure_detail(
        proc, project / ".remember")
    _assert_no_arithmetic_error(proc, project, "tmp/last-save-ts")
    ran = calls.read_text() if calls.is_file() else ""
    assert "extract" in ran, (
        "a marker of \"08\" should read as 8 seconds past the epoch — ancient, so "
        f"the cooldown is long expired and the save proceeds. calls: {ran!r}"
    )
