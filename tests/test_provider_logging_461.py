"""SKIP is ambiguous about WHICH route declined once more than one
summarizer provider exists (#460/#461).

Four Codex probe sessions all produced a plain `SKIP -- position -> N` with
no further detail. Investigating #461 (see the pull request body for the
control-experiment writeup) established this is NOT host-shaped: identical
content run through the Claude Code route also SKIPs, and identical richer
content SAVES via both routes. So this is not a fix to the model's judgment
threshold -- it stays exactly where it was -- it is a fix to what the log
CAN say. Every "was this host-shaped?" investigation from here on can read
which provider a given SKIP/REJECTED/appended line came from directly,
instead of reconstructing it from the branch name or which issue was being
worked at the time.

These tests pin the log wiring, not the model's judgment: the stub always
answers deterministically, so what is under test is only that `PROVIDER`
travels from ``pipeline.shell call-haiku``'s output through to the three log
lines that report a call's outcome.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

from subprocess_helpers import subprocess_failure_detail  # noqa: E402
from test_save_session_gates import _make_env, _memory_log_text, _run  # noqa: E402


def test_skip_names_the_claude_provider_by_default(tmp_path):
    """The historical, only-ever provider: an unset STUB_PROVIDER means the
    stub answers "claude" (matching what a live claude -p call reports), and
    the log says so -- the positive control for the codex case below."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=3, humans=3)
    result = _run(plugin, env, sid)

    assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
    log_text = _memory_log_text(project)
    assert "SKIP (provider: claude)" in log_text


def test_skip_names_the_codex_provider_when_that_route_answered(tmp_path):
    """A Codex-routed call's SKIP says "codex", not "claude" -- the whole
    point of #461: whichever route produced a verdict is nameable from the
    log line that reports it, not guessed at from context."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=3, humans=3)
    env["STUB_PROVIDER"] = "codex"
    result = _run(plugin, env, sid)

    assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
    log_text = _memory_log_text(project)
    assert "SKIP (provider: codex)" in log_text
    assert "SKIP (provider: claude)" not in log_text


def test_appended_entry_names_the_provider_that_produced_it(tmp_path):
    """The success path (a genuine save, not a SKIP) carries the same
    attribution -- so a saved entry's provider is not lost the moment it
    stops being a SKIP."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=5, humans=3)
    env["STUB_PROVIDER"] = "codex"
    env["STUB_HAIKU_SKIP"] = "0"
    result = _run(plugin, env, sid)

    assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
    log_text = _memory_log_text(project)
    assert "appended (provider: codex)" in log_text


def test_rejected_reply_names_the_provider_too(tmp_path):
    """A refusal-shaped reply (the reject gate, #136) is a distinct event
    from a genuine SKIP, and it now carries the same provider attribution as
    the other two outcomes -- no path was left out."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=3, humans=3)
    env["STUB_PROVIDER"] = "codex"
    env["STUB_HAIKU_REJECTED"] = "1"
    result = _run(plugin, env, sid)

    assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
    log_text = _memory_log_text(project)
    assert "REJECTED (provider: codex;" in log_text
