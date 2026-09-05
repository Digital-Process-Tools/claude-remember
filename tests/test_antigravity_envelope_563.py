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

from unittest.mock import patch

from pipeline import host as _host
from pipeline.extract import extract_messages, extract_session

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


# --- unmapped step type quarantine (#575) ---

def test_antigravity_step_is_unmapped_true_for_foreign_type():
    """Positive: a real step `type` this module has never seen (a future
    tool-call/reasoning step, per the module docstring's own caveat) reads
    as unmapped."""
    assert _host.antigravity_step_is_unmapped(
        {"step_index": 2, "source": "MODEL", "type": "TOOL_CALL_UNKNOWN", "content": "x"}
    ) is True


def test_antigravity_step_is_unmapped_false_for_known_types():
    """Negative control paired with the test above: both types this module
    DOES map must not read as unmapped -- an always-True stub would pass
    the positive test above for the wrong reason."""
    assert _host.antigravity_step_is_unmapped({"type": "USER_INPUT"}) is False
    assert _host.antigravity_step_is_unmapped({"type": "PLANNER_RESPONSE"}) is False


def test_extract_messages_stats_counts_unmapped_antigravity_steps(tmp_path):
    """extract_messages, given a `stats` dict, records how many steps in the
    read span carried a `type` this module cannot map -- the signal
    save-session.sh needs to tell "genuinely nothing happened" apart from
    "steps happened but this build cannot read them yet" (#575)."""
    path = tmp_path / "antigravity-mixed.jsonl"
    path.write_text(
        "\n".join([
            json.dumps({"step_index": 0, "source": "USER", "type": "USER_INPUT", "content": "hi"}),
            json.dumps({"step_index": 1, "source": "MODEL", "type": "TOOL_CALL_UNKNOWN", "content": "ls"}),
        ]) + "\n",
        encoding="utf-8",
    )
    stats: dict = {}
    msgs = extract_messages(str(path), skip_lines=0, envelope="antigravity", stats=stats)
    assert msgs == [("HUMAN", "hi")]
    assert stats.get("antigravity_unmapped_steps") == 1


def test_extract_messages_stats_stays_empty_for_all_known_types(tmp_path):
    """Negative control: an ordinary transcript with only known step types
    must not report any unmapped steps -- a stub that always increments
    would fail this while passing the test above."""
    path = tmp_path / "antigravity-clean.jsonl"
    path.write_text(
        "\n".join([
            json.dumps({"step_index": 0, "source": "USER", "type": "USER_INPUT", "content": "hi"}),
            json.dumps({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "ok"}),
        ]) + "\n",
        encoding="utf-8",
    )
    stats: dict = {}
    msgs = extract_messages(str(path), skip_lines=0, envelope="antigravity", stats=stats)
    assert msgs == [("HUMAN", "hi"), ("AGENT", "ok")]
    assert stats.get("antigravity_unmapped_steps", 0) == 0


def test_extract_session_signals_quarantine_for_unmapped_only_antigravity_span(tmp_path):
    """End to end: a span made ENTIRELY of step types `_ANTIGRAVITY_STEP_ROLES`
    does not know must extract to 0 exchanges (as before) but the
    ExtractResult must ALSO flag that the span needs quarantine -- distinct
    from a genuinely quiet session, which reports the same 0/0 counts but
    must NOT set the flag (paired negative control below)."""
    path = tmp_path / "antigravity-all-unmapped.jsonl"
    path.write_text(
        json.dumps({"step_index": 0, "source": "MODEL", "type": "TOOL_CALL_UNKNOWN", "content": "ls"}) + "\n",
        encoding="utf-8",
    )
    with patch("pipeline.extract.find_session", return_value=str(path)):
        result = extract_session(session_id="whatever", project_dir="/fake", show_all=True)
    assert result.envelope == "antigravity"
    assert result.human_count == 0
    assert result.assistant_count == 0
    assert result.envelope_has_unmapped_step is True


def test_extract_session_does_not_signal_quarantine_for_genuinely_quiet_antigravity_span():
    """Paired negative control: the real captured fixture, read past its own
    end (skip_lines beyond EOF == a genuinely quiet resume), must report 0/0
    same as the test above WITHOUT setting the quarantine flag -- proving
    the flag means "an unmapped step was actually seen", not "0 exchanges"."""
    with patch("pipeline.extract.find_session", return_value=ANTIGRAVITY_TRANSCRIPT), \
         patch("pipeline.extract.get_last_save_line", return_value=10_000):
        result = extract_session(session_id="whatever", project_dir="/fake")
    assert result.envelope == "antigravity"
    assert result.human_count == 0
    assert result.assistant_count == 0
    assert result.envelope_has_unmapped_step is False
