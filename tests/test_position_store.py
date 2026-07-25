"""Saved positions must survive sessions interleaving (issue #140).

last-save.json used to hold one session and one line. Two live sessions — two
terminals, or a background/worktree session sharing the store — then overwrote
each other: A saves, B saves, and A's next save no longer recognises its own
ID, resumes from 0, and re-summarizes its entire span. The reporter saw the
same 99-exchange span summarized twice, 2.5 hours apart, landing a near
identical triplet in the daily file.

Positions are keyed by session now. These pin the interleaving itself, the
bound on the store, and the two directions of compatibility with the old
single-slot file, since an upgrade lands mid-session for real users.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.extract import get_last_save_line
from pipeline.shell import _POSITION_SLOTS, cmd_save_position

SESSION_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


@pytest.fixture()
def store(tmp_path: Path):
    """A remember dir with tmp/, and the last-save.json path inside it."""
    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    return remember, str(remember / "tmp" / "last-save.json")


def test_interleaved_sessions_keep_their_own_positions(store):
    """The reported bug: B saving must not cost A its position."""
    remember, path = store
    cmd_save_position(path, SESSION_A, 120)
    cmd_save_position(path, SESSION_B, 40)

    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 120, (
        "A resumed from 0 and would re-summarize its whole span as duplicates"
    )
    assert get_last_save_line(SESSION_B, remember_dir=str(remember)) == 40


def test_a_session_saving_repeatedly_keeps_one_slot(store):
    """Re-saving the same session updates it, never appends a second entry."""
    remember, path = store
    for position in (10, 20, 30):
        cmd_save_position(path, SESSION_A, position)

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert list(data["sessions"]) == [SESSION_A]
    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 30


def test_unknown_session_resumes_from_zero(store):
    """A session the store has never seen starts at the beginning."""
    remember, path = store
    cmd_save_position(path, SESSION_A, 99)
    assert get_last_save_line(SESSION_B, remember_dir=str(remember)) == 0


def test_store_is_bounded_and_evicts_the_oldest(store):
    """The file is read on every tool call, so it cannot grow forever."""
    remember, path = store
    for i in range(_POSITION_SLOTS + 5):
        cmd_save_position(path, f"session-{i:04d}", i)

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert len(data["sessions"]) == _POSITION_SLOTS
    assert "session-0000" not in data["sessions"], "oldest was not evicted"
    newest = f"session-{_POSITION_SLOTS + 4:04d}"
    assert data["sessions"][newest] == _POSITION_SLOTS + 4


def test_a_session_that_keeps_saving_is_not_evicted(store):
    """Eviction is by last save, not by first — an active session must stay.

    Keyed on insertion alone, a long-running session would be evicted by newer
    ones while still live, and resume from 0 the next time it saved.
    """
    remember, path = store
    cmd_save_position(path, SESSION_A, 5)
    for i in range(_POSITION_SLOTS - 1):
        cmd_save_position(path, f"filler-{i:04d}", i)
        cmd_save_position(path, SESSION_A, 100 + i)

    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 100 + _POSITION_SLOTS - 2


def test_legacy_single_slot_file_is_still_honoured(store):
    """An upgrade lands mid-session; the old file must still resume it."""
    remember, path = store
    Path(path).write_text(json.dumps({"session": SESSION_A, "line": 77}), encoding="utf-8")

    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 77
    assert get_last_save_line(SESSION_B, remember_dir=str(remember)) == 0


def test_legacy_position_survives_the_first_new_save(store):
    """Upgrading must not throw away the position already recorded.

    Dropping it would re-extract the whole transcript once per upgrade — the
    same duplicate this issue is about, just triggered by a version bump.
    """
    remember, path = store
    Path(path).write_text(json.dumps({"session": SESSION_A, "line": 77}), encoding="utf-8")

    cmd_save_position(path, SESSION_B, 12)

    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 77
    assert get_last_save_line(SESSION_B, remember_dir=str(remember)) == 12


def test_legacy_keys_are_mirrored_for_older_readers(store):
    """Anything still reading .session/.line sees the most recent save.

    The plugin's own scripts are updated, but a half-upgraded install — or a
    third-party hook — should degrade to the old behaviour, not to nothing.
    """
    remember, path = store
    cmd_save_position(path, SESSION_A, 120)
    cmd_save_position(path, SESSION_B, 40)

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["session"] == SESSION_B
    assert data["line"] == 40


def test_corrupt_store_resumes_from_zero_and_is_rewritten(store):
    """A corrupt file must not wedge saving forever."""
    remember, path = store
    Path(path).write_text("{not json", encoding="utf-8")

    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 0
    cmd_save_position(path, SESSION_A, 15)
    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 15


def test_write_leaves_no_temp_file_behind(store):
    """The write goes through a temp file; it must not litter tmp/."""
    remember, path = store
    cmd_save_position(path, SESSION_A, 1)
    leftovers = list((remember / "tmp").glob("*.tmp"))
    assert leftovers == [], f"temp file left behind: {leftovers}"
