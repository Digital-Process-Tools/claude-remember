"""An unread span must survive an unrecognised envelope (#450).

`save-session.sh` advances the saved position even when 0 exchanges came from
a *failure to read* rather than from a quiet session -- observed live on
Codex before #443 taught the extractor to read that envelope:

    [extract] 0 exchanges (0 human)
    [extract] 0 exchanges, skip -- position -> 15

All 15 lines were unreadable; all 15 were marked consumed. When #443 later
taught the extractor to read that envelope, those exchanges were already
behind the saved position and did not come back.

Not advancing has its own failure mode -- #147, where a session that cannot
be read re-triggers the save path forever and never clears its cooldown. So
the position still advances unconditionally (that is what keeps #147's loop
closed); what changes is that an "unrecognised" envelope also quarantines the
unread span, in a sidecar next to last-save.json, at the earliest point not
yet actually read. A later build that CAN parse the envelope resumes
extraction from the quarantine point rather than from the advanced position,
recovering the span; the quarantine is cleared the moment that happens.

Every "must not silently consume" case here is paired with a "must actually
advance" positive control (a genuinely quiet, ENVELOPE-recognised session),
because "the quarantine holds the span" also passes when nothing ran at all.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.extract import (
    _unread_envelope_path,
    extract_session,
    get_last_save_line,
    mark_unread_envelope,
    read_unread_envelope,
)
from pipeline.shell import _POSITION_SLOTS, cmd_save_position

SESSION_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


@pytest.fixture()
def store(tmp_path: Path):
    """A remember dir with tmp/, and the last-save.json path inside it."""
    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    return remember, str(remember / "tmp" / "last-save.json")


def _write_jsonl(*lines: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for line in lines:
            f.write(line + "\n")
        return f.name


UNRECOGNISED_LINE = json.dumps({"unknown_shape": True, "nothing": "recognisable"})


def _claude_code_line(role: str, text: str) -> str:
    return json.dumps({"type": role, "message": {"content": text}})


# == The failure this issue is about: a span that was never actually read ===

def test_an_unrecognised_span_is_quarantined_not_lost(store):
    """The core case: 0 exchanges from a FAILURE to read must not be treated
    the same as a quiet session. save-position must still advance the saved
    position (#147), but must also record that this span was never actually
    read, so it is not gone the moment a later build can parse it.
    """
    remember, last_save = store
    path = _write_jsonl(UNRECOGNISED_LINE, UNRECOGNISED_LINE)
    try:
        with patch("pipeline.extract.find_session", return_value=path):
            result = extract_session(session_id=SESSION_A, project_dir="/fake",
                                      remember_dir=str(remember))
        assert result.envelope == "unrecognised"
        assert result.human_count == 0
        assert result.assistant_count == 0
        assert result.position == 2, "position is still the file's own line count"
        assert result.skip_lines == 0, "nothing was ever saved for this session yet"

        # This is exactly what save-session.sh's 0-exchange branch does.
        cmd_save_position(last_save, SESSION_A, result.position,
                           envelope=result.envelope, skip_lines=result.skip_lines)

        # #147 stays closed: the saved position DID advance.
        assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 2, (
            "the saved position must still advance, or a permanently unreadable "
            "session re-triggers the save path forever (#147)"
        )
        # But the span is quarantined, not silently consumed.
        unread = read_unread_envelope(_unread_envelope_path("/fake", str(remember)))
        assert unread.get(SESSION_A) == 0, (
            "an unrecognised-envelope span must be quarantined at its earliest "
            "unread point, not lost the moment the position advances past it"
        )
    finally:
        Path(path).unlink()


def test_a_quarantined_span_is_recovered_once_a_later_build_can_read_it(store):
    """The whole point: once the envelope becomes recognised, the NEXT
    extraction must resume from the quarantine point -- not from the already
    -advanced saved position -- so the previously-unreadable exchanges are
    actually re-read, and the quarantine is cleared once that happens.
    """
    remember, last_save = store
    unread_path = _unread_envelope_path("/fake", str(remember))

    # Simulate the FIRST run: unrecognised envelope, position advanced,
    # span quarantined at line 0 (nothing was ever read for this session).
    cmd_save_position(last_save, SESSION_A, 2, envelope="unrecognised", skip_lines=0)
    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 2
    assert read_unread_envelope(unread_path).get(SESSION_A) == 0

    # Simulate a LATER build that can parse this transcript's shape after
    # all -- same two exchanges, now Claude-Code shaped.
    path = _write_jsonl(
        _claude_code_line("user", "hello"),
        _claude_code_line("assistant", "hi there"),
    )
    try:
        with patch("pipeline.extract.find_session", return_value=path):
            result = extract_session(session_id=SESSION_A, project_dir="/fake",
                                      remember_dir=str(remember))

        assert result.skip_lines == 0, (
            "extraction must resume from the QUARANTINE point (0), not from "
            "the saved position (2) -- otherwise the recovered build still "
            "skips straight past the span it can now read"
        )
        assert result.envelope == "claude-code"
        assert result.human_count == 1 and result.assistant_count == 1, (
            "the previously-unreadable exchanges were not actually recovered"
        )

        # This is what a real, successful save does next.
        cmd_save_position(last_save, SESSION_A, result.position,
                           envelope=result.envelope, skip_lines=result.skip_lines)
        assert SESSION_A not in read_unread_envelope(unread_path), (
            "the quarantine must be cleared once the span has actually been read"
        )
    finally:
        Path(path).unlink()


# == The positive control: a genuinely quiet session must still just advance =

def test_a_genuinely_quiet_recognised_session_advances_and_is_never_quarantined(store):
    """MUST-FIRE control paired with the two tests above: a real 0-exchange
    span (envelope recognised, nothing worth summarizing) must advance
    exactly as before and must NOT be quarantined -- otherwise every quiet
    session would be treated as unread forever, and "the span is quarantined"
    would mean nothing because it is asserted for every save regardless.
    """
    remember, last_save = store
    unread_path = _unread_envelope_path("/fake", str(remember))
    # A system-only line: recognised envelope, 0 human/assistant exchanges.
    path = _write_jsonl(json.dumps({"type": "system", "message": {"content": "x"}}))
    try:
        with patch("pipeline.extract.find_session", return_value=path):
            result = extract_session(session_id=SESSION_B, project_dir="/fake",
                                      remember_dir=str(remember))
        assert result.envelope == "claude-code"
        assert result.human_count == 0 and result.assistant_count == 0

        cmd_save_position(last_save, SESSION_B, result.position,
                           envelope=result.envelope, skip_lines=result.skip_lines)

        assert get_last_save_line(SESSION_B, remember_dir=str(remember)) == result.position, (
            "a genuinely quiet session must still advance -- #147"
        )
        assert SESSION_B not in read_unread_envelope(unread_path), (
            "MUST-FIRE control failed: a genuinely quiet, recognised-envelope "
            "session must never be quarantined"
        )
    finally:
        Path(path).unlink()


# == The sidecar's own bookkeeping =========================================

def test_mark_unread_envelope_keeps_the_earliest_point(store):
    """The quarantine point must never creep forward across repeated
    still-unrecognised runs -- each one would otherwise narrow the span a
    future build recovers.
    """
    remember, _ = store
    path = _unread_envelope_path("/fake", str(remember))
    mark_unread_envelope(path, SESSION_A, 5)
    mark_unread_envelope(path, SESSION_A, 20)
    assert read_unread_envelope(path)[SESSION_A] == 5


def test_cmd_save_position_without_an_envelope_argument_leaves_quarantine_untouched(store):
    """Back-compat: a caller that predates #450 (an existing test, an older
    wrapper) says nothing about the envelope. It must not be able to erase a
    quarantine some OTHER, envelope-aware caller already set for the same
    session -- that would silently undo the very thing #450 exists for.
    """
    remember, last_save = store
    unread_path = _unread_envelope_path("/fake", str(remember))
    mark_unread_envelope(unread_path, SESSION_A, 3)

    cmd_save_position(last_save, SESSION_A, 99)  # no envelope/skip_lines at all

    assert read_unread_envelope(unread_path).get(SESSION_A) == 3, (
        "an envelope-unaware save-position call erased another caller's quarantine"
    )


def test_evicting_a_session_from_last_save_also_drops_its_quarantine(store):
    """A quarantine entry must not outlive the session's own position -- a
    surviving entry would be consulted by a later extraction that can no
    longer resume it against any real saved position at all (mirrors the
    #353 position-sidecar eviction rule).
    """
    remember, last_save = store
    unread_path = _unread_envelope_path("/fake", str(remember))

    cmd_save_position(last_save, SESSION_A, 5, envelope="unrecognised", skip_lines=0)
    assert SESSION_A in read_unread_envelope(unread_path)

    for i in range(_POSITION_SLOTS):
        cmd_save_position(last_save, f"filler-{i:04d}", i, envelope="claude-code", skip_lines=0)

    data = json.loads(Path(last_save).read_text(encoding="utf-8"))
    assert SESSION_A not in data["sessions"], "setup: A must actually have been evicted"
    assert SESSION_A not in read_unread_envelope(unread_path), (
        "A's quarantine entry survived its own eviction from last-save.json"
    )
