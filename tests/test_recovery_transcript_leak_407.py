"""#407 review finding: session-start-hook.sh's recovery block force-saves the
*previous* session (`PREV_ID`) in the background, but that background process
inherited `REMEMBER_TRANSCRIPT_PATH` -- exported from the *current* session's
own SessionStart payload a few lines earlier. `pipeline.extract.find_session()`
trusts that env var unconditionally, so the recovery save for `PREV_ID` would
have read and summarized the CURRENT session's transcript instead of the one
it was rescuing, while still labelling the record `PREV_ID`
(`extract_session()`'s `actual_id = session_id or ...`).

Caught by a self-review spawn before this ever shipped, not by a bug report --
pinned here so it cannot come back with the shape of a "helpful" future change
(e.g. someone moving the recovery spawn earlier or later in the script).
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
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

from pipeline.slug import session_dir_slug as _slug

OLDER = "aaaaaaaa-0000-4000-8000-000000000001"
PREV = "bbbbbbbb-0000-4000-8000-000000000002"
CURRENT = "cccccccc-0000-4000-8000-000000000003"
TOOL_USE_LINE = '{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}\\n'


def _env(home: Path, project: Path, remember: Path, plugin_root: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _plugin_root_with_env_recording_stub(tmp_path: Path, record: Path) -> Path:
    """A plugin root identical to the real one except that save-session.sh
    records its argv AND its inherited REMEMBER_TRANSCRIPT_PATH instead of
    saving, so the leak this test pins is observable from the outside."""
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
        f'printf "argv=%s transcript_path=%s\\n" "$*" "${{REMEMBER_TRANSCRIPT_PATH:-<unset>}}" >> "{record}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return root


def _startup_project(tmp_path: Path):
    """PREV is genuinely unsaved. CURRENT's own transcript already exists --
    the resume/clear/compact/fork shape, where recovery and this leak are
    both live (unlike a cold `source=startup`, where Claude Code has not yet
    created the current transcript and there is nothing for
    REMEMBER_TRANSCRIPT_PATH to point at)."""
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
    return home, project, remember, session_dir, current


def _payload(session_id: str, transcript_path: str, source: str = "resume") -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": transcript_path,
        "hook_event_name": "SessionStart",
        "source": source,
        "cwd": "/does/not/matter",
    })


def _await_record(record: Path, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if record.exists() and record.read_text().strip():
            return record.read_text().strip()
        time.sleep(0.05)
    return record.read_text().strip() if record.exists() else ""


def test_recovery_does_not_leak_the_current_sessions_transcript_path_to_the_previous_session_save(tmp_path):
    home, project, remember, _, current = _startup_project(tmp_path)
    record = tmp_path / "save-argv.txt"
    root = _plugin_root_with_env_recording_stub(tmp_path, record)

    payload = _payload(CURRENT, transcript_path=str(current), source="resume")
    result = subprocess.run(
        ["bash", str(root / "scripts" / "session-start-hook.sh")],
        env=_env(home, project, remember, root),
        input=payload, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr

    argv = _await_record(record)
    assert PREV in argv, f"recovery did not force-save the previous session: {argv!r}"
    assert f"transcript_path={current}" not in argv, (
        "the recovery save for the PREVIOUS session inherited the CURRENT "
        f"session's REMEMBER_TRANSCRIPT_PATH ({current}) -- its own extract "
        "would silently read and summarize the wrong transcript while still "
        f"labelling the record {PREV!r}: {argv!r}"
    )


def test_a_positive_control_the_stub_does_see_the_env_var_when_it_is_meant_to(tmp_path):
    """Without this, "not leaked" also passes if the stub simply never saw
    REMEMBER_TRANSCRIPT_PATH at all -- e.g. a typo in the stub itself, or
    something about the plugin-root symlink layout breaking export
    propagation generally. Invokes the SAME stub the negative assertion
    above reads, directly, with REMEMBER_TRANSCRIPT_PATH exported -- the
    legitimate case (a hook flushing ITS OWN, current, session) that the
    fix must not also break."""
    _, _, _, _, current = _startup_project(tmp_path)
    record = tmp_path / "save-argv-direct.txt"
    root = _plugin_root_with_env_recording_stub(tmp_path, record)

    env = {**os.environ, "REMEMBER_TRANSCRIPT_PATH": str(current)}
    result = subprocess.run(
        [str(root / "scripts" / "save-session.sh"), CURRENT, "--force"],
        env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr

    seen = record.read_text().strip()
    assert f"transcript_path={current}" in seen, (
        "the positive control itself did not observe the exported var reach "
        f"the stub -- a broken harness here would make the negative "
        f"assertion above vacuous: {seen!r}"
    )
