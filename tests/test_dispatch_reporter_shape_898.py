"""#898 r38-r40: the three dispatch reporters keep self-contained bodies.

Observed in the directory portal (2026-10-05): r38 and r39 were held with
COMMAND_SCRIPT_NOT_FOLLOWED on the post-tool, session-start and user-prompt
hooks; r40, identical except that _dispatch_report_failure/_skip/_timeout were
restored to their own named locals, own #618 flatten and own two writes,
was not. The held form was a one-line delegation with positional parameters
inside the message string:

    report_error "dispatch" "ERROR: hook failed: $1/$2 (exit $3): $4"

This reads scripts/log.sh and pins the r40 shape. Each negative assertion is
paired with a positive control on the held form.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

LOG_SH = Path(__file__).resolve().parent.parent / "scripts" / "log.sh"
REPORTERS = ("_dispatch_report_failure", "_dispatch_report_skip", "_dispatch_report_timeout")
HELD = ('_dispatch_report_failure() {\n'
        '    report_error "dispatch" "ERROR: hook failed: $1/$2 (exit $3): $4"\n'
        '}\n')


def _body(text: str, name: str) -> str:
    m = re.search(r"^" + re.escape(name) + r"\(\) \{\n(.*?)^\}\n", text, re.MULTILINE | re.DOTALL)
    assert m, f"{name} not found"
    return m.group(1)


def _held_shape(body: str) -> list:
    """What r38/r39 carried and r40 did not."""
    hits = []
    code = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    if any(re.match(r"\s*report_error\b", ln) for ln in code):
        hits.append("delegates to report_error")
    # A bare "$1" (the named-local assignment) is fine; "$1" inside a
    # longer message string is the held form.
    quoted = [q for ln in code for q in re.findall(r'"[^"]*"', ln)]
    if any(re.search(r"\$\{?[1-9]", q) and not re.fullmatch(r'"\$[1-9]"', q) for q in quoted):
        hits.append("positional parameter inside a quoted string")
    return hits


def test_held_shape_is_detected():
    """Positive control: the form the portal held is flagged on both counts."""
    assert len(_held_shape(_body(HELD, "_dispatch_report_failure"))) == 2


@pytest.mark.parametrize("name", REPORTERS)
def test_reporter_is_self_contained(name):
    body = _body(LOG_SH.read_text(encoding="utf-8"), name)
    assert not _held_shape(body), (name, _held_shape(body))
    # Its own #618 flatten and its own hook-errors.log write.
    assert "tr '[:cntrl:]' ' '" in body
    assert "hook-errors.log" in body
