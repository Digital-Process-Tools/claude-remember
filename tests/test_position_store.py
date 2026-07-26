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
import sys
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

    # This test used to interleave A with exactly _POSITION_SLOTS - 1 fillers,
    # which never overflowed the store — so the eviction loop never ran and the
    # test passed with the re-insertion deleted (#175). Interleaving alone is
    # not enough either: A is re-saved last on every round, so it is present at
    # the end whether or not it kept its slot.
    #
    # What separates the two is a burst of OTHER sessions after A's last save.
    # Re-inserted, A is newer than all of them and survives; keyed on first
    # insertion, A is the oldest thing in the store and is evicted — then
    # resumes from 0 and re-summarizes its whole span, which is #140.
    cmd_save_position(path, SESSION_A, 5)
    for i in range(_POSITION_SLOTS - 1):
        cmd_save_position(path, f"early-{i:04d}", i)

    cmd_save_position(path, SESSION_A, 999)

    for i in range(_POSITION_SLOTS - 1):
        cmd_save_position(path, f"late-{i:04d}", i)

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert len(data["sessions"]) == _POSITION_SLOTS, "the store never overflowed"
    assert SESSION_A in data["sessions"], (
        "an actively-saving session was evicted by newer ones — it will resume "
        "from 0 and re-summarize everything it already summarized (#140)"
    )
    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 999


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


# ── The shell readers must agree with the Python ones ───────────────────────
#
# session-start-hook.sh asks jq whether the previous session was ever saved.
# That answer has to mean the same thing as get_last_save_line's: a key with a
# non-int value is NOT a saved position, and treating it as one skips the
# recovery save while the real reader resumes from 0 — re-summarizing the whole
# span, which is the duplicate #140 exists to prevent.

import shutil
import subprocess

REPO_ROOT = Path(__file__).resolve().parent.parent


def _jq_saved_state(payload: str, session_id: str) -> str:
    """Run session-start-hook.sh's own jq expression against a store."""
    source = (REPO_ROOT / "scripts" / "session-start-hook.sh").read_text()
    query = next(
        line.split("=", 1)[1].strip().strip("'")
        for line in source.splitlines()
        if line.strip().startswith("SAVED_QUERY=")
    )
    import tempfile
    path = Path(tempfile.mkstemp(suffix=".json")[1])
    path.write_text(payload, encoding="utf-8")
    out = subprocess.run(["jq", "-r", "--arg", "id", session_id, query, str(path)],
                         capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize("payload,expected", [
    ('{"sessions": {"%s": 42}}' % SESSION_A, "saved"),
    ('{"sessions": {"%s": null}}' % SESSION_A, "unsaved"),
    ('{"sessions": {"%s": "5"}}' % SESSION_A, "unsaved"),
    ('{"session": "%s", "line": 9}' % SESSION_A, "saved"),
    ('{"session": "%s", "line": null}' % SESSION_A, "unsaved"),
    ('{"sessions": {}}', "unsaved"),
    # A float reads as a number to jq but is not an int to python; a bool is
    # the reverse, since bool subclasses int. Either disagreement means one
    # reader skips the recovery save while the other resumes from 0.
    ('{"sessions": {"%s": 12.5}}' % SESSION_A, "unsaved"),
    ('{"sessions": {"%s": true}}' % SESSION_A, "unsaved"),
    ('{"sessions": {"%s": false}}' % SESSION_A, "unsaved"),
    ('{"session": "%s", "line": 9.5}' % SESSION_A, "unsaved"),
    # An integral float is the trap: jq cannot tell 1.0 from 1 — it parses
    # every number to a double — so python has to accept it too, or the hook
    # calls the session saved while the reader resumes it from 0.
    ('{"sessions": {"%s": 1.0}}' % SESSION_A, "saved"),
    ('{"sessions": {"%s": 1e3}}' % SESSION_A, "saved"),
    ('{"session": "%s", "line": 1.0}' % SESSION_A, "saved"),
    # JSON has no infinity literal, but 1e400 overflows to one — and
    # floor(infinite) == infinite, so jq needed an explicit guard.
    ('{"sessions": {"%s": 1e400}}' % SESSION_A, "unsaved"),
    ('{"sessions": {"%s": -1e400}}' % SESSION_A, "unsaved"),
])
def test_jq_reader_agrees_with_the_python_reader(payload, expected, tmp_path):
    assert _jq_saved_state(payload, SESSION_A) == expected

    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (remember / "tmp" / "last-save.json").write_text(payload, encoding="utf-8")
    python_line = get_last_save_line(SESSION_A, remember_dir=str(remember))
    assert (python_line != 0) == (expected == "saved"), (
        f"jq says {expected} but get_last_save_line returned {python_line}"
    )


def test_an_integral_float_is_read_as_that_line(store):
    """jq sees 1.0 and 1 as the same number, so this reader must too.

    Rejecting it here while the hook accepted it meant the hook skipped the
    recovery save and this reader started over — a re-summarized span, which
    is the duplicate #140 is about.
    """
    remember, path = store
    Path(path).write_text(json.dumps({"sessions": {SESSION_A: 120.0}}), encoding="utf-8")
    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 120


def test_a_bool_in_a_legacy_file_is_not_carried_forward(store):
    """The legacy branch of _read_positions kept its bare isinstance check.

    bool subclasses int, so `true` survived the merge and was written back
    into the keyed store as a literal true — persisting the corruption, where
    the reader then treats it as 0 and re-extracts everything.
    """
    remember, path = store
    Path(path).write_text(json.dumps({"session": SESSION_A, "line": True}), encoding="utf-8")

    cmd_save_position(path, SESSION_B, 7)

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert SESSION_A not in data["sessions"], f"bool position carried over: {data}"
    assert data["sessions"][SESSION_B] == 7


# ── One reader, not five ────────────────────────────────────────────────────
#
# This rule lived in five places: two python modules, session-start's jq, the
# post-tool hook's inline json, and the hook's legacy branch. Three review
# rounds in a row found a copy that had missed an update. The python side is
# one implementation now, reachable from bash as `pipeline.shell read-position`,
# which leaves jq as the only reimplementation — and it is pinned against the
# python one by test_jq_reader_agrees_with_the_python_reader above.

def test_read_position_command_matches_the_library_reader(store):
    """The command bash calls must answer exactly what the library answers."""
    remember, path = store
    cmd_save_position(path, SESSION_A, 120)
    cmd_save_position(path, SESSION_B, 40)

    for session, expected in ((SESSION_A, 120), (SESSION_B, 40), ("never-seen", 0)):
        out = subprocess.run(
            [sys.executable, "-m", "pipeline.shell", "read-position", path, session],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == str(expected), (
            f"read-position({session}) said {out.stdout.strip()!r}, "
            f"library says {get_last_save_line(session, remember_dir=str(remember))}"
        )


def test_the_post_tool_hook_does_not_parse_the_store_itself():
    """It carried its own json reader, and that copy drifted (round 4).

    Every other reader learned that a bool is not a position and an integral
    float is; that one kept a bare isinstance check and reported 0 for
    positions the rest of the pipeline resumed from, so the delta throttle
    fired on almost every tool call for the life of a session.
    """
    hook = (REPO_ROOT / "scripts" / "post-tool-hook.sh").read_text()
    assert "read-position" in hook, "the hook no longer delegates to the shared reader"
    assert "json.load" not in hook, "the hook is parsing last-save.json itself again"


@pytest.mark.parametrize("payload", ["[]", "null", "42", "true", '"hello"', '[{"sessions": {}}]'])
def test_a_top_level_that_is_not_an_object_reads_as_empty(payload, store):
    """Valid JSON of the wrong shape must not crash the save.

    get_last_save_line had no isinstance(data, dict) guard and its except
    clause did not catch AttributeError, so `[]` or `null` raised inside
    extract_session — which runs backgrounded, disowned, with stderr
    discarded. The save died silently and kept dying while the file stayed
    that shape: lost memory with nothing to notice it by. The other copy of
    this reader had the guard; that is what two implementations buys you.
    """
    remember, path = store
    Path(path).write_text(payload, encoding="utf-8")
    assert get_last_save_line(SESSION_A, remember_dir=str(remember)) == 0


def test_the_two_modules_share_one_reader():
    """shell.py must not grow its own copy again.

    It had one, and it drifted twice — first missing the bool rule, then
    holding the top-level guard the other lacked. Three review rounds in a
    row found a copy that had missed an update.
    """
    import pipeline.extract as extract_mod
    import pipeline.shell as shell_mod

    assert shell_mod.read_positions is extract_mod.read_positions
    assert shell_mod._is_line_number is extract_mod._is_line_number
    source = (REPO_ROOT / "pipeline" / "shell.py").read_text()
    assert "def read_positions" not in source, "shell.py defined its own reader again"
    assert "def _is_line_number" not in source, "shell.py defined its own predicate again"
