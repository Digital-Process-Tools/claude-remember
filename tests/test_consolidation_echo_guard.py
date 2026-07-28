"""Pins for #202 — a hook block message echoed into the permanent memory record.

Two independent defects produced one corruption:

1. ``pipeline/haiku.py`` isolated the nested ``claude -p`` from MCP servers
   (#94) but from nothing else, so the *user's* hooks fired inside it. A
   ``UserPromptSubmit`` hook that blocks makes the CLI write its block message
   to stdout and exit **0**, reporting ``subtype: success`` — so the plugin
   reads the block message as the model's reply.
2. ``_is_valid_consolidation`` was positive-only. Every signal it looked for
   (``===RECENT===``, a ``## HH:MM |`` entry header) is present in the block
   message, because that message quotes the prompt back and the prompt embeds
   the staging files verbatim *and* the ``===RECENT===`` envelope from the
   output-format section of the template. A positive-only check cannot reject
   an echo of its own input.

The envelope reproduced by ``_hook_block_message`` below is not invented. It
was captured from claude-code 2.1.219 by installing a blocking
``UserPromptSubmit`` hook and running ``claude -p --output-format json``; the
CLI returned exit 0 with::

    "result": "UserPromptSubmit operation blocked by hook:\\n[<cmd>]: <stderr>\\n\\n\\nOriginal prompt: <prompt>\\n"

Reported by @kodown1k, who measured 29,153 bytes / 689 lines / 7 nesting
levels before the recursion was noticed.
"""

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.consolidate import (
    consolidate,
    _is_valid_consolidation,
    ConsolidationSkipped,
)
from pipeline.entry_header import contains_header
from pipeline.prompts import build_consolidation_prompt
from pipeline.haiku import call_haiku


# --- fixtures reproducing the real failure ---------------------------------

# Staging content of the shape save-session.sh actually writes: `## HH:MM |
# identity` headers. These are what make an echo indistinguishable from memory
# under a positive-only check.
STAGING = {
    "2026-07-20.md": (
        "# 2026-07-20\n\n"
        "## 09:14 | main\n"
        "Fixed the lock adoption path on Linux; `stat -f` was polluting stdout.\n\n"
        "## 14:32 | fix/198-lock-age\n"
        "Split the two stat probes into separate command substitutions, added a pin.\n"
    ),
    "2026-07-21.md": (
        "# 2026-07-21\n\n"
        "## 11:05 | main\n"
        "Wired the `debug` config key through to `config()`; it had never been read.\n"
    ),
}

RECENT = (
    "# Recent\n\n"
    "## 2026-07-19\n"
    "Shipped 0.8.9. Reworked the capture-gap notice to compare identities, not mtimes.\n"
)

ARCHIVE = (
    "# Archive\n\n"
    "## Week of 2026-07-13\n"
    "Slug unification across five readers. Lock primitive rewritten around adoption.\n"
)


def _hook_block_message(prompt: str) -> str:
    """The CLI's ``UserPromptSubmit`` block message, verbatim shape.

    Captured from claude-code 2.1.219 (see the module docstring). The whole
    prompt comes back after ``Original prompt:`` — that is the echo.
    """
    return (
        "UserPromptSubmit operation blocked by hook:\n"
        "[.claude/hooks/guard.sh]: refusing: session not in an allowed project\n"
        "\n\n"
        f"Original prompt: {prompt}\n"
    )


def _genuine_consolidation() -> str:
    """What Haiku actually returns on a healthy run."""
    return (
        "===RECENT===\n"
        "# Recent\n\n"
        "## 2026-07-20\n"
        "Lock adoption fixed on Linux: the two stat probes shared one command\n"
        "substitution and the failing one printed before exiting.\n\n"
        "## 2026-07-21\n"
        "`debug` config key wired through to `config()`.\n\n"
        "===ARCHIVE===\n"
        "# Archive\n\n"
        "## Week of 2026-07-13\n"
        "Slug unification across five readers. Lock primitive rewritten.\n"
    )


# --- layer 2: the guard must be able to reject an echo ----------------------


def test_the_echo_carries_every_signal_the_old_guard_trusted():
    """Establishes the premise, so the pin below cannot pass for a cheap reason.

    If this fails, the fixture has stopped being a realistic echo and the
    rejection pin proves nothing.
    """
    prompt = build_consolidation_prompt(STAGING, RECENT, ARCHIVE)
    echo = _hook_block_message(prompt)

    # The envelope delimiter — it is in the prompt's own output-format section.
    assert "===RECENT===" in echo
    assert "===ARCHIVE===" in echo
    # Real entry headers, carried in from the staging files.
    assert contains_header(echo)
    assert "## 14:32 | fix/198-lock-age" in echo
    # And the placeholder that landed in the reporter's archive.md.
    assert "[archive.md content here]" in echo


def test_hook_block_message_is_rejected():
    """THE pin. A block message quoting a prompt full of real entry headers
    must not be accepted as consolidated memory.

    Against the positive-only guard this returns True: `===RECENT===` is
    present (from the template's output-format block) and so is
    `## 14:32 | ...` (from the staging files).
    """
    prompt = build_consolidation_prompt(STAGING, RECENT, ARCHIVE)
    assert _is_valid_consolidation(_hook_block_message(prompt)) is False


def test_second_generation_echo_is_rejected():
    """The recursion step: once a block message is in recent.md, the next run
    quotes it into a new prompt and the block message nests one level deeper.

    Seven levels of this is what the reporter ended up with.
    """
    first = _hook_block_message(build_consolidation_prompt(STAGING, RECENT, ARCHIVE))
    poisoned_recent = "# Recent\n\n" + first
    second = _hook_block_message(
        build_consolidation_prompt(STAGING, poisoned_recent, ARCHIVE)
    )
    assert _is_valid_consolidation(second) is False


def test_echo_of_a_prompt_without_the_envelope_is_still_rejected():
    """Generality check: the rejection must not hang on `===RECENT===` alone.

    Some other channel could echo only the instruction body. The instruction
    text is still there, and a real consolidation never reproduces it.
    """
    prompt = build_consolidation_prompt(STAGING, RECENT, ARCHIVE)
    truncated = prompt.split("## Output format")[0]
    assert "===RECENT===" not in truncated
    assert contains_header(truncated)  # staging headers survive the truncation
    assert _is_valid_consolidation(_hook_block_message(truncated)) is False


def test_genuine_consolidation_is_still_accepted():
    """The guard must not have been made echo-rejecting by rejecting everything."""
    assert _is_valid_consolidation(_genuine_consolidation()) is True


def test_genuine_consolidation_without_the_envelope_is_still_accepted():
    """The entry-header fallback stays — it is the #139/#177 path."""
    assert _is_valid_consolidation(
        "# Recent\n\n## 2026-07-20\nCompressed the day's work.\n"
    ) is True


def test_consolidate_refuses_to_write_memory_from_an_echo():
    """End to end: an echoed block message must raise ConsolidationSkipped, so
    the caller neither overwrites recent.md/archive.md nor retires staging."""
    prompt = build_consolidation_prompt(STAGING, RECENT, ARCHIVE)
    echo = _hook_block_message(prompt)

    with patch("pipeline.consolidate.call_haiku") as mock_haiku:
        from pipeline.types import HaikuResult, TokenUsage

        mock_haiku.return_value = HaikuResult(
            text=echo, tokens=TokenUsage(1, 1, 0, 0.0), is_skip=False,
            is_rejected=False,
        )
        with pytest.raises(ConsolidationSkipped):
            consolidate(STAGING, RECENT, ARCHIVE)


# --- layer 1: the nested call must not inherit the user's hooks -------------


def _mock_claude_response(result_text: str) -> str:
    return json.dumps({
        "result": result_text,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
    })


@patch("pipeline.haiku.subprocess.run")
def test_nested_call_isolates_hooks(mock_run):
    """The nested `claude -p` must load no setting sources, the way #94 made it
    load no MCP servers. Hooks live in user/project/local settings files; with
    all three excluded none of them is registered.

    Asserted on the constructed argv, not on a mock's return value.
    """
    mock_run.return_value = MagicMock(
        returncode=0, stdout=_mock_claude_response("ok"), stderr="")
    call_haiku("p")
    cmd = mock_run.call_args[0][0]
    assert "--setting-sources" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""


@patch("pipeline.haiku.subprocess.run")
def test_older_cli_without_the_flag_degrades_instead_of_going_silent(mock_run):
    """The #204 trap: an unknown option makes the CLI exit non-zero, which
    becomes a RuntimeError, which means **no saves ever again**.

    That trades a corruption bug for a silent total outage, so an unknown
    `--setting-sources` must be retried without it rather than raised.

    The failure shape is claude-code's own, captured from 2.1.219:
    exit 1, empty stdout, `error: unknown option '--flag'` on stderr.
    """
    mock_run.side_effect = [
        MagicMock(
            returncode=1,
            stdout="",
            stderr="error: unknown option '--setting-sources'",
        ),
        MagicMock(returncode=0, stdout=_mock_claude_response("ok"), stderr=""),
    ]
    result = call_haiku("p")

    assert result.text == "ok"
    assert mock_run.call_count == 2
    first, second = [c[0][0] for c in mock_run.call_args_list]
    assert "--setting-sources" in first
    assert "--setting-sources" not in second
    # The rest of the sandbox must survive the retry — the fallback drops the
    # hook isolation only, never the MCP isolation of #94.
    assert "--strict-mcp-config" in second
    assert "--no-session-persistence" in second


@patch("pipeline.haiku.subprocess.run")
def test_unknown_option_for_some_other_flag_is_not_retried(mock_run):
    """An unknown option that is not ours says the CLI disagrees about
    something we did not just add — retrying without `--setting-sources`
    would not fix it and would spend twice to fail twice."""
    mock_run.return_value = MagicMock(
        returncode=1, stdout="",
        stderr="error: unknown option '--exclude-dynamic-system-prompt-sections'",
    )
    with pytest.raises(RuntimeError):
        call_haiku("p")
    assert mock_run.call_count == 1


# --- layer 2b: the seam where a non-reply arrives on stdout -----------------
#
# The template-derived check above protects consolidation only. The general
# failure — "something that is not the model's reply arrived on stdout" — is a
# property of the CLI boundary, so it is also answered there, once, for every
# pipeline stage. The save path has the same corruption available to it: a
# block message quoting a save prompt would be written to today's staging file
# as a session entry, which is how it reaches consolidation in the first place.


def test_parse_response_rejects_a_cli_block_message():
    """`claude -p` reports a blocked prompt as `subtype: success` with exit 0,
    so nothing downstream can tell it apart from a reply by status alone."""
    from pipeline.haiku import _parse_response

    blocked = _hook_block_message("## 09:14 | main\nsome earlier entry\n")
    result = _parse_response(_mock_claude_response(blocked))
    assert result.is_skip is True
    assert result.is_rejected is True


def test_parse_response_still_accepts_an_ordinary_entry():
    """The seam check must not reject real summaries."""
    from pipeline.haiku import _parse_response

    result = _parse_response(
        _mock_claude_response("## 14:32 | main\nFixed the lock adoption path.")
    )
    assert result.is_skip is False
    assert result.is_rejected is False


# --- layer 1: isolation must fail OPEN into layer 2, never into an outage ---


@patch("pipeline.haiku.subprocess.run")
def test_settings_file_auth_falls_back_instead_of_dying(mock_run):
    """Excluding every setting source also excludes `apiKeyHelper` and the
    `env` block, which is how enterprise / Bedrock / proxy installs are
    authenticated. Those users would get "Not logged in", a non-zero exit, and
    a permanent save outage — the #204 shape on a CURRENT CLI, not an old one.

    Neither attempt reaches the API, so the retry is free. Losing hook
    isolation puts that user back on layer 2, which is why there are two.
    """
    mock_run.side_effect = [
        MagicMock(
            returncode=1,
            stdout=json.dumps({"result": "Not logged in · Please run /login",
                               "is_error": True}),
            stderr="",
        ),
        MagicMock(returncode=0, stdout=_mock_claude_response("ok"), stderr=""),
    ]
    result = call_haiku("p")
    assert result.text == "ok"
    assert mock_run.call_count == 2
    assert "--setting-sources" not in mock_run.call_args_list[1][0][0]


@patch("pipeline.haiku.subprocess.run")
def test_a_billable_api_failure_is_never_retried(mock_run):
    """A rate limit or an overloaded upstream already cost money. Retrying it
    doubles the spend and hides the cause — the #129/#190 shape."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout=json.dumps({"error": {"message": "rate_limit_error: slow down"}}),
        stderr="",
    )
    with pytest.raises(RuntimeError) as excinfo:
        call_haiku("p")
    assert "rate_limit" in str(excinfo.value)
    assert mock_run.call_count == 1
