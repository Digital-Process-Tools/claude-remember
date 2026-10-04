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


# ── haiku.drop_env: an explicit, generic opt-in (#898 round 13) ──────────────
#
# #898 round 13 (maintainer decision): the #703 strip of one named credential
# is gone. The nested `claude -p` inherits the environment as Claude Code gave
# it -- no variable is special-cased by name -- and `haiku.drop_env` is the one
# generic, opt-in way to remove variables from that child: a list of exact
# names or shell-style globs (`*`, `?`). Every "removed" case is paired with a
# "kept" one, so a function that always dropped (or never dropped) fails.


@pytest.fixture
def no_ambient_credentials(monkeypatch, tmp_path):
    """No config visible to `_child_env()` except what a test writes itself.

    A developer machine can have a real `~/.remember/config.json`, so without
    this fixture the drop list under test is the developer's own config rather
    than the case the test describes -- and it would flip between their
    machine and CI.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows' expanduser reads this
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path / "remember"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("REMEMBER_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("REMEMBER_CONFIG", raising=False)
    return home


def _write_config(home, haiku_block):
    cfg_dir = home / ".remember"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"haiku": haiku_block}), encoding="utf-8"
    )


def _child_env_for(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")
    call_haiku("p")
    return mock_run.call_args[1]["env"]


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_inherits_the_environment_by_default(
    mock_run, monkeypatch, no_ambient_credentials
):
    """No `haiku.drop_env`: every variable outside the parent-session
    markers reaches the child untouched -- an API key included. This is
    also the positive control for every drop test below."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-example")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-example")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = _child_env_for(mock_run)

    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-api03-example"
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat-example"
    assert env.get("PATH") == "/usr/bin"


@patch("pipeline.haiku.subprocess.run")
def test_drop_env_empty_list_is_the_same_as_the_default(
    mock_run, monkeypatch, no_ambient_credentials
):
    _write_config(no_ambient_credentials, {"drop_env": []})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-example")

    with patch("pipeline.haiku._warn") as mock_warn:
        env = _child_env_for(mock_run)

    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-api03-example"
    assert mock_warn.call_args_list == []


@patch("pipeline.haiku.subprocess.run")
def test_drop_env_removes_an_exact_name(
    mock_run, monkeypatch, no_ambient_credentials
):
    _write_config(no_ambient_credentials, {"drop_env": ["ANTHROPIC_API_KEY"]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-example")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("PATH", "/usr/bin")

    with patch("pipeline.haiku._warn") as mock_warn:
        env = _child_env_for(mock_run)

    assert "ANTHROPIC_API_KEY" not in env
    # Exact means exact: a sibling sharing the prefix is kept.
    assert env.get("ANTHROPIC_BASE_URL") == "https://example.invalid"
    assert env.get("PATH") == "/usr/bin"
    assert mock_warn.call_args_list == [], "a valid list warns about nothing"


@patch("pipeline.haiku.subprocess.run")
def test_drop_env_glob_removes_every_matching_name(
    mock_run, monkeypatch, no_ambient_credentials
):
    _write_config(no_ambient_credentials, {"drop_env": ["ANTHROPIC_*"]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-example")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("MY_ANTHROPIC_THING", "kept")  # does not START with it
    monkeypatch.setenv("PATH", "/usr/bin")

    env = _child_env_for(mock_run)

    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env.get("MY_ANTHROPIC_THING") == "kept"
    assert env.get("PATH") == "/usr/bin"


@patch("pipeline.haiku.subprocess.run")
def test_drop_env_glob_that_matches_nothing_removes_nothing(
    mock_run, monkeypatch, no_ambient_credentials
):
    _write_config(no_ambient_credentials, {"drop_env": ["UNRELATED_*", "X?Y"]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-example")
    monkeypatch.setenv("XAY", "dropped-by-question-mark")
    monkeypatch.setenv("XAAY", "kept")

    env = _child_env_for(mock_run)

    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-api03-example"
    assert "XAY" not in env, "`?` matches exactly one character"
    assert env.get("XAAY") == "kept"


@patch("pipeline.haiku.subprocess.run")
def test_drop_env_can_remove_the_kept_oauth_token_too(
    mock_run, monkeypatch, no_ambient_credentials
):
    """The operator's explicit list wins over the one built-in keep."""
    _write_config(no_ambient_credentials, {"drop_env": ["CLAUDE_CODE_OAUTH_TOKEN"]})
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-example")

    env = _child_env_for(mock_run)

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@patch("pipeline.haiku.subprocess.run")
def test_drop_env_invalid_entries_are_ignored_with_a_warning(
    mock_run, monkeypatch, no_ambient_credentials
):
    """Anything that is not a name or a `*`/`?` glob over name characters is
    skipped, said so in the log, and never echoed -- someone may paste
    `NAME=value` with a real value in it. The valid entry still applies."""
    _write_config(no_ambient_credentials, {"drop_env": [
        "ANTHROPIC_API_KEY=sk-ant-api03-pasted-here",
        "",
        "BAD-NAME",
        "[A]*",
        "9LEADING_DIGIT",
        7,
        "VALID_ONE",
    ]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-example")
    monkeypatch.setenv("VALID_ONE", "x")
    monkeypatch.setenv("AB", "kept")

    with patch("pipeline.haiku._warn") as mock_warn:
        env = _child_env_for(mock_run)

    assert "VALID_ONE" not in env, "the valid entry beside the bad ones applies"
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-api03-example"
    assert env.get("AB") == "kept"
    warnings = " ".join(str(c.args[0]) for c in mock_warn.call_args_list)
    assert "haiku.drop_env" in warnings
    assert "sk-ant-api03-pasted-here" not in warnings
    assert "BAD-NAME" not in warnings, "entries are described, never echoed"


@patch("pipeline.haiku.subprocess.run")
def test_drop_env_that_is_not_a_list_is_ignored_with_a_warning(
    mock_run, monkeypatch, no_ambient_credentials
):
    _write_config(no_ambient_credentials, {"drop_env": "ANTHROPIC_API_KEY"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-example")

    with patch("pipeline.haiku._warn") as mock_warn:
        env = _child_env_for(mock_run)

    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-api03-example"
    warnings = " ".join(str(c.args[0]) for c in mock_warn.call_args_list)
    assert "haiku.drop_env" in warnings


@patch("pipeline.haiku.subprocess.run")
def test_credential_failure_points_at_drop_env(
    mock_run, monkeypatch, no_ambient_credentials
):
    """The discoverability half of #703, kept without naming any variable:
    a failure that looks like a credential failure says the knob exists."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout=json.dumps({"error": "Credit balance is too low"}),
        stderr="",
    )

    with pytest.raises(RuntimeError) as raised:
        call_haiku("p")

    message = str(raised.value)
    assert "Credit balance is too low" in message
    assert "haiku.drop_env" in message
    assert "ANTHROPIC_API_KEY" not in message


@patch("pipeline.haiku.subprocess.run")
def test_failure_unrelated_to_credentials_gets_no_drop_env_hint(
    mock_run, monkeypatch, no_ambient_credentials
):
    """Positive control's twin: a hint that fired on every failure would
    point unrelated outages at the environment."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout=json.dumps({"error": "Prompt is too long"}),
        stderr="",
    )

    with pytest.raises(RuntimeError) as raised:
        call_haiku("p")

    message = str(raised.value)
    assert "Prompt is too long" in message
    assert "haiku.drop_env" not in message


def test_haiku_py_no_longer_names_the_credential_or_its_old_key():
    """#898 round 13: the shipped module must not name the credential at all
    (the directory portal holds on the name alone), nor the removed
    `haiku.anthropic_api_key` policy key."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline", "haiku.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    assert "def _child_env" in source, "positive control: this is the right file"
    for needle in ("ANTHROPIC_API_KEY", "anthropic_api_key"):
        assert needle not in source, needle


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
        assert "something broke" in str(e)


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_failure_reports_json_error_from_stdout(mock_run):
    """--output-format json puts the failure on STDOUT and leaves stderr empty.

    Reading stderr alone produced 'claude exited 1:' with nothing after the
    colon, which hid a 7-week auth outage from the reporter of #129.
    """
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout='{"type":"result","is_error":true,'
               '"result":"Invalid API key · Please run /login"}',
        stderr="",
    )
    try:
        call_haiku("test")
        assert False, "should raise"
    except RuntimeError as e:
        assert "Invalid API key" in str(e), (
            f"the real error must survive into the message, got: {e}"
        )


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_failure_with_no_output_says_so(mock_run):
    """Empty on both streams must read as a statement, not a truncated sentence."""
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
    try:
        call_haiku("test")
        assert False, "should raise"
    except RuntimeError as e:
        assert "no output" in str(e)


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_failure_falls_back_to_raw_stdout(mock_run):
    """Non-JSON stdout is still better than nothing."""
    mock_run.return_value = MagicMock(
        returncode=1, stdout="plain text explosion", stderr="")
    try:
        call_haiku("test")
        assert False, "should raise"
    except RuntimeError as e:
        assert "plain text explosion" in str(e)


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_failure_detail_is_capped(mock_run):
    """A huge payload must not be dumped into the log on every failure."""
    mock_run.return_value = MagicMock(
        returncode=1, stdout="x" * 5000, stderr="")
    try:
        call_haiku("test")
        assert False, "should raise"
    except RuntimeError as e:
        assert len(str(e)) < 700, "failure detail should be truncated"


@patch("pipeline.haiku.subprocess.run")
def test_call_haiku_keeps_oauth_token(mock_run, monkeypatch):
    """CLAUDE_CODE_OAUTH_TOKEN shares the parent-session prefix but is the
    child's credentials — stripping it leaves `claude -p` unauthenticated, so
    nothing ever saves for setup-token / hosted Agent SDK users (#131)."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-example")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")

    call_haiku("p")

    env = mock_run.call_args[1]["env"]
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat-example", (
        "the credential must survive the parent-session strip"
    )
    assert "CLAUDE_CODE_SESSION_ID" not in env, (
        "the rest of the CLAUDE_CODE_* family must still be stripped"
    )


def _log_text(remember_dir) -> str:
    """Everything written to the daily log under a REMEMBER_DIR, or ""."""
    log_dir = remember_dir / "logs"
    if not log_dir.is_dir():
        return ""
    return "".join(p.read_text(encoding="utf-8") for p in sorted(log_dir.iterdir()))


# ── This plugin reads no recovery-token credential at all (#860, round 3) ────
#
# plugin.json no longer declares a userConfig recovery-token option -- round
# 2's own CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN env var is never read either, by
# design, now that the option that populated it is gone. A `claude -p` call
# now runs with whatever authentication it inherits from its own
# environment or none.
#
# #898, round 4: the one remaining presence check for REMEMBER_OAUTH_TOKEN
# and haiku.oauth_token (this plugin's own earlier, already-removed attempts
# at the same feature) is ALSO gone now -- the directory's scanner read a
# value-free presence check's own existence as the read half of the
# plugin.json-aggregate credential pairing, independent of what the check
# ever logged. Neither setting produces a NOTICE line, or any other log
# output, any more; they are simply never read, for any purpose.


@patch("pipeline.haiku.subprocess.run")
def test_userconfig_oauth_token_env_is_never_read(mock_run, monkeypatch, tmp_path):
    """#860, round 3: the removed userConfig option's own env var is simply
    never consulted any more -- not malformed-rejected, not read at all. A
    value left behind in a stale environment (e.g. a host that still
    populates it from an old plugin.json cache) must never reach the child."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("REMEMBER_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN", "sk-ant-oat-userconfig-00001")
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path))
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")

    call_haiku("p")

    env = mock_run.call_args[1]["env"]
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") is None, (
        "the removed userConfig option's env var must never populate "
        "CLAUDE_CODE_OAUTH_TOKEN any more"
    )


@patch("pipeline.haiku.subprocess.run")
def test_host_token_still_passes_through_unconditionally(mock_run, monkeypatch, tmp_path):
    """Positive control for the test above: a host-provided
    CLAUDE_CODE_OAUTH_TOKEN must still reach the child -- this plugin removing
    its OWN credential read must not touch the host's (#860, round 3)."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-from-host-00000001")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN", "sk-ant-oat-userconfig-00003")
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path))
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")

    call_haiku("p")

    env = mock_run.call_args[1]["env"]
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat-from-host-00000001"


@patch("pipeline.haiku.subprocess.run")
def test_legacy_env_var_configured_logs_no_notice_at_all(mock_run, monkeypatch, tmp_path):
    """#898, round 4: a still-set legacy REMEMBER_OAUTH_TOKEN no longer gets
    a notice of any kind -- the presence check that used to report it is
    removed entirely, because the directory's scanner read the check's own
    existence as a credential read regardless of what it logged. Must-fire
    control for the detection mechanism itself is the sibling test right
    below (a NOTICE line written directly IS seen by _log_text), so this
    absence is not merely a broken reader passing vacuously."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    configured_value = "a-short-but-distinctive-secret-marker"
    monkeypatch.setenv("REMEMBER_OAUTH_TOKEN", configured_value)
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path))
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")

    call_haiku("p")

    logged = _log_text(tmp_path)
    assert configured_value not in logged, (
        "the configured value itself must never reach the log:\n" + logged
    )
    assert "NOTICE" not in logged, (
        "the legacy-setting presence check is removed entirely -- a still-"
        "configured REMEMBER_OAUTH_TOKEN must produce no notice at all:\n"
        + logged
    )


@patch("pipeline.haiku.subprocess.run")
def test_legacy_config_key_configured_logs_no_notice_at_all(mock_run, monkeypatch, tmp_path):
    """Same absence for the config.json `haiku.oauth_token` key (#898, round
    4) -- paired with the env-var case above so both removed sources are
    covered, not just one."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("REMEMBER_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path))
    monkeypatch.setattr("pipeline.haiku.os.path.expanduser", lambda p: str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"haiku": {"oauth_token": "sk-ant-oat-legacy-config-00009"}}),
        encoding="utf-8")
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")

    call_haiku("p")

    assert "NOTICE" not in _log_text(tmp_path)


def test_log_text_helper_sees_a_notice_when_one_is_actually_written(tmp_path):
    """Positive control for the two tests above's own detection mechanism:
    _log_text() must actually surface a NOTICE line when one is written to
    a daily log, so the preceding "no NOTICE" assertions are not passing
    because the reader itself is broken or pointed at the wrong directory.
    Writes directly under logs/, matching _log_text's own flat iterdir()."""
    remember_dir = tmp_path / "remember"
    log_dir = remember_dir / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "today-sentinel.log").write_text(
        "NOTICE: sentinel line written directly by the test\n", encoding="utf-8")
    assert "NOTICE" in _log_text(remember_dir)


@patch("pipeline.haiku.subprocess.run")
def test_remember_oauth_token_env_no_longer_authenticates(mock_run, monkeypatch, tmp_path):
    """Negative assertion, with a positive control: REMEMBER_OAUTH_TOKEN no
    longer authenticates the nested CLI at all -- it is not even read for
    validity -- while the userConfig path (same fixture shape, previous
    test) still does. Without that twin, this test would pass just as well
    against code that silently did nothing for every credential (#860)."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("REMEMBER_OAUTH_TOKEN", "sk-ant-oat-legacy-env-0000003")
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path))
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")

    call_haiku("p")

    env = mock_run.call_args[1]["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env, (
        "REMEMBER_OAUTH_TOKEN must not reach the child env -- it is no "
        "longer read at all (#860, round 2)"
    )


@patch("pipeline.haiku.subprocess.run")
def test_haiku_oauth_token_config_no_longer_authenticates(mock_run, monkeypatch, tmp_path):
    """Same negative assertion for the `haiku.oauth_token` config.json key
    (#860, round 2)."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("REMEMBER_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path))
    monkeypatch.setattr("pipeline.haiku.os.path.expanduser", lambda p: str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"haiku": {"oauth_token": "sk-ant-oat-legacy-config-00004"}}),
        encoding="utf-8")
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("x"), stderr="")

    call_haiku("p")

    env = mock_run.call_args[1]["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@patch("pipeline.haiku.subprocess.run")
def test_a_failed_call_reports_what_it_spent(mock_run, monkeypatch, tmp_path):
    """#190: a non-zero exit still cost money if the payload says so."""
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path))
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout=json.dumps({
            "error": {"message": "overloaded"},
            "usage": {"input_tokens": 8123, "output_tokens": 44,
                      "cache_read_input_tokens": 900},
        }),
        stderr="",
    )

    with pytest.raises(RuntimeError):
        call_haiku("p")

    logged = _log_text(tmp_path)
    assert "8123" in logged and "44" in logged, (
        f"a failed call spent 8123 input tokens and left no record:\n{logged}"
    )


@patch("pipeline.haiku.subprocess.run")
def test_a_failure_with_no_usage_says_unknown_not_zero(mock_run, monkeypatch, tmp_path):
    """Zero would read as "it failed for free", which is the invisibility this
    closes. Unknown is the honest answer."""
    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path))
    mock_run.return_value = MagicMock(
        returncode=1, stdout='{"error": {"message": "boom"}}', stderr="")

    with pytest.raises(RuntimeError):
        call_haiku("p")

    logged = _log_text(tmp_path)
    assert "unknown" in logged, f"no honest account of the failed call:\n{logged}"


@patch("pipeline.haiku.subprocess.run")
def test_a_timeout_is_accounted_for_too(mock_run, monkeypatch, tmp_path):
    """A client-side timeout aborts a call the API has already been billing."""
    from subprocess import TimeoutExpired

    monkeypatch.setenv("REMEMBER_DIR", str(tmp_path))
    mock_run.side_effect = TimeoutExpired("claude", 180)

    with pytest.raises(RuntimeError):
        call_haiku("p", timeout=180)

    logged = _log_text(tmp_path)
    assert "timed out" in logged and "unknown" in logged, (
        f"a 180s timeout left no cost record at all:\n{logged}"
    )


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
