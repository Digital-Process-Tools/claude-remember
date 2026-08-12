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

from .prompts import build_consolidation_prompt, consolidation_template
from .haiku import call_haiku
from .entry_header import ANY_HEADER_ERE
from .types import ConsolidationResult, TokenUsage


# A real memory entry header: "## HH:MM", "## Week of ...", or "## YYYY-MM-DD".
# Was spelled out here as 24h-only, while save-session.sh accepted 12h too —
# two answers to one question, agreeing only because the header is normally
# rewritten (#177). The 12h form does reach memory, through the #139 fallback,
# and an entry this did not match read as prose rather than as memory.
_ENTRY_HEADER = re.compile(ANY_HEADER_ERE, re.MULTILINE)

# A fence opener: indent, a run of 3+ backticks or tildes, and an optional
# info string. CommonMark matches a fence by CHARACTER and RUN LENGTH, which is
# what lets a ````-wrapped body legally contain ``` blocks.
_FENCE_OPENER = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+.-]*)\s*$")

# Info strings a model uses when wrapping PROSE. Anything else (```bash,
# ```python, ```json) opens a code sample that belongs to the content.
_WRAP_TAGS = frozenset({"", "markdown", "md", "text", "txt", "plaintext"})


def _looks_like_a_section(body: str) -> bool:
    """Whether a body reads as consolidation output rather than pasted content.

    Used only to break a tie the fence grammar cannot: see the last branch of
    _strip_wrapping_fence. Deliberately narrow, and it must OPEN this way, not
    merely contain it. An entry header like ``## 12:00`` is not enough because
    a pasted log can carry one; and this runs once over the whole response,
    where every genuine envelope contains ``===RECENT===`` somewhere — so
    testing containment would make the tie always break the same way, which is
    no tiebreaker at all.

    The tie it cannot break: any fenced content whose first line merely STARTS
    with one of these strings — a quoted excerpt of an older recent.md, but
    equally a log headed ``# Recently Viewed`` — is read as a wrapper and loses
    its fence. Prefix matching is deliberate (a section header carries a date
    or title after it), and the cost is one fence line, never content. It is
    rarer than the truncation it is traded against, so it is accepted rather
    than patched around with a signal no more reliable than this one.

    Args:
        body: A body with its leading fence already removed.

    Returns:
        True if the body opens as a section or still carries its envelope.
    """
    return body.startswith(("# Recent", "# Archive", "===RECENT===", "===ARCHIVE==="))


def _strip_wrapping_fence(text: str) -> str:
    """Strip a code fence wrapping an entire body, and nothing else.

    Haiku occasionally returns its output wrapped in a ```markdown ... ``` fence.
    Left in place that fence defeats the ``startswith("# Recent")`` check in
    parse_consolidation_response, so a second header gets prepended — the
    doubled-header + orphaned-fence artifact of issue #126.

    The hard part is recognising a wrapper, not removing one. Two earlier
    attempts corrupted real content: stripping "a leading fence and any trailing
    fence" cut the terminator off a ```bash sample, and counting fence-ish lines
    for parity was defeated by any nested fence. So match fences the way
    CommonMark does — by fence character and run length:

    * an opener with no info string or a document-ish one (```markdown, ```txt)
      is taken at its word. Any other tag still gets read — an allowlist of
      five strings cannot anticipate ```yaml — but has to earn it by closing
      cleanly around a body that reads as a section, which ```bash around a
      code sample never does;
    * the closer must use the same character with a run at least as long, which
      is precisely what lets a ````-wrapped body contain ``` blocks untouched;
    * it is only a wrapper if that closer is the LAST line, reached at nesting
      depth zero. A bare ``` opening an inner block is textually identical to a
      closer, so the body has to be walked with a fence stack rather than
      scanned for the first match;
    * otherwise what is left OPEN at the end of the body decides. A fence still
      dangling there can be re-read as the leading fence's own closer — but
      only if it could actually close it: bare, same character, run at least as
      long. Then the leading fence enclosed part of the content, not all of it,
      and the whole thing is left alone — unless the body reads as a section,
      because a pasted log fenced at the top and a wrap truncated inside a bare
      code block are the same shape and only the content separates them;
    * anything else is a wrapper whose final fence never arrived — truncated
      model output — so strip just the opener. Inner blocks that all closed are
      no evidence the wrapper did, and a dangling ```bash or ~~~ could never
      have closed it, so neither may keep the wrapper alive.

    Args:
        text: A body, before header normalization.

    Returns:
        The body with a wrapping code fence removed, stripped.
    """
    t = text.strip()
    lines = t.split("\n")
    opener = _FENCE_OPENER.match(lines[0])
    if not opener:
        return t

    marker, info = opener.group(1), opener.group(2).lower()
    documentish = info in _WRAP_TAGS

    # A closer: same character, run at least as long, and no info string.
    closer = re.compile(r"^\s*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$")
    # Walk the body tracking nesting. An inner block opened with a bare ``` is
    # textually identical to this wrapper's closer, so scanning for the first
    # match read such an opener as closing mid-body and left the wrapper in
    # place. Only a fence at depth zero, on the LAST line, closes the wrapper.
    inner = None
    for i in range(1, len(lines)):
        fence = _FENCE_OPENER.match(lines[i])
        if not fence:
            continue
        mark, tag = fence.group(1), fence.group(2)
        # The last line is tested before the inner block gets to claim it, but
        # only against a BARE inner opener — that one is itself ambiguous, and
        # reading it as stray content beats leaving two fences in the output.
        # A tagged ```bash block is unambiguous and keeps its terminator.
        if i == len(lines) - 1 and closer.match(lines[i]) and not (inner and inner[1]):
            wrapped = "\n".join(lines[1:i]).strip()
            if documentish or _looks_like_a_section(wrapped):
                return wrapped
            return t
        if inner is not None:
            if not tag and mark[0] == inner[0][0] and len(mark) >= len(inner[0]):
                inner = None
            continue
        inner = (mark, tag)

    body = "\n".join(lines[1:]).strip()
    # Nothing closed the wrapper on the last line. What is left over decides:
    # a fence still open at the end of the body can be read as the leading
    # fence's own closer, but only if it could actually close it — bare, same
    # character, run at least as long. Anything else (everything balanced, or a
    # dangling ```bash or ~~~ that could never close it) is a wrapper whose
    # final fence never arrived, so only the opener goes.
    if inner is not None and not inner[1] and closer.match(inner[0]):
        # Grammar has run out: a pasted log fenced at the top of the body and a
        # wrap truncated inside a bare code block are the same shape, one
        # dangling fence with content after it. Only the content can tell them
        # apart, and we know what the payload is supposed to look like.
        if not _looks_like_a_section(body):
            return t
    elif not documentish and not _looks_like_a_section(body):
        # ```bash opens a code sample. Only a section body argues otherwise.
        return t
    return body


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


# A line has to be this long before it counts as an instruction. Short enough
# to catch `[archive.md content here]` (25) — the placeholder that replaced the
# reporter's whole archive.md in #202 — and long enough to exclude every line a
# real consolidation legitimately shares with the template: `===RECENT===` (12),
# `===ARCHIVE===` (13), `# Recent` (8), `# Archive` (9).
_MIN_MARKER_LEN = 24


def _instruction_markers() -> tuple[str, ...]:
    """Template lines that a real consolidation can never contain.

    Everything in the template that is not a ``{{PLACEHOLDER}}`` is an
    *instruction*: prose addressed to the model. The model's job is to return
    compressed memory, so it has no reason to reproduce any of it. An echo of
    the prompt, on the other hand, cannot avoid it.

    Computed from the template file rather than written out here, so editing
    the prompt cannot silently retire the guard.
    """
    markers = []
    for line in consolidation_template().splitlines():
        line = line.strip()
        if not line or "{{" in line or len(line) < _MIN_MARKER_LEN:
            continue
        markers.append(line)
    return tuple(markers)


def _echoes_the_prompt(text: str) -> bool:
    """Whether the response is (or embeds) a quotation of the prompt.

    This is the check that has to exist for #202, and the reason it is phrased
    negatively. The old guard asked "does this look like memory?", and every
    signal it accepted — the ``===RECENT===`` envelope, a ``## HH:MM |`` entry
    header — is present in the prompt itself: the envelope because the template
    prints it as the required output format, the headers because the staging
    files are embedded verbatim. So a hook block message, which quotes the
    whole prompt back, satisfied every positive test and was written to
    recent.md as consolidated memory. The next run quoted the poisoned file
    into a new prompt and it nested one level deeper each time.

    **A positive-only validity check cannot reject an echo of its own input**,
    because every signal it looks for is by construction present in that input.
    Adding another positive pattern reproduces the bug with more steps. The
    only thing that separates the two is what the echo carries and a real
    answer does not: the instructions.

    False positives cost a skipped consolidation, which is non-destructive —
    staging and memory are both left untouched and the next run retries. False
    negatives cost the permanent record. The asymmetry is deliberate.
    """
    return any(marker in text for marker in _instruction_markers())


def _is_valid_consolidation(text: str) -> bool:
    """Return True only if the model output is real consolidated memory.

    The expected output is the ``===RECENT===`` / ``===ARCHIVE===`` envelope.
    A response with neither the recent delimiter nor a single ``## `` entry
    header is conversational text (a refusal, a clarifying question, or a
    "here is what I would compress…" preamble) and must be rejected — writing
    it would replace memory with chatter and the source files would be lost.

    Those two positive tests are kept, but they now run only after the response
    has been shown not to be an echo of the prompt (#202) — see
    ``_echoes_the_prompt`` for why that order is the whole point.

    Args:
        text: Raw text response from Haiku.

    Returns:
        True if the text carries the recent delimiter or at least one memory
        entry header, and is not a quotation of the prompt; False otherwise.
    """
    t = text.strip()
    if not t:
        return False
    if _echoes_the_prompt(t):
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

    # The cap above is the size this pipeline refuses to SEND. Refusing to
    # WRITE more than that is the same number in the other direction, and the
    # other direction is where #346 came from: ``capture_output=True`` bounds
    # the CLI subprocess by a wall clock and not by bytes, so the size of a
    # response was never a quantity anything downstream measured.
    # ``cmd_consolidate`` writes ``result.recent`` to a temp file verbatim and
    # run-consolidation.sh copies it over recent.md — an overwrite, never an
    # append, and unbounded either way.
    #
    # One oversized write is permanent, which is what makes this the bug
    # rather than one bad round. recent.md is part of the input the cap is
    # measured on, so the round after an oversized write assembles an
    # oversized prompt and skips, and so does every round after that.
    # Rotating the archive is no escape when the bulk is recent.md. The file
    # can then never grow again and never shrink either — which from outside
    # is indistinguishable from a file that is only ever appended to, and is
    # exactly how the reporter read it.
    #
    # Deliberately NOT ConsolidationTooLarge: that subclass means "the input
    # was too big, shrink it and retry", and the caller acts on it by rotating
    # archive.md. Nothing about the input was wrong here, so a retry would
    # spend another model call to be handed another oversized response and
    # would rotate away a healthy archive for nothing.
    #
    # Skipping is the established non-destructive direction (see
    # ``_echoes_the_prompt``): staging and memory are both left intact and the
    # next run retries, so a false positive costs one round and a false
    # negative costs the permanent record.
    if max_prompt_bytes > 0:
        response_bytes = len(result.text.encode("utf-8"))
        if response_bytes > max_prompt_bytes:
            raise ConsolidationSkipped(
                f"consolidation response too large ({response_bytes} bytes > "
                f"{max_prompt_bytes} cap) -- refusing to write it to memory; "
                f"staging + memory left untouched"
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
