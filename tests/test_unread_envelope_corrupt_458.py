"""read_unread_envelope() renders "nothing quarantined" and "the sidecar
exists and could not be read" the same way (#458), one level inside the
mechanism #450 built to fix exactly this defect class.

#450 made an unrecognised-envelope save advance the position AND quarantine
the unread span in unread-envelope.json, so a later build that can parse the
envelope re-reads it. Recovery depends entirely on that sidecar being
readable. If it is corrupt -- a torn write from a crash mid-write, a disk
fault, a truncated file -- read_unread_envelope() returned {} exactly as it
does when nothing was ever quarantined, so the next extraction silently
resumed from the already-advanced position and the span was gone for good:
the pre-#450 behaviour, restored by a fault, reported as normal operation.

The full fix (a corrupt-sidecar signal reaching the shell-visible log) needs
a decision about scripts/save-session.sh's KEY=VALUE bridge that this issue
explicitly declines to fold in (see the issue body). What is fixed here,
inside pipeline/, is the part that mechanism actually depends on: the
Python-side distinction between "nothing quarantined" and "could not tell",
surfaced on ExtractResult and through pipeline.shell's existing shell
bridge as a new, additive key a future caller can read.

Every "must not fire" case (a genuinely empty, absent sidecar) is paired
with a "must fire" case (the same sidecar, corrupted) in the same fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.extract import (
    _unread_envelope_path,
    extract_session,
    read_unread_envelope,
    read_unread_envelope_status,
)

SESSION_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


@pytest.fixture()
def store(tmp_path: Path):
    remember = tmp_path / ".remember"
    (remember / "tmp").mkdir(parents=True)
    return remember


def _write_jsonl(tmp_path: Path, *lines: str) -> str:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _claude_code_line(role: str, text: str) -> str:
    return json.dumps({"type": role, "message": {"content": text}})


# == read_unread_envelope_status: the three-state helper itself ============

def test_absent_sidecar_is_ok_nothing_quarantined(store):
    """MUST-FIRE control: no sidecar at all -- the ordinary, expected case
    of a session that was never quarantined -- must read as ok, not corrupt.
    """
    path = _unread_envelope_path("/fake", str(store))
    sessions, corrupt = read_unread_envelope_status(path)
    assert sessions == {}
    assert corrupt is False


def test_corrupt_sidecar_is_reported_not_silently_emptied(store):
    """The defect itself: a sidecar that EXISTS but cannot be parsed (a torn
    write, truncated JSON) must be told apart from "nothing quarantined" --
    both currently render as {}, which is exactly what read_unread_envelope()
    (kept, for backward compatibility) still returns; the NEW status call
    must say the file was there and unreadable.
    """
    path = _unread_envelope_path("/fake", str(store))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text('{"aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa": 3, "trunc', encoding="utf-8")

    sessions, corrupt = read_unread_envelope_status(path)
    assert sessions == {}, "a corrupt sidecar cannot be trusted for any entry"
    assert corrupt is True

    # Old call site's contract is unchanged -- still {} either way.
    assert read_unread_envelope(path) == {}


def test_valid_sidecar_with_entries_is_not_reported_corrupt(store):
    """MUST-FIRE control's pair: a genuinely valid, non-empty sidecar must
    not be flagged corrupt just because it has content.
    """
    path = _unread_envelope_path("/fake", str(store))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({SESSION_A: 7}), encoding="utf-8")

    sessions, corrupt = read_unread_envelope_status(path)
    assert sessions == {SESSION_A: 7}
    assert corrupt is False


# == extract_session surfaces the same distinction on its own result =======

def test_extraction_reports_a_corrupt_quarantine_sidecar(store, tmp_path):
    """The consumer that matters: extract_session() must not silently
    resume from the saved position as though nothing were quarantined when
    the sidecar it consulted is actually corrupt -- it must say so on the
    result, the same shape ENVELOPE/SKIP_LINES already use to cross the
    shell bridge (pipeline/shell.py's cmd_extract).
    """
    path = _unread_envelope_path("/fake", str(store))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("{not valid json at all", encoding="utf-8")

    transcript = _write_jsonl(tmp_path, _claude_code_line("user", "hi"))
    with patch("pipeline.extract.find_session", return_value=transcript):
        result = extract_session(session_id=SESSION_A, project_dir="/fake",
                                  remember_dir=str(store))
    assert result.unread_sidecar_unreadable is True


def test_extraction_does_not_report_corruption_for_an_absent_sidecar(store, tmp_path):
    """MUST-FIRE control's pair, at the extract_session level: the ordinary
    case (no sidecar at all) must not be reported as corrupt.
    """
    transcript = _write_jsonl(tmp_path, _claude_code_line("user", "hi"))
    with patch("pipeline.extract.find_session", return_value=transcript):
        result = extract_session(session_id=SESSION_A, project_dir="/fake",
                                  remember_dir=str(store))
    assert result.unread_sidecar_unreadable is False


# == The shell bridge carries the same signal (pipeline/shell.py cmd_extract) =

def test_cmd_extract_prints_unread_sidecar_unreadable(capsys):
    """Additive to the existing ENVELOPE/SKIP_LINES keys (#443/#450): a
    consumer of the shell bridge must be able to read whether the
    quarantine sidecar this run consulted was actually corrupt, the same
    way it already reads whether the transcript envelope was recognised.
    """
    from pipeline.shell import cmd_extract
    from pipeline.types import ExtractResult

    fake_result = ExtractResult(
        exchanges="", position=2, human_count=0, assistant_count=0,
        envelope="claude-code", skip_lines=0,
        unread_sidecar_unreadable=True,
    )
    with patch("pipeline.shell.extract_session", return_value=fake_result):
        cmd_extract(session_id="sess-abc", project_dir="/tmp/fake")

    output = capsys.readouterr().out
    assert "UNREAD_SIDECAR_UNREADABLE=1" in output


def test_cmd_extract_prints_zero_when_sidecar_is_fine(capsys):
    """MUST-FIRE control's pair: the ordinary case prints 0, not a blank or
    absent key -- a downstream shell consumer must be able to test it
    unconditionally without first checking whether it is even present.
    """
    from pipeline.shell import cmd_extract
    from pipeline.types import ExtractResult

    fake_result = ExtractResult(
        exchanges="", position=2, human_count=0, assistant_count=0,
        envelope="claude-code", skip_lines=0,
        unread_sidecar_unreadable=False,
    )
    with patch("pipeline.shell.extract_session", return_value=fake_result):
        cmd_extract(session_id="sess-abc", project_dir="/tmp/fake")

    output = capsys.readouterr().out
    assert "UNREAD_SIDECAR_UNREADABLE=0" in output
