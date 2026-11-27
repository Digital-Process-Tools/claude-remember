"""#1086 review (item 2): capture-gap-check.sh's own recovery force-save
used to run detached (`& disown`) even though this whole file IS ALREADY the
backgrounded deferred phase session-start-hook.sh forked (#660) -- so the
per-session PID marker guarding against a second deferred phase
(deferred-<id>.pid, #1060) could vanish, removed by capture-gap-check.sh's
own EXIT trap, seconds into a save that took much longer. A resume racing in
during that window would see no live marker and relaunch a SECOND deferred
phase, which forces ANOTHER save-session.sh --force for the same still-
unsaved PREV_ID before the first had even finished -- the reporter's ~700
stacked forced saves and a save.lock wait of 30s+.

Two real processes, not a source-text assertion: N RAPID resume starts for
ONE session (#1060's own marker is keyed on the RESUMING session's own id,
not PREV_ID), each capable of triggering recovery for the SAME genuinely
unsaved PREV_ID, against a stand-in save-session.sh that BLOCKS on a sentinel
file the test releases (not a wall-clock sleep: a sleep raced the test's own
foreground runs on slow macOS runners, #1143). At most one of those starts may
actually reach the stub while the others overlap it.

TDD shape: the claim (repeated rapid resumes do not stack that save) is paired
with a control (a single resume genuinely forces a save, and a resume AFTER the
first recovery finished forces again) -- the "must fire" / "must not fire"
pairing this repo's own CLAUDE.md requires for a negative assertion.
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
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from subprocess_helpers import subprocess_failure_detail

from pipeline.slug import session_dir_slug as _slug

OLDER = "aaaaaaaa-1086-4000-8000-000000000001"
PREV = "bbbbbbbb-1086-4000-8000-000000000002"
CURRENT = "cccccccc-1086-4000-8000-000000000003"
TOOL_USE_LINE = '{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}\n'


def _env(home: Path, project: Path, remember: Path, plugin_root: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _plugin_root_with_slow_recording_stub(tmp_path: Path, record: Path, *, release: Path | None = None) -> Path:
    """A plugin root identical to the real one except that save-session.sh
    records its own invocation and then BLOCKS until the `release` file exists
    (no `release` -> exits at once). Blocking on a sentinel instead of `sleep N`
    keeps the save "in flight" exactly as long as the test needs, however slow
    the runner is: a wall-clock sleep raced the test's own 5 foreground hook
    runs on slow macOS runners, and once the sleep elapsed the first deferred
    phase legitimately exited and a later resume legitimately forced a second
    save (the flake this replaces). The loop is capped (3000 x 0.1s) so a test
    that dies never leaves the stub spinning. Same
    symlink-the-rest-of-the-tree shape as
    test_recovery_transcript_leak_407.py's own
    _plugin_root_with_env_recording_stub, for the same reason: every OTHER
    script this hook sources must be the real one.
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
    body = "#!/bin/bash\n" + f'printf "%s\n" "$*" >> "{record}"\n'
    if release is not None:
        body += (
            "_i=0\n"
            f'while [ ! -f "{release}" ] && [ "$_i" -lt 3000 ]; do sleep 0.1; _i=$((_i+1)); done\n'
        )
    body += "exit 0\n"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)
    return root


def _wait_for(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _resume(root: Path, env: dict, payload: str, remember: Path) -> None:
    # A foreground hook that waited on the (blocked) save would never return:
    # the timeout turns that into a loud failure, not a hang.
    result = subprocess.run(
        ["bash", str(root / "scripts" / "session-start-hook.sh")],
        env=env, input=payload, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, subprocess_failure_detail(result, remember)


def _calls(record: Path) -> list:
    return record.read_text().strip().splitlines() if record.exists() else []


def _marker_finished(marker: Path) -> bool:
    """True when the deferred phase is done: the marker is gone, OR it names a
    PID that is not alive. The second arm covers a phase that hit its EXIT
    trap before the foreground hook's `printf $!` wrote the marker (the
    marker then names a dead PID that nothing will ever remove).
    """
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return True
    if not text.isdigit():
        return False
    try:
        os.kill(int(text), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _startup_project(tmp_path: Path):
    """Same fixture shape as test_recovery_transcript_leak_407.py's own
    _startup_project: PREV is genuinely unsaved, with its own
    capture-alive.d marker planted (#1046) so the recovery guard actually
    vouches for it.
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
    current = session_dir / f"{CURRENT}.jsonl"
    current.write_text(TOOL_USE_LINE * 3)
    os.utime(current, (now, now))

    (remember / "tmp" / "last-save.json").write_text(json.dumps({"sessions": {}}))
    (remember / "tmp" / "capture-alive.d").mkdir(parents=True, exist_ok=True)
    (remember / "tmp" / "capture-alive.d" / PREV).write_text("", encoding="utf-8")
    return home, project, remember, current


def _payload(session_id: str, transcript_path: str, source: str = "resume") -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": transcript_path,
        "hook_event_name": "SessionStart",
        "source": source,
        "cwd": "/does/not/matter",
    })


def test_rapid_resume_starts_for_one_session_produce_at_most_one_forced_save_in_flight(tmp_path):
    home, project, remember, current = _startup_project(tmp_path)
    record = tmp_path / "save-calls.txt"
    release = tmp_path / "release-save"
    root = _plugin_root_with_slow_recording_stub(tmp_path, record, release=release)
    env = _env(home, project, remember, root)
    payload = _payload(CURRENT, transcript_path=str(current), source="resume")
    marker = remember / "tmp" / f"deferred-{CURRENT}.pid"

    try:
        _resume(root, env, payload, remember)
        # The first recovery save is now genuinely in flight (blocked on the
        # sentinel, so it stays in flight however slow this runner is).
        assert _wait_for(record.exists), "the first resume never reached the save stub"
        for _ in range(4):
            _resume(root, env, payload, remember)
        # Every later resume has returned (and SessionStart's foreground never
        # waited on the blocked save, or _resume's timeout would have fired).
        # Release the stub and wait until EVERY deferred phase has finished --
        # a wrongly-launched second phase would hold the marker until it did --
        # and only then count, so a slow second phase cannot be missed.
        release.write_text("", encoding="utf-8")
        assert _wait_for(lambda: _marker_finished(marker)), (
            "the deferred phase(s) never finished after the save was released"
        )
        calls = _calls(record)
        assert len(calls) == 1, (
            f"{len(calls)} forced saves ran for ONE session's repeated rapid "
            f"resume while the first was still in flight -- the deferred-phase "
            f"PID marker did not stay alive for the whole recovery: {calls!r}"
        )
        assert PREV in calls[0], f"the force-save did not name the unsaved previous session: {calls!r}"
    finally:
        release.write_text("", encoding="utf-8")


def test_control_a_resume_after_the_first_recovery_finished_forces_again(tmp_path):
    """Paired with the test above, the other half of the invariant, and its
    positive control (a single resume genuinely forces a save for the unsaved
    previous session): the marker guards only while the first recovery is IN
    FLIGHT. Once it has finished (the stub never marks PREV saved) a later
    resume legitimately forces again -- so the "exactly one" above is the
    marker working, not recovery being unable to fire twice or at all. This
    is also what the old sleep-based test hit on a slow runner: a legitimate
    second save, misread as stacking.
    """
    home, project, remember, current = _startup_project(tmp_path)
    record = tmp_path / "save-calls-twice.txt"
    root = _plugin_root_with_slow_recording_stub(tmp_path, record)
    env = _env(home, project, remember, root)
    payload = _payload(CURRENT, transcript_path=str(current), source="resume")
    marker = remember / "tmp" / f"deferred-{CURRENT}.pid"

    _resume(root, env, payload, remember)
    assert _wait_for(lambda: len(_calls(record)) >= 1), "first resume never forced a save"
    assert PREV in _calls(record)[0], f"the force-save did not name PREV: {_calls(record)!r}"
    assert _wait_for(lambda: _marker_finished(marker)), "first deferred phase never finished"
    _resume(root, env, payload, remember)
    assert _wait_for(lambda: len(_calls(record)) >= 2), (
        "a resume after the first recovery finished did not force again: "
        f"{_calls(record)!r}"
    )
