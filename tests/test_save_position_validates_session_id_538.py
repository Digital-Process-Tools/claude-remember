"""cmd_save_position must validate session_id before it becomes a path
component (#538).

pipeline/shell.py builds both the position sidecar path and the evicted-
sidecar removal path by interpolating session_id directly into a filename
(`position.{session_id}`), without ever calling
pipeline.extract._validate_session_id -- the same check find_session()
already runs before it does the equivalent join. Both shell callers
(scripts/post-tool-hook.sh, scripts/session-end-hook.sh) and
scripts/save-session.sh already filter the id before Python ever sees it,
so this is hardening against a future caller reaching cmd_save_position by
another route, not a fix for an exploit reachable today.

The negative case is paired with a positive control in the same test, per
CLAUDE.md's bar: a hostile session_id must raise, and a well-formed one
must still write its sidecar, so the "must raise" case cannot pass because
nothing happened at all (e.g. the write failing for an unrelated reason).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.shell import _POSITION_SLOTS, cmd_save_position

WELL_FORMED = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


@pytest.fixture()
def store(tmp_path: Path):
    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    return remember, str(remember / "tmp" / "last-save.json")


@pytest.mark.parametrize("hostile_id", [
    "../../etc/evil",
    "foo/bar",
    "foo\\bar",
    "..",
])
def test_hostile_session_id_is_rejected(store, hostile_id):
    """A session_id carrying a path separator or traversal must raise,
    never silently produce a sidecar path outside the store."""
    _remember, path = store
    with pytest.raises(ValueError):
        cmd_save_position(path, hostile_id, 1)


def test_well_formed_session_id_still_writes_sidecar(store):
    """Positive control: validation must not reject a normal id, and the
    sidecar must actually land, so the negative case above cannot be
    passing because cmd_save_position stopped doing anything at all."""
    remember, path = store
    cmd_save_position(path, WELL_FORMED, 120)

    sidecar = remember / "tmp" / f"position.{WELL_FORMED}"
    assert sidecar.exists(), "well-formed session_id must still write its sidecar"
    assert sidecar.read_text() == "120"


def test_poisoned_legacy_eviction_id_does_not_abort_the_save(store):
    """Reviewer finding (#538 self-review): a store written before this fix
    -- or hand-edited -- can hold an already-invalid key in `sessions`. When
    that key is the one about to be evicted, cmd_save_position must still
    complete the save (last-save.json committed, new sidecar written, evicted
    slot made room for) rather than raising ValueError out of the eviction
    loop and aborting everything after it, including the trailing #450
    quarantine bookkeeping.
    """
    import json

    remember, path = store
    poisoned = "../../etc/evil"
    sessions = {poisoned: 0}
    for i in range(_POSITION_SLOTS - 1):
        sessions[f"session-{i:04d}"] = i
    Path(path).write_text(json.dumps(
        {"sessions": sessions, "session": "session-0000", "line": 0}
    ))

    # This save evicts the oldest entry, which is the poisoned one (dicts
    # keep insertion order, and it was inserted first).
    cmd_save_position(path, WELL_FORMED, 42)

    saved = json.loads(Path(path).read_text())
    assert saved["sessions"][WELL_FORMED] == 42, (
        "the save itself must land even though eviction hit a poisoned id"
    )
    assert poisoned not in saved["sessions"], "the poisoned id must still be evicted"

    sidecar = remember / "tmp" / f"position.{WELL_FORMED}"
    assert sidecar.exists(), "the new session's own sidecar must still be written"
