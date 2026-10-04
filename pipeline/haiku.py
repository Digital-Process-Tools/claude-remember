"""Claude CLI wrapper for calling Haiku and parsing structured JSON responses.

Provides the single interface used by all pipeline stages to invoke Haiku.
Handles subprocess management, parent-session env stripping (CLAUDECODE +
CLAUDE_JOB_DIR + CLAUDE_CODE_*), JSON parsing, token counting, and cost
estimation.

The CLI is invoked in a sandboxed configuration: a fresh, empty `cwd`
created and torn down around each call (`_isolated_summarizer_cwd`, #724 --
NOT the shared system tempdir, which is where another concurrent save's own
tempfiles and the merged config live), every built-in tool made unavailable unless requested (`--tools ""`, #724,
F8 -- an empty `--allowedTools` alone leaves tools that need no approval
still callable, and a hand-maintained deny-list is only ever as complete as
its last update against the CLI's own tool inventory),
``max-turns`` configurable via ``REMEMBER_MAX_TURNS`` (default 4), no MCP
servers (#94), no setting sources and therefore no hooks (#202), and the
parent Claude Code session env vars are stripped (``CLAUDECODE`` to allow a
nested session; ``CLAUDE_JOB_DIR`` / ``CLAUDE_CODE_*`` so the child doesn't
masquerade as the parent's session, #95). ``REMEMBER_NESTED_SUMMARIZER`` is set
so the plugin's own hooks recognise the child and no-op (#204) — that covers
*our* hooks specifically, and stays load-bearing on the fallback path below,
where setting-source isolation has been dropped and the user's hooks are live.
The Codex route (`_call_codex`) additionally runs with an allow-listed child
environment rather than the Claude route's deny-list one, since its
``--sandbox read-only`` still permits command execution (#724, F9/F10).

The output of that call is NOT guaranteed to be the model speaking — a blocking
hook makes the CLI answer in its own voice, on stdout, with exit 0. See
``docs/nested-model-output.md`` before changing how it is validated.

Module-level constants:
    HAIKU_INPUT_PRICE: USD cost per input token.
    HAIKU_OUTPUT_PRICE: USD cost per output token.
    HAIKU_CACHE_PRICE: USD cost per cache-read input token.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from . import extract as _extract
from . import host as _host
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


def _resolve_codex_bin() -> str:
    """Full path to the ``codex`` executable, resolved before spawning (#460).

    Mirrors ``_resolve_claude_bin()``: ``REMEMBER_CODEX_BIN`` overrides the
    lookup, ``shutil.which`` honours PATHEXT on Windows, and a PATH that
    resolves nothing falls back to the bare name so a spawn failure reports
    what was actually tried rather than an internal resolution error.
    """
    override = os.environ.get("REMEMBER_CODEX_BIN", "").strip()
    if override:
        return override
    return shutil.which("codex") or "codex"


# CLAUDE_CODE_* vars are stripped as parent-session identity (#95) — but the
# prefix is a proxy, not a definition, and one member of the family is the
# child's own credential. Stripping it leaves `claude -p` unauthenticated, so
# nothing ever saves for anyone who authenticated with `claude setup-token`
# or runs under a hosted Agent SDK (#131). A suffix-only exception (any
# "CLAUDE_CODE_*_TOKEN") is NOT equivalent: real hosts set other
# CLAUDE_CODE_*_TOKEN variables for unrelated purposes (an internal
# messaging token, observed), and exempting those from the strip too would
# leak them into the child for no reason -- so this names the one exact
# variable, not a shape.
_CHILD_ENV_OAUTH_NAME = "CLAUDE_CODE_OAUTH_TOKEN"

# Set on the child, read by scripts/resolve-paths.sh (#204). Shared here as a
# constant so the tests pin one spelling against both sides of the contract.
NESTED_SUMMARIZER_ENV = "REMEMBER_NESTED_SUMMARIZER"


# ─── Host-native summarizer routing (#460) ─────────────────────────────────
#
# `claude -p` was the only summarizer this module ever shelled out to, so a
# session run entirely inside Codex still paid Anthropic to remember an
# OpenAI session, and needed an authenticated Claude CLI installed for no
# reason a Codex user chose. `codex exec` (verified working against
# codex-cli 0.150.1, #460) is the on-host equivalent and is already installed
# wherever this problem occurs.
#
# REMEMBER_SUMMARIZER selects the provider: "claude" (always the Claude CLI --
# the historical, only-ever behaviour), "codex" (always `codex exec`), or
# "auto" (the default), which reads the TRANSCRIPT the host actually wrote
# (#465) rather than its environment. A Claude Code session's own provider
# resolution is therefore untouched by this feature: it was already
# "claude", and "auto" still answers "claude" for it.
#
# "auto" used to ask pipeline.host.detect_host() -- env-var signatures
# (#460, keyed correctly only after #463). #465 found that mechanism cannot
# work for THIS call site: detect_host() runs inside the summarizer, which
# is spawned from scripts/haiku's caller (pipeline/haiku.py -> _call_codex /
# _call_claude), itself reached through scripts/save-session.sh ->
# scripts/session-end-hook.sh, a process Codex spawns as a HOOK, not the
# tool shell #464's own CODEX_SESSION_ID/CODEX_THREAD_ID fixture was
# captured from. Measured against a live codex-cli 0.150.1 SessionEnd hook
# invocation (env dumped from inside the hook, CLAUDE_CODE_* stripped):
# neither CODEX_SESSION_ID nor CODEX_THREAD_ID reached the process at all --
# only PLUGIN_ROOT/CLAUDE_PLUGIN_ROOT survived. So under "auto", every
# default-configured Codex user's hook-triggered save kept resolving
# "claude", #460's whole point, unreached.
#
# The replacement depends on what the host WROTE, not what it exported: the
# hooks already trust and export REMEMBER_TRANSCRIPT_PATH
# (pipeline.host.transcript_path()), and pipeline.extract.sniff_file_envelope()
# already tells a Codex rollout from a Claude Code transcript by shape
# (#443). A transcript a host wrote cannot be silently withdrawn the way a
# compatibility env var can -- the failure mode #463 and #465 are both
# instances of.
#
# pipeline.host.detect_host() itself is unchanged and still exercised
# directly by tests/test_codex_signature_463.py (env-signature detection is
# still a real, correct fact about a process); it is simply no longer the
# mechanism this router calls for "auto".
#
# REMEMBER_SUMMARIZER_FALLBACK is the opt-in for what happens when the
# resolved codex route cannot produce a result (binary missing, non-zero
# exit, empty output, timeout): unset means "could not summarize", raised
# loudly rather than silently retried against Anthropic's API; "claude" means
# fall back to `claude -p`, exactly as though REMEMBER_SUMMARIZER=claude had
# been set for this one call -- and it is logged every time it fires, because
# it reproduces this issue's own billing complaint, on purpose, only because
# the operator asked for it.
_SUMMARIZER_PROVIDERS = frozenset({"claude", "codex", "auto"})


def _resolve_summarizer_provider() -> str:
    """REMEMBER_SUMMARIZER, validated, else "auto"."""
    raw = os.environ.get("REMEMBER_SUMMARIZER", "").strip().lower()
    if not raw:
        return "auto"
    if raw in _SUMMARIZER_PROVIDERS:
        return raw
    _warn(
        f"WARNING: ignoring REMEMBER_SUMMARIZER={raw!r} -- must be one of "
        f"{sorted(_SUMMARIZER_PROVIDERS)}; using 'auto'"
    )
    return "auto"


def _resolve_summarizer_fallback() -> str | None:
    """REMEMBER_SUMMARIZER_FALLBACK, validated, else None (no fallback)."""
    raw = os.environ.get("REMEMBER_SUMMARIZER_FALLBACK", "").strip().lower()
    if not raw:
        return None
    if raw == "claude":
        return raw
    _warn(
        f"WARNING: ignoring REMEMBER_SUMMARIZER_FALLBACK={raw!r} -- 'claude' "
        "is the only supported fallback target; not falling back"
    )
    return None


def _choose_summarizer_provider() -> str:
    """Which provider this call should use: "claude" or "codex".

    "auto" (the default, and the only case that reads anything for a HOST
    rather than an explicit choice) follows the TRANSCRIPT the host wrote,
    not its environment (#465): pipeline.host.transcript_path() finds the
    file the hook already exported (REMEMBER_TRANSCRIPT_PATH), and
    pipeline.extract.sniff_file_envelope() sniffs that file's own first
    parseable line for a Codex-shaped or Claude-Code-shaped envelope (#443).
    No usable transcript (unset, unreadable, or a shape neither host wrote)
    answers "claude" -- the historical default every host got before Codex
    routing existed, and the safe side of an unrecognised signal either way.
    A transcript that WAS exported but could not be sniffed (deleted between
    export and this read, or a shape neither host wrote) logs why it fell to
    "claude" -- an operator debugging a wrongly-billed session must be able
    to tell "genuinely Claude Code" from "could not tell", the same
    distinction every other UNKNOWN-shaped result in this module already
    makes loudly (see pipeline.host.sniff_envelope()'s own docstring).
    """
    provider = _resolve_summarizer_provider()
    if provider != "auto":
        return provider
    path = _host.transcript_path()
    if not path:
        # transcript_path() collapses two different facts into one None:
        # the var was never set (ordinary -- no hook preamble, nothing to
        # say), and the var WAS set but the file it names is gone (#477 --
        # exported, then vanished before this read, the exact "deleted
        # between export and this read" case the docstring above already
        # promises a receipt for). Re-reading the environment here, rather
        # than widening transcript_path()'s own return shape, keeps that
        # function's contract ("a usable path or None") unchanged for every
        # other caller.
        raw = (os.environ.get("REMEMBER_TRANSCRIPT_PATH") or "").strip()
        if raw:
            _warn(
                f"WARNING: REMEMBER_TRANSCRIPT_PATH={raw!r} names a "
                "transcript that no longer exists (exported, then vanished "
                "before this read) -- REMEMBER_SUMMARIZER=auto is falling "
                "back to 'claude', which may not be correct"
            )
        return "claude"
    envelope, envelope_unreadable, envelope_capped = _extract.sniff_file_envelope_status(path)
    if envelope == "codex":
        return "codex"
    if envelope == "antigravity":
        # #567: Antigravity has no summarizer provider of its own --
        # _SUMMARIZER_PROVIDERS is still {"claude", "codex", "auto"} -- so
        # "claude" is the correct answer here, the same as it is for a
        # genuine Claude Code transcript. But unlike Claude Code, this is
        # a DIFFERENT host's session being billed through `claude -p`, the
        # exact shape #460/#477 already warn about elsewhere in this
        # function; teaching sniff_envelope() the Antigravity shape (#563)
        # moved this transcript out of the "unrecognised" arm below -- the
        # only arm that used to warn -- into a silent fall-through, with
        # no receipt for an operator debugging a wrongly-billed session.
        _warn(
            f"WARNING: transcript {path!r} is an Antigravity session -- "
            "REMEMBER_SUMMARIZER=auto has no Antigravity-native summarizer "
            "and is falling back to 'claude'"
        )
        return "claude"
    if envelope == "unrecognised":
        # #556: "unreadable or an unrecognised shape" used to be the whole
        # story, but it collapsed a THIRD cause into "unrecognised shape" --
        # the scan giving up at extract._ENVELOPE_SNIFF_SCAN_CAP without
        # ever exhausting the file. sniff_file_envelope_status() (rather
        # than the plain sniff_file_envelope() this used to call) is what
        # makes the cap visible here, so the warning can name it instead of
        # misfiling it under a shape genuinely never recognised.
        if envelope_unreadable:
            reason = "unreadable"
        elif envelope_capped:
            reason = "gave up after scanning too many unplaceable lines"
        else:
            reason = "an unrecognised shape"
        _warn(
            f"WARNING: could not identify the host from transcript {path!r} "
            f"({reason}) -- REMEMBER_SUMMARIZER=auto "
            "is falling back to 'claude', which may not be correct"
        )
    return "claude"


def _child_env() -> dict[str, str]:
    """Environment for the nested ``claude -p`` with the PARENT session vars
    stripped.

    ``CLAUDECODE`` blocks nested sessions. ``CLAUDE_JOB_DIR`` and the
    ``CLAUDE_CODE_*`` family (e.g. ``CLAUDE_CODE_SESSION_ID``) identify the
    parent Claude Code session; if they leak into the subprocess it looks like
    a resumable session to anything keying off them (#95). Everything else is
    passed through unchanged — including ``CLAUDE_CODE_OAUTH_TOKEN``, which
    shares the ``CLAUDE_CODE_`` prefix only by accident; see
    ``_CHILD_ENV_OAUTH_NAME`` for the one exact exemption, deliberately not
    widened to the rest of the family (#898).

    ``REMEMBER_NESTED_SUMMARIZER`` is then set as the positive counterpart to
    that stripping. Removing the parent markers is what lets the child start at
    all, and it also erases every trace that it IS a child — so the plugin's own
    hooks fire inside it, resolve a project from ``cwd`` (an isolated, per-call
    directory since #724 -- previously the shared ``gettempdir()``), and
    scaffold a memory directory there (#204). The hooks were always
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

    Nothing else is special-cased by name (#898 round 13: the earlier strip
    of one named API key is gone). The operator's own ``haiku.drop_env`` list
    -- exact names or ``*``/``?`` globs, see ``_configured_drop_env`` -- is the
    one opt-in way to keep more variables out of the child, and it applies to
    every name, the one kept OAuth credential included.
    """
    drop_globs = _configured_drop_env()
    # Every name below written out (#898 round 10), never compared against a
    # constant that holds it. The drop list is one more exclusion in the same
    # single walk -- the environment is never read by a configured name.
    child = {
        k: v
        for k, v in os.environ.items()
        if (
            k == "CLAUDE_CODE_OAUTH_TOKEN"
            or (
                k != "CLAUDECODE"
                and k != "CLAUDE_JOB_DIR"
                and k != "CLAUDE_PROJECT_DIR"
                and not k.startswith("CLAUDE_CODE_")
            )
        )
        and not any(fnmatch.fnmatchcase(k, g) for g in drop_globs)
    }
    child["REMEMBER_NESTED_SUMMARIZER"] = "1"
    return child


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
            f"call {what_happened}; tokens already spent are unknown -- the "
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
        joined = joined[:_FAILURE_DETAIL_MAX] + "..."
    return joined


# The nested `claude -p` needs its own credentials. Normally that is the
# host's own OAuth credential, kept across the strip by
# _CHILD_ENV_OAUTH_NAME above (#131). But some hosts never place it
# in a hook subprocess's environment at all — the
# Claude Code desktop / Agent SDK host withholds it from spawned children — so
# there is nothing to keep and `claude -p` is unauthenticated: the silent-save
# outage of #129 on a machine that *did* run `claude setup-token`.
#
# #860, round 3: there is no recovery path here at all any more. The plugin
# used to offer one -- a recovery token the operator could hand it, via a
# `oauth_token` userConfig option, to fill the host credential above when the
# host withheld it from this hook's own subprocess -- but reading ANY
# credential from the user's machine is itself the condition the directory's
# security scan holds on, independent of consent or provenance, and the
# aggregate kept pairing that read with the real, kept git-backup feature's
# own "sends data" shape. The nested `claude -p` now runs with whatever
# authentication it inherits from its own environment, or none at all; this
# module reads no credential of its own to offer it one.
#
# #898, round 4: the previous fix for this same pairing kept one value-free
# presence check (a function that only ever returned a bool, never a name or
# a value) so an operator with a still-set legacy setting would hear it is
# gone. The directory's scanner read that check's own existence as the read
# side of the pairing regardless -- a presence check of a now-dead setting
# is still a read of something the scanner treats as credential-shaped. That
# check, and the notice built on it, are removed entirely; the docs alone
# say the recovery token is gone.


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


def _remember_dir_is_project_local(remember_dir: str) -> bool:
    """True only when REMEMBER_DIR can be POSITIVELY shown to sit inside the
    project checkout -- the untrusted layout #726 is about, where
    ``.remember/config.json`` is a file the repository ships, not one the
    operator wrote.

    ``MEMORY_PROJECT_DIR`` (set by lib-memory-dir.sh, #56) is the project
    root memory is keyed to. When it is UNSET -- direct python use with no
    shell wrapper, exactly the case ``_config_candidates``'s own docstring
    already carves out as the reason the raw fallback path exists at all --
    this returns False rather than guessing, so that documented use keeps
    working. The real save path always has REMEMBER_CONFIG set, and
    ``_config_candidates`` tries that first; this raw fallback matters only
    when it is not, so a False here in that one specific case does not
    reopen #726 in practice.

    That is a different situation from ``MEMORY_PROJECT_DIR`` being SET but
    ``os.path.realpath`` then raising: there, the shell wrapper DID run (this
    is not the documented direct-python case above), so guessing "external,
    trust it" is the wrong default for a security-motivated check -- it
    returns True (treat as project-local, exclude the raw candidate) instead,
    so an unresolvable path fails safe rather than falling open.
    """
    project_dir = os.environ.get("MEMORY_PROJECT_DIR", "").strip()
    if not project_dir:
        return False
    try:
        remember_abs = os.path.realpath(remember_dir)
        project_abs = os.path.realpath(project_dir)
    except OSError:
        return True
    return remember_abs == project_abs or remember_abs.startswith(project_abs + os.sep)


def _config_candidates() -> list[str]:
    """Config files to search for ``haiku.*`` settings, highest priority first.

    ``REMEMBER_CONFIG`` is the merged config ``lib-memory-dir.sh`` builds from
    all three layers (plugin-bundled, user-global, per-project) and exports
    before invoking the pipeline — the repo's single source of truth for config
    resolution. Reading it, rather than re-deriving the layer order here, is
    what keeps this from becoming a second config reader free to drift from the
    shell one (#177). Since #726, that merge already strips an untrusted
    project layer's ``haiku`` block before REMEMBER_CONFIG is written, so this
    first candidate is safe as-is.

    The raw paths stay as a fallback for direct python use (tests, a manual
    ``python3 -m pipeline.shell`` call) where no shell wrapper ran -- but the
    raw ``${REMEMBER_DIR}/config.json`` is skipped when it can be shown to sit
    inside the project checkout (#726): unlike REMEMBER_CONFIG, this file was
    never passed through the untrusted-layer strip above, so trusting it here
    would reopen the same hole for exactly the code path this fallback exists
    to cover.
    """
    candidates = []
    merged = os.environ.get("REMEMBER_CONFIG", "").strip()
    if merged:
        candidates.append(merged)
    remember_dir = os.environ.get("REMEMBER_DIR", "").strip()
    if remember_dir and not _remember_dir_is_project_local(remember_dir):
        candidates.append(os.path.join(remember_dir, "config.json"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".remember", "config.json"))
    return candidates


# #898, round 4: the value-free presence check that used to live here (and
# the notice built on it) are gone entirely -- see the module note above.
# REMEMBER_OAUTH_TOKEN (an env var) and haiku.oauth_token (a config.json
# key) were this plugin's own earlier, now-removed attempts at a recovery
# token; this file reads neither any more, for any purpose.


# ── haiku.drop_env (#898 round 13; replaces the #703 strip) ──────────────────
#
# #703 (reported in #693) stripped one named API key from the nested call, on
# a `haiku.*` policy key. #898 round 13 (maintainer decision) removed both: the
# nested `claude -p` inherits the environment exactly as Claude Code gave it,
# no variable special-cased by name, and the directory scan has no credential
# name left to cite in this module. `haiku.drop_env` is the one generic,
# opt-in way to keep variables out of that child: a list of exact names or
# shell-style globs (`*`, `?`), matched against variable NAMES only. Default:
# an empty list, so nothing is dropped.
#
# Why the drop happens inside `_child_env()`'s existing walk and not anywhere
# else: that walk already builds the child mapping by excluding names, so one
# more exclusion there reads nothing new. No variable is looked up by a name
# known only at run time, no alias of the environment is taken, nothing is
# passed wholesale, and no parameter is named after it -- the read shapes the
# directory scan cites (claude-directory-publishing triggers.md).

# A name, or a `*`/`?` glob over name characters. Anything else -- `=`, `-`,
# brackets, whitespace -- is ignored with a warning.
_DROP_ENV_ENTRY = re.compile(r"[A-Za-z_*?][A-Za-z0-9_*?]*")


def _configured_drop_env() -> tuple[str, ...]:
    """`haiku.drop_env` from config: the names/globs to keep out of the child.

    The first config candidate that HAS the key decides, the same precedence
    every other `haiku.*` read here uses. A value that is not a list drops
    nothing; an entry that is not a name or a glob is skipped -- both are
    reported, and neither is ever echoed: an operator may have pasted
    ``NAME=value`` with a real value in it, and the daily log is a file on disk.

    On Windows the process environment's names are upper-cased by Python, so
    the globs are upper-cased there too: Windows names are case-insensitive,
    and a lower-case entry should still match.
    """
    for path in _config_candidates():
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(cfg, dict):
            continue
        haiku_cfg = cfg.get("haiku")
        if not isinstance(haiku_cfg, dict) or "drop_env" not in haiku_cfg:
            continue
        value = haiku_cfg["drop_env"]
        if not isinstance(value, list):
            _warn(
                f"WARNING: ignoring haiku.drop_env in {path} -- a "
                f"{type(value).__name__} value, not a list of variable names or "
                "globs; nothing is dropped from the summarizer's environment"
            )
            return ()
        globs = []
        for index, entry in enumerate(value):
            if isinstance(entry, str) and _DROP_ENV_ENTRY.fullmatch(entry):
                globs.append(entry.upper() if os.name == "nt" else entry)
                continue
            if isinstance(entry, str):
                shape = f"a {len(entry)}-character string"
            else:
                shape = f"a {type(entry).__name__} value"
            _warn(
                f"WARNING: ignoring entry {index} of haiku.drop_env in {path} -- "
                f"{shape}, not a variable name or a */? glob over letters, digits "
                "and underscores. The entry itself is not logged: it may hold a "
                "pasted value"
            )
        return tuple(globs)
    return ()


# Markers that a failed call plausibly died on credentials rather than on the
# prompt, the model or the network. Deliberately narrow: a hint that fires on
# every failure would point unrelated outages at an innocent variable, which is
# the shape of misdirection this whole issue is about.
_CREDENTIAL_FAILURE_MARKERS = (
    "credit balance",
    "takes precedence",
    "authentication",
    "unauthorized",
    "invalid api key",
    "invalid x-api-key",
    "rate limit",
)


def _drop_env_hint(detail: str) -> str:
    """The sentence a failure gets when it looks like a credential failure.

    The discoverability half of #703, kept after #898 round 13 removed the
    strip itself: the nested CLI inherits every variable it is given, and some
    of those can out-rank the CLI's own login. This names no variable -- it
    cannot know which one -- only the knob that keeps one out. Empty string
    when the failure does not look like a credential failure. It lands in the
    RuntimeError, which `save-session.sh` surfaces into `hook-errors.log` --
    the place an operator is already looking (#694).
    """
    ...


# #898, round 4: _warn_if_legacy_recovery_token_configured() used to live
# here. It only ever reported a value-free presence check (never a name or
# a value reaching the log), but the directory's scanner read the presence
# check's own existence as the read half of the plugin.json-aggregate
# credential pairing regardless of what it logged -- removed entirely,
# along with the check it called, per the module note above.


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
#
# The tuple is a list of spellings, and a list of spellings is never finished:
# #316 was a Bedrock proxy install where stripping CLAUDE_CODE_USE_BEDROCK and
# then declining to reload the `env` block left the child sending a proxy token
# to the real API, which answers "401 Invalid bearer token" — the exact outage
# this fallback exists for, in words none of the first four markers matched, so
# capture failed 100% of the time and never retried. "failed to authenticate"
# is the CLI's own prefix for that whole family; no rate limit or overload can
# produce it, so it costs nothing and does not need a fifth issue to be filed.
_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "invalid bearer token",
    "authentication_error",
    "failed to authenticate",
)


# The terminal record's fields that the CLI itself authors. This tuple is an
# assumption about a schema, not a fact about one — it was cross-checked against
# a measured CLI and against `_failure_detail`, which arrived at the same set
# independently, and that is the whole of the evidence for it (#320). If the CLI
# grows a new diagnostic field, everything below still runs and finds nothing,
# which is why `_marker_missed_by_the_scan` exists: the set being wrong must not
# be spelled the same way as there being nothing to find.
_SCANNED_FAILURE_FIELDS = ("error", "result", "message")
_SCANNED_FAILURE_LIST_FIELDS = ("errors",)


def _failure_haystack(stdout: str, stderr: str) -> str:
    """The text the *CLI itself* wrote about why it failed, lowercased.

    Scanning all of stdout for a marker reads more than the CLI's own error.
    Under `--output-format json` the CLI v2 array format carries assistant
    message content in the same blob as the terminal result record, so a
    conversation that merely *discusses* an auth failure — a dev debugging their
    login — could put "not logged in" in front of a scan whose answer decides
    whether the retry runs with the user's hooks live. That is the conversation
    choosing when isolation is dropped, which is not a decision it may make.

    So the scan reads the fields the CLI authors: `error` / `result` / `message`,
    plus the `errors` list (`error_max_turns` populates only that one — measured:
    it exits 1 and carries no `result` key at all), plus stderr.

    **Unparseable or unrecognised stdout falls back to the raw scan**, and that
    is deliberate rather than lazy. An older CLI, or a crash before any JSON is
    emitted, has an auth failure to report and no structure to report it in;
    refusing to look would fail *closed*, which is a permanent silent outage —
    the #316 shape exactly. The fallback applies only when nothing structured was
    found, so a payload that does explain itself is taken at its word.

    **The field set is an assumption and cannot be widened here** (#320). A
    terminal record that reports an auth failure in a field this does not read,
    while a field it does read holds something benign, is scanned and found
    clean — and the fallback cannot fire, because `authored` is non-empty.
    Scanning the raw blob on that branch instead would restore the very
    sensitivity to conversation content this docstring opens by rejecting. So
    the caller reports that case rather than acting on it: see
    `_marker_missed_by_the_scan`.
    """
    ...


# The tokens that decide the un-isolated retry. Finding one of these outside the
# scanned fields is the only case worth a line in the log: any other unscanned
# text is, by construction, text this function was never going to act on.
_DECIDING_TOKENS = _AUTH_FAILURE_MARKERS + ("unknown option",)


def _marker_missed_by_the_scan(stdout: str, haystack: str) -> tuple[str, str] | None:
    """A deciding token sitting in the terminal record, outside the scanned set.

    Returns ``(token, field)`` or None. Reads **only** the terminal record — the
    last element of the CLI v2 array, or the whole object — and only its
    unrecognised keys. Deliberately not the conversation: assistant content is
    what #318 removed from this decision, and a session that merely discusses an
    auth failure would otherwise make this fire on every ordinary failure.

    A token already present in ``haystack`` was scanned, so nothing was missed;
    that also covers the raw-scan fallback, where the haystack is everything.
    """
    ...


def _isolation_may_be_the_cause(stdout: str, stderr: str) -> bool:
    """Whether a failed call failed *because of* the hook-isolation flag.

    Deliberately narrow. Retrying a rate limit or an overloaded upstream would
    double a spend that already happened and hide the cause — the #129/#190
    shape, where a real failure was reported as costing nothing.
    """
    ...


def _build_cmd(tools: list[str] | None, isolate_hooks: bool) -> list[str]:
    """The nested CLI invocation, with hook isolation on or off.

    ``--tools`` (not just ``--allowedTools``) is what actually makes this
    summarizer tool-less (#724, F8). An empty ``--allowedTools`` only clears
    the AUTO-APPROVE list -- built-in tools that need no approval at all
    (Read, Glob, Grep, Task, ...) still run under it. A hand-maintained
    deny-list of "every built-in tool" was tried first and rejected: it is
    only as complete as whoever last updated it against the CLI's actual
    tool inventory, and this repo has no test cross-referencing the two, so
    a tool the CLI adds later (or one this list simply missed -- verified
    against `claude --help --restricted`'s own description, which names
    PowerShell and REPL as separate code-running tools neither an earlier
    version of this list nor `--allowedTools` alone would have caught) stays
    reachable by omission. ``--tools ""`` is the CLI's own primitive for
    exactly this ("Use \"\" to disable all tools"), verified present on
    Claude Code 2.1.261 -- the AVAILABLE set, not merely the pre-approved
    one, so nothing outside it exists for the nested session to call at
    all. ``--allowedTools`` is kept alongside it, unchanged, to pre-approve
    within whatever set ``--tools`` names, when a caller does ask for tools.
    """
    ...


@contextlib.contextmanager
def _isolated_summarizer_cwd():
    """A fresh, empty directory for one summarizer subprocess call (#724, F8).

    Both routes used to spawn with ``cwd=tempfile.gettempdir()`` -- the same
    shared directory another concurrent save's own ``remember-prompt-*`` /
    ``remember-codex-out-*`` tempfiles land in, and where the merged config
    (which can carry a live oauth token, see docs/git-backup-security.md) is
    written. A summarizer whose "no tools" guarantee turns out to be
    incomplete -- built-in tools the Claude route's empty ``--allowedTools``
    does not disable, or a command Codex's read-only sandbox still lets run
    -- could read any of that. An empty directory, created and torn down
    around exactly one call, has nothing project-specific in it either way.
    """
    ...


# Minimal environment for the nested `codex exec` PROCESS ITSELF (#724,
# F9/F10) -- NOT for a command that process spawns; that is a SEPARATE
# mechanism, `-c shell_environment_policy.inherit=none` in
# `_build_codex_cmd` (#798). Unlike the Claude route, Codex's
# `--sandbox read-only` still executes whatever commands the model issues
# (see _build_codex_cmd's docstring below), so stripping a deny-list off an
# otherwise-full os.environ (what _child_env() does) is not enough -- a
# command like `env`/`printenv` reads the child's environment directly.
# This keeps only what the CLI itself needs to run and resolve its own
# filesystem-based auth; #798's shell_environment_policy override is what
# keeps a command spawned BY that CLI from seeing this same dict.
#
# #751 (release-audit, reasoned not observed): the original list was
# PATH/HOME/LANG/LC_ALL/CODEX_HOME/TMPDIR/TEMP/TMP only -- no Windows entry,
# and no route for anyone who authenticates Codex through an env var or
# sits behind a proxy, on ANY platform. Both are widened here rather than
# switched to a deny-list: #724's own rationale above (a command the model
# runs can read the child's environment directly) is exactly as true on
# Windows and behind a proxy as it is everywhere else this allow-list
# already applied, so the fix is the same shape, just wider.
#
# The Windows-only names are listed UNCONDITIONALLY, not behind an
# ``os.name == "nt"`` branch: this dict comprehension only ever passes
# through a name that is ALSO a key in the parent's real ``os.environ``, so
# adding ``SYSTEMROOT``/``USERPROFILE``/``APPDATA``/``PATHEXT`` to the
# allow-list is a no-op on POSIX (nothing there sets them) and is exactly
# the widening Codex needs on Windows -- no platform check needed, and
# nothing here would read as "passing" on a platform it does not actually
# cover the way a branched implementation could.
_CODEX_CHILD_ENV_ALLOW = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "CODEX_HOME", "TMPDIR", "TEMP", "TMP",
    # Windows: resolving %SystemRoot%-relative paths, the user profile dir,
    # per-user app data, and which extensions CreateProcess treats as
    # executable when a bare command name (no extension) is looked up on
    # PATH -- without PATHEXT a bare "codex" can fail to resolve at all.
    "SYSTEMROOT", "USERPROFILE", "APPDATA", "PATHEXT",
    # Codex's own env-var credential (its filesystem-based CODEX_HOME/
    # auth.json is unaffected either way) plus the standard proxy/CA
    # variables -- every platform, not just Windows.
    "CODEX_API_KEY",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
})

# #792 (CI, windows-latest): matched case-sensitively, this allow-list would
# need BOTH "HTTPS_PROXY" and "https_proxy" listed to cover either casing a
# user might set -- and even then, CPython's own os.py folds EVERY
# os.environ key to uppercase on `nt` at process-startup time
# (_createenviron's `encodekey = key.upper()`, applied when the initial
# `data` dict is built from the inherited environment, not just when Python
# itself calls __setitem__), so a lowercase entry in this allow-list could
# never match anything on Windows regardless -- os.environ.items() never
# yields a lowercase key there. Matching case-insensitively removes the
# need to enumerate both cases at all: one canonical name per variable
# above, compared against `k.upper()` below, works identically on a
# platform that preserves case (POSIX -- a real https_proxy still gets
# through, since "HTTPS_PROXY" is in the allow-list and .upper() of either
# side lands on the same string) and one that folds it (Windows).
_CODEX_CHILD_ENV_ALLOW_UPPER = frozenset(name.upper() for name in _CODEX_CHILD_ENV_ALLOW)


def _codex_child_env() -> dict[str, str]:
    """Allow-listed environment for `_call_codex`'s subprocess (#724, F9/F10).

    ``PATH``/``HOME``: find and run the binary, resolve ``~``.
    ``LANG``/``LC_ALL``: locale-dependent CLI output.
    ``TMPDIR``/``TEMP``/``TMP``: whichever the platform sets, if any.
    ``CODEX_HOME``: where Codex's own ``auth.json`` lives, if overridden --
    unaffected by this allow-list either way, matching the note in
    `_build_codex_cmd`'s docstring.
    ``SYSTEMROOT``/``USERPROFILE``/``APPDATA``/``PATHEXT``: Windows-only in
    practice (#751) -- absent from ``os.environ`` everywhere else, so listing
    them here costs nothing on POSIX.
    ``CODEX_API_KEY``: Codex's own env-var credential, when that is how this
    host authenticates it rather than a filesystem ``auth.json`` (#751).
    ``HTTPS_PROXY``/``HTTP_PROXY``/``NO_PROXY`` and
    ``SSL_CERT_FILE``/``NODE_EXTRA_CA_CERTS``: anyone running this behind a
    proxy or a custom CA bundle (#751). Matched case-insensitively against
    the parent's real ``os.environ`` (#792) -- some HTTP client libraries
    only ever check the lowercase form of the proxy names, and Windows
    folds every ``os.environ`` key to uppercase regardless of which case
    the variable was actually set under, so a case-sensitive match would
    either miss the lowercase form everywhere, or need both cases listed
    and still never match the lowercase one on Windows.

    Nothing else -- no Anthropic key, no cloud credential, no unrelated
    shell secret this process's own environment happens to carry -- is
    passed through, because a command the model runs inside Codex's
    read-only sandbox can read the child's environment directly.
    """
    ...


def _build_codex_cmd(output_file: str, cwd: str) -> list[str]:
    """The nested ``codex exec`` invocation (#460).

    ``--sandbox read-only``: denies writes and network -- NOT command
    execution. Codex's read-only sandbox still runs whatever commands the
    model issues; this is NOT the tool-less guarantee the comment here used
    to claim (mirroring the Claude path's default empty ``--allowedTools``,
    itself incomplete for the same reason -- see #724, F8/F9). Two SEPARATE
    mechanisms bound the blast radius of a command run here, for two
    SEPARATE audiences:
      * `_codex_child_env` -- the ``env=`` kwarg passed to `subprocess.run`
        -- is what CODEX'S OWN PROCESS receives from this host (its CLI
        needs PATH/HOME to run at all, plus CODEX_API_KEY/proxy/CA vars to
        authenticate and reach the network, #751).
      * the ``-c shell_environment_policy.inherit=none`` override below is
        what a COMMAND CODEX SPAWNS internally receives. These are not the
        same environment: Codex does not hand a spawned command its own
        process env by default just because that is what this host gave
        it. Before #798, nothing here set this policy at all, so a
        transcript-injected instruction that got the model to run a shell
        command inside this sandbox could read CODEX_API_KEY and the proxy
        vars directly out of that command's environment -- exactly the class
        of secret #751 had just finished making Codex's OWN process able to
        see. `_codex_child_env`'s allow-list stays necessary (Codex's own
        auth/proxy needs do not go away); it was never sufficient for this.
        Confirmed against codex-cli 0.153.2's own ``--help`` and the
        official Codex manual (fetched 2026-09-26): `shell_environment_policy`
        is a real, documented dotted-path config key, and ``-c`` overrides
        apply regardless of whether ``config.toml`` is loaded -- relevant
        since this call also passes ``--ignore-user-config``. NOT re-verified
        against codex-cli 0.150.1, the version this repo's own README pins as
        "observed working" (unlike the ``--ignore-user-config`` isolation
        claim two paragraphs below, which was) -- `ShellEnvironmentPolicyInherit`
        is a long-standing enum in Codex's own config schema, not something
        new in 0.153.2, so the risk of it being absent on 0.150.1 is judged
        low, but this is reasoned, not observed, on that specific version.
        ``inherit=none`` also means a spawned command gets NO environment at
        all -- not just no secrets, but no ``PATH``/``HOME``/locale either.
        That is a real behavioral change from "full inherit", not only a
        narrowing of what secrets are visible: a command the model tries to
        run that relies on ``PATH`` to resolve a bare command name will now
        fail to find it. Accepted here because nothing about this
        summarizer's actual job (producing the model's final text message)
        depends on a spawned command succeeding -- unlike `_codex_child_env`,
        which deliberately keeps just enough (``PATH``/``HOME``/proxy/CA) for
        Codex's OWN process to run and authenticate, this policy governs a
        code path this feature does not intend to rely on at all.
        Unit tests here can only assert the argv carries this exact string;
        none can spawn a real `codex exec` to confirm the CLI actually
        empties a spawned command's environment when given it -- that trust
        boundary (does codex-cli honor its own documented flag at runtime)
        is not verifiable from this codebase.
    the fresh, empty `cwd` this call is given (`_isolated_summarizer_cwd`,
    #724) narrows what such a command could even find to act on, but does
    not touch what it can read from its own environment -- that is this
    policy's job, not the cwd's.
    ``--skip-git-repo-check``: cwd is a temp dir, never a git repo.
    ``--ephemeral``: no session file persisted for a one-shot summarizer call.
    ``--ignore-user-config``: Codex's own equivalent of the Claude path's
    ``--setting-sources ''`` hook isolation (#202) -- verified against
    codex-cli 0.150.1 that omitting this flag runs the operator's own Codex
    hooks (including this plugin's, if installed for Codex) inside the
    nested call; ``CODEX_HOME`` auth is unaffected by it.
    ``-o``: the model's final message, and nothing else Codex prints while it
    runs, written to its own file -- avoids parsing progress/reasoning noise
    out of stdout the way ``--output-format json`` lets the Claude path avoid
    it.
    ``-``: read the prompt from stdin, not argv (mirrors the Claude path's
    E2BIG concern -- see ``call_haiku``'s docstring).
    """
    ...


def _call_codex(prompt: str, timeout: int = 120) -> HaikuResult:
    """Call the on-host Codex CLI (``codex exec``) and return a structured
    result (#460).

    Same spawn-guard bound as the Claude path (#204): a codex-routed call is
    still a summarizer spawn, and nothing distinguishes the two for the
    purpose of the runaway-recursion cap.

    Raises RuntimeError for anything that stops this from producing a
    result -- codex missing, a non-zero exit, a timeout, or an empty final
    message. The caller (``call_haiku``) decides what to do with that: raise
    it further (the default -- "could not summarize", said loudly) or retry
    via the Claude CLI (only when REMEMBER_SUMMARIZER_FALLBACK=claude was
    set, and only for the ONE call that failed).
    """
    ...


def call_haiku(
    prompt: str,
    tools: list[str] | None = None,
    timeout: int = 120,
) -> HaikuResult:
    """Call the summarizer and return a structured result.

    Routes to one of two providers via ``_choose_summarizer_provider()``
    (#460): a detected Codex host summarizes via ``codex exec``
    (``_call_codex``, below), and every other host -- Claude Code, an
    unrecognised host, or an explicit ``REMEMBER_SUMMARIZER=claude`` override
    -- spawns a ``claude`` subprocess with ``--model haiku`` and
    ``--output-format json``, waits for completion, and parses the JSON
    response into a ``HaikuResult``. The codex route falls through to this
    same claude path only when it fails AND ``REMEMBER_SUMMARIZER_FALLBACK=
    claude`` was set; otherwise a failed codex route raises rather than
    silently falling back.

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
    ...


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
    ...


def _is_non_summary(text: str) -> bool:
    """True if the output looks like a refusal/clarification, not a summary."""
    ...


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
    ...


def _parse_response(raw: str) -> HaikuResult:
    """Parse JSON output from ``claude --output-format json``.

    Args:
        raw: Raw JSON string from the CLI's stdout.

    Returns:
        HaikuResult with extracted text, token usage, and skip detection.

    Raises:
        RuntimeError: If the raw string is not valid JSON.
    """
    ...


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
    ...
