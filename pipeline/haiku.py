"""Claude CLI wrapper for calling Haiku and parsing structured JSON responses.

Provides the single interface used by all pipeline stages to invoke Haiku.
Handles subprocess management, parent-session env stripping (CLAUDECODE +
CLAUDE_JOB_DIR + CLAUDE_CODE_*), JSON parsing, token counting, and cost
estimation.

The CLI is invoked in a sandboxed configuration: ``cwd=tempdir``, no tools
by default, ``max-turns`` configurable via ``REMEMBER_MAX_TURNS`` (default 4),
and the parent Claude Code session env vars are stripped (``CLAUDECODE`` to
allow a nested session; ``CLAUDE_JOB_DIR`` / ``CLAUDE_CODE_*`` so the child
doesn't masquerade as the parent's session, #95).

Module-level constants:
    HAIKU_INPUT_PRICE: USD cost per input token.
    HAIKU_OUTPUT_PRICE: USD cost per output token.
    HAIKU_CACHE_PRICE: USD cost per cache-read input token.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

from .types import HaikuResult, TokenUsage

# Haiku pricing (USD per token)
HAIKU_INPUT_PRICE = 0.80 / 1_000_000
HAIKU_OUTPUT_PRICE = 4.00 / 1_000_000
HAIKU_CACHE_PRICE = 0.08 / 1_000_000

# CC 2.x counts prompt-delivery as turn 1, so a cap of 1 exits error_max_turns
# before the model replies (#98/#100). Default 4 clears that plus a Stop-hook
# turn, with margin; overridable via REMEMBER_MAX_TURNS (1..MAX_ALLOWED_TURNS).
DEFAULT_MAX_TURNS = "4"
MAX_ALLOWED_TURNS = 20


def _resolve_max_turns() -> str:
    """REMEMBER_MAX_TURNS if it is an integer in [1, MAX_ALLOWED_TURNS], else
    the safe default.

    A bad value (0, negative, non-numeric, empty) or an absurd one must not
    flow through as a garbage ``--max-turns`` arg — that would break
    ``claude -p`` the same way the original hardcoded ``1`` did. The upper
    bound keeps a misconfiguration bounded instead of opening an unbounded run.
    Returns the normalized form (leading zeros stripped).
    """
    raw = os.environ.get("REMEMBER_MAX_TURNS", "").strip()
    if raw.isdigit() and 1 <= int(raw) <= MAX_ALLOWED_TURNS:
        return str(int(raw))
    return DEFAULT_MAX_TURNS


DEFAULT_MODEL = "haiku"


def _resolve_model() -> str:
    """REMEMBER_MODEL env override, else the safe default ("haiku").

    Memory consolidation is high-stakes (it writes the auto-injected memory
    layer) but low-complexity (extract + compress). A more capable model
    (e.g. "sonnet") improves salience and compression-cap compliance with no
    interactive-latency cost, since this runs backgrounded. Kept as an env knob,
    consistent with REMEMBER_MAX_TURNS / REMEMBER_TZ / REMEMBER_BRANCH.
    """
    raw = os.environ.get("REMEMBER_MODEL", "").strip()
    return raw if raw else DEFAULT_MODEL


def _resolve_claude_bin() -> str:
    """Full path to the ``claude`` executable, resolved before spawning.

    On Windows the npm global install ships the CLI only as a ``claude.cmd``
    shim (no ``claude.exe``). ``subprocess`` goes through ``CreateProcess``,
    which only resolves ``.exe`` from a bare name — so ``["claude", ...]`` dies
    with ``FileNotFoundError: [WinError 2]`` and silently kills every auto-save
    (#120). ``shutil.which`` honours ``PATHEXT`` and returns the full
    ``claude.cmd`` path, which ``subprocess`` launches fine (no ``shell=True``,
    no argv-length regression); on Linux/macOS it returns the plain path.

    REMEMBER_CLAUDE_BIN overrides the lookup (mirrors REMEMBER_MODEL /
    REMEMBER_MAX_TURNS). When ``which`` finds nothing, fall back to the bare
    name so behaviour matches the pre-fix code on a misconfigured PATH.
    """
    override = os.environ.get("REMEMBER_CLAUDE_BIN", "").strip()
    if override:
        return override
    return shutil.which("claude") or "claude"


def _child_env() -> dict[str, str]:
    """Environment for the nested ``claude -p`` with the PARENT session vars
    stripped.

    ``CLAUDECODE`` blocks nested sessions. ``CLAUDE_JOB_DIR`` and the
    ``CLAUDE_CODE_*`` family (e.g. ``CLAUDE_CODE_SESSION_ID``) identify the
    parent Claude Code session; if they leak into the subprocess it looks like
    a resumable session to anything keying off them (#95). Everything else is
    passed through unchanged.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k != "CLAUDECODE"
        and k != "CLAUDE_JOB_DIR"
        and not k.startswith("CLAUDE_CODE_")
    }


def call_haiku(
    prompt: str,
    tools: list[str] | None = None,
    timeout: int = 120,
) -> HaikuResult:
    """Call Haiku via the Claude CLI and return a structured result.

    Spawns a ``claude`` subprocess with ``--model haiku`` and
    ``--output-format json``, waits for completion, and parses the
    JSON response into a ``HaikuResult``.

    Args:
        prompt: The full prompt text to send to the model.
        tools: Optional list of allowed tool names (e.g., ["Read", "Write"]).
            Passed as a comma-separated string to ``--allowedTools``.
        timeout: Maximum seconds to wait for the subprocess before raising.

    Returns:
        HaikuResult containing the model's text, token usage, and skip flag.

    Raises:
        RuntimeError: If the subprocess times out or exits with a non-zero
            return code, or if the JSON response cannot be parsed.
    """
    # Prompt goes on STDIN, not argv: a session extract can exceed Linux's
    # MAX_ARG_STRLEN (128KB per single argument), which raises E2BIG ("Argument
    # list too long") at exec time and silently kills saves of long sessions.
    # `claude -p` with no positional prompt reads the prompt from stdin.
    cmd = [
        _resolve_claude_bin(),
        "-p",
        "--output-format", "json",
        "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections",
        "--model", _resolve_model(),
        "--max-turns", _resolve_max_turns(),
        "--allowedTools", ",".join(tools) if tools else "",
        # Sandbox MCP: no servers + strict, so the nested session inherits none (#94)
        "--mcp-config", '{"mcpServers":{}}',
        "--strict-mcp-config",
    ]

    env = _child_env()

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            # claude emits UTF-8; without this, text=True decodes with the
            # locale codec (cp1252 on Windows) → mojibake / UnicodeDecodeError (#91).
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            cwd=tempfile.gettempdir(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude timed out after {timeout}s")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"claude exited {result.returncode}: {stderr}")

    return _parse_response(result.stdout)


# Reject-gate: conversational refusals / clarifications must NEVER reach the
# memory layer (the audit found a model refusal stored verbatim as a memory).
# The DEFAULT pattern is deliberately NARROW — anchored at the start and limited
# to unambiguous refusal/clarification stems — so dense legitimate summaries
# (which may legitimately open "Unfortunately the build broke...", "There are no
# blockers...", "I notice the cache was stale...") are never silently dropped.
# Widen, override, or disable via REMEMBER_REJECT_PATTERN (see _resolve_reject_pattern).
DEFAULT_REJECT_PATTERN = (
    r"^\s*("
    r"i (cannot|can't|can not|won't|will not|am unable|'m unable|am not able)|"
    r"could you|please (provide|paste|share)|i'm sorry|i am sorry"
    r")\b"
)


def _resolve_reject_pattern() -> "re.Pattern[str] | None":
    """Compiled reject-gate pattern, or None when the gate is disabled.

    REMEMBER_REJECT_PATTERN overrides the default, mirroring the REMEMBER_MODEL /
    REMEMBER_MAX_TURNS env pattern: blank falls back to the narrow default, the
    literal "none" disables the gate entirely, anything else is used as a custom
    case-insensitive regex. An invalid custom regex falls back to the default
    rather than crashing the backgrounded consolidation run.
    """
    raw = os.environ.get("REMEMBER_REJECT_PATTERN", "").strip()
    if raw.lower() == "none":
        return None
    pattern = raw if raw else DEFAULT_REJECT_PATTERN
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return re.compile(DEFAULT_REJECT_PATTERN, re.I)


def _is_non_summary(text: str) -> bool:
    """True if the output looks like a refusal/clarification, not a summary."""
    pattern = _resolve_reject_pattern()
    return bool(pattern.match(text or "")) if pattern else False


def _parse_response(raw: str) -> HaikuResult:
    """Parse JSON output from ``claude --output-format json``.

    Args:
        raw: Raw JSON string from the CLI's stdout.

    Returns:
        HaikuResult with extracted text, token usage, and skip detection.

    Raises:
        RuntimeError: If the raw string is not valid JSON.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from claude: {e}")

    # Claude CLI v2.1.86+ returns a list of message objects instead of a
    # dict with a "result" key.  Normalize both formats.
    if isinstance(data, list):
        # Find the last assistant message with text content
        text = ""
        for msg in reversed(data):
            if msg.get("type") == "result":
                text = msg.get("result", "") or ""
                break
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                text = content
                break
            if isinstance(content, list):
                parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                if parts:
                    text = "\n".join(parts)
                    break
        tokens = _extract_tokens(data[-1] if data else {})
    else:
        text = data.get("result") or ""
        tokens = _extract_tokens(data)

    # Drop SKIP (model found nothing worth saving) AND refusals/clarifications
    # (the reject-gate) so neither is ever written to the memory layer.
    is_skip = text.strip().upper().startswith("SKIP") or _is_non_summary(text)

    return HaikuResult(text=text, tokens=tokens, is_skip=is_skip)


def _extract_tokens(data: dict) -> TokenUsage:
    """Extract token counts from the Claude CLI JSON response.

    Handles both nested (``usage.input_tokens``) and flat (``input_tokens``)
    JSON layouts. Uses ``total_cost_usd`` from the CLI when available,
    otherwise falls back to manual calculation from per-token prices.

    Args:
        data: Parsed JSON dict from the Claude CLI response.

    Returns:
        TokenUsage with input, output, cache counts and estimated cost.
    """
    usage = data.get("usage", {})
    input_tokens = usage.get("input_tokens", 0) or data.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0) or data.get("output_tokens", 0)
    cache_tokens = usage.get("cache_read_input_tokens", 0) or data.get("cache_read_input_tokens", 0)

    cost = data.get("total_cost_usd") or (
        (input_tokens - cache_tokens) * HAIKU_INPUT_PRICE
        + output_tokens * HAIKU_OUTPUT_PRICE
        + cache_tokens * HAIKU_CACHE_PRICE
    )

    return TokenUsage(
        input=input_tokens,
        output=output_tokens,
        cache=cache_tokens,
        cost_usd=cost,
    )
