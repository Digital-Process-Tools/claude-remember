"""Claude CLI wrapper for calling Haiku and parsing structured JSON responses.

Provides the single interface used by all pipeline stages to invoke Haiku.
Handles subprocess management, parent-session env stripping (CLAUDECODE +
CLAUDE_JOB_DIR + CLAUDE_CODE_*), JSON parsing, token counting, and cost
estimation.

The CLI is invoked in a sandboxed configuration: ``cwd=tempdir``, no tools
by default, ``max-turns`` configurable via ``REMEMBER_MAX_TURNS`` (default 4),
no MCP servers (#94), no setting sources and therefore no hooks (#202), and the
parent Claude Code session env vars are stripped (``CLAUDECODE`` to allow a
nested session; ``CLAUDE_JOB_DIR`` / ``CLAUDE_CODE_*`` so the child doesn't
masquerade as the parent's session, #95). ``REMEMBER_NESTED_SUMMARIZER`` is set
so the plugin's own hooks recognise the child and no-op (#204) — that covers
*our* hooks specifically, and stays load-bearing on the fallback path below,
where setting-source isolation has been dropped and the user's hooks are live.

The output of that call is NOT guaranteed to be the model speaking — a blocking
hook makes the CLI answer in its own voice, on stdout, with exit 0. See
``docs/nested-model-output.md`` before changing how it is validated.

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
import sys
import tempfile

from . import spawn_guard
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


# CLAUDE_CODE_* vars are stripped as parent-session identity (#95) — but the
# prefix is a proxy, not a definition, and one member of the family is the
# child's *credentials*. Stripping CLAUDE_CODE_OAUTH_TOKEN leaves `claude -p`
# unauthenticated, so nothing ever saves for anyone who authenticated with
# `claude setup-token` or runs under a hosted Agent SDK (#131). Keep it.
_CHILD_ENV_KEEP = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})

# Set on the child, read by scripts/resolve-paths.sh (#204). Shared here as a
# constant so the tests pin one spelling against both sides of the contract.
NESTED_SUMMARIZER_ENV = "REMEMBER_NESTED_SUMMARIZER"


def _child_env() -> dict[str, str]:
    """Environment for the nested ``claude -p`` with the PARENT session vars
    stripped.

    ``CLAUDECODE`` blocks nested sessions. ``CLAUDE_JOB_DIR`` and the
    ``CLAUDE_CODE_*`` family (e.g. ``CLAUDE_CODE_SESSION_ID``) identify the
    parent Claude Code session; if they leak into the subprocess it looks like
    a resumable session to anything keying off them (#95). Everything else is
    passed through unchanged — including the credentials in ``_CHILD_ENV_KEEP``,
    which only share the prefix by accident.

    ``REMEMBER_NESTED_SUMMARIZER`` is then set as the positive counterpart to
    that stripping. Removing the parent markers is what lets the child start at
    all, and it also erases every trace that it IS a child — so the plugin's own
    hooks fire inside it, resolve a project from ``cwd=gettempdir()``, and
    scaffold a memory directory for the temp dir (#204). The hooks were always
    meant to no-op here; they were reading a signal this function had deleted.
    A marker we set ourselves cannot be deleted by us and cannot false-positive
    on an unrelated session, which ``CLAUDE_CODE_ENTRYPOINT=sdk-cli`` would.

    ``CLAUDE_PROJECT_DIR`` goes too. It does not carry the ``CLAUDE_CODE_``
    prefix, so it was never covered by the rule above — while #204's report, and
    ``resolve-paths.sh``'s own comments, both describe the child as having none.
    Claude Code 2.1.219 overwrites it from the sandbox cwd, so the leak is inert
    there and is not the mechanism behind any report; a CLI that honoured the
    inherited value would aim the summarizer's hooks at the REAL project, which
    is the failure @ehutchinsonSFDC saw. Cheap to close, and it makes the code
    say what the comments already claim.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k in _CHILD_ENV_KEEP
        or (
            k != "CLAUDECODE"
            and k != "CLAUDE_JOB_DIR"
            and k != "CLAUDE_PROJECT_DIR"
            and not k.startswith("CLAUDE_CODE_")
        )
    }
    env[NESTED_SUMMARIZER_ENV] = "1"
    return env


def _usage_from_failure(stdout: object) -> TokenUsage | None:
    """Token counts out of a FAILED call's output, when it carried any.

    ``--output-format json`` makes the CLI report errors as a JSON object on
    stdout (that is what #129 was about), and that object can carry the same
    usage block a success does. A timeout usually leaves nothing parseable —
    the process was killed mid-write — so this returns None more often than not.
    """
    if isinstance(stdout, bytes):
        try:
            stdout = stdout.decode("utf-8", errors="replace")
        except Exception:
            return None
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(payload, list):
        payload = payload[-1] if payload else {}
    if not isinstance(payload, dict):
        return None
    usage = _extract_tokens(payload)
    # An all-zero reading means the payload had no usage block at all, which is
    # not the same as a call that cost nothing — say unknown rather than free.
    if usage.input or usage.output or usage.cache:
        return usage
    return None


def _log_failed_spend(what_happened: str, stdout: object) -> None:
    """Record what a call that FAILED cost (#190).

    Every accounting path in the pipeline hangs off a returned result, and a
    failure returns none — so a run where the model times out repeatedly showed
    errors in the log and zero reported cost, which reads as "it failed for
    free". It did not: a client-side timeout aborts a call the API has already
    been billing, and a mid-stream error has already consumed input.

    "unknown" is the honest answer when the payload carries no usage. Zero is
    not.
    """
    usage = _usage_from_failure(stdout)
    if usage is not None:
        _warn(f"call {what_happened} after spending tokens: {usage}")
    else:
        _warn(
            f"call {what_happened}; tokens already spent are unknown — the "
            "failure carried no usage block, so this run's reported cost is "
            "lower than what it actually cost"
        )


# Cap on the failure detail carried into the exception: enough to identify an
# auth error or a rate limit, not enough to dump a whole JSON payload into the
# log on every failure.
_FAILURE_DETAIL_MAX = 500


def _failure_detail(stdout: str, stderr: str) -> str:
    """Best available explanation for a non-zero ``claude`` exit.

    ``--output-format json`` makes the CLI report failures as a JSON object on
    **stdout** and leave stderr empty, so reading stderr alone produced
    ``claude exited 1:`` with nothing after the colon — which hid a 7-week auth
    outage from the reporter of #129. Prefer the structured message on stdout,
    fall back to raw stdout, then stderr, and say so explicitly when both are
    empty rather than trailing off.
    """
    detail = ""
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()

    if stdout:
        try:
            payload = json.loads(stdout)
        except ValueError:
            detail = stdout
        else:
            if isinstance(payload, dict):
                for key in ("error", "result", "message"):
                    value = payload.get(key)
                    if isinstance(value, dict):
                        value = value.get("message")
                    if isinstance(value, str) and value.strip():
                        detail = value.strip()
                        break
            detail = detail or stdout

    parts = [p for p in (detail, stderr) if p]
    if not parts:
        return "(no output on stdout or stderr)"
    joined = " | ".join(parts)
    if len(joined) > _FAILURE_DETAIL_MAX:
        joined = joined[:_FAILURE_DETAIL_MAX] + "…"
    return joined


# The nested `claude -p` needs its own credentials. Normally that is
# CLAUDE_CODE_OAUTH_TOKEN, kept across the strip by _CHILD_ENV_KEEP (#131).
# But some hosts never place it in a hook subprocess's environment at all — the
# Claude Code desktop / Agent SDK host withholds it from spawned children — so
# there is nothing to keep and `claude -p` is unauthenticated: the silent-save
# outage of #129 on a machine that *did* run `claude setup-token`.
#
# Recovery is consent-based: the operator hands THIS plugin a token to pass to
# the nested CLI, via the REMEMBER_OAUTH_TOKEN env var or a `haiku.oauth_token`
# key in config.json. It is used only when the child env has no
# CLAUDE_CODE_OAUTH_TOKEN, and only a value the operator deliberately
# configured — nothing is read from OS credential storage the platform withheld.
_MIN_TOKEN_LEN = 20


def _warn(message: str) -> None:
    """Surface a token-resolution problem where an operator will actually read it.

    The daily log is the one place shell and pipeline entries interleave.
    stderr is not: ``save-session.sh`` captures ``call-haiku``'s stderr to a
    temp file and only echoes it when the call *fails*, so a warning written
    there on the way to a successful call is discarded. Falls back to stderr
    only when REMEMBER_DIR is unset (direct python use, tests).

    Never raises — this sits on the path to authenticating, and a logging
    failure must not become an auth failure.
    """
    try:
        remember_dir = os.environ.get("REMEMBER_DIR", "").strip()
        if remember_dir:
            from .log import log

            log("haiku", message, os.path.join(remember_dir, "logs"))
        else:
            print(f"[haiku] {message}", file=sys.stderr)
    except Exception:
        pass


def _looks_like_token(value: object) -> bool:
    """A configured value is usable only if it is a non-empty, whitespace-free
    string of plausible length.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return len(stripped) >= _MIN_TOKEN_LEN and not any(c.isspace() for c in stripped)


def _accept_token(value: object, source: str) -> str | None:
    """The configured value if usable, else ``None`` **and a log line**.

    A rejected value used to vanish in silence, which made a typo'd or
    truncated token indistinguishable from never having configured one: the
    nested CLI ran unauthenticated and the operator got the same confusing auth
    error this fallback exists to prevent, now with a misleading origin.

    An empty value stays silent — that is how the bundled config ships the key
    (``"oauth_token": ""``), i.e. "not configured", and it reaches here on
    every save through the merged config.

    The value itself is never logged; only its source and its length.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not _looks_like_token(value):
        if isinstance(value, str):
            detail = f"{len(value.strip())} chars"
        else:
            detail = f"a {type(value).__name__} value"
        _warn(
            f"WARNING: ignoring {source} — not a plausible OAuth token "
            f"(want a whitespace-free string of at least {_MIN_TOKEN_LEN} "
            f"chars, got {detail}); the nested CLI will run unauthenticated "
            "unless the host provides a token of its own"
        )
        return None
    return str(value).strip()


def _config_candidates() -> list[str]:
    """Config files to search for ``haiku.oauth_token``, highest priority first.

    ``REMEMBER_CONFIG`` is the merged config ``lib-memory-dir.sh`` builds from
    all three layers (plugin-bundled, user-global, per-project) and exports
    before invoking the pipeline — the repo's single source of truth for config
    resolution. Reading it, rather than re-deriving the layer order here, is
    what keeps this from becoming a second config reader free to drift from the
    shell one (#177).

    The raw paths stay as a fallback for direct python use (tests, a manual
    ``python3 -m pipeline.shell`` call) where no shell wrapper ran.
    """
    candidates = []
    merged = os.environ.get("REMEMBER_CONFIG", "").strip()
    if merged:
        candidates.append(merged)
    remember_dir = os.environ.get("REMEMBER_DIR", "").strip()
    if remember_dir:
        candidates.append(os.path.join(remember_dir, "config.json"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".remember", "config.json"))
    return candidates


def _configured_oauth_token() -> str | None:
    """Operator-configured OAuth token for the nested CLI, or ``None``.

    Precedence: the ``REMEMBER_OAUTH_TOKEN`` env var, then a
    ``haiku.oauth_token`` key in the first config file that declares one (see
    ``_config_candidates``). Best-effort: any missing file, read error, or
    malformed JSON yields ``None`` and never raises — but a value that is
    present and unusable is logged rather than dropped silently.
    """
    env_token = os.environ.get("REMEMBER_OAUTH_TOKEN", "").strip()
    if env_token:
        token = _accept_token(env_token, "REMEMBER_OAUTH_TOKEN")
        if token:
            return token

    for path in _config_candidates():
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(cfg, dict):
            continue
        haiku_cfg = cfg.get("haiku")
        if not isinstance(haiku_cfg, dict) or "oauth_token" not in haiku_cfg:
            continue
        token = _accept_token(haiku_cfg["oauth_token"], f"haiku.oauth_token in {path}")
        if token:
            return token
    return None


def _inject_configured_oauth_token(env: dict[str, str]) -> dict[str, str]:
    """Fill CLAUDE_CODE_OAUTH_TOKEN from operator config when the child env
    lacks it (see the note above).

    Never overrides a value already present — the host-provided credential wins.
    Never raises: token resolution is entirely best-effort.
    """
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return env
    token = _configured_oauth_token()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


# Hook isolation (#202). The nested `claude -p` was sandboxed against MCP
# servers (#94) and against the parent's session identity (#95), but not
# against the user's HOOKS — which are registered from settings files, so
# `--setting-sources ''` (load none of user/project/local) registers none of
# them. Verified against claude-code 2.1.219: with a blocking UserPromptSubmit
# hook installed, the call returns the model's reply instead of the hook's
# block message, and OAuth auth is unaffected.
#
# Why this matters more than it sounds: a hook that BLOCKS does not make the
# call fail. The CLI writes its block message to stdout, quotes the prompt back
# under "Original prompt:", reports `subtype: success`, and exits 0 — so every
# status-based check downstream sees a healthy call and the block message is
# read as the model's reply. That is how #202 wrote a hook's refusal into the
# permanent memory record, seven levels deep.
_HOOK_ISOLATION_FLAG = "--setting-sources"

# Failures that mean the ISOLATION caused this, not the request. Both are
# resolved before the API is contacted, so neither has been billed and the
# retry below is free.
#
#   * an older CLI has no such flag. commander exits non-zero on an unknown
#     option, which became a RuntimeError, which meant NO SAVES EVER AGAIN —
#     trading a corruption bug for a silent total outage (the #204 trap).
#   * excluding every setting source also excludes `apiKeyHelper` and the `env`
#     block, which is how enterprise / Bedrock / proxy installs authenticate.
#     Those users get "Not logged in" and the same permanent outage, on a
#     CURRENT CLI.
#
# So isolation fails OPEN: it is dropped, loudly, and the memory record stays
# protected by the echo guard in consolidate.py. That is why there are two
# independent layers — this one can be degraded away, and the other cannot.
_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
)


def _isolation_may_be_the_cause(stdout: str, stderr: str) -> bool:
    """Whether a failed call failed *because of* the hook-isolation flag.

    Deliberately narrow. Retrying a rate limit or an overloaded upstream would
    double a spend that already happened and hide the cause — the #129/#190
    shape, where a real failure was reported as costing nothing.
    """
    haystack = f"{stdout or ''}\n{stderr or ''}".lower()
    if "unknown option" in haystack:
        # Only ours. An unknown option naming some other flag says the CLI
        # disagrees about something we did not just add, and dropping this one
        # would not fix it.
        return _HOOK_ISOLATION_FLAG in haystack
    return any(marker in haystack for marker in _AUTH_FAILURE_MARKERS)


def _build_cmd(tools: list[str] | None, isolate_hooks: bool) -> list[str]:
    """The nested CLI invocation, with hook isolation on or off."""
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
    if isolate_hooks:
        # Sandbox settings: no sources, so the nested session inherits no hooks (#202)
        cmd += [_HOOK_ISOLATION_FLAG, ""]
    return cmd


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
    env = _child_env()
    env = _inject_configured_oauth_token(env)

    # Bound the spawn before spawning (#204). Every defence above this line
    # depends on a signal reaching the child — an env marker a host can redact,
    # a CLI flag a CLI can reject — and when both failed, nothing limited how
    # many summarizers came into being. This does, from the parent side, through
    # the filesystem. Declining RAISES rather than returning a lesser result: a
    # cap that fires is a state the operator has to be able to see.
    try:
        slot = spawn_guard.claim(timeout=timeout)
    except spawn_guard.SummarizerSpawnDeclined as declined:
        _warn(f"WARNING: {declined}")
        raise
    if slot.degraded:
        _warn(
            "WARNING: the summarizer spawn guard could not use "
            f"{spawn_guard.record_dir()} ({slot.degraded}); this spawn is "
            "UNBOUNDED. Saves keep working — an unusable runtime directory must "
            "not become a permanent save outage (#204) — but nothing is "
            "counting summarizers until it is writable again."
        )

    def _run(isolate_hooks: bool):
        try:
            return subprocess.run(
                _build_cmd(tools, isolate_hooks),
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
        except subprocess.TimeoutExpired as timed_out:
            # A client-side timeout aborts a call the API has already been
            # billing. Whatever partial output arrived is the only evidence of
            # what it cost.
            _log_failed_spend(f"timed out after {timeout}s", timed_out.stdout)
            raise RuntimeError(f"claude timed out after {timeout}s")

    # One claimed slot covers the retry below as well: it is the same logical
    # summarizer, and the first attempt died resolving arguments or credentials
    # without reaching the API.
    try:
        result = _run(isolate_hooks=True)

        if result.returncode != 0 and _isolation_may_be_the_cause(
            result.stdout, result.stderr
        ):
            # Nothing was billed — the call died resolving arguments or
            # credentials — so this retry is free. Say so where an operator will
            # read it: the nested call is now running with the user's hooks live,
            # which is the exact condition #202 is about, and the only thing
            # still standing between a blocked prompt and the memory record is
            # the echo guard in consolidate.py.
            _warn(
                f"WARNING: this CLI rejected {_HOOK_ISOLATION_FLAG} "
                f"({_failure_detail(result.stdout, result.stderr)}); retrying "
                "WITHOUT hook isolation so saves keep working. The nested call "
                "will run with your hooks registered — a hook that blocks it "
                "returns its block message as if it were the model's reply "
                "(#202)."
            )
            result = _run(isolate_hooks=False)
    finally:
        slot.release()

    if result.returncode != 0:
        _log_failed_spend(f"exited {result.returncode}", result.stdout)
        raise RuntimeError(
            f"claude exited {result.returncode}: "
            f"{_failure_detail(result.stdout, result.stderr)}"
        )

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


# The CLI's own voice, arriving where the model's reply is expected (#202).
# `claude -p` reports a hook-blocked prompt as `subtype: success` with exit 0
# and puts the block message in `result`, so no status, exit code or usage
# figure distinguishes it from an answer — only the text does.
#
# This sits at the boundary where stdout is interpreted, which makes it the one
# place that covers every pipeline stage at once: consolidation has its own
# echo guard, but the save path would otherwise write a block message into
# today's staging file as a session entry, and that is how it reaches
# consolidation to begin with.
_CLI_NOTICE = re.compile(r"^\s*\w+ operation blocked by hook:", re.I)


def _is_cli_notice(text: str) -> bool:
    """True if this is the CLI talking about the call, not a reply to it."""
    return bool(_CLI_NOTICE.match(text or ""))


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
    model_skipped = text.strip().upper().startswith("SKIP")
    rejected = not model_skipped and (_is_non_summary(text) or _is_cli_notice(text))

    return HaikuResult(text=text, tokens=tokens,
                       is_skip=model_skipped or rejected, is_rejected=rejected)


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
