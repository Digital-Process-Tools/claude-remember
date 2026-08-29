"""#444: user-prompt-hook.sh and post-tool-hook.sh cannot resolve the project
root on a host that never sets CLAUDE_PROJECT_DIR (Codex, Gemini CLI).

#411 gave session-start-hook.sh and session-end-hook.sh a REMEMBER_HOOK_CWD
fallback, read from their own stdin `cwd`. #417 then had every OTHER hook
`unset REMEMBER_HOOK_CWD` at entry, closing a leak where one hook's exported
value could be inherited by a hook that had not validated it -- but that left
user-prompt-hook.sh and post-tool-hook.sh with no legitimate source for the
variable at all. UserPromptSubmit and PostToolUse both carry `cwd` on their
own stdin payload on all three hosts, the same premise #411 already relied on
for the other two events.

Simulated, not observed (same caveat tests/test_hook_cwd_end_to_end_411.py
already carries): no Codex or Gemini binary exists on this machine, so "no
CLAUDE_PROJECT_DIR, cwd on stdin" is built by hand.

Positive control for the silence assertion: a payload with an unusable cwd
and no CLAUDE_PROJECT_DIR must still exit 0 (the soft-fail contract) AND must
scaffold nothing -- the same shape
tests/test_hook_cwd_end_to_end_411.py already uses for session-start/-end, so
"nothing appeared" and "the harness never ran the hook at all" cannot be
confused with each other.
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
USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"
POST_TOOL = REPO_ROOT / "scripts" / "post-tool-hook.sh"

SESSION_ID = "aaaaaaaa-0000-4000-8000-000000000001"


def _user_prompt_payload(cwd: str, **extra) -> str:
    body = {
        "session_id": SESSION_ID,
        "transcript_path": "/does/not/matter.jsonl",
        "hook_event_name": "UserPromptSubmit",
        "cwd": cwd,
        "prompt": "hello",
    }
    body.update(extra)
    return json.dumps(body)


def _post_tool_payload(cwd: str, **extra) -> str:
    body = {
        "session_id": SESSION_ID,
        "transcript_path": "/does/not/matter.jsonl",
        "hook_event_name": "PostToolUse",
        "cwd": cwd,
        "tool_name": "Read",
        "tool_input": {"file_path": "/does/not/matter.py"},
        "tool_response": {"content": "ok"},
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

def test_user_prompt_hook_resolves_project_root_from_stdin_cwd_when_claude_project_dir_is_unset(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "codex-project"
    project.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=None)

    result = _run(USER_PROMPT, env, _user_prompt_payload(str(project)), cwd=project)

    assert result.returncode == 0, result.stderr
    assert (project / ".remember").is_dir(), (
        "user-prompt-hook.sh did not resolve PROJECT_DIR from the stdin cwd; "
        f"stderr:\n{result.stderr}"
    )
    assert "FATAL: Cannot resolve project root" not in result.stderr


def test_post_tool_hook_resolves_project_root_from_stdin_cwd_when_claude_project_dir_is_unset(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "codex-project"
    project.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=None)

    result = _run(POST_TOOL, env, _post_tool_payload(str(project)), cwd=project)

    assert result.returncode == 0, result.stderr
    assert (project / ".remember").is_dir(), (
        "post-tool-hook.sh did not resolve PROJECT_DIR from the stdin cwd; "
        f"stderr:\n{result.stderr}"
    )
    assert "FATAL: Cannot resolve project root" not in result.stderr


# --- positive control: without a usable cwd, nothing is scaffolded ---------

def test_user_prompt_hook_scaffolds_nothing_when_neither_project_dir_nor_cwd_is_usable(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "codex-project"
    project.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=None)

    result = _run(
        USER_PROMPT, env,
        _user_prompt_payload(str(tmp_path / "never-created")), cwd=project,
    )

    assert result.returncode == 0, (
        "user-prompt-hook.sh must exit 0 even on resolution failure "
        f"(soft-fail contract); stderr:\n{result.stderr}"
    )
    assert not (project / ".remember").exists()
    assert not (tmp_path / "never-created").exists()


def test_post_tool_hook_scaffolds_nothing_when_neither_project_dir_nor_cwd_is_usable(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "codex-project"
    project.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=None)

    result = _run(
        POST_TOOL, env,
        _post_tool_payload(str(tmp_path / "never-created")), cwd=project,
    )

    assert result.returncode == 0, (
        "post-tool-hook.sh must exit 0 even on resolution failure "
        f"(soft-fail contract); stderr:\n{result.stderr}"
    )
    assert not (project / ".remember").exists()
    assert not (tmp_path / "never-created").exists()


# --- precedence: CLAUDE_PROJECT_DIR wins over a disagreeing stdin cwd ------

def test_user_prompt_hook_prefers_claude_project_dir_over_a_disagreeing_stdin_cwd(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    winner = tmp_path / "claude-project"
    winner.mkdir()
    loser = tmp_path / "stdin-cwd"
    loser.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=str(winner))

    result = _run(USER_PROMPT, env, _user_prompt_payload(str(loser)), cwd=winner)

    assert result.returncode == 0, result.stderr
    assert (winner / ".remember").is_dir()
    assert not (loser / ".remember").exists(), (
        "a stdin cwd disagreeing with a SET CLAUDE_PROJECT_DIR must not win"
    )


def test_post_tool_hook_prefers_claude_project_dir_over_a_disagreeing_stdin_cwd(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    winner = tmp_path / "claude-project"
    winner.mkdir()
    loser = tmp_path / "stdin-cwd"
    loser.mkdir()
    env = _env(home, REPO_ROOT, claude_project_dir=str(winner))

    result = _run(POST_TOOL, env, _post_tool_payload(str(loser)), cwd=winner)

    assert result.returncode == 0, result.stderr
    assert (winner / ".remember").is_dir()
    assert not (loser / ".remember").exists(), (
        "a stdin cwd disagreeing with a SET CLAUDE_PROJECT_DIR must not win"
    )
