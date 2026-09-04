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

from pipeline.shell import cmd_save_position

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
