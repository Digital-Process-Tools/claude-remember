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
unsaved PREV_ID, against a SLOW stand-in save-session.sh. At most one of
those starts may actually reach the stub while the others overlap it.

TDD shape: a positive control (a single resume genuinely forces a save for
the unsaved previous session) is paired with the actual claim (repeated
rapid resumes do not stack that save) -- the "must fire" / "must not fire"
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


def _plugin_root_with_slow_recording_stub(tmp_path: Path, record: Path, *, sleep_seconds: float) -> Path:
    """A plugin root identical to the real one except that save-session.sh
    records its own invocation and then SLEEPS before exiting -- same
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
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s\n" "$*" >> "{record}"\n'
        f"sleep {sleep_seconds}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return root


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
    stub_sleep = 2.0
    root = _plugin_root_with_slow_recording_stub(tmp_path, record, sleep_seconds=stub_sleep)
    env = _env(home, project, remember, root)

    payload = _payload(CURRENT, transcript_path=str(current), source="resume")
    durations = []
    for _ in range(5):
        start = time.monotonic()
        result = subprocess.run(
            ["bash", str(root / "scripts" / "session-start-hook.sh")],
            env=env, input=payload, capture_output=True, text=True, timeout=60, check=False,
        )
        durations.append(time.monotonic() - start)
        assert result.returncode == 0, result.stderr
        time.sleep(0.2)

    # SessionStart's own FOREGROUND latency must stay unchanged -- this fix
    # moves where the recovery save WAITS (inside the already-backgrounded
    # deferred phase), never makes the foreground hook itself wait on it.
    slow = [d for d in durations if d >= stub_sleep]
    assert not slow, (
        f"a SessionStart call took as long as the stub's own sleep "
        f"({stub_sleep}s) -- the foreground hook is blocking on the "
        f"recovery save instead of backgrounding it: {durations!r}"
    )

    # Give the (at most one) backgrounded recovery save time to finish.
    deadline = time.time() + stub_sleep + 10
    while time.time() < deadline:
        time.sleep(0.2)

    calls = record.read_text().strip().splitlines() if record.exists() else []
    assert len(calls) <= 1, (
        f"{len(calls)} forced saves ran for ONE session's repeated rapid "
        f"resume while the first was still in flight -- the deferred-phase "
        f"PID marker did not stay alive for the whole recovery: {calls!r}"
    )


def test_a_positive_control_a_single_resume_does_force_save_the_unsaved_previous_session(tmp_path):
    """Paired with the test above: without this, "at most one" also passes
    if recovery is so broken it never fires at all.
    """
    home, project, remember, current = _startup_project(tmp_path)
    record = tmp_path / "save-calls-single.txt"
    root = _plugin_root_with_slow_recording_stub(tmp_path, record, sleep_seconds=0)
    env = _env(home, project, remember, root)

    payload = _payload(CURRENT, transcript_path=str(current), source="resume")
    result = subprocess.run(
        ["bash", str(root / "scripts" / "session-start-hook.sh")],
        env=env, input=payload, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr

    deadline = time.time() + 10
    while time.time() < deadline and not record.exists():
        time.sleep(0.05)
    assert record.exists(), (
        "positive control: a single resume for a genuinely unsaved previous "
        "session never triggered the recovery force-save at all"
    )
    assert PREV in record.read_text(), (
        f"the force-save that ran did not name the unsaved previous "
        f"session: {record.read_text()!r}"
    )
