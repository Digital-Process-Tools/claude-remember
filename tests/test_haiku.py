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
def test_call_haiku_success(mock_run):
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
    assert "claude" in cmd
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


@pytest.mark.parametrize("bad", ["0", "-1", "banana", "", "3.5"])
@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_invalid_max_turns_falls_back(mock_run, bad, monkeypatch):
    """A bad REMEMBER_MAX_TURNS must not flow through as a garbage --max-turns
    value (which would break claude -p the same way the original bug did)."""
    monkeypatch.setenv("REMEMBER_MAX_TURNS", bad)
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")
    call_haiku("p")
    assert _max_turns_in(mock_run.call_args[0][0]) == "4"


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
