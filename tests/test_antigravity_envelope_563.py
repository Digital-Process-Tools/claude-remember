"""Antigravity's transcript envelope: sniff, exchange parsing, extraction (#563).

Antigravity CLI (`agy`) writes its transcript to a `transcript_full.jsonl`
under `.system_generated/logs/` in a shape neither Claude Code's nor Codex's
reader understands -- a flat object per step: `{"step_index", "source",
"type", "content"}`, content a plain string, never nested under `message`
or `payload` the way the other two hosts are. This module answers #563's
acceptance item 2: whether the existing reader can consume it at all, by
extending it rather than replacing it, the same seam #443 built for Codex.

The fixture (tests/fixtures/antigravity-transcript-563.jsonl) is a verbatim
capture from a live `agy -p ... --output-format text` print-mode turn, `agy`
1.1.27, macOS darwin/arm64, this session, 2026-09-05 -- not constructed, for
the same reason every other fixture in this suite is not: a shape invented
to match the implementation proves nothing about the real host.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import host as _host
from pipeline.extract import extract_messages

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ANTIGRAVITY_TRANSCRIPT = os.path.join(FIXTURES, "antigravity-transcript-563.jsonl")


def _first_line(path):
    with open(path, encoding="utf-8") as f:
        return json.loads(f.readline())


# --- sniff_envelope ---

def test_sniff_envelope_antigravity():
    """Positive control: the real captured shape IS recognised."""
    assert _host.sniff_envelope(_first_line(ANTIGRAVITY_TRANSCRIPT)) == "antigravity"


def test_sniff_envelope_antigravity_does_not_collide_with_claude_code_or_codex():
    """Paired negative control: a shape carrying `step_index`/`source` but
    missing `content` as a string, or a shape that IS claude-code's/codex's,
    must not also read as antigravity -- a sniff that returns "antigravity"
    for everything would pass the positive test above for the wrong reason.
    """
    assert _host.sniff_envelope({"type": "user", "message": {"content": "hi"}}) != "antigravity"
    assert _host.sniff_envelope({"payload": {"type": "event_msg"}}) != "antigravity"
    assert _host.sniff_envelope({"step_index": 0, "source": "MODEL"}) != "antigravity"


# --- antigravity_exchange ---

def test_antigravity_exchange_user_input_is_human():
    obj = _first_line(ANTIGRAVITY_TRANSCRIPT)
    assert obj["type"] == "USER_INPUT"
    role, text = _host.antigravity_exchange(obj)
    assert role == "HUMAN"
    assert "reply with exactly: OK2" in text


def test_antigravity_exchange_planner_response_is_agent():
    with open(ANTIGRAVITY_TRANSCRIPT, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
    second = lines[1]
    assert second["type"] == "PLANNER_RESPONSE"
    assert _host.antigravity_exchange(second) == ("AGENT", "OK2")


def test_antigravity_exchange_unrecognised_step_type_is_skipped():
    """The must-not-capture half paired with the two positive cases above:
    a step whose `type` this module does not know (a future tool-call or
    reasoning step Antigravity might add) must come back None, not guessed
    at as HUMAN or AGENT -- the same discipline codex_exchange applies to
    an item_completed payload of an unrecognised item type."""
    assert _host.antigravity_exchange({"step_index": 2, "source": "MODEL", "type": "TOOL_CALL", "content": "rm -rf /"}) is None


def test_antigravity_exchange_blank_content_is_skipped():
    assert _host.antigravity_exchange({"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "   "}) is None


# --- extract_messages end to end ---

def test_extract_messages_antigravity_transcript_non_zero():
    """Would pass vacuously against an empty-messages stub if this only
    asserted `msgs` truthy -- pins the exact count and the exact roles,
    against the real captured fixture."""
    envelope = _host.sniff_envelope(_first_line(ANTIGRAVITY_TRANSCRIPT))
    assert envelope == "antigravity"
    msgs = extract_messages(ANTIGRAVITY_TRANSCRIPT, skip_lines=0, envelope=envelope)
    expected_human = (
        "<USER_REQUEST>\nreply with exactly: OK2\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\nThe current local time is: "
        "2026-09-05T14:29:28+02:00.\n</ADDITIONAL_METADATA>"
    )
    assert msgs == [("HUMAN", expected_human), ("AGENT", "OK2")]


def test_extract_messages_antigravity_negative_control_no_recognised_steps(tmp_path):
    """The positive control above proves real content IS extracted; this is
    its paired negative control, in the same file family, so a stub that
    always returns [] could not pass both -- a transcript made entirely of
    step types this module does not recognise must extract to nothing,
    proven alongside the fixture where extraction genuinely happens (#563
    acceptance item 3: a silent no-op capture path is this project's worst
    failure mode, and looks identical to a session with nothing to say)."""
    path = tmp_path / "antigravity-no-messages.jsonl"
    path.write_text(
        "{\"step_index\":0,\"source\":\"MODEL\",\"type\":\"TOOL_CALL\",\"content\":\"ls\"}\n"
        "{\"step_index\":1,\"source\":\"MODEL\",\"type\":\"REASONING\",\"content\":\"thinking...\"}\n",
        encoding="utf-8",
    )
    envelope = _host.sniff_envelope(json.loads(path.read_text().splitlines()[0]))
    assert envelope == "antigravity"
    msgs = extract_messages(str(path), skip_lines=0, envelope=envelope)
    assert msgs == []
