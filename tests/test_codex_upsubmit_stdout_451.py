"""Codex reads UserPromptSubmit as Failed once the hook actually prints (#451).

## The mechanism

Codex's own hook engine sniffs the first non-whitespace byte of a
`UserPromptSubmit` handler's stdout: `{` or `[` means "this is my JSON
contract" (`codex-rs/hooks/src/engine/output_parser.rs::looks_like_json`,
read from `openai/codex` @ 2026-08-29 -- Codex ships no separate hooks.md
naming this). That contract is
`codex-rs/hooks/schema/generated/user-prompt-submit.command.output.schema.json`,
consumed by `codex-rs/hooks/src/events/user_prompt_submit.rs::parse_completed`.
A byte that opens with `{`/`[` but fails to parse against that schema marks
the whole hook run `HookRunStatus::Failed` -- not because the hook errored
(exit code and stderr are irrelevant on this path), but because Codex tried
to read plain text as its own wire format.

`scripts/user-prompt-hook.sh` has always printed `[HH:MM TZ -- user]` on
stdout -- a prompt stamp that opens with `[`. Before #444, path resolution
hit a FATAL on a host that never sets `CLAUDE_PROJECT_DIR` and the hook
printed nothing at all, which is why this collision was invisible until
#444 made the hook succeed on Codex for the first time.

## The oracle

`_codex_upsubmit_status` below is a line-for-line port of that Rust
exit-0 branch, restricted to the two shapes this file ever hands it (a
plain string, and the JSON envelope the fix emits) -- it is not a general
JSON-Schema validator and is not fed attacker-controlled input.
`test_oracle_matches_codexs_own_fixtures` pins it against literal stdout
strings lifted from `codex-rs/hooks/src/engine/output_parser.rs`'s own
`structured_output_rejects_invalid_shapes_and_types` test, so a change to
this file's oracle that drifts from Codex's real parser fails on its own,
independent of anything `scripts/user-prompt-hook.sh` does.

## What is NOT covered here

Whether `scripts/session-start-hook.sh` has the same defect was the open
question in #451's brief. It does not: every stdout write in that script is
either a `=== ... ===` header or a line beginning with a letter (see
`test_session_start_first_byte_is_never_bracket_or_brace` below, which
drives the real hook end-to-end against a full store, a repeated-handoff
delivery, and an empty store -- the three shapes that produce this file's
own bracket-opening line, `"[already delivered ..."`, and it is always
preceded on the same run by an unconditional `echo "=== LAST HANDOFF ==="`
one line earlier). OBSERVED against those three shapes, not proven for
every reachable branch of a 1400-line script.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# NOT a module-level `pytestmark`: three tests below (the oracle tests) are
# pure Python against `_codex_upsubmit_status` -- no `bash`, no subprocess, no
# POSIX dependency -- and a blanket skip would silently drop them from the
# Windows leg along with the ones that actually need it, for a reason that
# does not apply to them (#451 review). Applied per-test instead, to the six
# that do drive a real `bash` subprocess.
WINDOWS_SKIP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

# ── The oracle: codex-rs/hooks/src/engine/output_parser.rs, ported ─────────

_UPSUBMIT_TOP_KEYS = {
    "continue", "decision", "hookSpecificOutput", "reason",
    "stopReason", "suppressOutput", "systemMessage",
}
_HOOK_SPECIFIC_KEYS = {"additionalContext", "hookEventName"}


def _parse_user_prompt_submit_wire(trimmed: str):
    """Mirrors `parse_json::<UserPromptSubmitCommandOutputWire>` -- an object,
    `additionalProperties: false` at both levels, `hookEventName` pinned to
    the literal string `"UserPromptSubmit"` when present, and every other
    field typed the way serde's derive would refuse a mismatch for (the
    `{"systemMessage":123}` fixture below is exactly this: valid JSON,
    invalid wire type)."""
    try:
        value = json.loads(trimmed)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    if not set(value).issubset(_UPSUBMIT_TOP_KEYS):
        return None
    if "continue" in value and not isinstance(value["continue"], bool):
        return None
    if "decision" in value and value["decision"] not in (None, "block"):
        return None
    for str_field in ("reason", "stopReason", "systemMessage"):
        if str_field in value and value[str_field] is not None \
                and not isinstance(value[str_field], str):
            return None
    if "suppressOutput" in value and not isinstance(value["suppressOutput"], bool):
        return None
    hso = value.get("hookSpecificOutput")
    if hso is not None:
        if not isinstance(hso, dict):
            return None
        if not set(hso).issubset(_HOOK_SPECIFIC_KEYS):
            return None
        if hso.get("hookEventName") != "UserPromptSubmit":
            return None  # "hookEventName" is `required` in the schema
        if "additionalContext" in hso and not isinstance(hso["additionalContext"], str):
            return None
    return value


def _codex_looks_like_json(stdout: str) -> bool:
    """`output_parser::looks_like_json`: sniffs the first non-whitespace byte."""
    trimmed = stdout.lstrip()
    return trimmed.startswith(("{", "["))


def _codex_upsubmit_status(stdout: str) -> str:
    """The exit-0 branch of `events/user_prompt_submit.rs::parse_completed`,
    for a hook that always exits 0 (this one is documented to)."""
    trimmed = stdout.strip()
    if trimmed == "":
        return "Completed"
    if _parse_user_prompt_submit_wire(trimmed) is not None:
        return "Completed"
    if _codex_looks_like_json(stdout):
        return "Failed"
    return "Completed"


# Lifted verbatim (as stdout strings) from codex-rs's own
# structured_output_rejects_invalid_shapes_and_types -- every one of these is
# JSON-shaped and invalid, so Codex's real parser reports Failed for each.
_CODEX_OWN_INVALID_FIXTURES = [
    "[]",
    '{"systemMessage":123}',
    '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":123}}',
    '{"hookSpecificOutput":{"additionalContext":"missing event name"}}',
    '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","updatedInput":{}}}',
    '{"unexpectedField":true}',
    "{",
]


def test_oracle_matches_codexs_own_fixtures():
    """Pins the port against Codex's own test vectors, independent of anything
    this repo emits -- if this file's oracle drifts from the real parser, this
    is where it is caught."""
    for stdout in _CODEX_OWN_INVALID_FIXTURES:
        assert _codex_upsubmit_status(stdout) == "Failed", stdout


def test_oracle_accepts_the_hookspecificoutput_envelope_this_fix_emits():
    valid = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "[10:54 CEST -- sanitized-user]",
        }
    })
    assert _codex_upsubmit_status(valid) == "Completed"


def test_oracle_marks_the_bare_stamp_failed():
    """The defect, stated as an assertion about the oracle itself: the exact
    line this hook has always printed opens with `[` and is not valid JSON,
    so Codex's own parser marks it Failed."""
    assert _codex_upsubmit_status("[10:54 CEST -- sanitized-user]") == "Failed"


# ── Driving the real hook ───────────────────────────────────────────────────

WHO = "codextester451"


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".remember" / "tmp").mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    return home, project


def _env_codex(home: Path, project: Path) -> dict:
    """Codex-shaped: no CLAUDE_PROJECT_DIR, PLUGIN_ROOT set (the vendor-neutral
    name Codex sets natively per resolve-paths.sh), cwd only on stdin."""
    env = {**os.environ, "HOME": str(home), "USER": WHO,
           "PLUGIN_ROOT": str(REPO_ROOT), "TMPDIR": str(project.parent / "tmp")}
    for key in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "REMEMBER_DIR",
                "REMEMBER_TZ", "REMEMBER_PROMPT_STAMP", "USERNAME",
                "_LIB_MEMORY_DIR_LOADED"):
        env.pop(key, None)
    (project.parent / "tmp").mkdir(exist_ok=True)
    return env


def _env_claude_code(home: Path, project: Path) -> dict:
    env = {**os.environ, "HOME": str(home), "USER": WHO,
           "CLAUDE_PROJECT_DIR": str(project), "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
           "TMPDIR": str(project.parent / "tmp")}
    for key in ("REMEMBER_DIR", "REMEMBER_TZ", "REMEMBER_PROMPT_STAMP",
                "USERNAME", "_LIB_MEMORY_DIR_LOADED"):
        env.pop(key, None)
    (project.parent / "tmp").mkdir(exist_ok=True)
    return env


def _run_upsubmit(env: dict, *, stdin: str | None = None) -> subprocess.CompletedProcess:
    kwargs = {"capture_output": True, "text": True, "env": env, "timeout": 30}
    if stdin is not None:
        kwargs["input"] = stdin
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run(["bash", str(USER_PROMPT)], check=False, **kwargs)


def _write_config(home: Path, config: dict) -> None:
    (home / ".remember" / "config.json").write_text(json.dumps(config), encoding="utf-8")


# ── Claude Code: byte-for-byte, unconditionally (the regression that matters
#    most -- #301 and #280 already pin this hook's Claude Code output; this
#    is the same control, restated here so this file stands on its own) ────

@WINDOWS_SKIP
def test_claude_code_payload_is_unchanged(tmp_path):
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC"})
    result = _run_upsubmit(_env_claude_code(home, project))
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    assert out.startswith("["), out
    assert out.endswith("]"), out
    assert _codex_upsubmit_status(result.stdout) == "Failed", (
        "if this assertion ever fails, the Claude Code path silently "
        "started emitting something Codex-safe -- which is a change to a "
        "byte-for-byte pinned contract, not a bug in this test"
    )


# ── Codex: must fire (context reaches the model) ────────────────────────────

@WINDOWS_SKIP
def test_codex_payload_wraps_the_stamp_and_reads_completed(tmp_path):
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC"})
    stdin = json.dumps({"session_id": "s", "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    result = _run_upsubmit(_env_codex(home, project), stdin=stdin)
    assert result.returncode == 0, result.stderr
    assert _codex_upsubmit_status(result.stdout) == "Completed", result.stdout
    wire = _parse_user_prompt_submit_wire(result.stdout.strip())
    assert wire is not None, result.stdout
    ctx = wire["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("["), ctx  # the stamp itself survives inside the envelope
    assert WHO in ctx, ctx


# ── Codex: must NOT fire when there is nothing to say (positive control for
#    the "stays silent" branch -- pairs with the test above so silence here
#    is verified to mean "correctly nothing to inject", not "the harness
#    never ran") ──────────────────────────────────────────────────────────

@WINDOWS_SKIP
def test_codex_payload_with_stamp_off_prints_nothing_and_still_completes(tmp_path):
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC", "prompt_stamp": "off"})
    stdin = json.dumps({"session_id": "s", "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    result = _run_upsubmit(_env_codex(home, project), stdin=stdin)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", repr(result.stdout)
    assert _codex_upsubmit_status(result.stdout) == "Completed"


@WINDOWS_SKIP
def test_codex_payload_without_jq_stays_silent_and_logs_the_loss(tmp_path):
    """jq is already a hard dependency of this hook's slow path (config
    reads), so its absence here is a real if rare edge -- and it must not
    regress into printing the bracketed stamp raw (that IS the #451 defect)
    just because jq could not build the envelope. Silence on stdout is the
    honest option; silence everywhere is not, so the loss is logged."""
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC"})
    env = _env_codex(home, project)
    env["JQ"] = "/nonexistent/jq"
    env["REMEMBER_ENV_CACHE"] = "0"  # force the slow path, where log() is defined
    stdin = json.dumps({"session_id": "s", "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    result = _run_upsubmit(env, stdin=stdin)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", repr(result.stdout)  # never the raw "[...]"
    assert _codex_upsubmit_status(result.stdout) == "Completed"
    log_files = list((project / ".remember" / "logs").glob("memory-*.log"))
    assert log_files, "no memory-*.log written -- the loss went unlogged"
    logged = log_files[0].read_text(encoding="utf-8")
    assert "jq unavailable" in logged, logged


@WINDOWS_SKIP
def test_codex_payload_notice_lands_in_systemmessage(tmp_path):
    home, project = _project(tmp_path)
    _write_config(home, {"timezone": "UTC"})
    (project / ".remember" / "tmp" / "capture-gap-notice").write_text(
        "your previous session was not captured\n", encoding="utf-8")
    stdin = json.dumps({"session_id": "s", "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    result = _run_upsubmit(_env_codex(home, project), stdin=stdin)
    assert result.returncode == 0, result.stderr
    assert _codex_upsubmit_status(result.stdout) == "Completed", result.stdout
    wire = _parse_user_prompt_submit_wire(result.stdout.strip())
    assert wire is not None, result.stdout
    assert wire.get("systemMessage", "").strip() == "your previous session was not captured"
    assert wire["hookSpecificOutput"]["additionalContext"].startswith("[")


# ── SessionStart (#451's hidden judgment call): confirm or refute, do not
#    assume -- driven end-to-end against real fixtures, not reasoned about ──

def _session_start_env(home: Path, remember: Path) -> dict:
    env = {**os.environ, "HOME": str(home), "REMEMBER_DIR": str(remember),
           "_LIB_MEMORY_DIR_LOADED": "1", "REMEMBER_NO_PRINTF_T": "1",
           "PLUGIN_ROOT": str(REPO_ROOT)}
    for key in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT"):
        env.pop(key, None)
    return env


def _session_start_payload(session_id: str, cwd: str) -> str:
    return json.dumps({"session_id": session_id, "cwd": cwd,
                        "hook_event_name": "SessionStart",
                        "transcript_path": f"/does/not/matter/{session_id}.jsonl"})


@WINDOWS_SKIP
def test_session_start_first_byte_is_never_bracket_or_brace(tmp_path):
    """Three shapes, all driven through the real hook: a full store (recap
    injects everything), a store whose handoff was already delivered once
    (the ONLY place in this script that ever emits a line opening with `[` --
    see the module docstring), and an empty store. In every one, the first
    stdout byte is a letter or `=`, never `{`/`[` -- REFUTING #451's
    hypothesis that SessionStart shares this defect. OBSERVED for these
    three shapes; NOT a proof for every branch of a 1400-line script."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)

    # Shape 1: empty store.
    result = subprocess.run(
        ["bash", str(SESSION_START)], env=_session_start_env(home, remember),
        input=_session_start_payload("s1", str(project)),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    first = result.stdout.lstrip()[:1]
    assert first not in ("{", "["), repr(result.stdout[:120])

    # Shape 2: a full store -- every memory file present, forces the largest
    # recap this hook produces.
    for name, body in {
        "identity.md": "IDENTITY-BODY-451", "core-memories.md": "CORE-BODY-451",
        "now.md": "NOW-BODY-451", "recent.md": "RECENT-BODY-451",
        "archive.md": "ARCHIVE-BODY-451",
    }.items():
        (remember / name).write_text(body + "\n", encoding="utf-8")
    (remember / "remember.md").write_text("HANDOFF-BODY-451\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(SESSION_START)], env=_session_start_env(home, remember),
        input=_session_start_payload("s2", str(project)),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "HANDOFF-BODY-451" in result.stdout  # positive control: the recap ran
    first = result.stdout.lstrip()[:1]
    assert first not in ("{", "["), repr(result.stdout[:120])

    # Shape 3: the handoff delivered once already -- the run that reaches
    # this script's one bracket-opening line, "[already delivered ...]".
    result = subprocess.run(
        ["bash", str(SESSION_START)], env=_session_start_env(home, remember),
        input=_session_start_payload("s3", str(project)),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "already delivered" in result.stdout, (
        "this shape did not reach the bracket-opening line at all -- the "
        "test proves nothing about it; " + repr(result.stdout[:200])
    )
    first = result.stdout.lstrip()[:1]
    assert first not in ("{", "["), repr(result.stdout[:200])
