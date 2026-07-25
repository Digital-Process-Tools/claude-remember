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
    _strip_wrapping_fence,
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


# --- Wrapping code fence: strip it so headers aren't doubled (issue #126) ---

def test_parse_strips_stray_leading_fence_no_double_header():
    """A stray leading ``` before the body must not trigger a doubled
    '# Recent' header (the orphaned-fence artifact seen in recent.md)."""
    text = (
        "===RECENT===\n```\n\n# Recent\n\n## 2026-07-02\n"
        "Committed CF skill pack.\n\n===ARCHIVE===\n# Archive\n\nold"
    )
    recent, archive = parse_consolidation_response(text)
    assert recent.startswith("# Recent")
    assert recent.count("# Recent") == 1
    assert "```" not in recent
    assert "2026-07-02" in recent


def test_parse_strips_wrapping_markdown_fence():
    """A ```markdown … ``` fence wrapping the whole recent body is removed
    (opening + matching closing), leaving a single header."""
    text = (
        "===RECENT===\n```markdown\n# Recent\n\n## 2026-07-02\nDid stuff.\n```\n"
        "\n===ARCHIVE===\n```\n# Archive\n\nold\n```"
    )
    recent, archive = parse_consolidation_response(text)
    assert recent.startswith("# Recent")
    assert recent.count("# Recent") == 1
    assert "```" not in recent
    assert archive.startswith("# Archive")
    assert "```" not in archive


# --- Fence handling (#126 fix, #154 regression) -----------------------------
#
# The original #126 fix stripped a leading fence and then *any* trailing fence.
# That corrupted legitimate content whose last line closed a code sample, and
# it still left an orphan fence in archive.md for the shape #126 was actually
# reported with. These pin both the fix and the regression.


def test_wrapping_fence_is_stripped():
    """The plain case: a whole body wrapped in ```markdown ... ```."""
    body = "```markdown\n# Recent\n\n## 12:00\n- did a thing\n```"
    assert parse_consolidation_response(body)[0] == "# Recent\n\n## 12:00\n- did a thing"


def test_bare_wrapping_fence_is_stripped():
    body = "```\n# Recent\n\n## 12:00\n- did a thing\n```"
    assert parse_consolidation_response(body)[0] == "# Recent\n\n## 12:00\n- did a thing"


def test_code_sample_inside_summary_keeps_its_terminator():
    """A truncated wrap whose body ends in a code sample must keep that sample closed.

    Stripping 'the last fence' here removes the ```bash block's own terminator,
    so the block never ends and everything after it renders as code (#154).
    """
    body = "```markdown\n# Recent\n\n## 12:00\nRan:\n\n```bash\nls -la\n```"
    recent = parse_consolidation_response(body)[0]
    assert recent.endswith("```"), f"inner code block lost its terminator: {recent!r}"
    assert recent.count("```") % 2 == 0, f"unbalanced fences: {recent!r}"


def test_plain_code_block_is_not_treated_as_a_wrapper():
    """```bash opens a code sample, not a wrapper — it must survive intact."""
    body = "```bash\nls -la\n```"
    recent = parse_consolidation_response(body)[0]
    assert "```bash" in recent, f"code fence was eaten: {recent!r}"
    assert "ls -la" in recent


def test_whole_response_wrap_does_not_orphan_a_fence_in_archive():
    """A fence around the WHOLE response closes inside the archive section.

    The archive half then has no opening fence of its own, so a per-section
    strip cannot see the closer and the orphan ``` lands in archive.md — the
    shape #126 was originally reported with.
    """
    text = ("```markdown\n===RECENT===\n# Recent\n\n## 2026-07-02\nx\n"
            "===ARCHIVE===\n# Archive\n\nold\n```")
    recent, archive = parse_consolidation_response(text)
    assert "```" not in archive, f"orphan fence in archive: {archive!r}"
    assert "```" not in recent, f"orphan fence in recent: {recent!r}"
    assert archive.startswith("# Archive")
    assert recent.count("# Recent") == 1


# --- Fence matching by character + run length (review of #154) --------------
#
# Two earlier attempts corrupted content: "strip a leading fence and any
# trailing fence" cut the terminator off a code sample, and counting fence-ish
# lines for parity was defeated by any nested fence. These pin the shapes that
# broke each of them.

BT3 = "`" * 3
BT4 = "`" * 4


def test_four_backtick_wrapper_is_stripped():
    """A model wrapping content that itself contains fences uses 4 backticks.

    Matching only 3-backtick openers left this wrapper in place, reproducing the
    doubled header and the orphan fence in archive.md — the #126 artifact.
    """
    text = (f"{BT4}markdown\n===RECENT===\n# Recent\n\n## 12:00\nx\n"
            f"===ARCHIVE===\n# Archive\n\nold\n{BT4}")
    recent, archive = parse_consolidation_response(text)
    assert "`" not in archive, f"orphan fence in archive: {archive!r}"
    assert recent.count("# Recent") == 1, f"doubled header: {recent!r}"


def test_nested_longer_fence_does_not_cost_an_inner_block_its_terminator():
    """A 4-backtick block nested in a 3-backtick wrap must not skew the match.

    Every inner block here closes: ```` opens and closes around a bare ```,
    then ```bash opens and closes. Nothing is left that could be read as the
    wrapper's own closer, so this is a truncated wrap — the opener goes and the
    body survives line for line. Counting ``` as a substring cannot check that,
    because it reads each ```` as one, so pin the fence lines themselves.
    """
    body = (f"{BT3}markdown\n# Recent\n\nFence example:\n\n{BT4}\n{BT3}\n{BT4}\n\n"
            f"Ran:\n\n{BT3}bash\nls\n{BT3}")
    out = parse_consolidation_response(body)[0]
    fences = [line for line in out.split("\n") if line.startswith("`")]
    assert fences == [BT4, BT3, BT4, f"{BT3}bash", BT3], f"body altered: {out!r}"
    assert out.rstrip().endswith(BT3), f"inner block lost its terminator: {out!r}"
    assert f"{BT3}markdown" not in out, f"wrapper survived: {out!r}"
    assert out.count("# Recent") == 1, f"doubled header: {out!r}"


def test_leading_text_sample_is_not_a_wrapper():
    """```text is a plausible tag for a pasted log — and it closes mid-body.

    A fence whose closer is not the last line encloses part of the content, so
    the body must be left exactly as it is.
    """
    body = f"{BT3}text\nERROR log line\n{BT3}\n\n## 12:00\n- fixed it"
    out = parse_consolidation_response(body)[0]
    assert f"{BT3}text" in out, f"opener was eaten: {out!r}"
    assert out.count(BT3) % 2 == 0, f"unbalanced fences: {out!r}"


def test_tilde_wrapper_is_stripped():
    """~~~ is a CommonMark-legal fence and models do emit it."""
    body = "~~~markdown\n# Recent\n\n## 12:00\n- x\n~~~"
    out = parse_consolidation_response(body)[0]
    assert "~~~" not in out, f"tilde fence survived: {out!r}"
    assert out == "# Recent\n\n## 12:00\n- x"


def test_bare_inner_fence_does_not_veto_a_whole_response_wrap():
    """An info-less inner block is textually a closer — it must not read as one.

    Pasted shell output is usually fenced with a bare ```, whose opening line is
    indistinguishable from the wrapper's own closer. Taking the first match read
    that as closing mid-body and left the wrapper in place, reviving #126.
    """
    text = (f"{BT3}markdown\n===RECENT===\n# Recent\n\n## 12:00\nRan:\n\n"
            f"{BT3}\nls -la\n{BT3}\n===ARCHIVE===\n# Archive\n\nold\n{BT3}")
    recent, archive = parse_consolidation_response(text)
    assert recent.count("# Recent") == 1, f"doubled header: {recent!r}"
    assert f"{BT3}markdown" not in recent, f"wrapper survived: {recent!r}"
    assert recent.count(BT3) == 2, f"inner block damaged: {recent!r}"
    assert "`" not in archive, f"orphan fence in archive: {archive!r}"


def test_truncated_wrap_ending_mid_code_sample_still_loses_its_opener():
    """Generation cut off inside an inner block must not save the wrapper.

    Treating "an inner block never closed" as "the leading fence is that
    block's own opener" left the wrapper in place for the commonest truncation
    shape there is, reviving #126.
    """
    body = f"{BT3}markdown\n# Recent\n\n## 12:00\nRan:\n\n{BT3}bash\nls -la"
    out = parse_consolidation_response(body)[0]
    assert out.count("# Recent") == 1, f"doubled header: {out!r}"
    assert f"{BT3}markdown" not in out, f"wrapper survived: {out!r}"
    assert out.endswith("ls -la"), f"content altered: {out!r}"


def test_closed_inner_block_does_not_pass_for_the_wrappers_own_closer():
    """A balanced inner block is not evidence that the wrapper closed.

    Scanning the raw body for any closer-shaped line counted the fences of an
    inner block that had already opened AND closed, read that as the wrapper
    closing mid-body, and kept the wrapper — the #126 shape again, on output
    truncated after a complete code sample.
    """
    body = (f"{BT3}markdown\n# Recent\n\n## 12:00\nRan:\n\n{BT3}\nls -la\n{BT3}\n\n"
            "then it was cut off")
    out = parse_consolidation_response(body)[0]
    assert out.count("# Recent") == 1, f"doubled header: {out!r}"
    assert f"{BT3}markdown" not in out, f"wrapper survived: {out!r}"
    assert out.endswith("then it was cut off"), f"content altered: {out!r}"


def test_truncation_inside_a_bare_inner_block_still_loses_the_wrapper():
    """The tagged and untagged truncation shapes must end up the same way.

    A bare ``` cut off mid-block is textually the same shape as a pasted log
    fenced at the top of the body — one dangling fence with content after it —
    so the fence grammar alone read it as the leading fence's own closer and
    kept the wrapper. What separates them is the content.
    """
    body = f"{BT3}markdown\n# Recent\n\n## 12:00\nRan:\n\n{BT3}\nls -la"
    out = parse_consolidation_response(body)[0]
    assert out.count("# Recent") == 1, f"doubled header: {out!r}"
    assert f"{BT3}markdown" not in out, f"wrapper survived: {out!r}"
    assert out.endswith("ls -la"), f"content altered: {out!r}"


def test_stray_bare_fence_does_not_shadow_the_wrappers_own_closer():
    """A bare inner opener must not claim the last line before the wrapper can.

    The inner-close branch ran first, so the wrapper's own final fence was
    spent closing a stray bare fence and BOTH lines survived into memory. A
    bare opener is itself ambiguous; reading it as stray content leaves one
    fence, not two. A tagged block is not ambiguous and still keeps its
    terminator — see test_code_sample_inside_summary_keeps_its_terminator.
    """
    out = _strip_wrapping_fence(f"{BT3}markdown\n# Recent\n## 12:00 x\n{BT3}\n{BT3}")
    assert out.count(BT3) == 1, f"wrapper closer was spent on the stray: {out!r}"
    assert out.startswith("# Recent"), f"content altered: {out!r}"


def test_wrapper_tagged_outside_the_allowlist_is_still_stripped():
    """A tag we did not think of must not veto a cleanly closed wrapper.

    The allowlist gated the whole algorithm, so ```yaml around a section kept
    its fence and doubled the header — while ```bash around a code sample must
    still survive. What separates them is not the tag, it is that one closes
    cleanly around a section body and the other does not.
    """
    for tag in ("yaml", "output", "response"):
        text = f"{BT3}{tag}\n===RECENT===\n# Recent\n\n## 12:00\nstuff\n{BT3}"
        out = parse_consolidation_response(text)[0]
        assert out == "# Recent\n\n## 12:00\nstuff", f"{tag}: {out!r}"
    sample = f"{BT3}bash\nls -la\n{BT3}"
    assert parse_consolidation_response(sample)[0].count(f"{BT3}bash") == 1


def test_log_fenced_ahead_of_the_envelope_keeps_its_fence():
    """The tiebreaker must read the body's OPENING, not merely search it.

    This runs once over the whole response, where every genuine envelope has
    ===RECENT=== somewhere in it. Testing containment broke the tie the same
    way every time, so a log fenced ahead of the envelope lost its opener and
    orphaned its closer.
    """
    text = (f"{BT3}\n$ ls -la\ntotal 24\n{BT3}\n===RECENT===\n# Recent\n\n## 12:00\n"
            f"Ran ls.\n\n===ARCHIVE===\n# Archive\n\nnothing yet")
    recent, archive = parse_consolidation_response(text)
    assert recent.count(BT3) == 2, f"log fence broken up: {recent!r}"
    assert "$ ls -la" in recent, f"log content lost: {recent!r}"
    assert archive.startswith("# Archive")


def test_dangling_fence_of_another_character_does_not_save_the_wrapper():
    """A stray ~~~ cannot close a ``` wrapper, so it cannot vouch for it either."""
    assert _strip_wrapping_fence(f"{BT3}markdown\nbody\n~~~") == "body\n~~~"


def test_degenerate_fence_inputs_do_not_crash():
    """Empty, whitespace-only, and lone-fence inputs must parse to nothing."""
    for src in ("", "   \n  ", BT3, f"{BT3}markdown"):
        recent, archive = parse_consolidation_response(src)
        assert archive == ""
        assert "`" not in recent, f"fence leaked through for {src!r}: {recent!r}"
