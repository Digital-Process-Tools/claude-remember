"""#431 — save-session.sh, doctor.sh and a direct pipeline.extract call still
trust an ambient REMEMBER_TRANSCRIPT_PATH; this pins the decision, not a bug.

#424 asked what containment means for a transcript path when the host will
not tell you where transcripts live. #430 hardened the two hooks that have no
transcript_path of their own to offer (post-tool-hook.sh, user-prompt-hook.sh)
by unsetting the variable outright. This closes the remainder of that row: a
*manual* invocation -- a user running save-session.sh or doctor.sh by hand, or
calling pipeline.extract directly -- has no hook preamble to clear anything in,
and the decision recorded in pipeline/host.py and README.md is that such a
caller trusts its own process environment by design, the same way it already
trusts $PATH or $HOME. The hooks remain the hardened boundary.

Two things are pinned:

1. save-session.sh's own file never grows a silent `unset
   REMEMBER_TRANSCRIPT_PATH` -- that would flip the documented decision without
   telling anyone, exactly what the issue calls "the absence of a check that
   nobody decided" in the other direction.
2. `pipeline.extract.find_session()`, called directly (the manual-invocation
   shape), still honours a supplied path that lives nowhere near any project
   or session directory -- documenting that this is existence-only by design,
   not accidentally uncontained.
3. doctor.sh, run with the variable ambiently set, says so loudly (WARN) with
   the path in its report, rather than silently proceeding -- this is the one
   real behavioural fix in this issue, since doctor.sh currently says nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import extract as E

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_SESSION = REPO_ROOT / "scripts" / "save-session.sh"
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

# Scoped per-test, not module-wide: only the tests below that actually read a
# .sh file's text or spawn `bash` need POSIX semantics. The one that calls
# pipeline.extract / pipeline.host in-process (monkeypatch + pathlib only) has
# no such dependency, and a blanket skip would report Windows coverage this
# suite does not have for the #431 trust decision itself.
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)


@_posix_only
def test_save_session_never_silently_clears_transcript_path():
    """MUST NOT FIRE: manual invocation honours the variable by design."""
    text = SAVE_SESSION.read_text(encoding="utf-8")
    assert "unset REMEMBER_TRANSCRIPT_PATH" not in text, (
        "save-session.sh now clears REMEMBER_TRANSCRIPT_PATH -- that reverses "
        "the #431 decision that a manual invocation trusts its own "
        "environment. If that decision changed, update pipeline/host.py's "
        "docstring and README.md's transcript-path section, not just this line."
    )


def test_manual_extract_honours_a_path_outside_any_session_store(tmp_path, monkeypatch):
    """MUST FIRE: this is the documented trust, not a gap. A direct
    pipeline.extract call -- the doctor.sh / manual shape -- uses a supplied
    path verbatim even when it is nowhere near a project or session dir."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    outside = tmp_path / "nowhere-near-a-session-store" / "file.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text('{"type": "user", "message": {"role": "user", "content": "hi"}}\n',
                        encoding="utf-8")
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(outside))
    assert E.find_session("whatever-id", str(proj)) == str(outside)


def _doctor_project(tmp_path: Path):
    from pipeline.slug import session_dir_slug as _slug
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    return home, project, remember


def _run_doctor(home, project, remember, extra_env=None):
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
        **(extra_env or {}),
    }
    return subprocess.run(["bash", str(DOCTOR)], env=env,
                          capture_output=True, text=True, timeout=120)


@_posix_only
def test_doctor_warns_loudly_when_transcript_path_is_ambiently_set(tmp_path):
    """MUST FIRE: this is the real behavioural fix. Before it, doctor.sh said
    nothing about a REMEMBER_TRANSCRIPT_PATH left in the environment -- the
    exact "absence of a check that nobody decided" the issue forbids."""
    home, project, remember = _doctor_project(tmp_path)
    victim = tmp_path / "victim.jsonl"
    victim.write_text("not doctor's business", encoding="utf-8")

    result = _run_doctor(home, project, remember,
                         {"REMEMBER_TRANSCRIPT_PATH": str(victim)})

    assert result.returncode == 0, result.stderr
    assert "REMEMBER_TRANSCRIPT_PATH" in result.stdout, (
        f"doctor.sh did not mention an ambient REMEMBER_TRANSCRIPT_PATH at "
        f"all:\n{result.stdout}"
    )
    assert str(victim) in result.stdout


@_posix_only
def test_doctor_says_nothing_when_transcript_path_is_unset(tmp_path):
    """MUST NOT FIRE (positive control): no ambient value, no warning --
    proves the check above is not just always-on noise."""
    home, project, remember = _doctor_project(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "REMEMBER_TRANSCRIPT_PATH"}
    result = subprocess.run(
        ["bash", str(DOCTOR)],
        env={**env, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project),
             "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT), "REMEMBER_DIR": str(remember),
             "_LIB_MEMORY_DIR_LOADED": "1"},
        capture_output=True, text=True, timeout=120,
    )
    assert "REMEMBER_TRANSCRIPT_PATH" not in result.stdout


@_posix_only
def test_doctor_warning_survives_path_resolution_failure(tmp_path):
    """MUST FIRE: the WARN must not depend on path resolution succeeding.

    A first version of this fix placed the check after resolve-paths.sh's own
    early `exit 0` on failure, so the one loud thing this issue asked for
    silently never fired on exactly the run where a human most needs a full
    report -- an unresolvable install. Pointing CLAUDE_PROJECT_DIR at a
    directory that does not exist is resolve-paths.sh's own
    "PROJECT_DIR does not exist" failure shape (its FATAL, not a guess).
    """
    victim = tmp_path / "victim.jsonl"
    victim.write_text("not doctor's business", encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "CLAUDE_PROJECT_DIR": str(tmp_path / "does-not-exist"),
           "REMEMBER_TRANSCRIPT_PATH": str(victim)}
    result = subprocess.run(["bash", str(DOCTOR)], env=env, cwd=str(tmp_path),
                            capture_output=True, text=True, timeout=120)
    assert "problem" in result.stdout, (
        f"test setup did not actually hit resolve-paths.sh's failure branch:"
        f"\n{result.stdout}\n{result.stderr}"
    )
    assert "REMEMBER_TRANSCRIPT_PATH" in result.stdout, (
        f"doctor.sh dropped the transcript-path warning on a path-resolution "
        f"failure:\n{result.stdout}\n{result.stderr}"
    )


@_posix_only
def test_doctor_strips_control_characters_from_transcript_path(tmp_path):
    """MUST FIRE: an embedded newline in the ambient value must not forge a
    second report line (#408's own reasoning, applied to this new WARN)."""
    home, project, remember = _doctor_project(tmp_path)
    forged = str(tmp_path / "victim.jsonl") + "\nOK   FAKE = fine"
    result = _run_doctor(home, project, remember,
                         {"REMEMBER_TRANSCRIPT_PATH": forged})
    # The substring surviving is fine -- it is still inside the WARN's own
    # indented value line. What must never happen is the forged text landing
    # as ITS OWN report line, indistinguishable from a genuine OK/WARN/FAIL.
    assert "OK   FAKE = fine" not in result.stdout.splitlines(), (
        f"an embedded newline in REMEMBER_TRANSCRIPT_PATH forged a fake "
        f"report line of its own:\n{result.stdout}"
    )
