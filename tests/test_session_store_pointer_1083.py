"""#1083: SessionStart pins the resolved store at a session-keyed pointer
OUTSIDE every project store, so write-handoff.sh and doctor.sh never have to
guess it back from cwd via an ancestor walk. The two gaps #1058/PR #1081 left
standing (both pre-existing #743/#776 architecture, not a regression in that
PR) are structural, not tuning: an ancestor walk can only ever see directories
on the chain from $(pwd) up to the git toplevel, so it is categorically blind
to a resumed session's hint at a SIBLING directory, and to a nested repo or
non-git sibling it never even walks into. The pointer removes the guess
entirely for any session that reaches this fix; the ancestor walk survives
only as a fallback for a session that started before it, and even there it
now REFUSES on an ambiguous result instead of tie-breaking by mtime -- a
same-second write defeated that tie-break outright.

Every case below pairs a positive control: the thing that must still work
when the new mechanism is NOT the one in play (no pointer at all, a healthy
single-candidate fallback, directory layouts the pointer never touches).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX fixtures -- not portable to Windows runners (#79); "
    "the one Windows-shaped case below is pure string handling and is NOT skipped.",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WRITE_HANDOFF = REPO_ROOT / "scripts" / "write-handoff.sh"
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
LIB_SESSION_STORE = REPO_ROOT / "scripts" / "lib-session-store.sh"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_store(path: Path, data_dir: str = ".remember") -> Path:
    remember = path / data_dir
    (remember / "tmp").mkdir(parents=True, exist_ok=True)
    (remember / "config.json").write_text(json.dumps({"data_dir": data_dir}))
    return remember


def _write_pointer(home: Path, session_id: str, store: Path) -> None:
    store_dir = home / ".remember" / "tmp" / "session-store"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / session_id).write_text(str(store) + "\n")


def _write_handoff(cwd: Path, home: Path, note: str, session_id: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env["HOME"] = str(home)
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    return subprocess.run(
        ["bash", str(WRITE_HANDOFF)],
        input=note,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _doctor(cwd: Path, home: Path, session_id: str, args=()) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env["HOME"] = str(home)
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    return subprocess.run(
        ["bash", str(DOCTOR), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _session_start(cwd: Path, home: Path, session_id: str, remember: Path, source: str = "startup"):
    """Invokes the real hook so the pointer is the one SessionStart itself
    writes, not one this fixture fabricated."""
    env = {**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(cwd),
           "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT), "REMEMBER_DIR": str(remember),
           "_LIB_MEMORY_DIR_LOADED": "1", "REMEMBER_DEFER": "0"}
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": source,
        "cwd": str(cwd),
    })
    return subprocess.run(
        ["bash", str(SESSION_START)], input=payload, env=env,
        capture_output=True, text=True, timeout=60, cwd=str(cwd), check=False,
    )


# ---------------------------------------------------------------------------
# Case 1: resume into a child/sibling, then cd somewhere else entirely
# ---------------------------------------------------------------------------


def test_resume_into_sibling_then_cd_elsewhere_still_resolves_via_pointer(tmp_path):
    """Start in r/pkg, resume in r/other (a different directory republishes
    the pointer for the SAME session id), then write from a THIRD directory
    the ancestor walk was never going to see either way. The pointer alone
    decides -- cwd at write time is irrelevant."""
    root = tmp_path / "r"
    pkg = root / "pkg"
    other = root / "other"
    elsewhere = root / "elsewhere"
    for d in (pkg, other, elsewhere):
        d.mkdir(parents=True)
    pkg_store = _init_store(pkg)
    other_store = _init_store(other)
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    session_id = "resume-session-1"

    _write_pointer(home, session_id, pkg_store)
    result = _write_handoff(elsewhere, home, "note before resume\n", session_id)
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    assert (pkg_store / "remember.md").exists()

    # Resume republishes the pointer at `other` -- same session id.
    _write_pointer(home, session_id, other_store)
    result = _write_handoff(elsewhere, home, "note after resume\n", session_id)
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    assert "note after resume" in (other_store / "remember.md").read_text()
    assert "note after resume" not in (pkg_store / "remember.md").read_text(), (
        "resume republished the pointer, but the note still landed at the "
        "ORIGINAL store -- the pointer is not being read fresh each time"
    )


def test_positive_control_no_pointer_single_candidate_ancestor_walk_still_works(tmp_path):
    """No pointer at all (a session that predates #1083) with exactly ONE
    ancestor carrying a hint -- the ordinary #1058/PR #1081 fallback shape,
    unaffected by this change."""
    project = tmp_path / "proj"
    project.mkdir()
    _git("init", "-q", cwd=project)
    _git("config", "user.email", "t@e.com", cwd=project)
    _git("config", "user.name", "T", cwd=project)
    sub = project / "pkg"
    sub_store = _init_store(sub)
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    session_id = "legacy-single-session"
    (sub_store / "tmp" / f"handoff-path.{session_id}").write_text(str(sub_store / "remember.md"))

    result = _write_handoff(sub, home, "legacy single-candidate note\n", session_id)
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    assert "legacy single-candidate note" in (sub_store / "remember.md").read_text()


# ---------------------------------------------------------------------------
# Case 2: a repository or submodule inside another repository
# ---------------------------------------------------------------------------


def test_nested_repo_inside_another_repo_resolves_via_pointer_not_the_inner_toplevel(tmp_path):
    """outer/ is a git repo; outer/inner/x is INSIDE A SEPARATE git repo
    (standing in for a submodule) -- the ancestor walk from `x` would stop at
    INNER's own toplevel and never see outer's store at all, the gap #1083
    exists to close. SessionStart resolved outer's store; writing from deep
    inside inner must still land there."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _git("init", "-q", cwd=outer)
    _git("config", "user.email", "t@e.com", cwd=outer)
    _git("config", "user.name", "T", cwd=outer)
    outer_store = _init_store(outer)

    inner = outer / "inner"
    inner.mkdir()
    _git("init", "-q", cwd=inner)
    _git("config", "user.email", "t@e.com", cwd=inner)
    _git("config", "user.name", "T", cwd=inner)
    x = inner / "x"
    x.mkdir()

    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    session_id = "nested-repo-session"
    _write_pointer(home, session_id, outer_store)

    result = _write_handoff(x, home, "note from inside the nested repo\n", session_id)
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    assert "note from inside the nested repo" in (outer_store / "remember.md").read_text(), (
        "the note did not land in the outer project's own store"
    )
    inner_remember = inner / ".remember"
    assert not inner_remember.exists(), (
        "a store was created at the INNER repository's own toplevel -- "
        "exactly the gap an ancestor walk, bounded at the inner .git, "
        "cannot avoid on its own"
    )


# ---------------------------------------------------------------------------
# Case 3: a non-git sibling, and a git sibling with no hint
# ---------------------------------------------------------------------------


def test_non_git_sibling_resolves_via_pointer(tmp_path):
    """a/proj is a git project with a resolved store; a/other has NO .git at
    all. The ancestor walk never even enters its disambiguation branch for a
    non-git cwd (`git rev-parse --show-toplevel` returns nothing) -- the
    pointer is the only thing that can still get this right."""
    a = tmp_path / "a"
    proj = a / "proj"
    other = a / "other"
    proj.mkdir(parents=True)
    other.mkdir(parents=True)
    _git("init", "-q", cwd=proj)
    _git("config", "user.email", "t@e.com", cwd=proj)
    _git("config", "user.name", "T", cwd=proj)
    proj_store = _init_store(proj)

    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    session_id = "non-git-sibling-session"
    _write_pointer(home, session_id, proj_store)

    result = _write_handoff(other, home, "note from a non-git sibling\n", session_id)
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    assert "note from a non-git sibling" in (proj_store / "remember.md").read_text()
    assert not (other / ".remember").exists()


def test_git_sibling_with_no_hint_resolves_via_pointer(tmp_path):
    """Same shape, but the sibling IS its own git repository -- the ancestor
    walk fallback (if it ran at all) would stop at ITS OWN toplevel, finding
    no hint there and falling through to a store created at the sibling's
    own root. The pointer pre-empts that walk entirely."""
    a = tmp_path / "a"
    proj = a / "proj"
    sibling = a / "sibling"
    proj.mkdir(parents=True)
    sibling.mkdir(parents=True)
    for d in (proj, sibling):
        _git("init", "-q", cwd=d)
        _git("config", "user.email", "t@e.com", cwd=d)
        _git("config", "user.name", "T", cwd=d)
    proj_store = _init_store(proj)

    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    session_id = "git-sibling-no-hint-session"
    _write_pointer(home, session_id, proj_store)

    result = _write_handoff(sibling, home, "note from a git sibling with no hint\n", session_id)
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    assert "note from a git sibling with no hint" in (proj_store / "remember.md").read_text()
    assert not (sibling / ".remember").exists()


# ---------------------------------------------------------------------------
# Case 4: a same-second mtime tie -- the pointer makes it irrelevant
# ---------------------------------------------------------------------------


def test_same_second_hint_tie_is_irrelevant_once_a_pointer_exists(tmp_path):
    """Two ancestors both carry this session's hint, written in the SAME
    second (indistinguishable by whole-second mtime -- worse on FAT, 2s
    resolution) -- exactly the shape that defeated the pre-#1083
    freshest-mtime tie-break. A pointer naming a THIRD store entirely proves
    the ancestor walk, and its mtimes, are never even consulted: the walk
    only runs as a fallback once the pointer read has already failed."""
    project = tmp_path / "proj"
    project.mkdir()
    _git("init", "-q", cwd=project)
    _git("config", "user.email", "t@e.com", cwd=project)
    _git("config", "user.name", "T", cwd=project)
    root_store = _init_store(project)

    pkg = project / "pkg"
    pkg_store = _init_store(pkg)

    session_id = "same-second-tie-session"
    root_hint = root_store / "tmp" / f"handoff-path.{session_id}"
    pkg_hint = pkg_store / "tmp" / f"handoff-path.{session_id}"
    root_hint.write_text(str(root_store / "remember.md"))
    pkg_hint.write_text(str(pkg_store / "remember.md"))
    tie = time.time()
    os.utime(root_hint, (tie, tie))
    os.utime(pkg_hint, (tie, tie))

    home = tmp_path / "home"
    external = home / ".remember" / "the-real-store"
    external.mkdir(parents=True)
    (external / "config.json").write_text(json.dumps({"data_dir": str(external)}))
    (home / ".claude" / "projects").mkdir(parents=True)
    _write_pointer(home, session_id, external)

    result = _write_handoff(pkg, home, "note despite the tied ancestors\n", session_id)
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    assert "note despite the tied ancestors" in (external / "remember.md").read_text(), (
        "the pointer's own target was not used -- the tied ancestors' "
        "mtimes should never have been consulted at all"
    )


def test_positive_control_same_second_tie_with_no_pointer_refuses_rather_than_guesses(tmp_path):
    """Same tied-ancestor fixture, but with NO pointer -- the #1083 fallback
    shape. The pre-#1083 code picked one of the two by mtime (effectively at
    random, since they tie); #1083's ruling is to REFUSE an ambiguous result
    outright instead."""
    project = tmp_path / "proj"
    project.mkdir()
    _git("init", "-q", cwd=project)
    _git("config", "user.email", "t@e.com", cwd=project)
    _git("config", "user.name", "T", cwd=project)
    root_store = _init_store(project)
    pkg = project / "pkg"
    pkg_store = _init_store(pkg)

    session_id = "same-second-tie-no-pointer-session"
    root_hint = root_store / "tmp" / f"handoff-path.{session_id}"
    pkg_hint = pkg_store / "tmp" / f"handoff-path.{session_id}"
    root_hint.write_text(str(root_store / "remember.md"))
    pkg_hint.write_text(str(pkg_store / "remember.md"))
    tie = time.time()
    os.utime(root_hint, (tie, tie))
    os.utime(pkg_hint, (tie, tie))

    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)

    result = _write_handoff(pkg, home, "should never be written\n", session_id)
    assert result.returncode == 1
    assert "REFUSED" in result.stderr
    assert not (root_store / "remember.md").exists()
    assert not (pkg_store / "remember.md").exists()


# ---------------------------------------------------------------------------
# Case 5: an external ~/.remember/{slug} store
# ---------------------------------------------------------------------------


def test_external_slug_store_resolves_via_pointer(tmp_path):
    """The pointer names an external store path outright -- not derived from
    cwd at all. Any cwd, including one with no relationship whatsoever to
    the external store's own slug, must still land there."""
    project = tmp_path / "some-project"
    project.mkdir()
    home = tmp_path / "home"
    external = home / ".remember" / "some-slug"
    external.mkdir(parents=True)
    (external / "tmp").mkdir()
    (home / ".claude" / "projects").mkdir(parents=True)

    session_id = "external-slug-session"
    _write_pointer(home, session_id, external)

    result = _write_handoff(project, home, "note into the external slug store\n", session_id)
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    assert "note into the external slug store" in (external / "remember.md").read_text()


# Case 6 (a Windows path case, NOT skipif(win32)) lives in its own file,
# tests/test_session_store_shape_windows_1083.py -- scheduler review,
# finding 3: this module is skipif(win32) at the TOP, which pytest applies
# to every item in the module INCLUDING a class inside it, regardless of
# what the class's own docstring claims; a class-level `pytestmark` only
# ADDS to the module's, it cannot override it. The only way to make this
# case actually run on windows-latest is to put it somewhere the module
# skip never reaches.


# ---------------------------------------------------------------------------
# doctor.sh reports the same resolution write-handoff.sh would use
# ---------------------------------------------------------------------------


def test_doctor_reports_the_pointer_resolved_store_not_a_cwd_guess(tmp_path):
    a = tmp_path / "a"
    proj = a / "proj"
    other = a / "other"
    proj.mkdir(parents=True)
    other.mkdir(parents=True)
    _git("init", "-q", cwd=proj)
    _git("config", "user.email", "t@e.com", cwd=proj)
    _git("config", "user.name", "T", cwd=proj)
    proj_store = _init_store(proj)

    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    session_id = "doctor-pointer-session"
    _write_pointer(home, session_id, proj_store)

    result = _doctor(other, home, session_id)
    assert result.returncode == 0
    assert str(proj_store) in result.stdout, (
        f"doctor did not report the pointer-resolved store:\n{result.stdout}"
    )


def test_doctor_warns_rather_than_silently_guessing_on_an_ambiguous_fallback(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _git("init", "-q", cwd=project)
    _git("config", "user.email", "t@e.com", cwd=project)
    _git("config", "user.name", "T", cwd=project)
    root_store = _init_store(project)
    pkg = project / "pkg"
    pkg_store = _init_store(pkg)

    session_id = "doctor-ambiguous-session"
    root_hint = root_store / "tmp" / f"handoff-path.{session_id}"
    pkg_hint = pkg_store / "tmp" / f"handoff-path.{session_id}"
    root_hint.write_text(str(root_store / "remember.md"))
    pkg_hint.write_text(str(pkg_store / "remember.md"))

    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)

    result = _doctor(pkg, home, session_id)
    assert result.returncode == 0
    assert "ambiguous" in result.stdout.lower() or "more than one candidate" in result.stdout.lower(), (
        f"doctor did not disclose the ambiguous resolution:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# SessionStart actually writes the pointer, and respects REMEMBER_DIR_UNSAFE
# ---------------------------------------------------------------------------


def test_session_start_writes_a_pointer_outside_the_project_store(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    remember = project / ".remember"
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    session_id = "pointer-write-session"

    result = _session_start(project, home, session_id, remember)
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"

    pointer = home / ".remember" / "tmp" / "session-store" / session_id
    assert pointer.exists(), "SessionStart did not write the session-store pointer"
    assert pointer.read_text().strip() == str(remember)


def test_session_start_overwrites_the_pointer_on_resume(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    other_remember = other / ".remember"
    (other_remember / "tmp").mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    session_id = "pointer-resume-session"

    _session_start(project, home, session_id, remember, source="startup")
    pointer = home / ".remember" / "tmp" / "session-store" / session_id
    assert pointer.read_text().strip() == str(remember)

    _session_start(other, home, session_id, other_remember, source="resume")
    assert pointer.read_text().strip() == str(other_remember), (
        "the pointer was not overwritten on --resume"
    )


def test_session_start_deletes_a_stale_pointer_when_resume_lands_on_an_unsafe_store(tmp_path):
    """Scheduler review, finding 2: start in A (writes a good pointer),
    resume in C whose own REMEMBER_DIR fails the #1064/#920 safety check --
    the resume must not leave A's pointer behind for write-handoff.sh to
    still read. Red before the fix: the resume's write was skipped but the
    EARLIER pointer for this same session id survived untouched, so
    write-handoff.sh (run from C, no cwd relation to A at all) still wrote
    into A's store -- the #1058 defect again, bypassing #1064's own
    unsafe-store refusal entirely."""
    project_a = tmp_path / "a"
    project_a.mkdir()
    remember_a = project_a / ".remember"
    (remember_a / "tmp").mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    session_id = "resume-onto-unsafe-session"

    _session_start(project_a, home, session_id, remember_a, source="startup")
    pointer = home / ".remember" / "tmp" / "session-store" / session_id
    assert pointer.read_text().strip() == str(remember_a)

    project_c = tmp_path / "c"
    project_c.mkdir()
    env = {**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project_c),
           "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT), "REMEMBER_DIR": "relative/not-absolute",
           "REMEMBER_DIR_UNSAFE": "1", "_LIB_MEMORY_DIR_LOADED": "1", "REMEMBER_DEFER": "0"}
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "resume",
        "cwd": str(project_c),
    })
    subprocess.run(["bash", str(SESSION_START)], input=payload, env=env,
                    capture_output=True, text=True, timeout=60, cwd=str(project_c),
                    check=False)

    assert not pointer.exists(), (
        "the resume onto an unsafe store left the earlier, good pointer in "
        "place -- write-handoff.sh would still read it and write into A"
    )

    result = _write_handoff(project_c, home, "must not reach A\n", session_id)
    assert not (remember_a / "remember.md").exists(), (
        f"write-handoff.sh still wrote into A's store via the stale "
        f"pointer.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def test_session_start_does_not_write_a_pointer_for_an_unsafe_remember_dir(tmp_path):
    """#1064/#920: never write a pointer into (or naming) an unsafe store."""
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    session_id = "pointer-unsafe-session"

    env = {**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project),
           "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT), "REMEMBER_DIR": "relative/not-absolute",
           "REMEMBER_DIR_UNSAFE": "1", "_LIB_MEMORY_DIR_LOADED": "1", "REMEMBER_DEFER": "0"}
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(project),
    })
    subprocess.run(["bash", str(SESSION_START)], input=payload, env=env,
                    capture_output=True, text=True, timeout=60, cwd=str(project),
                    check=False)

    pointer = home / ".remember" / "tmp" / "session-store" / session_id
    assert not pointer.exists(), (
        "a pointer was written even though REMEMBER_DIR_UNSAFE was set"
    )
