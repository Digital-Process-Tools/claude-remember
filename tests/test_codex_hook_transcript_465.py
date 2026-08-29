"""Summarizer "auto" routing reads the transcript the host wrote, not its
environment (#465).

#464 keyed Codex detection on CODEX_SESSION_ID/CODEX_THREAD_ID, captured
from a live `codex exec` run -- but that capture came from a Codex *tool
shell*, not from the SessionEnd *hook* process that actually spawns
Remember's summarizer (pipeline/haiku.py, reached via
scripts/save-session.sh <- scripts/session-end-hook.sh). A live capture from
inside a real SessionEnd hook (env dumped with CLAUDE_CODE_* stripped, one
live `codex exec` session, macOS, codex-cli 0.150.1) confirms neither
CODEX_SESSION_ID nor CODEX_THREAD_ID reaches that process: `detect_host()`
could never return CODEX from inside the one call site that mattered, so
every default-configured Codex user's hook-triggered save still resolved
"claude" -- #460's whole point, unreached.

The fix reads REMEMBER_TRANSCRIPT_PATH (already exported by every hook,
#407) and sniffs the file's own first parseable line
(pipeline.extract.sniff_file_envelope(), #443) for a Codex- or
Claude-Code-shaped envelope, instead of asking the environment for a
signature that may or may not have survived into this particular child.

The controls that matter, same as #460/#461/#463 before this:
  * a Claude Code session (or any transcript-less/unrecognised state) still
    resolves "claude" -- the regression invisible until it ships;
  * REMEMBER_SUMMARIZER=claude/codex still overrides detection entirely,
    transcript or no transcript;
  * a real Codex transcript resolves "codex" under "auto" with NO
    Codex-signature environment variable present at all -- the positive
    control that #465 is actually fixed, not just re-described.

Every "must not fire" case is paired with a "must fire" case in the same
fixture shape (CLAUDE.md: a negative assertion needs a positive control).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import _choose_summarizer_provider

FIXTURES = Path(__file__).parent / "fixtures"
CODEX_TRANSCRIPT = FIXTURES / "codex-rollout.jsonl"
CLAUDE_TRANSCRIPT = FIXTURES / "sample-session.jsonl"


def _clear_host_signals(monkeypatch):
    for var in ("CODEX_HOME", "PLUGIN_ROOT", "CODEX_SESSION_ID",
                "CODEX_THREAD_ID", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_ROOT",
                "REMEMBER_SUMMARIZER", "REMEMBER_SUMMARIZER_FALLBACK",
                "REMEMBER_TRANSCRIPT_PATH"):
        monkeypatch.delenv(var, raising=False)


def test_codex_transcript_alone_resolves_codex_with_no_env_signature(monkeypatch):
    """The positive control the whole issue is about: a real Codex
    transcript, with NONE of CODEX_SESSION_ID/CODEX_THREAD_ID/CODEX_HOME/
    PLUGIN_ROOT set -- exactly the state measured inside a live SessionEnd
    hook process -- still resolves "codex" under "auto"."""
    _clear_host_signals(monkeypatch)
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(CODEX_TRANSCRIPT))
    assert _choose_summarizer_provider() == "codex"


def test_claude_code_transcript_resolves_claude(monkeypatch):
    """Regression control: a Claude Code transcript still resolves "claude"
    -- the exact same outcome as before #465, just reached a different way."""
    _clear_host_signals(monkeypatch)
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(CLAUDE_TRANSCRIPT))
    assert _choose_summarizer_provider() == "claude"


def test_no_transcript_at_all_resolves_claude(monkeypatch):
    """No REMEMBER_TRANSCRIPT_PATH (a bare `python3 -m pipeline.shell` run,
    or any caller with no hook preamble) is not Codex -- same historical
    default every host got before Codex routing existed."""
    _clear_host_signals(monkeypatch)
    assert _choose_summarizer_provider() == "claude"


def test_unusable_transcript_path_resolves_claude(monkeypatch):
    """A REMEMBER_TRANSCRIPT_PATH naming a file that does not exist must not
    be read as Codex just because it is present -- the must-not-fire half
    paired with the positive control above."""
    _clear_host_signals(monkeypatch)
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(FIXTURES / "does-not-exist.jsonl"))
    assert _choose_summarizer_provider() == "claude"


def test_codex_signature_env_vars_alone_no_longer_select_codex(monkeypatch):
    """The #465 regression control, inverted from #460's own test: setting
    CODEX_SESSION_ID with NO transcript must not select "codex" any more --
    if it did, this fix would just be re-hiding the #465 bug behind a
    passing test, since a hook process that had never gotten this variable
    could still fake it in a hand-built test environment."""
    _clear_host_signals(monkeypatch)
    monkeypatch.setenv("CODEX_SESSION_ID", "01a04d64-fake-codex-session")
    assert _choose_summarizer_provider() == "claude"


def test_unusable_transcript_logs_why_it_fell_to_claude(monkeypatch):
    """The auditor's own finding on #465: a transcript that WAS exported but
    could not be sniffed (unreadable, or a shape neither host wrote) must
    log why "auto" fell to "claude" -- otherwise a genuinely-Claude-Code
    session and a broken sniff are indistinguishable to an operator
    debugging a wrongly-billed session. Positive control: fires when the
    path exists but is genuinely unrecognisable."""
    _clear_host_signals(monkeypatch)
    unrecognised = FIXTURES / "unrecognised-465.jsonl"
    unrecognised.write_text('{"not": "a host shape"}\n', encoding="utf-8")
    try:
        monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(unrecognised))
        with patch("pipeline.haiku._warn") as mock_warn:
            assert _choose_summarizer_provider() == "claude"
        mock_warn.assert_called_once()
        assert "could not identify the host" in mock_warn.call_args[0][0]
    finally:
        unrecognised.unlink()


def test_no_transcript_at_all_does_not_warn(monkeypatch):
    """The must-not-fire half paired with the positive control above: the
    ordinary, expected case of no REMEMBER_TRANSCRIPT_PATH at all (a bare
    pipeline invocation with no hook preamble) is not a failure and must
    not log a warning every time."""
    _clear_host_signals(monkeypatch)
    with patch("pipeline.haiku._warn") as mock_warn:
        assert _choose_summarizer_provider() == "claude"
    mock_warn.assert_not_called()


def test_explicit_override_wins_regardless_of_transcript(monkeypatch):
    """REMEMBER_SUMMARIZER=claude/codex must still short-circuit detection
    entirely -- transcript-based routing only ever applies to "auto"."""
    _clear_host_signals(monkeypatch)
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(CODEX_TRANSCRIPT))
    monkeypatch.setenv("REMEMBER_SUMMARIZER", "claude")
    assert _choose_summarizer_provider() == "claude"

    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(CLAUDE_TRANSCRIPT))
    monkeypatch.setenv("REMEMBER_SUMMARIZER", "codex")
    assert _choose_summarizer_provider() == "codex"
