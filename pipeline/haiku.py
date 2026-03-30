"""Claude CLI wrapper for calling Haiku and parsing structured JSON responses.

Provides the single interface used by all pipeline stages to invoke Haiku.
Handles subprocess management, CLAUDECODE env stripping, JSON parsing,
token counting, and cost estimation.

The CLI is invoked in a sandboxed configuration: ``cwd=tempdir``, no tools
by default, ``max-turns 1``, and the ``CLAUDECODE`` env var is stripped
to allow nested Claude sessions.

Module-level constants:
    HAIKU_INPUT_PRICE: USD cost per input token.
    HAIKU_OUTPUT_PRICE: USD cost per output token.
    HAIKU_CACHE_PRICE: USD cost per cache-read input token.
"""

import json
import os
import subprocess
import tempfile

from .types import HaikuResult, TokenUsage

# Haiku pricing (USD per token)
HAIKU_INPUT_PRICE = 0.80 / 1_000_000
HAIKU_OUTPUT_PRICE = 4.00 / 1_000_000
HAIKU_CACHE_PRICE = 0.08 / 1_000_000


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
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--model", "haiku",
        "--max-turns", "1",
        "--allowedTools", ",".join(tools) if tools else "",
    ]

    # CLAUDECODE env var blocks nested sessions — must strip it
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
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

    is_skip = text.strip().upper().startswith("SKIP")

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
