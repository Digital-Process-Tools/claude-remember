"""doctor.sh has no SessionEnd liveness check (#370).

`PostToolUse`'s alive-marker check (scripts/doctor.sh, "5. Capture health")
reads a freshness window because that hook fires many times inside a single
live session -- a marker refreshed a few seconds ago means the hook is
running now. `SessionEnd` fires at most once per session, so that reading
does not transfer: "how old is the marker" is not the same question as "did
the hook run last time it had the chance". Filed rather than fixed inside
#368 (the SessionEnd hook itself), because a new check needs its own marker
convention and #345's acceptance criteria only covered README and docs.

No new marker file is introduced by this fix, and scripts/session-end-hook.sh
is not touched at all: that hook already leaves usable evidence of its own
accord, as a side effect of its background flush -- a
logs/autonomous/session-end-<HHMMSS>.log file, written unconditionally once
the hook gets past its SAVE_SCRIPT-missing check (see that hook's own
comments around its `_END_LOG` redirect). doctor.sh's job is reading that
signal, not producing a new one.

Three states, not two, and the fixtures below pin all three:

  * at least one session-end log exists -> fired, OK, and it must not appear
    as a problem (the must-fire case a silence assertion needs a positive
    control for);
  * no such log, but a transcript OTHER than the one running doctor.sh right
    now went quiet AFTER remember's own store started existing for this
    project -> the hook had the chance to fire and did not, FAIL, and it has
    to reach the VERDICT line;
  * no such log, and nothing shows a prior session, attributable to this
    project's remember store, ever finished -- either because 0-or-1
    transcript exists at all (the "just installed" and "mid-first-session"
    shapes), or because every quiet transcript PREDATES the store itself
    (#392 -- prior Claude Code history in a project remember was only just
    installed into) -> the third state the issue calls out by name: this
    must render as neither of the other two. A check that answered FAIL here
    would flag every fresh install, and every upgrade into a project with
    history, as broken before it ever had the chance to prove itself.

#392 added the third bullet's second half. Only *quietness* was being
measured -- a quiet transcript predating this project's own remember store is
indistinguishable, on quietness alone, from one whose session ended after the
hook was installed and simply failed to fire. doctor.sh reads
$REMEMBER_DIR/.gitignore's mtime as its install baseline, NOT REMEMBER_DIR's
own: bootstrap-dirs.sh writes that file exactly once, gated on it not already
existing, and nothing else ever touches it again -- unlike REMEMBER_DIR
itself, whose mtime save-session.sh resets on every ordinary save (a
mktemp-in-REMEMBER_DIR + mv both bump the directory's own mtime, not just
now.md's, so it reads as "time since the last save", not "time since
install", on any project with ongoing captures). A transcript is only
evidence of a genuine SessionEnd failure when it went quiet no earlier than
that marker. Tests below that mean to pin a GENUINE failure call
`_install_store()` to backdate that marker further into the past than the
transcript, precisely to establish the precondition -- a transcript backdated
past a store with no such marker (i.e. never `_install_store()`-ed) is, by
construction, the #392 false-positive shape instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

sys.path.insert(0, str(REPO_ROOT))

from pipeline.slug import session_dir_slug as _slug


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    return home, project, remember, session_dir


def _run(
    home: Path, project: Path, remember: Path, extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(DOCTOR)], env=env,
        capture_output=True, text=True, timeout=180, check=False,
    )


def _verdict(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("VERDICT:"):
            return line
    raise AssertionError("no VERDICT line in output:\n" + stdout)


def _backdate(path: Path, seconds: int) -> None:
    """Set a path's mtime `seconds` in the past -- doctor.sh's staleness
    check (>900s quiet) is how it tells "a prior session ended" apart from
    "another window on this project is open right now", and (#392) whether a
    transcript's mtime precedes the store's own install marker is how it
    tells a session that ended before remember was installed apart from one
    that ended after. Works on directories as well as files -- `os.utime`
    does not care.
    """
    when = time.time() - seconds
    os.utime(path, (when, when))


def _install_store(remember: Path, seconds_ago: int) -> None:
    """Simulate remember having been bootstrapped `seconds_ago` -- doctor.sh
    reads $REMEMBER_DIR/.install-marker's mtime as its install baseline
    (#392, #401), NOT REMEMBER_DIR's own: bootstrap-dirs.sh writes that file
    exactly once, gated on it not already existing, and nothing else in this
    codebase ever touches it again -- unlike REMEMBER_DIR itself, whose mtime
    save-session.sh resets on every ordinary save (mktemp-in-REMEMBER_DIR +
    mv both bump the directory's own mtime, not just now.md's), and unlike
    the ORIGINAL choice of $REMEMBER_DIR/.gitignore, which
    hooks.d/after_save/50-git-backup.sh deletes as a one-time cleanup once a
    migrated, backed-up store lands its first save (#401). A test that
    backdated REMEMBER_DIR instead would pin a baseline production code no
    longer reads; see test_doctor_session_end_survives_backup_cleanup_401.py
    for the fixture pinning the .gitignore-deletion case specifically.
    """
    marker = remember / ".install-marker"
    marker.write_text("installed\n", encoding="utf-8")
    _backdate(marker, seconds_ago)


def test_one_fresh_transcript_alone_is_still_the_third_state(tmp_path):
    """Exactly one transcript, un-backdated -- the shape doctor.sh itself
    produces when run mid-session, since a Bash tool call touches the
    current session's own transcript at (or just before) invocation. One
    file existing at all must not, by itself, be read as a prior session
    having ended -- that needs staleness, not mere existence.
    """
    home, project, remember, session_dir = _project(tmp_path)
    (session_dir / "aaaa-this-session.jsonl").write_text("{}\n", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "a single, freshly-touched transcript was read as a session having "
        "ended:\n" + result.stdout
    )
    assert "SessionEnd" not in _verdict(result.stdout)


def test_one_stale_transcript_after_install_is_enough_to_fail(tmp_path):
    """The other side of that boundary: ONE transcript is sufficient
    evidence once it has gone quiet, PROVIDED it went quiet after remember's
    own store started existing here -- the count never mattered, only
    whether something demonstrably stopped being active while the hook could
    have serviced it.

    The store's install marker (`_install_store()`) is backdated further than
    the transcript so the transcript is unambiguously attributable to a
    session that ran after the store (and so the hook) existed for this
    project -- the #392 precondition this test exists to pin now that
    quietness alone is not accepted as proof.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _install_store(remember, 7200)
    stale = session_dir / "aaaa-quiet-session.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    _backdate(stale, 3600)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd has never fired" in result.stdout, (
        "a single stale transcript, quiet after the store existed, was not "
        "enough to flag SessionEnd's silence:\n" + result.stdout
    )
    assert "SessionEnd" in _verdict(result.stdout)


def test_no_marker_and_no_prior_session_is_the_third_state_not_a_failure(tmp_path):
    """Fresh install / still inside the first session: must not read as broken.

    Zero transcripts in the session dir -- the shape a brand-new project or a
    doctor run from inside the very first session both produce. Nothing has
    had the chance to prove SessionEnd works or does not; a FAIL here would
    be the false alarm this test exists to rule out.
    """
    home, project, remember, _session_dir = _project(tmp_path)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "a store with no prior session was flagged as a SessionEnd failure:\n"
        + result.stdout
    )
    assert "SessionEnd" not in _verdict(result.stdout), (
        "the third state (nothing has ended yet) reached the VERDICT line "
        "as though it were a finding either way:\n" + result.stdout
    )


def test_prior_sessions_ended_after_install_fails_and_reaches_verdict(tmp_path):
    """The hook had its chance, after the store existed, and stayed silent --
    must fire, and must be FAIL.

    The store's install marker is backdated further than the transcripts,
    establishing the #392 precondition (this project's remember store already
    existed when the quiet session ran) that separates this from the false
    positive #392 reports. No end-marker despite that is the exact silent
    failure #370 reports: a SessionEnd hook that never fires reads as a
    healthy install.

    Paired with test_prior_cc_history_predating_the_store_is_not_a_failure
    below, the same transcript shape with the store NOT backdated -- the
    #392 false positive this precondition exists to rule out.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _install_store(remember, 7200)
    earlier = session_dir / "aaaa-earlier-session.jsonl"
    earlier.write_text("{}\n", encoding="utf-8")
    _backdate(earlier, 3600)
    later = session_dir / "bbbb-another-earlier-session.jsonl"
    later.write_text("{}\n", encoding="utf-8")
    _backdate(later, 1800)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd has never fired" in result.stdout, (
        "doctor did not flag SessionEnd's silence despite a prior session "
        "having demonstrably ended after the store existed:\n" + result.stdout
    )
    assert "SessionEnd" in _verdict(result.stdout), (
        "SessionEnd's silent failure did not reach the VERDICT line:\n"
        + result.stdout
    )


def test_two_concurrently_open_windows_do_not_false_positive(tmp_path):
    """Positive control for the FAIL case above, from the other direction:
    two LIVE, recently-touched transcripts must not be read as a session
    having ended.

    An earlier version of this fix counted transcripts rather than checking
    whether any had gone quiet, and treated "two or more *.jsonl files"
    alone as proof a session had ended -- which two ordinary, simultaneously
    open Claude Code windows on the same project also produce, with nothing
    broken and no session having ended at all. Without the staleness check,
    this fixture would false-positive exactly like the one above does
    correctly positive.
    """
    home, project, remember, session_dir = _project(tmp_path)
    (session_dir / "aaaa-window-one.jsonl").write_text("{}\n", encoding="utf-8")
    (session_dir / "bbbb-window-two.jsonl").write_text("{}\n", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "two concurrently open, recently-touched transcripts were read as "
        "a prior session having ended:\n" + result.stdout
    )
    assert "SessionEnd" not in _verdict(result.stdout), (
        "two live windows on the same project reached a SessionEnd problem "
        "verdict:\n" + result.stdout
    )


def test_marker_present_reports_ok_and_never_a_session_end_problem(tmp_path):
    """Positive control for the FAIL case above: the hook DID fire.

    Without this, a fix that flagged every store with two-or-more
    transcripts as broken -- never checking for the session-end log at all
    -- would still pass the FAIL test above. The fixture writes the exact
    file session-end-hook.sh's own background flush leaves behind
    (logs/autonomous/session-end-<HHMMSS>.log), not a purpose-built marker
    -- this fix reads that file, it does not introduce one.
    """
    home, project, remember, session_dir = _project(tmp_path)
    (session_dir / "aaaa-earlier-session.jsonl").write_text("{}\n", encoding="utf-8")
    (session_dir / "bbbb-another-earlier-session.jsonl").write_text("{}\n", encoding="utf-8")
    (remember / "logs" / "autonomous").mkdir(parents=True)
    (remember / "logs" / "autonomous" / "session-end-093000.log").write_text(
        "", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "OK   SessionEnd has fired at least once" in result.stdout, (
        "a store with a genuine session-end log was not reported OK:\n"
        + result.stdout
    )
    assert "problem — SessionEnd" not in _verdict(result.stdout), (
        "a working SessionEnd hook still reached a SessionEnd problem verdict:\n"
        + result.stdout
    )


def test_session_end_failure_outranks_the_generic_capture_is_working_verdict(tmp_path):
    """Ladder placement: SessionEnd's own silent failure must not hide behind
    a healthy-looking PostToolUse verdict.

    doctor.sh's own VERDICT header states the ladder's rule: specific causes
    are named before the general one. PostToolUse capture can be entirely
    healthy while SessionEnd -- a distinct hook, a distinct failure mode --
    has never fired once. Reaching "capture is working" first would be
    exactly the invisibility #370 reports, just moved one line down.

    The store's install marker is backdated past the transcripts so this
    stays the genuine-fail shape rather than sliding into the #392 false
    positive below, whose whole point is that a healthy PostToolUse verdict
    is the CORRECT verdict when the quiet transcripts predate the store.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _install_store(remember, 7200)
    earlier = session_dir / "aaaa-earlier-session.jsonl"
    earlier.write_text("{}\n", encoding="utf-8")
    _backdate(earlier, 3600)
    (session_dir / "bbbb-another-earlier-session.jsonl").write_text("{}\n", encoding="utf-8")
    _backdate(session_dir / "bbbb-another-earlier-session.jsonl", 1800)
    (remember / "tmp" / "capture-alive").write_text("sess-1", encoding="utf-8")
    (remember / "tmp" / "last-save.json").write_text(
        '{"session": "sess-1", "line": 500}', encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert _verdict(result.stdout).startswith("VERDICT: problem — SessionEnd"), (
        "PostToolUse capture being healthy masked SessionEnd's own silent "
        "failure instead of the specific cause outranking the general "
        "success line:\n" + result.stdout
    )


def test_prior_cc_history_predating_the_store_is_not_a_failure(tmp_path):
    """#392, row 1: a fresh install into a project with prior Claude Code
    history must not read SessionEnd as broken, and must not displace the
    correct fresh-install remediation.

    REMEMBER_DIR is NOT backdated (it is created "now", by the fixture,
    exactly as bootstrap-dirs.sh would on the very first hook invocation),
    while the transcripts are backdated 3 days -- prior history the plugin
    had nothing to do with. Nothing has been captured either (no
    capture-alive, no last-save): PostToolUse has genuinely never fired, and
    that is the actionable finding this report must surface -- not a
    SessionEnd verdict about sessions the hook was never installed for.

    Paired with test_prior_sessions_ended_after_install_fails_and_reaches_verdict
    above, the same transcript shape with the store genuinely predating them.
    """
    home, project, remember, session_dir = _project(tmp_path)
    for name in ("aaaa-old-a.jsonl", "bbbb-old-b.jsonl", "cccc-old-c.jsonl"):
        f = session_dir / name
        f.write_text("{}\n", encoding="utf-8")
        _backdate(f, 3 * 24 * 3600)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "prior Claude Code history predating this project's remember store "
        "was read as SessionEnd's own silent failure:\n" + result.stdout
    )
    assert "SessionEnd" not in _verdict(result.stdout), (
        "SessionEnd's silence on history it could never have serviced "
        "reached the VERDICT line:\n" + result.stdout
    )
    assert _verdict(result.stdout).startswith(
        "VERDICT: problem — PostToolUse has never fired"
    ), (
        "a fresh install with prior CC history and nothing captured did not "
        "surface the correct, actionable PostToolUse remediation:\n"
        + result.stdout
    )


def test_healthy_capture_with_prior_history_reports_capture_is_working(tmp_path):
    """#392, row 2: a demonstrably healthy install must not read as `problem`
    merely because the project has prior Claude Code history and no session
    has ended since remember was installed.

    Same predating-history shape as the test above, but PostToolUse HAS
    fired and a save HAS completed -- the report's job is to say so, not to
    manufacture a SessionEnd problem out of history the hook was never
    installed for.
    """
    home, project, remember, session_dir = _project(tmp_path)
    for name in ("aaaa-old-a.jsonl", "bbbb-old-b.jsonl", "cccc-old-c.jsonl"):
        f = session_dir / name
        f.write_text("{}\n", encoding="utf-8")
        _backdate(f, 3 * 24 * 3600)
    (remember / "tmp" / "capture-alive").write_text("sess-1", encoding="utf-8")
    (remember / "tmp" / "last-save.json").write_text(
        '{"session": "sess-1", "line": 500}', encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "a healthy capture with prior CC history was read as a SessionEnd "
        "failure:\n" + result.stdout
    )
    assert _verdict(result.stdout).startswith("VERDICT: capture is working"), (
        "a demonstrably healthy install with prior CC history did not "
        "reach the plain success verdict:\n" + result.stdout
    )


def test_an_unreadable_transcript_mtime_is_not_counted_as_evidence(tmp_path):
    """#392, defect 2: an unreadable mtime was counted into the transcript
    total and then `continue`d past the staleness question, so the WARN
    made a claim ("N transcript(s) ... none quiet long enough to call
    finished") about a file whose quietness was never established.

    `[ -f "$_tf" ]` is a bash BUILTIN -- its own stat() syscall, unaffected
    by PATH -- so it still reports the file present and regular. Only
    `_file_age_seconds`'s calls to the external `stat` command go through
    PATH, so a shim that fails for exactly this one filename (and delegates
    every other call to the real `stat`, including the ones the SessionEnd
    baseline and PostToolUse marker checks make) reproduces "found, but its
    age could not be read" deterministically -- the same third state
    already handled correctly for the PostToolUse marker 60 lines above
    this arm in doctor.sh.
    """
    home, project, remember, session_dir = _project(tmp_path)
    target = session_dir / "aaaa-unreadable.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "stat"
    shim.write_text(
        "#!/bin/sh\n"
        'for _a in "$@"; do\n'
        '  case "$_a" in\n'
        f'    */{target.name}) exit 1 ;;\n'
        "  esac\n"
        "done\n"
        'exec /usr/bin/stat "$@"\n'
    )
    shim.chmod(0o755)

    result = _run(home, project, remember,
                  {"PATH": f"{bindir}:{os.environ['PATH']}"})

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "a transcript whose mtime could not be read was treated as proof a "
        "prior session went quiet:\n" + result.stdout
    )
    assert "SessionEnd" not in _verdict(result.stdout), (
        "an unreadable transcript reached the VERDICT line as though its "
        "quietness had been established:\n" + result.stdout
    )
    assert "could not be read" in result.stdout, (
        "a transcript whose age could not be read was folded silently into "
        "the count instead of being named the way this file already names "
        "an unreadable PostToolUse marker:\n" + result.stdout
    )


def test_ongoing_saves_do_not_mask_a_genuine_failure(tmp_path):
    """Regression for a defect caught in self-review before this fix shipped:
    an earlier version of this fix read REMEMBER_DIR's OWN mtime as the
    install baseline, rather than $REMEMBER_DIR/.gitignore's. That is wrong
    -- save-session.sh writes now.md via mktemp directly inside REMEMBER_DIR
    followed by `mv` over the existing file, and BOTH operations update the
    directory's own mtime, not just now.md's. On any project with ongoing
    captures REMEMBER_DIR's mtime therefore reads as "time since the last
    save", continuously resetting toward "now" -- which would have silently
    turned every genuine SessionEnd failure back into the #392 false
    positive this fix exists to correct, the moment a single save landed
    after the quiet transcript.

    This fixture reproduces exactly that: the store's install marker is
    genuinely old (2 days), a transcript went stale after that (1 hour ago),
    and THEN an ordinary save's mktemp+mv into REMEMBER_DIR is simulated --
    bumping REMEMBER_DIR's own mtime to "just now" -- before doctor.sh runs.
    A correct fix reads the untouched .gitignore marker and still FAILs; the
    defect this pins would have read the freshly-bumped directory and
    silently downgraded to WARN instead.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _install_store(remember, 2 * 24 * 3600)
    stale = session_dir / "aaaa-quiet-session.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    _backdate(stale, 3600)

    # Simulate save-session.sh's own now.md update: mktemp a sibling directly
    # inside REMEMBER_DIR, then mv it over the target -- the same two
    # directory-mutating operations save-session.sh performs, in the same
    # directory, without touching .gitignore at all.
    now_md = remember / "now.md"
    now_md.write_text("placeholder\n", encoding="utf-8")
    append_tmp = remember / "now.md.append-simulated"
    append_tmp.write_text("## 12:00\nnew entry\n", encoding="utf-8")
    append_tmp.replace(now_md)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd has never fired" in result.stdout, (
        "an ordinary save landing after the genuinely-old install marker "
        "masked a real SessionEnd failure -- REMEMBER_DIR's own churned "
        "mtime was read instead of the stable .gitignore marker:\n"
        + result.stdout
    )
    assert "SessionEnd" in _verdict(result.stdout)
