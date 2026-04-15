"""Consolidation logic — compress staging files into recent and archive memory.

This is the pipeline stage that runs overnight (or on demand) to merge
daily staging files into the two long-lived memory files:

- **recent.md** — active, high-relevance entries from the last few days.
- **archive.md** — older entries, compressed and deduplicated.

The flow is: shell collects file contents -> this module builds the prompt
and calls Haiku -> Haiku returns a structured response with ``===RECENT===``
and ``===ARCHIVE===`` delimiters -> this module parses and returns the
sections -> shell writes the output files.

Typical usage::

    from pipeline.consolidate import consolidate
    result = consolidate(staging_contents, recent_text, archive_text)
    # result.recent  -> new content for recent.md
    # result.archive -> new content for archive.md
"""

from __future__ import annotations

from .prompts import build_consolidation_prompt
from .haiku import call_haiku
from .types import ConsolidationResult, TokenUsage


def consolidate(
    staging_contents: dict[str, str],
    recent: str,
    archive: str,
) -> ConsolidationResult:
    """Run full consolidation: build prompt, call Haiku, parse response.

    Args:
        staging_contents: Mapping of ``{filename: content}`` for each
            staging file to consolidate.
        recent: Current content of recent.md (may be empty).
        archive: Current content of archive.md (may be empty).

    Returns:
        ConsolidationResult with new recent/archive content and token usage.

    Raises:
        RuntimeError: If the Haiku call fails or times out.
    """
    prompt = build_consolidation_prompt(staging_contents, recent, archive)
    result = call_haiku(prompt, timeout=180)

    recent_new, archive_new = parse_consolidation_response(result.text)

    return ConsolidationResult(
        recent=recent_new,
        archive=archive_new,
        tokens=result.tokens,
    )


def parse_consolidation_response(text: str) -> tuple[str, str]:
    """Parse Haiku's structured response into recent and archive sections.

    Splits on ``===RECENT===`` and ``===ARCHIVE===`` delimiters. If
    delimiters are missing, falls back gracefully: missing archive treats
    the entire response as recent; missing both delimiters uses the raw
    text as recent. Ensures ``# Recent`` and ``# Archive`` headers are
    present in the output.

    Expected format::

        ===RECENT===
        # Recent
        ...content...

        ===ARCHIVE===
        # Archive
        ...content...

    Args:
        text: Raw text response from Haiku.

    Returns:
        Tuple of ``(recent_content, archive_content)``, both stripped
        and with headers ensured. Archive may be empty if the model
        did not produce an archive section.
    """
    recent = ""
    archive = ""

    if "===RECENT===" in text and "===ARCHIVE===" in text:
        parts = text.split("===ARCHIVE===", 1)
        recent = parts[0].replace("===RECENT===", "").strip()
        archive = parts[1].strip()
    elif "===RECENT===" in text:
        recent = text.replace("===RECENT===", "").strip()
    else:
        # Fallback: treat entire response as recent
        recent = text.strip()

    # Ensure headers are present
    if recent and not recent.startswith("# Recent"):
        recent = "# Recent\n\n" + recent
    if archive and not archive.startswith("# Archive"):
        archive = "# Archive\n\n" + archive

    return recent, archive
