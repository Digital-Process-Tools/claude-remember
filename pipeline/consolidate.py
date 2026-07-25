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

import re

from .prompts import build_consolidation_prompt
from .haiku import call_haiku
from .types import ConsolidationResult, TokenUsage


# A real memory entry header: "## HH:MM", "## Week of ...", or "## YYYY-MM-DD".
_ENTRY_HEADER = re.compile(r"(?m)^## (\d{2}:\d{2}|Week of |\d{4}-\d{2}-\d{2})")

# Any line that opens or closes a Markdown code fence.
_FENCE_LINE = re.compile(r"^\s*```")

# The opener of a fence a model would wrap PROSE in: bare, or tagged with a
# document-ish language. A ```bash / ```python opener is a code sample that
# belongs to the content and must never be treated as a wrapper.
_WRAP_OPENER = re.compile(r"^\s*```\s*(markdown|md|text|txt|plaintext)?\s*$", re.I)

# A bare closing fence.
_BARE_FENCE = re.compile(r"^\s*```\s*$")


def _strip_wrapping_fence(text: str) -> str:
    """Strip a Markdown code fence wrapping an entire body, and nothing else.

    Haiku occasionally returns its output wrapped in a ```markdown ... ``` fence.
    Left in place that fence defeats the ``startswith("# Recent")`` check in
    parse_consolidation_response, so a second header gets prepended — the
    doubled-header + orphaned-fence artifact of issue #126.

    The hard part is not stripping it, it is *recognising* it. Naively removing
    a leading fence and then any trailing fence corrupts legitimate content: a
    summary whose last line closes a ```bash sample loses that terminator and
    the code block never ends (#154). So a fence is treated as a wrapper only
    when it genuinely looks like one:

    * the first line must be a bare or document-tagged fence — ```bash opens a
      code sample, not a wrapper;
    * the closing fence is removed only when the fences in between are balanced,
      so an inner code block keeps its own terminator;
    * when they are unbalanced (truncated output — the model dropped its final
      fence), only the opener is stripped, leaving inner blocks intact.

    Args:
        text: A body, before header normalization.

    Returns:
        The body with a wrapping code fence removed, stripped.
    """
    t = text.strip()
    lines = t.split("\n")
    if not lines or not _WRAP_OPENER.match(lines[0]):
        return t

    body = lines[1:]
    # Balanced inner fences mean the last line is this wrapper's own closer.
    if body and _BARE_FENCE.match(body[-1]):
        inner = sum(1 for line in body[:-1] if _FENCE_LINE.match(line))
        if inner % 2 == 0:
            body = body[:-1]

    return "\n".join(body).strip()


class ConsolidationSkipped(Exception):
    """Raised when Haiku declined (SKIP) or returned non-conforming output.

    Signals that the consolidation produced no usable result. The caller
    MUST NOT overwrite recent.md/archive.md and MUST NOT retire (rename to
    ``.done.md``) the source staging files — they are left for the next run.
    """


class ConsolidationTooLarge(ConsolidationSkipped):
    """Raised when the assembled prompt exceeds ``max_prompt_bytes``.

    Subclass of ConsolidationSkipped so existing ``except ConsolidationSkipped``
    handlers keep their "leave everything untouched" behavior unchanged. Callers
    that CAN shrink the input (e.g. rotate archive.md to a dated sibling and
    retry with a fresh archive) may catch this subclass specifically to recover
    instead of skipping forever.
    """


def _is_valid_consolidation(text: str) -> bool:
    """Return True only if the model output is real consolidated memory.

    The expected output is the ``===RECENT===`` / ``===ARCHIVE===`` envelope.
    A response with neither the recent delimiter nor a single ``## `` entry
    header is conversational text (a refusal, a clarifying question, or a
    "here is what I would compress…" preamble) and must be rejected — writing
    it would replace memory with chatter and the source files would be lost.

    Args:
        text: Raw text response from Haiku.

    Returns:
        True if the text carries the recent delimiter or at least one memory
        entry header; False for empty or purely conversational responses.
    """
    t = text.strip()
    if not t:
        return False
    if "===RECENT===" in t:
        return True
    return _ENTRY_HEADER.search(t) is not None


def consolidate(
    staging_contents: dict[str, str],
    recent: str,
    archive: str,
    max_prompt_bytes: int = 0,
) -> ConsolidationResult:
    """Run full consolidation: build prompt, call Haiku, parse response.

    Args:
        staging_contents: Mapping of ``{filename: content}`` for each
            staging file to consolidate.
        recent: Current content of recent.md (may be empty).
        archive: Current content of archive.md (may be empty).
        max_prompt_bytes: Upper bound on the assembled prompt's UTF-8 byte
            size. ``0`` disables the guard. When the prompt would exceed the
            bound, consolidation is SKIPPED (not truncated) -- see below.

    Returns:
        ConsolidationResult with new recent/archive content and token usage.

    Raises:
        RuntimeError: If the Haiku call fails or times out.
        ConsolidationSkipped: If the assembled prompt exceeds
            ``max_prompt_bytes``, or the model declined (SKIP) or returned
            non-conforming output. The caller must not write or retire files.
    """
    prompt = build_consolidation_prompt(staging_contents, recent, archive)

    # Oversized-prompt guard. Mirrors the save path's extract_max_bytes cap
    # (build-prompt), but SKIPS instead of truncating: consolidation rewrites
    # recent.md/archive.md, so tail-truncating the input would feed the model a
    # partial archive and permanently drop the dropped head on the next write.
    # Skipping leaves staging + memory untouched (handled by the caller's
    # ConsolidationSkipped path) -- strictly better than firing a doomed call
    # that fails with "Prompt is too long" and stalls every subsequent save.
    if max_prompt_bytes > 0:
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > max_prompt_bytes:
            raise ConsolidationTooLarge(
                f"consolidation prompt too large ({prompt_bytes} bytes > "
                f"{max_prompt_bytes} cap) -- skipping to avoid a context-window "
                f"overflow; staging + memory left untouched"
            )

    result = call_haiku(prompt, timeout=180)

    # Guard: never let a SKIP or a conversational reply overwrite memory.
    # Without this, a non-conforming response falls through to the parser's
    # fallback and chatter is written as recent.md (and the source staging
    # files are then irreversibly renamed to .done.md by the shell).
    if result.is_skip or not _is_valid_consolidation(result.text):
        raise ConsolidationSkipped(
            "Haiku returned no usable consolidation "
            "(SKIP or missing ===RECENT===/entry headers)"
        )

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

    # A wrap around the WHOLE response puts its closing fence at the very end,
    # inside the archive section — which then has no opening fence of its own,
    # so a per-section strip cannot see it and the orphan ``` lands in
    # archive.md. That is the shape #126 was originally reported with, so the
    # whole-response wrapper has to come off before the split (#154).
    text = _strip_wrapping_fence(text)

    if "===RECENT===" in text and "===ARCHIVE===" in text:
        parts = text.split("===ARCHIVE===", 1)
        recent = _strip_wrapping_fence(parts[0].replace("===RECENT===", ""))
        archive = _strip_wrapping_fence(parts[1])
    elif "===RECENT===" in text:
        recent = _strip_wrapping_fence(text.replace("===RECENT===", ""))
    else:
        # Fallback: treat entire response as recent
        recent = _strip_wrapping_fence(text)

    # Ensure headers are present
    if recent and not recent.startswith("# Recent"):
        recent = "# Recent\n\n" + recent
    if archive and not archive.startswith("# Archive"):
        archive = "# Archive\n\n" + archive

    return recent, archive
