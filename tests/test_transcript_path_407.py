"""#407 — the host hands us the transcript path; read it instead of rebuilding it.

`_session_dir()` reconstructs `<config>/projects/<slug>/<id>.jsonl` from the
project path. Every failure behind #263, #174 and #157 is the same shape: the
reconstruction disagreed with where Claude Code actually wrote, the glob
returned nothing, and the pipeline no-oped in silence. A path handed over on the
hook payload cannot disagree.

Each negative case here is paired with a positive control, because "does not
read the wrong transcript" also passes when nothing is read at all.
"""

from __future__ import annotations

import json
import os

import pytest

from pipeline import extract as E


def _write_jsonl(path, marker: str) -> None:
    """A minimal two-message transcript carrying an identifiable marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "message": {"role": "user", "content": marker}},
        {"type": "assistant",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ack " + marker}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose derived session dir exists and holds one transcript."""
    proj = tmp_path / "proj"
    proj.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("REMEMBER_TRANSCRIPT_PATH", raising=False)
    derived = tmp_path / "derived"
    monkeypatch.setattr(E, "_session_dir", lambda _pd: str(derived))
    _write_jsonl(derived / "aaaa-1111.jsonl", "DERIVED")
    return proj, derived


# --- positive control: the derivation still works when nothing is supplied ---

def test_derivation_is_used_when_no_transcript_path_is_supplied(project):
    proj, derived = project
    assert E.find_session("aaaa-1111", str(proj)) == str(derived / "aaaa-1111.jsonl")


def test_derived_transcript_content_is_the_one_extracted(project):
    proj, _ = project
    assert "DERIVED" in E.extract_session(session_id="aaaa-1111",
                                          project_dir=str(proj), show_all=True).exchanges


# --- the supplied path wins ---

def test_supplied_transcript_path_is_used_verbatim(project, tmp_path, monkeypatch):
    proj, _ = project
    supplied = tmp_path / "elsewhere" / "rollout-2026-08-28-bbbb-2222.jsonl"
    _write_jsonl(supplied, "SUPPLIED")
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(supplied))
    assert E.find_session("aaaa-1111", str(proj)) == str(supplied)


def test_supplied_path_survives_a_derivation_that_finds_nothing(project, tmp_path, monkeypatch):
    """The #263 shape: the slug misses, so the derived directory is empty.

    Today that raises FileNotFoundError and the save silently no-ops. With the
    path supplied there is nothing left to get wrong.
    """
    proj, _ = project
    monkeypatch.setattr(E, "_session_dir", lambda _pd: str(tmp_path / "C--never-created"))
    supplied = tmp_path / "real" / "cccc-3333.jsonl"
    _write_jsonl(supplied, "SUPPLIED")
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(supplied))
    assert "SUPPLIED" in E.extract_session(session_id="cccc-3333",
                                           project_dir=str(proj), show_all=True).exchanges


# --- the position key must not follow the basename ---

def test_position_is_keyed_on_the_session_id_not_the_filename(project, tmp_path, monkeypatch):
    """A host may name the file anything (Codex uses `rollout-<date>-<uuid>`).

    `extract_session` derives `actual_id` from the basename. If that leaks into
    the resume key, every save re-summarizes the whole transcript from line 0 —
    the duplicate #140 exists to prevent.
    """
    proj, _ = project
    supplied = tmp_path / "elsewhere" / "rollout-2026-08-28-dddd-4444.jsonl"
    _write_jsonl(supplied, "SUPPLIED")
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(supplied))

    seen: list[str] = []
    monkeypatch.setattr(E, "get_last_save_line",
                        lambda sid, *a, **k: seen.append(sid) or 0)
    E.extract_session(session_id="dddd-4444", project_dir=str(proj))
    assert seen == ["dddd-4444"]


def test_reported_session_id_is_the_supplied_one(project, tmp_path, monkeypatch):
    proj, _ = project
    supplied = tmp_path / "elsewhere" / "rollout-2026-08-28-eeee-5555.jsonl"
    _write_jsonl(supplied, "SUPPLIED")
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(supplied))
    out = E.extract_session(session_id="eeee-5555", project_dir=str(proj), show_all=True)
    assert out.exchanges.startswith("Session: eeee-5555")


# --- an unusable value must degrade to today's behaviour, never crash ---

@pytest.mark.parametrize("value", [
    pytest.param("", id="empty"),
    pytest.param("   ", id="whitespace"),
])
def test_blank_supplied_path_falls_back_to_derivation(project, monkeypatch, value):
    proj, derived = project
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", value)
    assert E.find_session("aaaa-1111", str(proj)) == str(derived / "aaaa-1111.jsonl")


def test_missing_supplied_path_falls_back_to_derivation(project, tmp_path, monkeypatch):
    proj, derived = project
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(tmp_path / "gone" / "nope.jsonl"))
    assert E.find_session("aaaa-1111", str(proj)) == str(derived / "aaaa-1111.jsonl")


def test_directory_supplied_falls_back_to_derivation(project, tmp_path, monkeypatch):
    """A directory is readable and exists; it is still not a transcript."""
    proj, derived = project
    adir = tmp_path / "a-directory.jsonl"
    adir.mkdir()
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(adir))
    assert E.find_session("aaaa-1111", str(proj)) == str(derived / "aaaa-1111.jsonl")


def test_unusable_supplied_path_still_raises_when_derivation_also_fails(project, tmp_path, monkeypatch):
    """Falling back must not turn a genuine miss into a silent success."""
    proj, _ = project
    monkeypatch.setattr(E, "_session_dir", lambda _pd: str(tmp_path / "empty-dir"))
    (tmp_path / "empty-dir").mkdir()
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(tmp_path / "gone.jsonl"))
    with pytest.raises(FileNotFoundError):
        E.find_session("aaaa-1111", str(proj))
