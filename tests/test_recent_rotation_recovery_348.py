"""A store already over the consolidation cap must be able to recover (#348).

#347 stopped a store *getting* into the over-cap state and stopped an oversized
file freezing the session. It does not get anybody *out* of it. Once
``recent.md`` alone exceeds ``thresholds.consolidate_max_bytes``, every round
sizes the store, finds it over, and skips -- forever. ``_rotate_archive`` is the
only escape hatch in the tree and it only ever touches ``archive.md``, so it
cannot help when ``recent.md`` is the bulk, which is exactly #346's shape. The
staging files never retire either, because retirement happens after a
*successful* round, so they accumulate for as long as the condition lasts.

The recovery is the move ``archive.md`` has had since #123: rotate to a dated
sibling, start a fresh empty file, keep every byte on disk and reachable
(#124 named those siblings at session start so the slice stops being invisible).

**The condition that triggers it is the whole design decision.** "The store is
over the cap" is not the same question as "``recent.md`` is why". If the sum is
over because staging is enormous and ``recent.md`` is 40 KB, rotating
``recent.md`` heals nothing, throws away the span's continuity for no gain, and
the next round skips in exactly the same way. So the rule implemented here is a
measured one, not a guess:

    rotate ``recent.md`` only when dropping it brings the round under the cap --
    i.e. when the staging bytes alone fit.

That is an escalating ladder. Drop ``archive.md`` first (the existing #123
move); if staging + recent still will not fit, drop ``recent.md`` too; and if
staging alone is over the cap, rotate **nothing** and skip, because no rotation
available here would make the next round any different.

Every case below is paired. A test that only asserts a rotation did *not*
happen passes just as well against a pipeline that does nothing at all, so each
"must not rotate" case sits in the same fixture as a "must rotate" one.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline import consolidate as consolidate_mod
from pipeline import shell as shell_mod
from pipeline._tz import today_str
from pipeline.types import HaikuResult, TokenUsage

# Small on purpose. The guard measures real file sizes, so a realistic 600000
# would mean writing megabytes per test for no extra coverage.
CAP = 10000

RECENT_SENTINEL = "# Recent\n\n## 2026-01-02\n\nRECENT-SENTINEL-348\n"
ARCHIVE_SENTINEL = "# Archive\n\n## Week of 2025-12-22\n\nARCHIVE-SENTINEL-348\n"


def _response(recent_body: str = "a compressed day") -> HaikuResult:
    """A well-formed, small consolidation envelope.

    Valid on every axis except the one under test, so a pipeline with no
    rotation at all could not pass these by accident.
    """
    return HaikuResult(
        text=("===RECENT===\n# Recent\n\n## 2026-01-03\n\n" + recent_body + "\n\n"
              "===ARCHIVE===\n# Archive\n\n## Week of 2025-12-29\n\nfine\n"),
        is_skip=False,
        is_rejected=False,
        tokens=TokenUsage(input=0, output=0, cache=0, cost_usd=0.0),
    )


def _store(tmp_path: Path, *, staging: int, recent: int, archive: int) -> tuple[Path, Path]:
    """Build a store whose three parts have roughly the requested byte sizes."""
    recent_f = tmp_path / "recent.md"
    archive_f = tmp_path / "archive.md"
    if staging:
        head = "## 10:00 | main\n\n"
        (tmp_path / "today-2026-01-01.md").write_text(
            head + "s" * max(1, staging - len(head)), encoding="utf-8")
    if recent:
        recent_f.write_text(
            RECENT_SENTINEL + "r" * max(0, recent - len(RECENT_SENTINEL)), encoding="utf-8")
    if archive:
        archive_f.write_text(
            ARCHIVE_SENTINEL + "a" * max(0, archive - len(ARCHIVE_SENTINEL)), encoding="utf-8")
    return recent_f, archive_f


def _run(tmp_path: Path, recent_f: Path, archive_f: Path,
         response: HaikuResult | None = None) -> tuple[str, list[int]]:
    """Run one consolidation round, returning its stdout and a call counter."""
    called: list[int] = []

    def _fake(prompt, timeout=180):
        called.append(1)
        return response if response is not None else _response()

    buf = io.StringIO()
    with patch.object(consolidate_mod, "call_haiku", _fake):
        with redirect_stdout(buf):
            shell_mod.cmd_consolidate(str(tmp_path), str(recent_f), str(archive_f), CAP, "")
    return buf.getvalue(), called


def _rotated_recents(directory: Path) -> list[Path]:
    return sorted(directory.glob("recent-*.md"))


def _rotated_archives(directory: Path) -> list[Path]:
    return sorted(directory.glob("archive-*.md"))


# -- 1. The recovery itself -------------------------------------------------


def test_a_store_whose_recent_is_the_bulk_rotates_and_consolidates(tmp_path):
    """The #346 shape, and the state its reporter is in today.

    recent.md alone is over the cap. Nothing in the tree could shrink it, so
    every round skipped forever and the only cure was deleting the file. After
    this the round goes through: recent.md is rotated to a dated sibling and
    consolidation resumes with a fresh one.
    """
    recent_f, archive_f = _store(tmp_path, staging=500, recent=CAP * 2, archive=300)

    out, called = _run(tmp_path, recent_f, archive_f)

    assert "CONSOLIDATION_STATUS=ok" in out, (
        "an over-cap store still skips -- nothing recovers it:\n" + out
    )
    assert called, "the round never reached the model, so nothing was consolidated"
    rotated = _rotated_recents(tmp_path)
    assert len(rotated) == 1, f"recent.md was not rotated to a dated sibling: {rotated!r}"
    assert rotated[0].name == f"recent-{today_str()}.md"


def test_the_rotated_recent_keeps_every_byte(tmp_path):
    """Non-destructive is the whole point.

    The recovery this replaces was ``mv recent.md recent.md.bak && touch``,
    which discards the history. A rotation that dropped or truncated bytes
    would be that same loss wearing a better name.
    """
    recent_f, archive_f = _store(tmp_path, staging=500, recent=CAP * 2, archive=300)
    before = recent_f.read_bytes()

    _run(tmp_path, recent_f, archive_f)

    rotated = _rotated_recents(tmp_path)
    assert len(rotated) == 1
    assert rotated[0].read_bytes() == before, "the rotated slice is not byte-identical"


def test_staging_retires_once_the_round_goes_through(tmp_path):
    """Staging accumulates for as long as the condition lasts (#348).

    Retirement happens after a *successful* round, so a store stuck skipping
    never retires anything. The rotation has to actually unblock that, not just
    move a file: STAGING_PATHS_FILE is what run-consolidation.sh reads to
    rename the consumed files to .done.md.
    """
    recent_f, archive_f = _store(tmp_path, staging=500, recent=CAP * 2, archive=300)

    out, _ = _run(tmp_path, recent_f, archive_f)

    assert "STAGING_PATHS_FILE=" in out, (
        "the round produced no retire list, so staging keeps piling up:\n" + out
    )


# -- 2. The condition: rotating only when rotating helps --------------------


def test_staging_alone_over_the_cap_does_not_rotate_recent(tmp_path):
    """The judgment call, stated as a test.

    Here recent.md is small and healthy; the sum is over because staging is
    enormous. Rotating recent.md would lose an unconsolidated span's continuity
    and the very next round would skip in exactly the same way -- a destructive
    no-op. The honest path is to skip and leave the store alone.

    Its positive control is the test directly below, same fixture shape, same
    cap, differing only in which part is the bulk.
    """
    recent_f, archive_f = _store(tmp_path, staging=CAP * 2, recent=400, archive=300)
    recent_before = recent_f.read_bytes()

    out, called = _run(tmp_path, recent_f, archive_f)

    assert "CONSOLIDATION_STATUS=skip" in out, out
    assert not called, "an over-cap round must not reach the model"
    assert _rotated_recents(tmp_path) == [], (
        "recent.md was rotated where rotating it heals nothing -- the next round "
        "would skip identically, and an unconsolidated span was split for it"
    )
    assert recent_f.read_bytes() == recent_before
    assert _rotated_archives(tmp_path) == [], (
        "archive.md was rotated where rotating it heals nothing either"
    )


def test_the_paired_case_where_recent_is_the_bulk_does_rotate(tmp_path):
    """Positive control for the test above.

    Same store, same cap, same helper -- only the distribution of the bytes
    differs. Without this pair, a pipeline that rotated nothing ever would
    satisfy the "must not rotate" assertion perfectly.
    """
    recent_f, archive_f = _store(tmp_path, staging=400, recent=CAP * 2, archive=300)

    out, called = _run(tmp_path, recent_f, archive_f)

    assert "CONSOLIDATION_STATUS=ok" in out, out
    assert called, "the round never reached the model"
    assert len(_rotated_recents(tmp_path)) == 1, "recent.md was not rotated"


def test_archive_being_the_bulk_still_rotates_only_the_archive(tmp_path):
    """#123's behaviour is unchanged, and recent.md is not collateral.

    Dropping archive.md is enough here, so recent.md -- an unconsolidated span
    -- must not be rotated away as well.
    """
    recent_f, archive_f = _store(tmp_path, staging=400, recent=400, archive=CAP * 2)

    out, called = _run(tmp_path, recent_f, archive_f)

    assert "CONSOLIDATION_STATUS=ok" in out, out
    assert called
    assert len(_rotated_archives(tmp_path)) == 1, "archive.md was not rotated"
    assert "ARCHIVE-SENTINEL-348" in _rotated_archives(tmp_path)[0].read_text(encoding="utf-8")
    assert _rotated_recents(tmp_path) == [], (
        "recent.md was rotated even though dropping the archive already fit"
    )


def test_a_healthy_store_rotates_nothing(tmp_path):
    """The control that stops all of the above from being satisfied by a
    pipeline that rotates on every round."""
    recent_f, archive_f = _store(tmp_path, staging=400, recent=400, archive=300)

    out, called = _run(tmp_path, recent_f, archive_f)

    assert "CONSOLIDATION_STATUS=ok" in out, out
    assert called
    assert _rotated_recents(tmp_path) == []
    assert _rotated_archives(tmp_path) == []


def test_an_absent_recent_over_the_cap_skips_rather_than_rotating_nothing(tmp_path):
    """recent.md absent and staging over the cap: there is nothing to rotate.

    Rotating an empty or missing file would create a dated sibling no recall
    ever wants and would still not heal the round. Same contract
    ``_rotate_archive`` has held since #123.
    """
    recent_f, archive_f = _store(tmp_path, staging=CAP * 2, recent=0, archive=0)

    out, called = _run(tmp_path, recent_f, archive_f)

    assert "CONSOLIDATION_STATUS=skip" in out, out
    assert not called
    assert _rotated_recents(tmp_path) == []


# -- 3. Two rotations in one round ------------------------------------------


def test_both_files_rotate_when_neither_alone_is_enough(tmp_path):
    """staging + archive is still over the cap after recent.md goes.

    Both have to move for the round to fit, and both siblings must survive --
    the second rotation must not orphan or clobber the first.
    """
    recent_f, archive_f = _store(tmp_path, staging=int(CAP * 0.5),
                                 recent=CAP * 2, archive=int(CAP * 0.7))

    out, called = _run(tmp_path, recent_f, archive_f)

    assert "CONSOLIDATION_STATUS=ok" in out, out
    assert called
    assert len(_rotated_recents(tmp_path)) == 1, "recent.md was not rotated"
    assert len(_rotated_archives(tmp_path)) == 1, "archive.md was not rotated"
    assert "ARCHIVE-SENTINEL-348" in _rotated_archives(tmp_path)[0].read_text(encoding="utf-8")
    assert "RECENT-SENTINEL-348" in _rotated_recents(tmp_path)[0].read_text(encoding="utf-8")


def test_both_rotations_are_undone_when_the_round_does_not_go_through(tmp_path):
    """The undo discipline #347 established for the archive, for both files.

    A round that never happened must leave nothing moved: the next run has to
    find recent.md and archive.md where it expects them. A half-undone pair is
    worse than no rotation, because the store is then split across names
    nothing consolidated.
    """
    recent_f, archive_f = _store(tmp_path, staging=int(CAP * 0.5),
                                 recent=CAP * 2, archive=int(CAP * 0.7))
    recent_before = recent_f.read_bytes()
    archive_before = archive_f.read_bytes()

    def _boom(prompt, timeout=180):
        raise RuntimeError("the model call failed")

    buf = io.StringIO()
    with patch.object(consolidate_mod, "call_haiku", _boom):
        with redirect_stdout(buf):
            with pytest.raises(RuntimeError):
                shell_mod.cmd_consolidate(str(tmp_path), str(recent_f), str(archive_f),
                                          CAP, "")

    assert recent_f.exists() and recent_f.read_bytes() == recent_before, (
        "recent.md was left rotated away after a round that never happened"
    )
    assert archive_f.exists() and archive_f.read_bytes() == archive_before, (
        "archive.md was left rotated away after a round that never happened"
    )
    assert _rotated_recents(tmp_path) == []
    assert _rotated_archives(tmp_path) == []


def test_a_declined_round_leaves_both_files_in_place(tmp_path):
    """The model returning SKIP is not a failure and must not move anything."""
    recent_f, archive_f = _store(tmp_path, staging=int(CAP * 0.5),
                                 recent=CAP * 2, archive=int(CAP * 0.7))
    recent_before = recent_f.read_bytes()
    archive_before = archive_f.read_bytes()

    skip = HaikuResult(text="SKIP", is_skip=True, is_rejected=False,
                       tokens=TokenUsage(input=0, output=0, cache=0, cost_usd=0.0))

    out, _ = _run(tmp_path, recent_f, archive_f, response=skip)

    assert "CONSOLIDATION_STATUS=skip" in out, out
    assert recent_f.read_bytes() == recent_before, "recent.md left rotated after a decline"
    assert archive_f.read_bytes() == archive_before, "archive.md left rotated after a decline"
    assert _rotated_recents(tmp_path) == []
    assert _rotated_archives(tmp_path) == []


def test_a_rotated_recent_is_restored_when_the_template_still_will_not_fit(tmp_path):
    """The narrow band where the pre-read guard passes and the prompt does not.

    The guard sizes staging + recent.md + archive.md; the prompt is that plus
    the template and the per-file labels. So a store can clear the ladder,
    rotate recent.md, and still assemble a prompt over the cap by a few hundred
    bytes.

    That path used to reach an exit that returns WITHOUT undoing anything --
    the ``rotated is None`` skip, which was written when only archive.md could
    be rotated and there was nothing else to undo. Reaching it with recent.md
    already moved would leave the store split across a dated sibling for a
    round that never happened, which is the worst outcome available here: not
    a skip, not a recovery, a permanent split nothing consolidated.
    """
    # staging just under the cap, so the template tips the assembled prompt
    # over it. No archive at all, so the archive rung has nothing to offer.
    recent_f, archive_f = _store(tmp_path, staging=CAP - 10, recent=CAP * 2, archive=0)
    recent_before = recent_f.read_bytes()

    out, called = _run(tmp_path, recent_f, archive_f)

    assert "CONSOLIDATION_STATUS=skip" in out, out
    assert not called, "the prompt was over the cap; it must not have been sent"
    assert recent_f.exists() and recent_f.read_bytes() == recent_before, (
        "recent.md was left rotated away after a round that never went through"
    )
    assert _rotated_recents(tmp_path) == [], (
        "a dated sibling survives a round that produced nothing -- the store is "
        "now split across two names and nothing consolidated either of them"
    )


# -- 4. Same-day collision --------------------------------------------------


def test_a_second_rotation_the_same_day_does_not_overwrite_the_first(tmp_path):
    """Two over-cap rounds on one day must not silently eat the first slice.

    ``_rotate_archive`` has answered this with a ``-N`` suffix since #123 and
    #124 taught the read path to order those correctly; recent.md follows the
    same naming so the same ordering applies.
    """
    recent_f, archive_f = _store(tmp_path, staging=400, recent=CAP * 2, archive=300)
    first = tmp_path / f"recent-{today_str()}.md"
    first.write_text("FIRST-SLICE-348\n", encoding="utf-8")

    _run(tmp_path, recent_f, archive_f)

    assert first.read_text(encoding="utf-8") == "FIRST-SLICE-348\n", (
        "an existing same-day slice was overwritten -- that is the history loss "
        "this issue exists to remove"
    )
    second = tmp_path / f"recent-{today_str()}-2.md"
    assert second.exists(), "the second same-day rotation did not take a -2 suffix"
    assert "RECENT-SENTINEL-348" in second.read_text(encoding="utf-8")


# -- 5. The rotated slice has to be reachable -------------------------------

bash_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX layout -- not portable to Windows runners",
)

SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"


def _run_session_start(tmp_path: Path) -> str:
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True, exist_ok=True)
    (remember / "logs").mkdir(parents=True, exist_ok=True)
    home.mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        env={
            **os.environ,
            "HOME": str(home),
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "REMEMBER_DIR": str(remember),
            "_LIB_MEMORY_DIR_LOADED": "1",
        },
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@bash_only
def test_a_rotated_recent_is_named_at_session_start(tmp_path):
    """#124's finding, for the new sibling.

    A slice nothing names is a slice no recall reaches -- "no memory lost" true
    mechanically and false in practice. The read path globs ``archive-*.md``;
    a ``recent-*.md`` sibling is invisible to it until it is taught otherwise.
    """
    remember = tmp_path / "project" / ".remember"
    (remember / "tmp").mkdir(parents=True, exist_ok=True)
    (remember / "recent-2026-06-29.md").write_text(
        "# Recent\n\nthe span that only lives here\n", encoding="utf-8")

    out = _run_session_start(tmp_path)

    assert "recent-2026-06-29.md" in out, (
        "the rotated recent span is not named anywhere in the session surface, "
        "so no recall can reach it:\n" + out
    )


@bash_only
def test_a_rotated_recent_is_not_injected(tmp_path):
    """Named, not pasted -- it was rotated because it was too large to send."""
    remember = tmp_path / "project" / ".remember"
    (remember / "tmp").mkdir(parents=True, exist_ok=True)
    (remember / "recent-2026-06-29.md").write_text(
        "# Recent\n\nSENTINEL-ROTATED-RECENT-348\n", encoding="utf-8")

    out = _run_session_start(tmp_path)

    assert "SENTINEL-ROTATED-RECENT-348" not in out, (
        "the rotated recent span was injected -- that rebuilds the oversized "
        "prompt the rotation exists to avoid"
    )


@bash_only
def test_a_store_with_only_a_rotated_recent_still_announces_memory(tmp_path):
    """The #124 defect exactly: rotate an oversized recent.md and the fresh one
    is empty, so a store can hold nothing but the rotated slice. Gating the
    section on the fixed file list would print nothing at all."""
    remember = tmp_path / "project" / ".remember"
    (remember / "tmp").mkdir(parents=True, exist_ok=True)
    (remember / "recent-2026-06-29.md").write_text(
        "# Recent\n\nonly memory in the store\n", encoding="utf-8")

    out = _run_session_start(tmp_path)

    assert "=== MEMORY ===" in out, (
        "a store holding only rotated slices printed no MEMORY section:\n" + out
    )


@bash_only
def test_nothing_is_announced_when_no_recent_rotation_has_happened(tmp_path):
    """Paired with the three above: the announcement must be caused by the
    file, not printed unconditionally."""
    remember = tmp_path / "project" / ".remember"
    (remember / "tmp").mkdir(parents=True, exist_ok=True)
    (remember / "recent.md").write_text("# Recent\n\nhealthy\n", encoding="utf-8")

    out = _run_session_start(tmp_path)

    assert "/recent-" not in out, (
        "a rotated-slice line was printed for a store that has never rotated:\n" + out
    )


def test_the_history_hint_names_the_rotated_recent_pattern():
    """The hint is what tells the agent the slice is greppable."""
    hint = (REPO_ROOT / "prompts" / "session-history-hint.txt").read_text(encoding="utf-8")
    assert "recent-YYYY-MM-DD" in hint, (
        "the session hint does not name the rotated recent pattern, so nothing "
        "tells the agent those files exist"
    )
