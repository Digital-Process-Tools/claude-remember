"""#803: `_transcript_is_pluginless_sdk()`'s message-field skip (#745) can mask
the exact bug it exists to prevent.

The function skips any line carrying a top-level `"message"` key outright, on the
theory that such a line is "real dialogue, never a bookkeeping record" (the code's
own comment). That theory is wrong for a bookkeeping record that carries a
`"message"` key of its own as a plain STRING (an error/status field on, say, a
`queue-operation` record) rather than the nested `{role, content}` OBJECT real
dialogue always uses. If that same line also carries the `"entrypoint"` field this
whole function exists to find, the substring-only skip discards it unseen -- the
pluginless-SDK exclusion silently never fires, and the false notice / force-save
#745 exists to fix reappears for that transcript.

Fixed by discriminating on the message field's own JSON *shape*, not just its
presence: `_stdin_json_string` only succeeds when a value opens with a quote, so it
fails on the object shape and succeeds on the string shape -- an existing helper
already in the file, no new JSON-parsing machinery needed.

Positive control: a bookkeeping-shaped line (string "message", sdk- "entrypoint")
must still be recognized as pluginless-SDK. Negative control, paired per this
repo's own convention: a genuine dialogue line (object "message") that happens to
mention the literal `"entrypoint":"sdk-` substring in its own conversational text
must still be skipped -- proving the fix does not simply stop skipping altogether.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash on this host (#432)"
)


def _function_body(name: str) -> str:
    """Same extraction technique as test_previous_transcript_no_sort_691.py's
    _function_bodies -- pulls the real shipped function verbatim rather than a
    hand-copied stand-in that can drift from it."""
    source = SESSION_START.read_text(encoding="utf-8")
    start_marker = f"\n{name}() {{\n"
    start = source.index(start_marker)
    end = source.index("\n}\n", start) + len("\n}")
    return source[start + 1 : end]


SNIFF_SCRIPT = r"""
_ENTRYPOINT_SNIFF_CAP=50
%s
_transcript_is_pluginless_sdk "$1"
printf '%%s' "$?"
"""


def _run_sniff(transcript_path: Path) -> str:
    body = "\n".join(
        _function_body(name) for name in ("_stdin_json_string", "_transcript_is_pluginless_sdk")
    )
    script = SNIFF_SCRIPT % body
    result = subprocess.run(
        [BASH, "-c", script, "bash", str(transcript_path)],
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_bookkeeping_string_message_does_not_mask_entrypoint(tmp_path):
    """Must-fire case: a bookkeeping record whose own "message" field is a plain
    string, on the same line as an sdk- "entrypoint", must still be detected as
    pluginless-SDK (exit 0) -- not silently skipped as if it were dialogue."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"queue-operation","message":"waiting for tool",'
        '"entrypoint":"sdk-py"}\n'
    )
    exit_code = _run_sniff(transcript)
    assert exit_code == "0", (
        "a string-shaped bookkeeping \"message\" field must not mask the "
        f"\"entrypoint\" field on the same line, got exit {exit_code!r}"
    )


def test_real_dialogue_object_message_is_still_skipped(tmp_path):
    """Negative control, paired with the case above: genuine dialogue (a nested
    {role, content} OBJECT "message" field) that happens to mention the literal
    entrypoint substring in its own conversational text must still be skipped --
    the fix must discriminate by shape, not simply stop skipping "message" lines
    altogether."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"this fixture literally says '
        '\\"entrypoint\\":\\"sdk-py\\" in its own prose"}]}}\n'
    )
    exit_code = _run_sniff(transcript)
    assert exit_code == "1", (
        "a real dialogue line (object-shaped \"message\") must still be skipped "
        f"even when it mentions the entrypoint substring in its own text, got exit {exit_code!r}"
    )


def test_empty_string_message_does_not_mask_entrypoint(tmp_path):
    """Must-fire case, sibling of the string-message case above: an empty-string
    "message" field (`"message":""`) is still a STRING, not the {role, content}
    OBJECT real dialogue uses -- but _stdin_json_string's own non-empty-value
    guard fails identically on both shapes, so the naive fix (auditor finding,
    self-review round) would collapse "object" and "empty string" into the same
    skip. An entrypoint on the same line as an empty "message" string must still
    be detected."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"queue-operation","message":"","entrypoint":"sdk-py"}\n'
    )
    exit_code = _run_sniff(transcript)
    assert exit_code == "0", (
        "an empty-string bookkeeping \"message\" field must not mask the "
        f"\"entrypoint\" field on the same line, got exit {exit_code!r}"
    )


# The real file's own _ENTRYPOINT_SNIFF_CAP, duplicated here (not imported --
# it lives in shell, not Python) because the two cap-enforcement tests below
# need it to build a fixture on either side of the boundary. If the shipped
# value ever changes, SNIFF_SCRIPT's own hardcoded 50 (which the real function
# body never sees, since _function_body only extracts the function itself)
# must change with it, so this constant and that literal must always agree.
_CAP = 50


def _dialogue_line(i: int) -> str:
    """An ordinary object-shaped-"message" dialogue line, indistinguishable
    from real content, carrying no "entrypoint" field of its own."""
    return (
        '{"type":"assistant","message":{"role":"assistant","content":'
        f'[{{"type":"text","text":"dialogue line {i}"}}]}}}}\n'
    )


def test_cap_still_enforced_past_dialogue_lines(tmp_path):
    """Regression test for the cap-bypass finding (self-review round): the fix's
    skip-branch for real dialogue must fall through to the CAP check on every
    iteration, not `continue` past it. Built as a paired must-fire/must-not-fire
    case sharing one fixture shape: an sdk- entrypoint sitting AFTER the cap,
    behind a wall of ordinary dialogue lines, must NOT be found (the scan must
    still stop at the cap) -- if a `continue` were bypassing the cap check, this
    line would incorrectly be found instead."""
    transcript = tmp_path / "session.jsonl"
    lines = [_dialogue_line(i) for i in range(_CAP + 4)]
    lines[_CAP + 3] = (
        '{"type":"queue-operation","message":"waiting","entrypoint":"sdk-py"}\n'
    )
    transcript.write_text("".join(lines))
    exit_code = _run_sniff(transcript)
    assert exit_code == "1", (
        "an sdk- entrypoint past the scan cap, behind a wall of dialogue lines, "
        f"must not be found -- the cap must still stop the scan, got exit {exit_code!r}"
    )


def test_entrypoint_within_cap_past_dialogue_lines_is_still_found(tmp_path):
    """Positive control, paired with the case above: the identical entrypoint
    line, moved to WITHIN the cap (still behind a wall of dialogue lines), must
    still be found -- proving the cap test above fails because of the cap, not
    because dialogue lines break detection generally."""
    transcript = tmp_path / "session.jsonl"
    lines = [_dialogue_line(i) for i in range(_CAP - 5)]
    lines.append(
        '{"type":"queue-operation","message":"waiting","entrypoint":"sdk-py"}\n'
    )
    transcript.write_text("".join(lines))
    exit_code = _run_sniff(transcript)
    assert exit_code == "0", (
        "an sdk- entrypoint within the scan cap, behind a wall of dialogue "
        f"lines, must still be found, got exit {exit_code!r}"
    )
