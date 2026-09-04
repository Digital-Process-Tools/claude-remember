"""Gemini CLI's own docs say it sets `CLAUDE_PROJECT_DIR` (#456) -- what that
means for `scripts/user-prompt-hook.sh`'s `UserPromptSubmit` JSON-envelope
gate (#534).

## What #534 found here

The gate has always been ``[ -n "${CLAUDE_PROJECT_DIR:-}" ]`` -- present
means plain stdout (Claude Code's own contract), unset means wrap the
`[HH:MM TZ -- user]` stamp in Codex's JSON envelope (#451/#452, because
Codex's own hook engine misreads a bare `[`-opening stdout as a failed
attempt at its own JSON contract). At the time, Codex and Gemini CLI were
both believed to leave `CLAUDE_PROJECT_DIR` unset, so "unset" doubled as
"Codex or Gemini, wrap it". #456 found that belief wrong for Gemini
specifically (its own bundled docs list `CLAUDE_PROJECT_DIR` as a
compatibility alias it DOES set).

#534 considered replacing the gate with a check on Codex's own signature
(`CODEX_SESSION_ID`/`CODEX_THREAD_ID`, the pair `pipeline/host.py`'s
`CODEX.signature_vars` uses) so the JSON envelope would be Codex-only by
construction. That was tried, and self-reviewed out: #465 already measured,
live, that neither variable survives into a process Codex spawns as a HOOK
-- only into a Codex TOOL-SHELL command (`pipeline/haiku.py`'s own note;
`tests/test_codex_signature_463.py`'s docstring). This script is registered
as a `UserPromptSubmit` HOOK in `hooks/hooks.codex.json`, so gating it on
that pair would have silently disabled the envelope on every real Codex
invocation and reopened #451/#452.

So the gate is UNCHANGED (`CLAUDE_PROJECT_DIR` set/unset) -- what changed is
only that "unset" is no longer read as "Codex or Gemini": it is Codex
(confirmed absent, live, #463), or any other host that genuinely never sets
it. Gemini setting `CLAUDE_PROJECT_DIR`, per its own docs, means Gemini now
falls into the SAME branch Claude Code already does -- plain stdout, no
JSON envelope -- which is REASONED, not observed (Gemini CLI's own
`BeforeAgent` stdout contract has never been driven live, #532).

## What this file actually pins

Since the gate did not change, there is no NEW red-then-green story for the
gate itself -- the tests below are a positive/negative CONTROL pinning the
gate's documented behaviour under the two identities #534 had to reason
about (Gemini-shaped, and a genuinely signal-less host), so a future change
that reintroduces the rejected Codex-signature idea (or otherwise narrows
the gate) is caught here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"

WINDOWS_SKIP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

WHO = "geminitester534"


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".remember" / "tmp").mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    return home, project


def _base_env(home: Path, project: Path) -> dict:
    env = {**os.environ, "HOME": str(home), "USER": WHO,
           "PLUGIN_ROOT": str(REPO_ROOT), "TMPDIR": str(project.parent / "tmp")}
    for key in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "REMEMBER_DIR",
                "REMEMBER_TZ", "REMEMBER_PROMPT_STAMP", "USERNAME",
                "_LIB_MEMORY_DIR_LOADED", "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
        env.pop(key, None)
    (project.parent / "tmp").mkdir(exist_ok=True)
    return env


def _env_gemini(home: Path, project: Path) -> dict:
    """Gemini-shaped, per its own bundled docs (#456): `CLAUDE_PROJECT_DIR`
    IS set (the compatibility alias). No Codex signature anywhere -- moot
    for this gate now, but kept absent so this fixture cannot be confused
    with a Codex one."""
    env = _base_env(home, project)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    return env


def _run_upsubmit(env: dict, *, stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(USER_PROMPT)], check=False, input=stdin,
                           capture_output=True, text=True, env=env, timeout=30)


def _write_config(home: Path, config: dict) -> None:
    (home / ".remember" / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _assert_plain_stdout(out: str) -> None:
    assert out.startswith("["), out
    assert out.endswith("]"), out
    parsed = None
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        pass
    assert parsed is None, f"stdout was wrapped in a JSON envelope: {out!r}"


def _assert_json_envelope(out: str) -> dict:
    parsed = json.loads(out)
    assert "hookSpecificOutput" in parsed, out
    return parsed


@WINDOWS_SKIP
def test_gemini_env_with_claude_project_dir_set_gets_plain_stdout(tmp_path):
    """Gemini now sets CLAUDE_PROJECT_DIR per its own docs, so it takes the
    SAME branch Claude Code already does -- plain stdout, no envelope. This
    is REASONED (Gemini CLI's own stdout contract is unverified, #532), not
    a claim it is what Gemini's BeforeAgent contract actually wants."""
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC"})
    stdin = json.dumps({"session_id": "s", "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    result = _run_upsubmit(_env_gemini(home, project), stdin=stdin)
    assert result.returncode == 0, result.stderr
    _assert_plain_stdout(result.stdout.strip())


@WINDOWS_SKIP
def test_a_genuinely_signal_less_host_still_gets_the_json_envelope(tmp_path):
    """The positive control on the OTHER side of the same gate: a host that
    sets no CLAUDE_PROJECT_DIR at all (Codex, live-confirmed, #463 -- or any
    other host this repo has never seen) must still get the envelope, since
    the gate was deliberately kept unchanged rather than narrowed to
    Codex's own (hook-unreachable) signature. If this ever starts failing
    because the gate was narrowed again, it is regressing #451/#452 on
    Codex, not just breaking a test."""
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC"})
    env = _base_env(home, project)
    stdin = json.dumps({"session_id": "s", "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    result = _run_upsubmit(env, stdin=stdin)
    assert result.returncode == 0, result.stderr
    wire = _assert_json_envelope(result.stdout.strip())
    ctx = wire["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("["), ctx
    assert WHO in ctx, ctx
