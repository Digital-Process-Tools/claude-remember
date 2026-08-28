"""Regression pin for #367: scripts/session-start-hook.sh must never print
non-ASCII bytes to a stream.

The reported mechanism (a Python `UnicodeEncodeError`) is DOUBTED in the
issue -- bash's `echo` performs no encode step, so that specific crash
cannot happen in this file. What is doubted stays doubted: neither this
test nor anything else here claims to have reproduced a Windows/cp1252
failure, because no Windows machine was available to produce one. This test
is the piece that IS testable without one: it pins the file-wide decision
the issue argues for -- this file may not print non-ASCII to a stream at
all, on any of its lines, present or future -- as a real regression guard,
so mojibake-or-worse on a cp1252 console (whichever of the two candidate
outcomes is real) simply cannot occur here again.

Scope, matching the issue's own framing: PRINTED lines only. The ~94 other
em-dashes in this file's comments never reach a stream and are untouched.
"must fire" is covered by the four originally-reported echo lines (now
ASCII); the "must not fire" pairing is the same walk finding no OTHER
printed line has since grown a non-ASCII character either -- a scan with no
real coverage (an empty file, a harness that never opened the real path)
would pass this by construction too, so the test also asserts the file has
the sizable line count and echo/printf count this hook is known to have,
which a broken read could not produce by accident.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "session-start-hook.sh"

# A line that writes literal text to a stream: `echo "..."` / `printf ...`,
# or a call to this file's own `log()` helper (scripts/log.sh) -- log()
# normally appends to $MEMORY_LOG_FILE, but falls back to a bare `echo ...
# >&2` when that file cannot be written (a degraded/read-only store, exactly
# the state this hook is built to tolerate), so a `log` call is one failed
# write away from being a stream write too. Redirection to a file (a notice
# dropped for a LATER hook to read and print, e.g. capture-gap-notice) is
# still eventually a printed line -- session-start-hook.sh's own scope ends
# at what IT emits, and the originally-reported lines are exactly the ones
# this file itself sends to a stream, hands to a sibling hook to print
# unchanged, or could fall back to stderr for; anything genuinely written
# only for machine consumption (a delivery record, a config dump) is out of
# scope for this file-wide rule and is not touched here.
_ECHO_OR_PRINTF = re.compile(r'^\s*(echo|printf|log)\b')
_COMMENT_LINE = re.compile(r'^\s*#')


def _printed_lines() -> list[tuple[int, str]]:
    lines = HOOK.read_text(encoding="utf-8").splitlines()
    out = []
    for i, line in enumerate(lines, start=1):
        if _COMMENT_LINE.match(line):
            continue
        if _ECHO_OR_PRINTF.match(line):
            out.append((i, line))
    return out


def test_file_has_the_echo_and_printf_lines_this_pin_expects_to_scan():
    """Positive control for the scan itself: a broken read (empty file,
    wrong path, a harness that silently found nothing) must not let the
    assertion below pass by vacuous truth. session-start-hook.sh is known to
    carry well over 40 echo/printf statements."""
    printed = _printed_lines()
    assert len(printed) > 40, (
        f"expected well over 40 echo/printf lines in {HOOK}, found "
        f"{len(printed)} -- the scan likely did not read the real file"
    )


def test_no_printed_line_contains_non_ascii():
    """MUST FIRE (before the fix): the four originally-reported lines (87,
    ~774, ~865, ~1003) each carry a U+2014 em-dash and are echo statements.
    After the fix, no printed line in this file may contain a byte outside
    ASCII."""
    printed = _printed_lines()
    offenders = [
        (n, l) for n, l in printed
        if any(ord(ch) > 127 for ch in l)
    ]
    assert not offenders, (
        "printed (echo/printf) lines in scripts/session-start-hook.sh "
        "contain non-ASCII characters -- this file's printed lines must be "
        "pure ASCII (#367):\n"
        + "\n".join(f"  line {n}: {l!r}" for n, l in offenders)
    )


def test_comment_em_dashes_are_untouched():
    """The file-wide decision is scoped to PRINTED lines only -- comment
    em-dashes (the other ~94) are explicitly out of scope and must survive.
    A fix that stripped every em-dash in the file rather than deciding
    per-stream would pass the test above too, so this pins the boundary."""
    text = HOOK.read_text(encoding="utf-8")
    comment_dashes = sum(
        1 for line in text.splitlines()
        if _COMMENT_LINE.match(line) and "—" in line
    )
    assert comment_dashes > 20, (
        "expected this file's comments to still carry plenty of em-dashes "
        f"(found {comment_dashes}) -- the fix must be scoped to printed "
        "lines, not a blanket strip across the whole file"
    )
