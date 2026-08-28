"""#411 end-to-end: a real SessionStart/SessionEnd payload's `cwd` field
resolves PROJECT_DIR through the actual hook process, not just through
resolve-paths.sh sourced in isolation (tests/test_project_root_cwd_fallback_411.py
covers that half).

Simulated, not observed (issue #411's own instruction): no Codex or Gemini
binary exists on this machine, so "no CLAUDE_PROJECT_DIR, cwd on stdin" is
built by hand. It is a real test of this repo's own precedence chain and not
evidence that either host's payload is shaped exactly this way in practice.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
SESSION_END = REPO_ROOT / "scripts" / "session-end-hook.sh"


def _payload(cwd: str, **extra) -> str:
    body = {
        "session_id": "aaaaaaaa-0000-4000-8000-000000000001",
        "transcript_path": "/does/not/matter.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": cwd,
    }
    body.update(extra)
    return json.dumps(body)


def _env(home: Path, plugin_root: Path, claude_project_dir: str | None) -> dict:
    env = {
        **os.environ,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
    }
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("REMEMBER_HOOK_CWD", None)
    if claude_project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = claude_project_dir
    return env


def _run(script: Path, env: dict, payload: str, cwd: Path):
    return subprocess.run(
        ["bash", str(script)], env=env, input=payload,
        capture_output=True, text=True, timeout=60, cwd=str(cwd), check=False,
    )


# --- the gap itself: no CLAUDE_PROJECT_DIR, stdin cwd carries the project ---

def test_session_start_scaffolds_under_the_stdin_cwd_when_claude_project_dir_is_unset(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "codex-project"
    project.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=None)

    result = _run(SESSION_START, env, _payload(str(project)), cwd=project)

    assert result.returncode == 0, result.stderr
    assert (project / ".remember").is_dir(), (
        "session-start-hook.sh did not resolve PROJECT_DIR from the stdin "
        f"cwd; stderr:\n{result.stderr}"
    )


def test_session_end_scaffolds_under_the_stdin_cwd_when_claude_project_dir_is_unset(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "codex-project"
    project.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=None)
    payload = json.dumps({
        "session_id": "aaaaaaaa-0000-4000-8000-000000000001",
        "transcript_path": "/does/not/matter.jsonl",
        "hook_event_name": "SessionEnd",
        "reason": "other",
        "cwd": str(project),
    })

    result = _run(SESSION_END, env, payload, cwd=project)

    assert result.returncode == 0, result.stderr
    assert (project / ".remember").is_dir(), (
        "session-end-hook.sh did not resolve PROJECT_DIR from the stdin "
        f"cwd; stderr:\n{result.stderr}"
    )


# --- positive control: without a usable cwd, nothing is scaffolded ---------

def test_session_start_scaffolds_nothing_when_neither_project_dir_nor_cwd_is_usable(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # cwd in the payload names a path that does not exist -- resolve-paths.sh
    # must reject it exactly as it rejects an unusable REMEMBER_TRANSCRIPT_PATH.
    project = tmp_path / "codex-project"
    project.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=None)

    result = _run(SESSION_START, env, _payload(str(tmp_path / "never-created")), cwd=project)

    assert result.returncode == 0, (
        "session-start-hook.sh must exit 0 even on resolution failure "
        f"(soft-fail contract); stderr:\n{result.stderr}"
    )
    assert not (project / ".remember").exists()
    assert not (tmp_path / "never-created").exists()


# --- precedence: CLAUDE_PROJECT_DIR wins over a disagreeing stdin cwd ------

def test_session_start_prefers_claude_project_dir_over_a_disagreeing_stdin_cwd(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    winner = tmp_path / "claude-project"
    winner.mkdir()
    loser = tmp_path / "stdin-cwd"
    loser.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=str(winner))

    result = _run(SESSION_START, env, _payload(str(loser)), cwd=winner)

    assert result.returncode == 0, result.stderr
    assert (winner / ".remember").is_dir()
    assert not (loser / ".remember").exists(), (
        "a stdin cwd disagreeing with a SET CLAUDE_PROJECT_DIR must not win"
    )
