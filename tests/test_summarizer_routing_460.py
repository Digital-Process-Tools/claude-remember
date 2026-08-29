"""Host-native summarizer routing (#460).

A Codex session summarized by shelling `claude -p` needs an authenticated
Claude CLI and bills Anthropic for remembering an OpenAI session, having
asked for neither. These tests pin the three states this issue asks for:

    * a Claude Code session (or any non-Codex host) still routes to
      `claude -p`, exactly as before this issue -- the regression nobody
      would notice until it shipped;
    * a Codex session routes to `codex exec` instead;
    * when the codex route cannot produce a result, the call fails LOUDLY
      by default (never a quiet claude -p fallback reproducing #460 one
      layer down), and only falls back when the operator opted in via
      REMEMBER_SUMMARIZER_FALLBACK=claude.

Every "must not fire" case (codex NOT spawned / claude NOT spawned) is
paired with a "must fire" case in the same fixture shape, so a broken mock
that spawns nothing cannot pass as "the other provider was correctly
avoided" (see CLAUDE.md: a negative assertion needs a positive control).
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import call_haiku
from pipeline.spawn_guard import SummarizerSpawnDeclined

# #465: "auto" no longer asks the environment for a Codex signature -- it
# reads the transcript the host wrote (REMEMBER_TRANSCRIPT_PATH,
# pipeline.extract.sniff_file_envelope()). A "Codex host" is therefore
# simulated here by pointing REMEMBER_TRANSCRIPT_PATH at a real Codex
# rollout fixture, not by setting CODEX_SESSION_ID -- setting only the env
# var is now exactly the #465 regression this file's own
# test_codex_signature_env_vars_alone_no_longer_select_codex (in
# tests/test_codex_hook_transcript_465.py) pins as "must not fire".
_CODEX_TRANSCRIPT = str(Path(__file__).parent / "fixtures" / "codex-rollout.jsonl")


def _mock_claude_stdout(text: str) -> str:
    import json
    return json.dumps({
        "result": text,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
    })


def _clear_host_signals(monkeypatch):
    for var in ("CODEX_HOME", "PLUGIN_ROOT", "CODEX_SESSION_ID",
                "CODEX_THREAD_ID", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_ROOT",
                "REMEMBER_SUMMARIZER", "REMEMBER_SUMMARIZER_FALLBACK",
                "REMEMBER_TRANSCRIPT_PATH"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_summarizer_routing_env(monkeypatch):
    """Every test in this file starts from a known host + provider state.

    Ambient CODEX_SESSION_ID/CODEX_HOME/CLAUDE_CODE_* ever leaking from the
    environment that launched pytest must not silently reroute an existing
    (pre-#460) test's expectations -- see the module docstring's "must not
    fire" pairing.
    """
    _clear_host_signals(monkeypatch)


def _write_codex_output(cmd, **kwargs):
    """side_effect for the mocked subprocess.run of a codex exec call.

    Writes to the `-o <file>` argument, mirroring what codex exec itself
    does, so `_call_codex`'s read-back of that file has real content.
    """
    out_path = cmd[cmd.index("-o") + 1]
    text = _write_codex_output.next_text
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return MagicMock(returncode=0, stdout="", stderr="")


@patch("pipeline.haiku.subprocess.run")
def test_claude_code_host_still_uses_claude(mock_run, monkeypatch):
    """Regression control: a Claude Code (non-Codex) session is untouched."""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_stdout("did a thing"), stderr="")
    result = call_haiku("test prompt")
    assert result.text == "did a thing"
    cmd = mock_run.call_args[0][0]
    assert os.path.basename(cmd[0]).startswith("claude")
    assert "-p" in cmd


@patch("pipeline.haiku.subprocess.run")
def test_unknown_host_still_uses_claude(mock_run):
    """No signature at all (e.g. a bare `python3 -m pipeline.shell` run,
    or a host this module has never heard of) is not Codex -- it gets the
    historical default, same as every host before this issue existed."""
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_stdout("did a thing"), stderr="")
    result = call_haiku("test prompt")
    assert result.text == "did a thing"
    cmd = mock_run.call_args[0][0]
    assert os.path.basename(cmd[0]).startswith("claude")


@patch("pipeline.haiku.subprocess.run")
def test_codex_host_routes_to_codex_exec(mock_run, monkeypatch):
    """A detected Codex host uses `codex exec`, not `claude -p` -- the
    positive control paired with every "claude was NOT called" assertion
    below. Simulated by REMEMBER_TRANSCRIPT_PATH (#465), not
    CODEX_SESSION_ID -- see the module-level note on why."""
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", _CODEX_TRANSCRIPT)
    _write_codex_output.next_text = "## did a codex thing"
    mock_run.side_effect = _write_codex_output

    result = call_haiku("test prompt")

    assert result.text == "## did a codex thing"
    assert result.is_skip is False
    cmd = mock_run.call_args[0][0]
    assert os.path.basename(cmd[0]) == "codex"
    assert "exec" in cmd
    assert "--ignore-user-config" in cmd  # hook isolation, the #202 equivalent
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    # the "must not fire" half: claude was never spawned for this call
    assert not any("claude" in os.path.basename(str(c)) for c in cmd)


@patch("pipeline.haiku.subprocess.run")
def test_explicit_claude_override_wins_under_codex_host(mock_run, monkeypatch):
    """A Codex user who explicitly wants Claude gets it -- possible, just
    not unconditional (the issue's own framing)."""
    monkeypatch.setenv("CODEX_SESSION_ID", "01a04d64-fake-codex-session")
    monkeypatch.setenv("REMEMBER_SUMMARIZER", "claude")
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_stdout("via claude"), stderr="")
    result = call_haiku("test prompt")
    assert result.text == "via claude"
    cmd = mock_run.call_args[0][0]
    assert os.path.basename(cmd[0]).startswith("claude")


@patch("pipeline.haiku.subprocess.run")
def test_explicit_codex_override_wins_under_claude_code_host(mock_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("REMEMBER_SUMMARIZER", "codex")
    _write_codex_output.next_text = "via codex"
    mock_run.side_effect = _write_codex_output
    result = call_haiku("test prompt")
    assert result.text == "via codex"
    cmd = mock_run.call_args[0][0]
    assert os.path.basename(cmd[0]) == "codex"


@patch("pipeline.haiku.subprocess.run")
def test_codex_unavailable_fails_loudly_with_no_fallback_configured(mock_run, monkeypatch):
    """The route being unavailable must never silently reproduce #460 one
    layer down by falling back to claude -p on its own. No fallback opted
    into -> loud RuntimeError, and claude is never spawned."""
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", _CODEX_TRANSCRIPT)
    mock_run.side_effect = FileNotFoundError("no such file: codex")

    with pytest.raises(RuntimeError, match="could not summarize"):
        call_haiku("test prompt")

    # the "must not fire" half: no silent claude -p reproduction of #460
    for call in mock_run.call_args_list:
        cmd = call[0][0]
        assert not os.path.basename(cmd[0]).startswith("claude")


@patch("pipeline.haiku.subprocess.run")
def test_codex_unavailable_falls_back_when_opted_in(mock_run, monkeypatch):
    """REMEMBER_SUMMARIZER_FALLBACK=claude is the operator's explicit
    opt-in -- the one case where this route is allowed to reach claude -p."""
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", _CODEX_TRANSCRIPT)
    monkeypatch.setenv("REMEMBER_SUMMARIZER_FALLBACK", "claude")

    calls = []

    def _side_effect(cmd, **kwargs):
        calls.append(cmd)
        if os.path.basename(cmd[0]) == "codex":
            raise FileNotFoundError("no such file: codex")
        return MagicMock(returncode=0, stdout=_mock_claude_stdout("fell back"), stderr="")

    mock_run.side_effect = _side_effect
    result = call_haiku("test prompt")
    assert result.text == "fell back"
    # both were attempted, in order: codex first, then the opted-in fallback
    assert os.path.basename(calls[0][0]) == "codex"
    assert os.path.basename(calls[1][0]).startswith("claude")


@patch("pipeline.haiku.subprocess.run")
def test_codex_empty_output_is_a_loud_failure_not_a_silent_skip(mock_run, monkeypatch):
    """An empty -o file (codex exited 0 but wrote nothing) must not be
    read as a legitimate SKIP -- it is a failure of the route, distinct
    from the model choosing SKIP."""
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", _CODEX_TRANSCRIPT)
    _write_codex_output.next_text = ""
    mock_run.side_effect = _write_codex_output

    with pytest.raises(RuntimeError, match="could not summarize"):
        call_haiku("test prompt")


@patch("pipeline.haiku.spawn_guard.claim")
@patch("pipeline.haiku.subprocess.run")
def test_codex_spawn_declined_is_never_treated_as_unavailable(mock_run, mock_claim, monkeypatch):
    """A spawn-guard decline (#204) is "skip this span, retry later", not
    "route unavailable" -- it must propagate as-is, never triggering a
    fallback claude -p spawn for the same span."""
    monkeypatch.setenv("CODEX_SESSION_ID", "01a04d64-fake-codex-session")
    monkeypatch.setenv("REMEMBER_SUMMARIZER_FALLBACK", "claude")
    mock_claim.side_effect = SummarizerSpawnDeclined("declined: too many concurrent")

    with pytest.raises(SummarizerSpawnDeclined):
        call_haiku("test prompt")

    mock_run.assert_not_called()


@patch("pipeline.haiku.subprocess.run")
def test_invalid_summarizer_env_falls_back_to_auto(mock_run):
    """A typo'd REMEMBER_SUMMARIZER value must not silently do something
    unexpected -- it is reported and treated as unset (\"auto\"), same
    pattern as an unusable oauth_token."""
    with patch.dict(os.environ, {"REMEMBER_SUMMARIZER": "bogus"}):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=_mock_claude_stdout("ok"), stderr="")
        result = call_haiku("test prompt")
    assert result.text == "ok"
    cmd = mock_run.call_args[0][0]
    assert os.path.basename(cmd[0]).startswith("claude")
