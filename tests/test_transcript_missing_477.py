"""#477: transcript_path() collapses two different facts into one None --
the var was never set, and the var was set but the file it names is gone --
and pipeline.haiku._choose_summarizer_provider() read that single None as
"nothing to say", so a Codex session whose exported transcript vanished
between export and read was silently billed to Anthropic with no receipt.

The four probes from the issue body, reproduced as a fixture:

    A unset                       : provider='claude' stderr=''
    B set-but-file-deleted        : provider='claude' stderr=''
    C set + real codex transcript : provider='codex'  stderr=''
    D set + unrecognised shape    : provider='claude' stderr="...WARNING..."

D already logs (test_codex_hook_transcript_465.py::
test_unusable_transcript_logs_why_it_fell_to_claude). A was already known not
to log (test_no_transcript_at_all_does_not_warn). B was indistinguishable
from A -- this file is the missing case: B must now warn, distinctly from A,
so an operator can tell "genuinely nothing to read" from "there WAS a
transcript and it is gone".

Every "must not fire" case (A) is paired with a "must fire" case (B) in the
same fixture, per CLAUDE.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import _choose_summarizer_provider

FIXTURES = Path(__file__).parent / "fixtures"


def _clear_host_signals(monkeypatch):
    for var in ("CODEX_HOME", "PLUGIN_ROOT", "CODEX_SESSION_ID",
                "CODEX_THREAD_ID", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_ROOT",
                "REMEMBER_SUMMARIZER", "REMEMBER_SUMMARIZER_FALLBACK",
                "REMEMBER_TRANSCRIPT_PATH"):
        monkeypatch.delenv(var, raising=False)


def test_set_but_missing_transcript_warns_distinctly_from_unset(monkeypatch):
    """Probe B: REMEMBER_TRANSCRIPT_PATH was set to a real path, but the
    file it names does not exist -- deleted between export and this read.
    The provider still safely falls back to 'claude' (unchanged), but must
    now say why, the same way probe D (unrecognised shape) already does --
    otherwise 'genuinely nothing exported' and 'exported, then vanished'
    render identically to an operator debugging a wrongly-billed session."""
    _clear_host_signals(monkeypatch)
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(FIXTURES / "does-not-exist-477.jsonl"))
    with patch("pipeline.haiku._warn") as mock_warn:
        assert _choose_summarizer_provider() == "claude"
    mock_warn.assert_called_once()
    message = mock_warn.call_args[0][0]
    assert "does-not-exist-477.jsonl" in message
    assert "no longer" in message or "vanished" in message or "gone" in message


def test_unset_transcript_still_does_not_warn(monkeypatch):
    """Probe A, the positive control's pair: no REMEMBER_TRANSCRIPT_PATH at
    all is the ordinary case (a bare invocation with no hook preamble) and
    must stay silent -- this is what tells B's warning apart from a warning
    that fires on every ordinary run."""
    _clear_host_signals(monkeypatch)
    with patch("pipeline.haiku._warn") as mock_warn:
        assert _choose_summarizer_provider() == "claude"
    mock_warn.assert_not_called()
