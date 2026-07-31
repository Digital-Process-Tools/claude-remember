"""session-start-hook.sh must identify the previous session, not guess its slot (#270).

Both the recovery block and the capture-gap check resolved "the previous
session" with the same positional expression::

    ls -t "$SESSIONS_DIR"/*.jsonl | tail -n +2 | head -1

``tail -n +2`` skips slot 1 on the premise that the *current* session's
transcript already exists and sorts newest. At ``source=startup`` that premise
is false: Claude Code creates the transcript **after** the hook has run. For
that window the newest ``.jsonl`` *is* the previous session, so skipping it
lands on an arbitrarily older one.

The fixtures below pin that timing as a **state**, not as a sleep: a sessions
directory in which the newest transcript is genuinely the previous session,
because the current one does not exist yet. Every consequence of the off-by-one
is reachable from there without Claude Code and without a race.

Two consequences, and they pull in opposite directions, which is why both are
pinned here:

* a **false** capture-gap warning, judging a session old enough to predate the
  evidence store; and
* recovery **force-saving the wrong session**, leaving the real previous
  session's tail unsaved — the half that costs data.

The fix must not trade the loud bug for the quiet one, so the tests that assert
silence are matched one-for-one by tests that assert the warning still fires
for a genuine gap, and that it names the right session when it does.
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
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

from pipeline.slug import session_dir_slug as _slug  # noqa: E402
from .spawn_counting import make_shim_dir, spawns  # noqa: E402
from .subprocess_helpers import subprocess_failure_detail  # noqa: E402

TOOL_USE_LINE = '{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}\n'

# Three ids. OLDER is the session *before* the previous one — the one the
# positional pick lands on at startup. PREV is the genuine previous session.
# CURRENT is the session now starting, whose transcript does not exist yet.
OLDER = "aaaaaaaa-0000-4000-8000-000000000001"
PREV = "bbbbbbbb-0000-4000-8000-000000000002"
CURRENT = "cccccccc-0000-4000-8000-000000000003"


def _env(home: Path, project: Path, remember: Path, plugin_root: Path | None = None) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root or REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _startup_project(tmp_path: Path):
    """A project mid-`source=startup`: two past transcripts, no current one.

    This is the whole defect in one fixture. `ls -t` puts PREV first because
    the session now starting has not had its transcript created yet, so the
    positional pick's "slot 2 is the previous session" is off by one here and
    only here. mtimes are stated outright rather than raced for — the same
    reason test_capture_gap_notice.py states them.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)

    now = int(time.time())
    older = session_dir / f"{OLDER}.jsonl"
    older.write_text(TOOL_USE_LINE * 5)
    os.utime(older, (now - 600, now - 600))
    prev = session_dir / f"{PREV}.jsonl"
    prev.write_text(TOOL_USE_LINE * 5)
    os.utime(prev, (now - 60, now - 60))

    return home, project, remember, session_dir


def _vouch(remember: Path, session_id: str):
    """Evidence that PostToolUse was wired for this session — a per-session
    marker, the same one post-tool-hook.sh writes."""
    store = remember / "tmp" / "capture-alive.d"
    store.mkdir(parents=True, exist_ok=True)
    (store / session_id).write_text("")


def _payload(session_id: str, source: str = "startup") -> str:
    """A SessionStart stdin payload, shaped like the real one."""
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": source,
        "cwd": "/does/not/matter",
    })


def _run(env: dict, stdin_payload, script: Path = SESSION_START):
    kwargs = dict(env=env, capture_output=True, text=True, timeout=60)
    if stdin_payload is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_payload
    return subprocess.run(["bash", str(script)], **kwargs)


def _notice(remember: Path) -> Path:
    return remember / "tmp" / "capture-gap-notice"


def _reported(remember: Path) -> str:
    f = remember / "tmp" / "capture-gap-reported"
    return f.read_text().strip() if f.exists() else ""


def _plugin_root_with_stub_save(tmp_path: Path, record: Path) -> Path:
    """A plugin root identical to the real one except that save-session.sh
    records its argv instead of saving.

    Recovery force-saves in the background and nothing logs which session it
    chose, so the argv is the only place the choice is observable. Everything
    else is symlinked, so the hook under test is the hook under test.
    """
    root = tmp_path / "plugin"
    root.mkdir()
    for entry in REPO_ROOT.iterdir():
        if entry.name == "scripts":
            continue
        (root / entry.name).symlink_to(entry)
    scripts = root / "scripts"
    scripts.mkdir()
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.name == "save-session.sh":
            continue
        (scripts / entry.name).symlink_to(entry)
    stub = scripts / "save-session.sh"
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{record}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return root


def _await_record(record: Path, timeout: float = 30.0) -> str:
    """Recovery forks, so the argv lands asynchronously. Wait for the file
    rather than assuming the fork has run — an empty read here would fail the
    test for the wrong reason."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if record.exists() and record.read_text().strip():
            return record.read_text().strip()
        time.sleep(0.05)
    return record.read_text().strip() if record.exists() else ""


# ---------------------------------------------------------------------------
# The false positive
# ---------------------------------------------------------------------------


def test_a_healthy_previous_session_is_not_accused_at_startup(tmp_path):
    """The reported symptom. PREV is healthy — PostToolUse ran for it and left
    its marker. OLDER predates the evidence store entirely, so nothing can ever
    vouch for it. The positional pick judges OLDER and warns about a healthy
    install; the identity pick judges PREV and stays quiet.

    The dedupe cannot save this: it is keyed on the previous-session id, and a
    wrong id changes on every startup, so each restart mints a fresh unreported
    id and warns again.
    """
    home, project, remember, _ = _startup_project(tmp_path)
    _vouch(remember, PREV)

    result = _run(_env(home, project, remember), _payload(CURRENT))
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    assert not _notice(remember).exists(), (
        "a healthy install was told its previous session was not captured — "
        f"the check judged {_reported(remember) or 'some other session'} "
        f"instead of {PREV}, which has a capture marker"
    )


def test_the_check_says_nothing_when_it_cannot_know_which_session_is_ours(tmp_path):
    """No stdin, so no session id, so "which of these is the previous one" has
    no answer — the positional guess is right at resume and wrong at startup
    and the hook cannot tell which it is in.

    Silence is the right degradation *for the warning specifically*: a missed
    gap is still caught by /remember:doctor and by the recovery block, whereas
    a false one accuses a healthy install and spends the credibility this
    warning needs the one time it is true. It is not silence for free, though —
    see the disclosure test below.
    """
    home, project, remember, _ = _startup_project(tmp_path)
    _vouch(remember, PREV)

    result = _run(_env(home, project, remember), None)
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    assert not _notice(remember).exists(), (
        "the hook accused a session it had no way to identify"
    )


def test_a_check_that_did_not_run_says_so(tmp_path):
    """Silence is a positive claim too. "No warning" must not mean both "the
    previous session was captured" and "the question was never asked" — that
    conflation is this repo's own defect class (#144, #263, #266).

    So the skip leaves a marker, and doctor reports it.
    """
    home, project, remember, _ = _startup_project(tmp_path)
    _vouch(remember, PREV)

    _run(_env(home, project, remember), None)

    skipped = remember / "tmp" / "capture-gap-skipped"
    assert skipped.exists(), (
        "the capture-gap check did not run and left no trace — an absence the "
        "checker produced, indistinguishable from an absence in the world"
    )

    doc = _run(_env(home, project, remember), None, script=DOCTOR)
    assert "capture-gap" in doc.stdout, (
        "doctor does not disclose that the capture-gap check has been skipping:\n"
        + doc.stdout
    )


def test_the_marker_is_cleared_once_the_check_runs_again(tmp_path):
    """A stale marker would make doctor warn forever about a check that is now
    running fine — the opposite failure, and just as misleading."""
    home, project, remember, _ = _startup_project(tmp_path)
    _vouch(remember, PREV)
    (remember / "tmp" / "capture-gap-skipped").write_text("stale")

    _run(_env(home, project, remember), _payload(CURRENT))

    assert not (remember / "tmp" / "capture-gap-skipped").exists(), (
        "the skip marker outlived the condition it reports"
    )


# ---------------------------------------------------------------------------
# The safety net it must not cost
# ---------------------------------------------------------------------------


def test_a_genuine_gap_still_warns_and_names_the_right_session(tmp_path):
    """The inverse of the first test, and the reason this is not "just stop
    warning". PREV really was not captured; OLDER was. The warning must fire,
    and the id it dedupes on must be PREV — deduping on OLDER is what let the
    same wrong answer be re-reported on every restart.
    """
    home, project, remember, _ = _startup_project(tmp_path)
    _vouch(remember, OLDER)

    result = _run(_env(home, project, remember), _payload(CURRENT))
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    assert _notice(remember).exists(), (
        "a real capture gap went unreported — the fix removed the safety net "
        "instead of aiming it"
    )
    assert _reported(remember) == PREV, (
        f"the gap was recorded against {_reported(remember)!r}, not the session "
        f"it is about ({PREV})"
    )


def test_our_own_transcript_is_never_the_previous_session(tmp_path):
    """At resume/clear/compact/fork the current transcript exists and sorts
    newest, and at `/clear` the id is reused and the transcript shared. Picking
    "newest that is not ours" has to stay correct there, not just at startup.
    """
    home, project, remember, session_dir = _startup_project(tmp_path)
    cur = session_dir / f"{CURRENT}.jsonl"
    cur.write_text(TOOL_USE_LINE * 3)
    now = int(time.time())
    os.utime(cur, (now, now))
    _vouch(remember, OLDER)

    result = _run(_env(home, project, remember), _payload(CURRENT, source="resume"))
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    assert _reported(remember) == PREV, (
        f"judged {_reported(remember)!r} — the current session is not its own "
        "predecessor, and neither is the one before the previous one"
    )


# ---------------------------------------------------------------------------
# Recovery: the half that costs data
# ---------------------------------------------------------------------------


def test_recovery_rescues_the_previous_session_not_the_one_before_it(tmp_path):
    """Observed on a healthy install: recovery force-saved a 71-line session
    that was already complete while the genuine previous session kept 181
    unsaved lines. Nothing logs it and doctor reports healthy, correctly, since
    capture *is* healthy — the tail is simply never picked up.
    """
    home, project, remember, _ = _startup_project(tmp_path)
    (remember / "tmp" / "last-save.json").write_text(json.dumps({"sessions": {}}))
    record = tmp_path / "save-argv.txt"
    root = _plugin_root_with_stub_save(tmp_path, record)

    result = _run(
        _env(home, project, remember, plugin_root=root),
        _payload(CURRENT),
        script=root / "scripts" / "session-start-hook.sh",
    )
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    argv = _await_record(record)
    assert PREV in argv, (
        f"recovery force-saved {argv!r} — the previous session's unsaved tail "
        "was left on disk, and nothing will report it"
    )
    assert OLDER not in argv, (
        f"recovery rescued the session before the previous one: {argv!r}"
    )


def test_recovery_and_the_gap_check_agree_on_which_session_is_previous(tmp_path):
    """Two independent resolutions are how these drifted apart in the first
    place. Pinned by outcome: whichever session recovery rescues is the one the
    gap check reports on."""
    home, project, remember, _ = _startup_project(tmp_path)
    (remember / "tmp" / "last-save.json").write_text(json.dumps({"sessions": {}}))
    record = tmp_path / "save-argv.txt"
    root = _plugin_root_with_stub_save(tmp_path, record)

    _run(
        _env(home, project, remember, plugin_root=root),
        _payload(CURRENT),
        script=root / "scripts" / "session-start-hook.sh",
    )

    argv = _await_record(record)
    assert _reported(remember) and _reported(remember) in argv, (
        f"recovery rescued {argv!r} while the gap check reported on "
        f"{_reported(remember)!r} — the same two answers, drifting again"
    )


# ---------------------------------------------------------------------------
# Reading stdin must never cost a session its start
# ---------------------------------------------------------------------------


def test_the_hook_does_not_block_on_an_open_stdin_that_is_never_written(tmp_path):
    """A pipe held open with nothing in it. This hook runs before the session
    exists, so blocking here is not a slow start — it is a session that never
    begins, with no output and nothing to say why. `read -t 1` bounds it."""
    home, project, remember, _ = _startup_project(tmp_path)

    proc = subprocess.Popen(
        ["bash", str(SESSION_START)], env=_env(home, project, remember),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("the hook blocked on stdin — the session would never start")
    finally:
        if proc.stdin:
            proc.stdin.close()

    assert proc.returncode == 0


def test_the_hook_does_not_block_on_a_terminal_stdin(tmp_path):
    """Invoked by hand from a shell, stdin is a tty nobody will type into."""
    # Imported here, not at module scope: `pty` pulls in `termios`, which does
    # not exist on Windows, and a module-level import runs at COLLECTION time —
    # before the module's own skipif(win32) can apply.
    import pty

    home, project, remember, _ = _startup_project(tmp_path)
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["bash", str(SESSION_START)], env=_env(home, project, remember),
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("the hook blocked on a terminal stdin")
    finally:
        os.close(master)
        os.close(slave)

    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# The race between recovery and the check that follows it
# ---------------------------------------------------------------------------


def test_the_gap_check_does_not_re_read_the_file_recovery_is_rewriting(tmp_path):
    """Recovery force-saves in the *background*, and the gap check immediately
    re-reads last-save.json through capture_was_seen → session_was_saved. So a
    session being rescued can read as unsaved in the very invocation that is
    rescuing it — or as saved, depending on which process wins. The answer to
    "was this captured" must not depend on whether a fork has finished.

    Asserted by count rather than by timing: last-save.json is consulted once,
    before the fork, and the answer is reused. Racing the fork to observe the
    inconsistency would be a test that fails on a loaded runner and passes on a
    quiet one, which pins nothing.
    """
    home, project, remember, _ = _startup_project(tmp_path)
    (remember / "tmp" / "last-save.json").write_text(json.dumps({"sessions": {}}))
    record = tmp_path / "save-argv.txt"
    root = _plugin_root_with_stub_save(tmp_path, record)
    log = tmp_path / "spawns.log"
    shims = make_shim_dir(tmp_path, log)

    env = _env(home, project, remember, plugin_root=root)
    env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
    env["SPAWN_LOG"] = str(log)

    result = _run(env, _payload(CURRENT),
                  script=root / "scripts" / "session-start-hook.sh")
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    # The saved-state query is identifiable by its own text, whichever tool
    # detect-tools.sh picked to run it.
    reads = [s for s in spawns(log) if "isline" in s]
    assert len(reads) == 1, (
        "last-save.json was read "
        f"{len(reads)} times, so the gap check re-asked a question recovery "
        "had already answered — and asked it of a file a background fork is "
        f"rewriting:\n" + "\n".join(reads)
    )
