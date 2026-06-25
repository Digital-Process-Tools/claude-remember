"""Tests for Haiku CLI wrapper (mocked — no real claude calls)."""

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import call_haiku, _parse_response, _extract_tokens


def _mock_claude_response(result_text: str, input_tokens: int = 500,
                          output_tokens: int = 100, cache: int = 200) -> str:
    return json.dumps({
        "result": result_text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache,
    })


def test_parse_response_basic():
    raw = _mock_claude_response("## 10:30 | did stuff\ndetails")
    result = _parse_response(raw)
    assert result.text == "## 10:30 | did stuff\ndetails"
    assert result.is_skip is False
    assert result.tokens.input == 500
    assert result.tokens.output == 100
    assert result.tokens.cache == 200


def test_parse_response_skip():
    raw = _mock_claude_response("SKIP — duplicate of previous entry")
    result = _parse_response(raw)
    assert result.is_skip is True
    assert "duplicate" in result.text


def test_parse_response_invalid_json():
    try:
        _parse_response("not json at all")
        assert False, "should raise"
    except RuntimeError as e:
        assert "invalid JSON" in str(e)


def test_extract_tokens_cost():
    data = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 400,
    }
    t = _extract_tokens(data)
    assert t.input == 1000
    assert t.output == 200
    assert t.cache == 400
    # cost = (1000-400)*0.80/1M + 200*4.00/1M + 400*0.08/1M
    expected = 600 * 0.80e-6 + 200 * 4.00e-6 + 400 * 0.08e-6
    assert abs(t.cost_usd - expected) < 1e-10


def test_extract_tokens_no_cache():
    data = {"input_tokens": 1000, "output_tokens": 200}
    t = _extract_tokens(data)
    assert t.cache == 0
    expected = 1000 * 0.80e-6 + 200 * 4.00e-6
    assert abs(t.cost_usd - expected) < 1e-10


def test_extract_tokens_nested_usage():
    """Real claude CLI output: tokens under usage, cost at top level."""
    data = {
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 18389,
            "output_tokens": 1008,
        },
        "total_cost_usd": 0.0101689,
    }
    t = _extract_tokens(data)
    assert t.input == 10
    assert t.output == 1008
    assert t.cache == 18389
    assert abs(t.cost_usd - 0.0101689) < 1e-10


def test_extract_tokens_flat_still_works():
    """Legacy flat layout still works (backwards compat)."""
    data = {"input_tokens": 500, "output_tokens": 100, "cache_read_input_tokens": 200}
    t = _extract_tokens(data)
    assert t.input == 500
    assert t.output == 100
    assert t.cache == 200


def test_parse_response_raw_conversation_echo():
    """_parse_response accepts raw conversation echo — format validation is the shell's job."""
    raw = _mock_claude_response("[HUMAN] hello\n[ASSISTANT] hi there")
    result = _parse_response(raw)
    assert result.text == "[HUMAN] hello\n[ASSISTANT] hi there"
    assert result.is_skip is False


def test_parse_response_headerless_summary():
    """_parse_response accepts summary without ## header — format validation is the shell's job."""
    raw = _mock_claude_response("Fixed authentication bug in login flow and deployed to staging")
    result = _parse_response(raw)
    assert result.text == "Fixed authentication bug in login flow and deployed to staging"
    assert result.is_skip is False


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_success(mock_run, monkeypatch):
    monkeypatch.delenv("REMEMBER_MODEL", raising=False)  # assert the default model
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=_mock_claude_response("hello from haiku"),
        stderr="",
    )
    result = call_haiku("test prompt")
    assert result.text == "hello from haiku"
    assert result.is_skip is False

    args = mock_run.call_args
    cmd = args[0][0]
    assert os.path.basename(cmd[0]).startswith("claude")
    assert "--model" in cmd
    assert "haiku" in cmd
    # One-shot summarization subprocess: never resume these, never write to disk
    assert "--no-session-persistence" in cmd
    assert "--exclude-dynamic-system-prompt-sections" in cmd
    # Sandboxed MCP: no servers, strict (so the nested session can't inherit any) — #94
    assert "--mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in cmd
    # CLAUDECODE must be stripped from env
    env = args[1]["env"]
    assert "CLAUDECODE" not in env


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_sends_prompt_on_stdin_not_argv(mock_run):
    """The prompt is delivered on STDIN, never as an argv string.

    A session extract can exceed Linux's MAX_ARG_STRLEN (131072 bytes / 128KB
    per single argument); the old ``claude -p <prompt>`` form fails at exec()
    with OSError E2BIG ("Argument list too long"), silently losing the save.
    Guard both halves so the regression can't silently return: the prompt must
    arrive via ``input=`` and must NOT appear in the command argv (``-p`` is a
    bare flag, immediately followed by the next option)."""
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("ok"), stderr="")
    call_haiku("the full prompt text")
    args = mock_run.call_args
    cmd = args[0][0]
    assert args[1]["input"] == "the full prompt text"
    assert "the full prompt text" not in cmd
    assert cmd[cmd.index("-p") + 1] == "--output-format"


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_strips_parent_session_env(mock_run, monkeypatch):
    """The nested claude -p must not inherit the PARENT Claude Code session
    vars — else it looks like a resumable session to anything keying off them
    (#95). Strip CLAUDECODE, CLAUDE_JOB_DIR, and all CLAUDE_CODE_*; keep the
    rest of the environment intact."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_JOB_DIR", "/some/job/dir")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("PATH", "/usr/bin")  # an unrelated var must survive
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")
    call_haiku("p")
    env = mock_run.call_args[1]["env"]
    assert "CLAUDECODE" not in env
    assert "CLAUDE_JOB_DIR" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env.get("PATH") == "/usr/bin"


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_with_tools(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=_mock_claude_response("done"),
        stderr="",
    )
    call_haiku("prompt", tools=["Read", "Write"])
    cmd = mock_run.call_args[0][0]
    assert "--allowedTools" in cmd
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == "Read,Write"


def _max_turns_in(cmd: list[str]) -> str:
    return cmd[cmd.index("--max-turns") + 1]


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_default_max_turns_clears_cc2x(mock_run, monkeypatch):
    """Default must be >=2 — CC 2.x counts prompt-delivery as turn 1, so a cap
    of 1 exits error_max_turns before the model replies (#98/#100). This is the
    consolidation path (consolidate.py -> call_haiku), not just save-session.sh."""
    monkeypatch.delenv("REMEMBER_MAX_TURNS", raising=False)
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")
    call_haiku("p")
    assert _max_turns_in(mock_run.call_args[0][0]) == "4"


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_max_turns_env_override(mock_run, monkeypatch):
    monkeypatch.setenv("REMEMBER_MAX_TURNS", "6")
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")
    call_haiku("p")
    assert _max_turns_in(mock_run.call_args[0][0]) == "6"


@pytest.mark.parametrize("bad", ["0", "-1", "banana", "", "3.5", "21", "999999"])
@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_invalid_max_turns_falls_back(mock_run, bad, monkeypatch):
    """A bad/out-of-range REMEMBER_MAX_TURNS must not flow through as a garbage
    --max-turns value (which would break claude -p the same way the original
    bug did). Includes the upper-bound cap so a misconfig is bounded."""
    monkeypatch.setenv("REMEMBER_MAX_TURNS", bad)
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")
    call_haiku("p")
    assert _max_turns_in(mock_run.call_args[0][0]) == "4"


@pytest.mark.parametrize("raw,expected", [("2", "2"), ("20", "20"), ("007", "7")])
@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_valid_max_turns_normalized(mock_run, raw, expected, monkeypatch):
    """In-range values pass through, normalized (leading zeros stripped)."""
    monkeypatch.setenv("REMEMBER_MAX_TURNS", raw)
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")
    call_haiku("p")
    assert _max_turns_in(mock_run.call_args[0][0]) == expected


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_nonzero_exit(mock_run):
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="something broke",
    )
    try:
        call_haiku("test")
        assert False, "should raise"
    except RuntimeError as e:
        assert "exited 1" in str(e)


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_timeout(mock_run):
    from subprocess import TimeoutExpired
    mock_run.side_effect = TimeoutExpired("claude", 120)
    try:
        call_haiku("test", timeout=120)
        assert False, "should raise"
    except RuntimeError as e:
        assert "timed out" in str(e)


def test_parse_response_missing_result_key():
    """Missing 'result' key should return empty text, not crash."""
    raw = json.dumps({"input_tokens": 10, "output_tokens": 5})
    result = _parse_response(raw)
    assert result.text == ""
    assert result.is_skip is False


def test_parse_response_null_result():
    """Null result value should return empty string."""
    raw = json.dumps({"result": None, "input_tokens": 10, "output_tokens": 5})
    result = _parse_response(raw)
    assert result.text == ""
    assert result.is_skip is False


def test_extract_tokens_empty_dict():
    """Empty dict should return all zeros."""
    t = _extract_tokens({})
    assert t.input == 0
    assert t.output == 0
    assert t.cache == 0
    assert t.cost_usd == 0.0


def test_extract_tokens_nested_wins_over_flat():
    """When both flat and nested keys are present, nested (usage) should win."""
    data = {
        "input_tokens": 999,
        "output_tokens": 999,
        "cache_read_input_tokens": 999,
        "usage": {
            "input_tokens": 42,
            "output_tokens": 7,
            "cache_read_input_tokens": 3,
        },
    }
    t = _extract_tokens(data)
    assert t.input == 42
    assert t.output == 7
    assert t.cache == 3


# --- REMEMBER_MODEL env knob (mirrors REMEMBER_MAX_TURNS) --------------------
from pipeline.haiku import _resolve_model


def test_resolve_model_default(monkeypatch):
    monkeypatch.delenv("REMEMBER_MODEL", raising=False)
    assert _resolve_model() == "haiku"


def test_resolve_model_env_override(monkeypatch):
    monkeypatch.setenv("REMEMBER_MODEL", "sonnet")
    assert _resolve_model() == "sonnet"


def test_resolve_model_blank_falls_back(monkeypatch):
    monkeypatch.setenv("REMEMBER_MODEL", "   ")
    assert _resolve_model() == "haiku"


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_uses_resolved_model(mock_run, monkeypatch):
    monkeypatch.setenv("REMEMBER_MODEL", "sonnet")
    mock_run.return_value = MagicMock(returncode=0, stdout=_mock_claude_response("x"), stderr="")
    call_haiku("p")
    cmd = mock_run.call_args[0][0]
    assert cmd[cmd.index("--model") + 1] == "sonnet"


# --- reject-gate: refusals/clarifications never reach memory -----------------
@pytest.mark.parametrize("refusal", [
    "I cannot and will not invent timestamps or fabricate session details. Do you want to:",
    "I can't summarize without the actual session text.",
    "Could you paste the session text you want summarized?",
    "Please provide the conversation to summarize.",
    "I'm sorry, there is no content to summarize.",
])
def test_parse_response_rejects_refusal(refusal):
    """A model refusal/clarification must be treated as skip so it is never
    written to the memory layer (it was, historically: a refusal stored
    verbatim as a memory entry)."""
    result = _parse_response(_mock_claude_response(refusal))
    assert result.is_skip is True


@pytest.mark.parametrize("good", [
    "## 10:30 | main\nFixed auth bug; deployed staging.",
    "Fixed authentication bug in login flow and deployed to staging",
    "[HUMAN] hello\n[ASSISTANT] hi there",
    "===RECENT===\n# Recent\n## 2026-06-22 did things",
])
def test_parse_response_keeps_real_summaries(good):
    """The reject-gate is anchored at the start and must not drop legitimate
    summaries, including the headerless / raw-echo cases _parse_response is
    deliberately permissive about (format validation stays the shell's job)."""
    result = _parse_response(_mock_claude_response(good))
    assert result.is_skip is False


# --- reject-gate: narrow default must NOT eat legit hedged summaries ----------
@pytest.mark.parametrize("good", [
    "There are no blockers; merged !24648 and the pipeline is green.",
    "Unfortunately the build broke on flaky DNS; retried and it is green now.",
    "It seems the cache was stale — cleared it and the page renders.",
    "I notice the staging DB drifted from prod; resynced via the script.",
    "Sorry state machine had a missing transition; added PENDING->DONE.",
])
def test_parse_response_keeps_hedged_summaries(good):
    """Regression guard for the over-broad pattern: legitimate summaries that
    happen to open with a hedge word ("Unfortunately", "There are no",
    "It seems", "I notice", "Sorry ...") must be preserved, not silently
    dropped from the memory layer."""
    result = _parse_response(_mock_claude_response(good))
    assert result.is_skip is False


# --- REMEMBER_REJECT_PATTERN env knob (mirrors REMEMBER_MODEL) ----------------
from pipeline.haiku import _resolve_reject_pattern, DEFAULT_REJECT_PATTERN


def test_resolve_reject_pattern_default(monkeypatch):
    monkeypatch.delenv("REMEMBER_REJECT_PATTERN", raising=False)
    assert _resolve_reject_pattern().pattern == DEFAULT_REJECT_PATTERN


def test_resolve_reject_pattern_blank_falls_back(monkeypatch):
    monkeypatch.setenv("REMEMBER_REJECT_PATTERN", "   ")
    assert _resolve_reject_pattern().pattern == DEFAULT_REJECT_PATTERN


def test_resolve_reject_pattern_none_disables(monkeypatch):
    monkeypatch.setenv("REMEMBER_REJECT_PATTERN", "none")
    assert _resolve_reject_pattern() is None


def test_resolve_reject_pattern_custom(monkeypatch):
    monkeypatch.setenv("REMEMBER_REJECT_PATTERN", r"^banana")
    assert _resolve_reject_pattern().pattern == r"^banana"


def test_resolve_reject_pattern_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("REMEMBER_REJECT_PATTERN", r"(unclosed")
    assert _resolve_reject_pattern().pattern == DEFAULT_REJECT_PATTERN


def test_reject_gate_disabled_keeps_refusal(monkeypatch):
    """With the gate disabled, only the literal SKIP contract applies — a
    refusal is no longer rejected by the pattern."""
    monkeypatch.setenv("REMEMBER_REJECT_PATTERN", "none")
    result = _parse_response(_mock_claude_response("I cannot do that."))
    assert result.is_skip is False


def test_reject_gate_custom_pattern_applies(monkeypatch):
    monkeypatch.setenv("REMEMBER_REJECT_PATTERN", r"^banana")
    assert _parse_response(_mock_claude_response("banana split")).is_skip is True
    assert _parse_response(_mock_claude_response("I cannot do that.")).is_skip is False


# --- REMEMBER_CLAUDE_BIN: resolve the claude.cmd shim on Windows (#120) -------
from pipeline.haiku import _resolve_claude_bin


def test_resolve_claude_bin_uses_which(monkeypatch):
    """Default resolves the full path via shutil.which (queried for "claude")."""
    monkeypatch.delenv("REMEMBER_CLAUDE_BIN", raising=False)
    with patch("pipeline.haiku.shutil.which", return_value="/usr/local/bin/claude") as w:
        assert _resolve_claude_bin() == "/usr/local/bin/claude"
        w.assert_called_once_with("claude")


def test_resolve_claude_bin_windows_cmd_shim(monkeypatch):
    """shutil.which honours PATHEXT and returns the full claude.cmd path, which
    subprocess CAN launch — a bare "claude" cannot (CreateProcess only resolves
    .exe from a bare name), which is what kills every auto-save on Windows (#120)."""
    monkeypatch.delenv("REMEMBER_CLAUDE_BIN", raising=False)
    shim = r"C:\Users\x\AppData\Roaming\npm\claude.cmd"
    with patch("pipeline.haiku.shutil.which", return_value=shim):
        assert _resolve_claude_bin() == shim


def test_resolve_claude_bin_env_override(monkeypatch):
    """REMEMBER_CLAUDE_BIN wins over which (mirrors REMEMBER_MODEL / _MAX_TURNS)."""
    monkeypatch.setenv("REMEMBER_CLAUDE_BIN", "/opt/claude/bin/claude")
    with patch("pipeline.haiku.shutil.which", return_value="/usr/local/bin/claude"):
        assert _resolve_claude_bin() == "/opt/claude/bin/claude"


def test_resolve_claude_bin_blank_override_falls_back(monkeypatch):
    monkeypatch.setenv("REMEMBER_CLAUDE_BIN", "   ")
    with patch("pipeline.haiku.shutil.which", return_value="/usr/local/bin/claude"):
        assert _resolve_claude_bin() == "/usr/local/bin/claude"


def test_resolve_claude_bin_not_on_path_falls_back(monkeypatch):
    """which finds nothing → fall back to the bare name, preserving the prior
    behaviour on a misconfigured PATH instead of returning None / crashing."""
    monkeypatch.delenv("REMEMBER_CLAUDE_BIN", raising=False)
    with patch("pipeline.haiku.shutil.which", return_value=None):
        assert _resolve_claude_bin() == "claude"


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_uses_resolved_bin(mock_run, monkeypatch):
    """cmd[0] must be the RESOLVED binary path, not the bare "claude" — else
    Windows' CreateProcess raises WinError 2 on the claude.cmd shim (#120)."""
    monkeypatch.delenv("REMEMBER_CLAUDE_BIN", raising=False)
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")
    with patch("pipeline.haiku.shutil.which", return_value="/usr/local/bin/claude"):
        call_haiku("p")
    assert mock_run.call_args[0][0][0] == "/usr/local/bin/claude"
