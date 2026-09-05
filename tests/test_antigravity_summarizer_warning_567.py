"""An Antigravity transcript resolving under REMEMBER_SUMMARIZER=auto must
warn before falling back to the ``claude`` summarizer (#567).

Teaching ``sniff_envelope()`` the Antigravity shape (#563, ``pipeline/host.py``)
moved an Antigravity transcript out of the ``"unrecognised"`` arm of
``pipeline.haiku._choose_summarizer_provider()`` -- the only arm that used to
warn -- into a silent fall-through to ``return "claude"``. Every other
"we identified a specific non-default host situation but still answer
'claude'" arm in this function warns: the vanished-Codex-transcript case
(#477) and the genuinely-unrecognised-shape case both log before returning
"claude". An Antigravity transcript is exactly that shape of case -- a
positively identified host with no summarizer of its own -- and #567 is the
report that it stopped doing what its siblings still do.

There is no antigravity-native summarizer CLI (``_SUMMARIZER_PROVIDERS`` is
still ``{"claude", "codex", "auto"}``): routing to ``claude`` is the correct
outcome, exactly as it is for a Claude Code transcript. What #567 asks for is
the receipt, not a different provider -- the same distinction #477's own
fix drew for a vanished transcript.

v0.27.0 fired this warning for the same fixture (before #563 taught the
sniffer the Antigravity shape at all, an Antigravity transcript could not be
placed and fell into the "unrecognised" arm) -- that is the positive control
this test reproduces, paired against the "still resolves claude" half so a
future change cannot silently drop the routing itself while only restoring
the log line.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import _choose_summarizer_provider

FIXTURES = Path(__file__).parent / "fixtures"
ANTIGRAVITY_TRANSCRIPT = FIXTURES / "antigravity-transcript-563.jsonl"
CLAUDE_TRANSCRIPT = FIXTURES / "sample-session.jsonl"


def _clear_host_signals(monkeypatch):
    for var in ("CODEX_HOME", "PLUGIN_ROOT", "CODEX_SESSION_ID",
                "CODEX_THREAD_ID", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_ROOT",
                "REMEMBER_SUMMARIZER", "REMEMBER_SUMMARIZER_FALLBACK",
                "REMEMBER_TRANSCRIPT_PATH", "REMEMBER_DIR"):
        monkeypatch.delenv(var, raising=False)


def test_antigravity_transcript_warns_before_falling_back_to_claude(monkeypatch, capsys):
    """The fix: an Antigravity transcript still resolves "claude" (there is
    no other provider to route it to) but now logs that it is doing so,
    the same as the vanished-Codex-transcript and unrecognised-shape arms."""
    _clear_host_signals(monkeypatch)
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(ANTIGRAVITY_TRANSCRIPT))
    assert _choose_summarizer_provider() == "claude"
    err = capsys.readouterr().err
    assert "antigravity" in err.lower()
    assert "claude" in err.lower()


def test_claude_code_transcript_still_resolves_claude_silently(monkeypatch, capsys):
    """The positive control's must-not-fire half: a genuine Claude Code
    transcript is the CORRECT match for "claude", not a fallback, and must
    stay silent -- this is the case the fix must not turn into noise."""
    _clear_host_signals(monkeypatch)
    monkeypatch.setenv("REMEMBER_TRANSCRIPT_PATH", str(CLAUDE_TRANSCRIPT))
    assert _choose_summarizer_provider() == "claude"
    err = capsys.readouterr().err
    assert err == ""
