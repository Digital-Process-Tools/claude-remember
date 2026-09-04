"""Gemini CLI's own docs say it sets `CLAUDE_PROJECT_DIR` (#456) -- so the
`UserPromptSubmit` JSON-envelope branch in `scripts/user-prompt-hook.sh`
must not key off "is `CLAUDE_PROJECT_DIR` set" any more (#534).

## Why this branch specifically

`tests/test_codex_upsubmit_stdout_451.py` fixed Codex's stdout-contract
mismatch by wrapping the prompt stamp in a JSON envelope, gated on
``[ -n "${CLAUDE_PROJECT_DIR:-}" ]`` -- Claude Code always sets it, and at
the time (#451) Codex and Gemini CLI were both believed never to. #456 (this
repo's own `pipeline/host.py`) found that belief wrong for Gemini: its
bundled docs list `CLAUDE_PROJECT_DIR` as a compatibility alias it does set
(`tests/test_gemini_project_dir_var_456.py`). The old gate happened to give
Gemini the right answer (plain stdout) for the wrong reason -- "not set"
was standing in for "not Codex" -- and that stand-in stops being correct the
moment ANY host that is not Codex and does not set `CLAUDE_PROJECT_DIR`
shows up: the old gate would route it through Codex's JSON envelope by
default, unverified, on the strength of one missing variable.

## The fix, and the test that actually discriminates it

The gate now keys on Codex's own signature, `CODEX_SESSION_ID` /
`CODEX_THREAD_ID` -- the exact pair `pipeline/host.py`'s `CODEX.signature_vars`
already uses, captured from a real `codex exec` process
(`tests/fixtures/codex-env-463.txt`) and never observed on any other host.
That makes the JSON envelope Codex-only BY CONSTRUCTION, rather than
"everything that isn't visibly Claude Code" -- a deliberate, documented
choice among the two the issue names as live options: it is NOT a claim
that plain stdout is correct for Gemini's `BeforeAgent` contract (that stays
open, unverified, tracked by #532), only that guessing "JSON envelope" for
an unverified host is no safer than guessing "plain", and Codex is the only
host whose stdout contract is actually known to be a JSON schema.

`test_host_with_no_signal_at_all_gets_plain_stdout_not_json_envelope` below
is the one that is RED on the pre-fix gate: a host with neither
`CLAUDE_PROJECT_DIR` nor a Codex signature used to fall into the `else`
branch (JSON envelope) simply because `CLAUDE_PROJECT_DIR` was unset. The
Gemini-shaped test alongside it already passes on the unfixed gate too
(Gemini DOES set `CLAUDE_PROJECT_DIR`, so the old "is it set" gate already
answered "plain stdout" for it, for the wrong reason) -- it is kept here as
a positive-control pin on the new, more principled gate, not as the
evidence the fix changed anything.
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

sys.path.insert(0, str(REPO_ROOT))
from pipeline.host import CODEX

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
                "_LIB_MEMORY_DIR_LOADED", *CODEX.signature_vars):
        env.pop(key, None)
    (project.parent / "tmp").mkdir(exist_ok=True)
    return env


def _env_gemini(home: Path, project: Path) -> dict:
    """Gemini-shaped, per its own bundled docs (#456): `CLAUDE_PROJECT_DIR`
    IS set (the compatibility alias), and neither `CODEX_SESSION_ID` nor
    `CODEX_THREAD_ID` is present -- nothing Codex-specific about this
    process at all."""
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
    assert parsed is None, (
        f"stdout was wrapped in a JSON envelope, but this environment "
        f"carries no Codex signature at all: {out!r}"
    )


@WINDOWS_SKIP
def test_gemini_env_with_claude_project_dir_set_still_gets_plain_stdout(tmp_path):
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC"})
    stdin = json.dumps({"session_id": "s", "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    result = _run_upsubmit(_env_gemini(home, project), stdin=stdin)
    assert result.returncode == 0, result.stderr
    _assert_plain_stdout(result.stdout.strip())


@WINDOWS_SKIP
def test_host_with_no_signal_at_all_gets_plain_stdout_not_json_envelope(tmp_path):
    """The defect, stated directly: a host that sets neither
    `CLAUDE_PROJECT_DIR` nor a Codex signature (not Claude Code, not
    Codex, not Gemini as its docs describe it -- a genuinely unknown host,
    or a bare manual invocation) must NOT be routed through Codex's JSON
    envelope just because one variable happens to be unset. This is RED on
    the pre-#534 gate, which used exactly that absence as its whole signal."""
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC"})
    env = _base_env(home, project)
    stdin = json.dumps({"session_id": "s", "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    result = _run_upsubmit(env, stdin=stdin)
    assert result.returncode == 0, result.stderr
    _assert_plain_stdout(result.stdout.strip())


@WINDOWS_SKIP
def test_codex_signature_present_gets_json_envelope_even_without_claude_project_dir(tmp_path):
    """The positive half of the same control: a REAL Codex environment (no
    `CLAUDE_PROJECT_DIR`, but `CODEX_SESSION_ID`/`CODEX_THREAD_ID` set, the
    exact pair captured live in `tests/fixtures/codex-env-463.txt`) must
    still get the JSON envelope -- proving the new gate did not just delete
    Codex's own fix while narrowing who else it applies to."""
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC"})
    env = _base_env(home, project)
    env["CODEX_SESSION_ID"] = "01a04d64-fake-session"
    env["CODEX_THREAD_ID"] = "01a04d64-fake-thread"
    stdin = json.dumps({"session_id": "s", "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    result = _run_upsubmit(env, stdin=stdin)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    parsed = json.loads(out)
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("["), ctx
    assert WHO in ctx, ctx
