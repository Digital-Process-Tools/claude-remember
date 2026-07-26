"""Tests for the mutations that survived a green suite (#175).

A mutation-testing pass changed 34 things about `pipeline/*.py` and
`scripts/log.sh`; the suite stayed green for eight of them. A surviving mutant
is a line the tests do not actually depend on — the code could be wrong there
and nothing would say so.

Each test below was written against its mutant: applied by hand, confirmed to
make the test fail, reverted. A test that does not fail against the change it
describes is decoration.

The two high-severity survivors are covered where they belong —
`tests/test_position_store.py` (LRU eviction) and `tests/test_log_sh.py`
(the TZ fallback, which needed a clock-independent zone pair). These are the
rest.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.consolidate import (  # noqa: E402
    ConsolidationSkipped,
    ConsolidationTooLarge,
    consolidate,
    parse_consolidation_response,
)
from pipeline.prompts import build_consolidation_prompt  # noqa: E402
from pipeline.shell import _POSITION_SLOTS  # noqa: E402
from pipeline.types import HaikuResult, TokenUsage  # noqa: E402


def _result(text: str, *, is_skip: bool = False) -> HaikuResult:
    return HaikuResult(
        text=text,
        tokens=TokenUsage(input=10, output=10, cache=0, cost_usd=0.0),
        is_skip=is_skip,
    )


def test_a_skip_is_refused_even_when_the_text_looks_valid():
    """`result.is_skip or not _is_valid_consolidation(...)` — both halves.

    The `is_skip` half was removable because every SKIP in the fixtures also
    failed the shape check, so the second condition caught them anyway. A
    response that says SKIP *and* carries a header would then have been written
    into memory as a consolidation.
    """
    skip_with_header = _result(
        "SKIP\n\n===RECENT===\n# Recent\n\n## 2026-06-01\nnothing to do\n",
        is_skip=True,
    )
    with patch("pipeline.consolidate.call_haiku", return_value=skip_with_header):
        with pytest.raises(ConsolidationSkipped):
            consolidate(
                staging_contents={"today-2026-06-01.md": "x"},
                recent="",
                archive="",
            )


def test_the_archive_keeps_everything_after_the_first_delimiter():
    """`split("===ARCHIVE===", 1)` — the maxsplit is load-bearing.

    Memory is prose the model wrote, and prose about this pipeline can quote
    its own delimiters. Without the limit, an archive that mentions
    `===ARCHIVE===` is silently cut at the mention and everything past it is
    dropped from permanent memory.
    """
    text = (
        "===RECENT===\n# Recent\n\n## 2026-06-01\nrecent work\n\n"
        "===ARCHIVE===\n# Archive\n\n"
        "## 2026-05-01\nWe changed the ===ARCHIVE=== delimiter handling.\n"
        "TAIL_MARKER\n"
    )
    _, archive = parse_consolidation_response(text)

    assert "TAIL_MARKER" in archive, (
        "everything after the archive's own mention of the delimiter was "
        "dropped — memory truncated by its own vocabulary"
    )


def test_a_prompt_exactly_at_the_cap_is_allowed():
    """`prompt_bytes > max_prompt_bytes`, not `>=`.

    Off by one at the boundary means a prompt of exactly the configured size is
    refused, and consolidation skips a run it could have completed. Nothing
    exercised the boundary itself, so the comparison could flip unnoticed.
    """
    staging = {"today-2026-06-01.md": "some work"}
    exact = len(build_consolidation_prompt(staging, "", "").encode("utf-8"))

    ok = _result("===RECENT===\n# Recent\n\n## 2026-06-01\nx\n\n===ARCHIVE===\n# Archive\n")
    with patch("pipeline.consolidate.call_haiku", return_value=ok):
        consolidate(staging, "", "", max_prompt_bytes=exact)  # must not raise

    with patch("pipeline.consolidate.call_haiku", return_value=ok):
        with pytest.raises(ConsolidationTooLarge):
            consolidate(staging, "", "", max_prompt_bytes=exact - 1)


def test_staging_files_reach_the_prompt_in_date_order():
    """`sorted(staging_contents.items())` — fixtures had one file each.

    dict order is insertion order, and the caller builds that dict by globbing
    a directory. Unsorted, a consolidation can be handed 2026-06-03 before
    2026-06-01 and write a recent.md whose days run backwards.
    """
    out_of_order = {
        "today-2026-06-03.md": "THIRD",
        "today-2026-06-01.md": "FIRST",
        "today-2026-06-02.md": "SECOND",
    }
    prompt = build_consolidation_prompt(out_of_order, "", "")

    positions = [prompt.index(marker) for marker in ("FIRST", "SECOND", "THIRD")]
    assert positions == sorted(positions), (
        f"staging days reached the prompt out of order: {positions} — the "
        "consolidation would narrate them in the order the glob happened to "
        "return"
    )


def test_the_position_slot_count_is_the_number_we_think_it_is():
    """The existing assertion imports the constant it checks, so it holds for
    any value. This pins the number itself.

    It is not arbitrary: the store is read on every tool call, and it bounds
    how many interleaved sessions keep their resume position. Changing it is a
    real decision, so it should require editing a test that says so.
    """
    assert _POSITION_SLOTS == 32


def test_the_home_lookup_prefers_the_environment(monkeypatch):
    """Covered in test_slug_parity.py; asserted here too because this file is
    where someone will look after the next mutation run."""
    from pipeline.extract import _session_dir

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", "/env-home")
    monkeypatch.setattr("pipeline.extract.os.path.expanduser", lambda p: "/other")
    assert _session_dir("/p").startswith("/env-home/")
