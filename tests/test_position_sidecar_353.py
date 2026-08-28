"""A bash-readable position sidecar, keeping `pipeline.shell read-position`
off the hot path once a save has landed (#353, part 2 of #350).

`last-save.json` already holds the position. A sidecar is a SECOND source of
truth for the same number, and the two can disagree -- a partial write, a
crash between them, a stale file surviving a session-id reuse. Part 1
(#352) measured the win; this file is about the disagreement, which is the
whole reason #353 was split out on its own rather than riding along.

Two guarantees this file pins:

  * WRITE ORDER (pipeline/shell.py::cmd_save_position). The sidecar is
    written strictly AFTER last-save.json is committed, never before. A
    crash between the two writes therefore leaves the sidecar holding the
    PREVIOUS position -- stale, but never a value ahead of the truth.
    Reproduced by making the sidecar write itself fail (os.replace raised
    the second time it is called) and showing last-save.json still landed.

  * READ DETECTION (scripts/post-tool-hook.sh). Because the write order
    above makes "ahead of the truth" impossible on a healthy store, a
    sidecar value greater than the CURRENT run's own line count is
    self-evidently wrong -- the only ways to reach it are corruption or a
    session-id collision, never a legitimate crash window. The hot path
    treats that as a loud disagreement: log a warning and fall back to
    `read-position`, never trust the sidecar silently. Paired against the
    "must not fire" case in the same fixture, per this issue's own
    acceptance criteria.

Every "must not fire" case here is paired with a "must fire" case in the
same test, so a broken harness (one that never actually launches the real
hook, or never actually looks at the log) cannot pass by producing nothing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.shell import _POSITION_SLOTS, cmd_save_position
from tests.spawn_counting import make_shim_dir
from tests.spawn_counting import spawns as _spawn_lines
from tests.test_post_tool_fast_path_350 import (
    _cmds,
    _env,
    _prime,
    _project,
    _reap,
    _run,
)

SESSION_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


# == The write side: pipeline/shell.py::cmd_save_position writes the sidecar ==

def test_save_position_writes_a_plain_integer_sidecar(tmp_path: Path):
    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    last_save = remember / "tmp" / "last-save.json"

    cmd_save_position(str(last_save), SESSION_A, 120)

    sidecar = remember / "tmp" / f"position.{SESSION_A}"
    assert sidecar.exists(), "cmd_save_position did not write a sidecar file"
    assert sidecar.read_text(encoding="utf-8").strip() == "120", (
        "sidecar does not hold a plain bash-`read`-able integer"
    )


def test_two_interleaved_sessions_get_separate_sidecars(tmp_path: Path):
    """The same #140 lesson last-save.json itself already learned: one slot
    shared by two live sessions means the second overwrites the first."""
    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    last_save = remember / "tmp" / "last-save.json"

    cmd_save_position(str(last_save), SESSION_A, 50)
    cmd_save_position(str(last_save), SESSION_B, 7)

    assert (remember / "tmp" / f"position.{SESSION_A}").read_text().strip() == "50"
    assert (remember / "tmp" / f"position.{SESSION_B}").read_text().strip() == "7"


def test_sidecar_write_leaves_no_temp_file_behind(tmp_path: Path):
    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    last_save = remember / "tmp" / "last-save.json"

    cmd_save_position(str(last_save), SESSION_A, 1)

    leftovers = list((remember / "tmp").glob("*.tmp"))
    assert leftovers == [], f"temp file left behind: {leftovers}"


def test_a_crash_between_the_two_writes_leaves_the_sidecar_stale_not_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The load-bearing ordering guarantee. last-save.json is committed via
    `os.replace` first; the sidecar's own `os.replace` is second and is made
    to fail here, simulating a process killed in between. last-save.json must
    already hold the NEW position -- the write that matters was not lost --
    and the sidecar must hold either nothing (first save) or the PREVIOUS
    position, never a value that leapfrogs the truth."""
    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    last_save = remember / "tmp" / "last-save.json"

    cmd_save_position(str(last_save), SESSION_A, 10)
    sidecar = remember / "tmp" / f"position.{SESSION_A}"
    assert sidecar.read_text(encoding="utf-8").strip() == "10"

    real_replace = os.replace
    calls = {"n": 0}

    def _flaky_replace(src, dst):
        calls["n"] += 1
        # The FIRST os.replace this second call makes is last-save.json's own
        # commit -- let it through. The SECOND is the sidecar's -- fail it,
        # as though the process died right there.
        if calls["n"] == 2:
            raise OSError("simulated crash between the two writes")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _flaky_replace)
    with pytest.raises(OSError):
        cmd_save_position(str(last_save), SESSION_A, 99)

    data = json.loads(last_save.read_text(encoding="utf-8"))
    assert data["sessions"][SESSION_A] == 99, (
        "last-save.json must be committed before the sidecar write is even "
        "attempted -- the source of truth must not depend on the sidecar "
        "succeeding"
    )
    assert sidecar.read_text(encoding="utf-8").strip() == "10", (
        "the sidecar leapfrogged the truth after a simulated crash between "
        "the two writes -- it must be stale, at most, never ahead"
    )


def test_an_evicted_session_sidecar_is_removed_not_left_stale(tmp_path: Path):
    """last-save.json bounds itself to `_POSITION_SLOTS` sessions, oldest
    evicted first (#140) -- read-position then correctly answers 0 for an
    evicted session. Its sidecar must not survive the eviction: a stale
    sidecar still `<= CURRENT_LINES` on some later, unrelated transcript
    would be silently TRUSTED by the hot path's bounds check, which cannot
    see that last-save.json itself has already forgotten this session --
    reintroducing exactly the #140 duplicate-resummarization bug the sidecar
    must not bring back."""
    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    last_save = remember / "tmp" / "last-save.json"

    cmd_save_position(str(last_save), SESSION_A, 5)
    sidecar_a = remember / "tmp" / f"position.{SESSION_A}"
    assert sidecar_a.exists(), "setup: A's own sidecar must exist before eviction"

    for i in range(_POSITION_SLOTS):
        cmd_save_position(str(last_save), f"filler-{i:04d}", i)

    data = json.loads(last_save.read_text(encoding="utf-8"))
    assert SESSION_A not in data["sessions"], (
        "setup: A must actually have been evicted, or this test proves nothing"
    )
    assert not sidecar_a.exists(), (
        "A's sidecar survived its own eviction from last-save.json -- a stale "
        "position is now reachable to the hot path with nothing left to say "
        "it no longer applies"
    )


# == The read side: scripts/post-tool-hook.sh's hot path =====================

def _write_sidecar(remember: Path, session_id: str, value: str) -> None:
    (remember / "tmp" / f"position.{session_id}").write_text(value, encoding="utf-8")


def _write_last_save(remember: Path, session_id: str, position: int) -> None:
    (remember / "tmp" / "last-save.json").write_text(
        json.dumps({"sessions": {session_id: position}, "session": session_id,
                    "line": position}),
        encoding="utf-8",
    )


def _logged(remember: Path) -> str:
    logs = remember / "logs"
    if not logs.is_dir():
        return ""
    return "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(logs.glob("memory-*.log"))
    )


def _traced_run(env, tmp_path: Path, label: str):
    log = tmp_path / ("spawns-" + label + ".log")
    shims = make_shim_dir(tmp_path)
    result = _run(env, count_into=log, shims=shims)
    assert result.returncode == 0, repr(result.stderr[:400])
    return result, _spawn_lines(log)


def test_a_valid_agreeing_sidecar_skips_the_read_position_spawn(tmp_path: Path):
    """The entire point of #353: once a save has landed, the hot path must
    not fork `python3 -m pipeline.shell read-position` on every later tool
    call. 55 of 60 lines already saved, threshold 50 -- must not fork a
    save."""
    home, project, remember = _project(tmp_path, jsonl_lines=60,
                                        session_id=SESSION_A)
    _write_last_save(remember, SESSION_A, 55)
    _write_sidecar(remember, SESSION_A, "55")
    env = _env(tmp_path, home, project)
    _prime(env)

    _result, lines = _traced_run(env, tmp_path, "sidecar-agrees")
    _reap(remember)

    assert "python3" not in _cmds(lines) and "python" not in _cmds(lines), (
        "a valid, agreeing sidecar did not stop the hook from spawning "
        "python for read-position. Spawned:\n  " + "\n  ".join(lines)
    )
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "delta should be 5 against a threshold of 50 -- no save should have "
        "been forked"
    )
    assert "disagrees with last-save.json" not in _logged(remember), (
        "NEGATIVE CONTROL: an agreeing sidecar must not log a disagreement"
    )


def test_a_missing_sidecar_still_falls_back_to_read_position(tmp_path: Path):
    """Regression guard: before any save has written a sidecar (or on an
    install that predates #353), the hot path must still consult the real
    source of truth rather than silently trusting a position of 0."""
    home, project, remember = _project(tmp_path, jsonl_lines=60,
                                        session_id=SESSION_A)
    _write_last_save(remember, SESSION_A, 55)
    # Deliberately no sidecar file.
    env = _env(tmp_path, home, project)
    _prime(env)

    _result, lines = _traced_run(env, tmp_path, "sidecar-missing")
    _reap(remember)

    assert "python3" in _cmds(lines) or "python" in _cmds(lines), (
        "MUST-FIRE control: a missing sidecar must still spawn read-position "
        "-- if it does not, position 55 was never consulted at all. "
        "Spawned:\n  " + "\n  ".join(lines)
    )
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "read-position must still have reported 55, suppressing the fork"
    )


def test_a_sidecar_past_the_transcripts_own_line_count_is_a_loud_disagreement(
    tmp_path: Path,
):
    """The disagreement #353's acceptance criteria requires be loud. A
    sidecar can only ever be written AFTER last-save.json (pinned above), so
    it can never legitimately report a position past this run's own line
    count -- reaching one means corruption or a session-id collision, never
    a real crash window. MUST fall back to read-position (never silently
    trust it) and MUST log the disagreement."""
    home, project, remember = _project(tmp_path, jsonl_lines=60,
                                        session_id=SESSION_A)
    _write_last_save(remember, SESSION_A, 55)
    # 999 is past the transcript's own 60 lines -- impossible on a healthy
    # store, exactly the corrupted-or-colliding case this must catch.
    _write_sidecar(remember, SESSION_A, "999")
    env = _env(tmp_path, home, project)
    _prime(env)

    _result, lines = _traced_run(env, tmp_path, "sidecar-disagrees")
    _reap(remember)

    assert "python3" in _cmds(lines) or "python" in _cmds(lines), (
        "MUST-FIRE control: a disagreeing sidecar must fall back to "
        "read-position rather than being trusted silently. Spawned:\n  "
        + "\n  ".join(lines)
    )
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "read-position must still have won and reported the real 55, "
        "suppressing the fork"
    )
    body = _logged(remember)
    assert "disagrees with last-save.json" in body, (
        "a sidecar reporting a position past this run's own line count must "
        "be a LOUD finding, not a silent fallback -- log tail: "
        + repr(body[-2000:])
    )


def test_a_non_numeric_sidecar_is_not_trusted_and_falls_back(tmp_path: Path):
    """A garbled write (partial, or hand-edited) must not become a silent 0
    or a silent anything -- fall back to the authoritative source."""
    home, project, remember = _project(tmp_path, jsonl_lines=60,
                                        session_id=SESSION_A)
    _write_last_save(remember, SESSION_A, 55)
    _write_sidecar(remember, SESSION_A, "not-a-number")
    env = _env(tmp_path, home, project)
    _prime(env)

    _result, lines = _traced_run(env, tmp_path, "sidecar-garbage")
    _reap(remember)

    assert "python3" in _cmds(lines) or "python" in _cmds(lines), (
        "a non-numeric sidecar must fall back to read-position. Spawned:\n  "
        + "\n  ".join(lines)
    )
    assert not (remember / "tmp" / "save-session.pid").exists()
    body = _logged(remember)
    assert "disagrees with last-save.json" in body, (
        "a non-numeric sidecar must be logged as loudly as an out-of-range "
        "one -- both are the SAME acceptance criterion (#353): a sidecar "
        "disagreement is a loud finding. Log tail: " + repr(body[-2000:])
    )
