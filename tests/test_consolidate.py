"""Tests for consolidation logic (response parsing — no real Haiku calls)."""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.consolidate import (
    parse_consolidation_response,
    consolidate,
    _is_valid_consolidation,
    ConsolidationSkipped,
    ConsolidationTooLarge,
)
from pipeline.types import HaikuResult, TokenUsage, ConsolidationResult


def test_parse_both_sections():
    text = """===RECENT===
# Recent

## 2026-03-12
Built memory pipeline. Refactored shell scripts.

===ARCHIVE===
# Archive

## Week of 2026-03-09
Memory infra completion. Blog standardization."""

    recent, archive = parse_consolidation_response(text)
    assert recent.startswith("# Recent")
    assert "2026-03-12" in recent
    assert "memory pipeline" in recent
    assert archive.startswith("# Archive")
    assert "Week of 2026-03-09" in archive


def test_parse_recent_only():
    text = """===RECENT===
# Recent

## 2026-03-12
Did stuff."""

    recent, archive = parse_consolidation_response(text)
    assert "2026-03-12" in recent
    assert archive == ""


def test_parse_fallback_no_markers():
    text = "## 2026-03-12\nSome content without markers"
    recent, archive = parse_consolidation_response(text)
    assert "2026-03-12" in recent
    assert recent.startswith("# Recent")
    assert archive == ""


def test_parse_preserves_headers():
    text = """===RECENT===
# Recent

stuff

===ARCHIVE===
# Archive

more stuff"""

    recent, archive = parse_consolidation_response(text)
    assert recent.startswith("# Recent")
    assert archive.startswith("# Archive")


def test_parse_adds_missing_headers():
    text = """===RECENT===
## 2026-03-12
no header

===ARCHIVE===
## Week of 2026-03-09
also no header"""

    recent, archive = parse_consolidation_response(text)
    assert recent.startswith("# Recent")
    assert archive.startswith("# Archive")


def test_parse_empty_response():
    recent, archive = parse_consolidation_response("")
    assert archive == ""


def test_parse_identity_candidates():
    text = """===RECENT===
# Recent

## 2026-03-12
Built stuff.

## Identity Candidates
- IDENTITY CANDIDATE: Memory is identity

===ARCHIVE===
# Archive

old stuff"""

    recent, archive = parse_consolidation_response(text)
    assert "IDENTITY CANDIDATE" in recent
    assert "Memory is identity" in recent


def test_consolidate_returns_consolidation_result():
    """consolidate() wires prompt + haiku + parser into a ConsolidationResult."""
    fake_haiku_response = HaikuResult(
        text="===RECENT===\n# Recent\n\n## 2026-03-12\nDid things.\n\n===ARCHIVE===\n# Archive\n\nOld things.",
        tokens=TokenUsage(input=100, output=50, cache=0, cost_usd=0.0001),
    )
    with patch("pipeline.consolidate.call_haiku", return_value=fake_haiku_response):
        result = consolidate(
            staging_contents={"today-2026-03-12.md": "Did things."},
            recent="# Recent\n\nold recent",
            archive="# Archive\n\nold archive",
        )

    assert isinstance(result, ConsolidationResult)
    assert "2026-03-12" in result.recent
    assert "Old things" in result.archive
    assert result.tokens.input == 100


def test_parse_archive_only_marker_no_recent_marker():
    """===ARCHIVE=== without ===RECENT=== falls through to the else branch — entire text treated as recent."""
    text = "some content before\n===ARCHIVE===\n# Archive\narchive stuff"
    recent, archive = parse_consolidation_response(text)
    # No ===RECENT=== marker — full text lands in recent via else fallback
    assert "some content before" in recent
    assert recent.startswith("# Recent")
    # archive marker is not parsed without the RECENT marker present
    assert archive == ""


def test_parse_empty_sections_between_markers():
    """Markers present but nothing between them — both sections are empty strings."""
    text = "===RECENT===\n===ARCHIVE==="
    recent, archive = parse_consolidation_response(text)
    # Both sections strip to "" — headers are only added when content is non-empty
    assert recent == ""
    assert archive == ""


# --- Validation guard: reject conversational / SKIP responses (issue #89) ---

def test_is_valid_rejects_refusal_text():
    assert not _is_valid_consolidation(
        "I cannot complete this compression task. The input is incomplete:"
    )


def test_is_valid_rejects_clarifying_question():
    assert not _is_valid_consolidation(
        "I don't see a specific task. What would you like help with?"
    )


def test_is_valid_rejects_empty():
    assert not _is_valid_consolidation("   \n  ")


def test_is_valid_accepts_envelope():
    assert _is_valid_consolidation("===RECENT===\n# Recent\n## 2026-06-01\nx")


def test_is_valid_accepts_bare_body_with_entries():
    assert _is_valid_consolidation("## 2026-06-01\nDid the thing.")
    assert _is_valid_consolidation("## 14:32 | main\nDid the thing.")
    assert _is_valid_consolidation("# Archive\n## Week of 2026-06-01\nx")


def test_consolidate_skips_on_refusal():
    """A conversational refusal must raise ConsolidationSkipped, not be written."""
    refusal = HaikuResult(
        text="I cannot complete this compression task. The input is incomplete.",
        tokens=TokenUsage(input=100, output=20, cache=0, cost_usd=0.0001),
    )
    with patch("pipeline.consolidate.call_haiku", return_value=refusal):
        with pytest.raises(ConsolidationSkipped):
            consolidate(
                staging_contents={"today-2026-06-01.md": "Did things."},
                recent="# Recent\n\nold",
                archive="# Archive\n\nold",
            )


def test_consolidate_skips_on_skip_flag():
    """An explicit SKIP response must raise ConsolidationSkipped."""
    skip = HaikuResult(
        text="SKIP",
        tokens=TokenUsage(input=50, output=1, cache=0, cost_usd=0.0),
        is_skip=True,
    )
    with patch("pipeline.consolidate.call_haiku", return_value=skip):
        with pytest.raises(ConsolidationSkipped):
            consolidate(
                staging_contents={"today-2026-06-01.md": "x"},
                recent="",
                archive="",
            )


def test_consolidate_accepts_valid_envelope():
    """A well-formed envelope still consolidates normally."""
    ok = HaikuResult(
        text="===RECENT===\n# Recent\n\n## 2026-06-01\nDid things.\n\n===ARCHIVE===\n# Archive\n\nOld.",
        tokens=TokenUsage(input=100, output=50, cache=0, cost_usd=0.0001),
    )
    with patch("pipeline.consolidate.call_haiku", return_value=ok):
        result = consolidate(
            staging_contents={"today-2026-06-01.md": "Did things."},
            recent="",
            archive="",
        )
    assert "2026-06-01" in result.recent
    assert "Old" in result.archive


# --- Oversized-prompt guard (consolidation parity with the save-path cap) ---

def test_consolidate_skips_when_prompt_exceeds_cap():
    """An assembled prompt over max_prompt_bytes must skip BEFORE calling Haiku.

    Mirrors the save path's extract_max_bytes cap, but skips (not truncates)
    because consolidation rewrites recent/archive - truncating the input would
    permanently drop archived memory. The skip leaves staging + memory untouched.
    """
    huge_staging = {"today-2026-01-01.md": "x" * 5000}
    with patch("pipeline.consolidate.call_haiku") as mock_haiku:
        with pytest.raises(ConsolidationTooLarge) as exc:
            consolidate(huge_staging, recent="", archive="", max_prompt_bytes=1000)
    # subclass of ConsolidationSkipped so existing handlers keep working
    assert isinstance(exc.value, ConsolidationSkipped)
    mock_haiku.assert_not_called()  # never fire a doomed context-overflow call


def test_consolidate_proceeds_when_under_cap():
    """Under the cap, consolidation proceeds normally and calls Haiku once."""
    ok = HaikuResult(
        text="===RECENT===\n# Recent\n\n## 2026-01-01\nwork\n\n===ARCHIVE===\n# Archive\n",
        tokens=TokenUsage(input=10, output=5, cache=0, cost_usd=0.0),
    )
    with patch("pipeline.consolidate.call_haiku", return_value=ok) as mock_haiku:
        res = consolidate({"today-2026-01-01.md": "small"}, recent="", archive="",
                          max_prompt_bytes=10_000_000)
    mock_haiku.assert_called_once()
    assert res.recent.startswith("# Recent")


def test_consolidate_no_cap_by_default():
    """max_prompt_bytes defaults to 0 = disabled, preserving prior behavior."""
    ok = HaikuResult(
        text="===RECENT===\n# Recent\n\n## 2026-01-01\nx\n\n===ARCHIVE===\n# Archive\n",
        tokens=TokenUsage(input=10, output=5, cache=0, cost_usd=0.0),
    )
    big_staging = {"today-2026-01-01.md": "x" * 50000}
    with patch("pipeline.consolidate.call_haiku", return_value=ok) as mock_haiku:
        consolidate(big_staging, recent="", archive="")  # no cap arg -> uncapped
    mock_haiku.assert_called_once()
