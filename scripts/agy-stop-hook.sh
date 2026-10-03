#!/bin/bash
# ============================================================================
# agy-stop-hook.sh — Antigravity CLI (agy) Stop adapter (#563)
# ============================================================================
#
# DESCRIPTION
#   Antigravity's Stop fires at the end of EVERY turn/invocation -- it is a
#   turn boundary, not a teardown. manaflow-ai/cmux#5000 (cited in #563)
#   documents the expensive way to learn this: treating a restorable agent's
#   turn-end as a session end destroyed their restore record after the
#   FIRST turn. Wiring this straight into session-end-hook.sh -- which is
#   built to run exactly ONCE, at the actual end of a session, and does
#   backup/consolidation work on that assumption -- would repeat whatever
#   one-shot work that script does on every single turn boundary. This
#   script deliberately does NOT do that, and does not import or source
#   session-end-hook.sh at all.
#
#   Instead it does what post-tool-hook.sh already does safely many times a
#   session: ask save-session.sh (WITHOUT --force) to flush if there is
#   anything new and its own cooldown has elapsed. save-session.sh's cooldown
#   and lock (mkdir, timeout 0 on a plain call) make a call that finds
#   nothing new -- or loses a race to a concurrent save -- a safe, silent
#   no-op; see save-session.sh's own "Cooldown" and "Lock" sections for why
#   that call is idempotent to repeat. That is what makes THIS script safe
#   to run on every turn, unlike session-end-hook.sh's own
#   cleanup/consolidation, which is not idempotent against running once per
#   turn and is why this script exists as a separate, smaller thing rather
#   than a Stop binding straight to that script.
#
#   What this script does NOT attempt: genuine session-end semantics (the
#   final archiving/consolidation session-end-hook.sh performs once a real
#   session actually ends). No Antigravity signal observed so far
#   corresponds to a real teardown -- Stop fires after every turn including
#   the first, and of the four events #553/#563 confirmed loading and firing
#   (SessionStart, PreInvocation, PostInvocation, Stop), none is a
#   process-exit or conversation-close event. That gap is left open and
#   reported (see the #563 changelog entry and docs/install-antigravity.md),
#   not silently worked around by reusing Stop for a purpose it does not fit.
#
# STDIN
#   The Stop JSON payload agy hands the hook: {"conversationId",
#   "transcriptPath", "terminationReason", "fullyIdle", "executionNum",
#   "error", ...}. Only `conversationId` and `transcriptPath` are read here.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT is exported here for the same reason
#   agy-session-start-hook.sh exports it -- see that script's own comment.
#
#   REMEMBER_TRANSCRIPT_PATH is exported fresh from this hook's own stdin
#   payload, the same trusted-input contract session-start-hook.sh and
#   session-end-hook.sh already follow (#407, #431) -- pipeline/host.py's
#   find_session() returns it verbatim, before any session-id-based
#   reconstruction is attempted, which is what lets a bare `conversationId`
#   (not a Claude Code session UUID, not a path under any conventional
#   sessions directory) work as save-session.sh's positional session id at
#   all: that argument is only ever used for locking/position-file naming
#   once REMEMBER_TRANSCRIPT_PATH is set, never to locate the file itself.
#
# DEPENDENCIES
#   python3, save-session.sh
#
# EXIT CODES
#   0   Always -- a missing conversationId/transcriptPath is treated as
#       nothing to do, not a failure; this hook must never block a turn.
#
# ============================================================================

set -e

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="$(dirname "$_SCRIPT_DIR")"

_STDIN=$(cat)
# #568: a malformed payload (bad JSON, or no python3 at all) and a genuinely
# empty-but-valid `{}` payload both used to produce this exact same output
# -- three empty fields -- so this hook took its "nothing to capture" exit
# identically in both cases, with no receipt distinguishing "there was
# nothing to do" from "the payload could not even be read". A 4th line
# ("ok"/"malformed: ...") tells them apart without touching stdout's
# existing three-field shape any callers already depend on -- self-review
# caught an earlier draft of this fix routing the message through a
# `mktemp`-captured stderr file instead of this 4th line, which both (a)
# printed the wrong reason for a plain JSON-syntax error (python never
# writes to stderr in that branch, only to this 4th stdout line) and (b)
# added a `mktemp` call with no failure guard under `set -e`, turning an
# unwritable/full TMPDIR into an interpreter-level abort instead of this
# script's own documented "0 Always" exit contract -- exactly the failure
# `save-session.sh`'s own `mktemp ... || { ...; exit 1; }` (its "Step 6"
# section) already guards against. Routing the message through stdout
# instead avoids both: no temp file, no extra failure mode. Settled by
# #568 itself: a log line on this arm is safe -- agy parses these hooks'
# STDOUT as protojson, but stdout already goes nowhere here, so stderr and
# the daily log are both free to use.
# #578: the four fields below are extracted POSITIONALLY (one `sed -n Np`
# per line), so an embedded newline inside any one of them would shift every
# field after it by one line -- transcriptPath could land in
# _WORKSPACE_PATH, or worse, a later field could land in _CONVERSATION_ID.
# _field() rejects \n/\r at the point of entry instead, the same convention
# session-end-hook.sh and post-tool-hook.sh already apply to their own
# stdin-sourced fields (their case "..." in *$'\n'*|*$'\r'*) guard) --
# so a field carrying either becomes empty rather than shifting its
# neighbours out of position.
_FIELDS=$(printf '%s' "$_STDIN" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    parse_status = "ok"
except Exception as exc:
    d = {}
    parse_status = f"malformed: {exc}"
workspace_paths = d.get("workspacePaths") or []

def _field(v):
    if not isinstance(v, str):
        return ""
    return "" if ("\n" in v or "\r" in v) else v

print(_field(d.get("conversationId", "")))
print(_field(d.get("transcriptPath", "")))
print(_field(workspace_paths[0] if workspace_paths else ""))
print(parse_status)
' 2>/dev/null) || _FIELDS=""

_CONVERSATION_ID=$(printf '%s\n' "$_FIELDS" | sed -n 1p)
_TRANSCRIPT_PATH=$(printf '%s\n' "$_FIELDS" | sed -n 2p)
_WORKSPACE_PATH=$(printf '%s\n' "$_FIELDS" | sed -n 3p)
_PARSE_STATUS=$(printf '%s\n' "$_FIELDS" | sed -n 4p)

# #579 (Windows/Git-Bash, reasoned not observed live -- no Windows runner was
# available to establish this): python3 -c 'print(...)' writes CRLF line
# endings there, and command substitution ($(...)) strips only a TRAILING
# newline from the whole captured stream, not the \r that would precede
# each INTERNAL line terminator -- so every field but the last would
# otherwise carry a trailing \r no caller expects. Stripped unconditionally;
# a no-op everywhere this \r never appears.
_CONVERSATION_ID="${_CONVERSATION_ID%$'\r'}"
_TRANSCRIPT_PATH="${_TRANSCRIPT_PATH%$'\r'}"
_WORKSPACE_PATH="${_WORKSPACE_PATH%$'\r'}"
_PARSE_STATUS="${_PARSE_STATUS%$'\r'}"

# #576: _CONVERSATION_ID reaches save-session.sh below as argv[1] with no
# shape requirement of its own -- unlike post-tool-hook.sh's SESSION_ID,
# which must also name a real, already-existing transcript file before it is
# trusted (see that script's STDIN_SESSION_ID_TRUSTED gate). save-session.sh's
# own argument loop (`for arg in "$@"`) reads a literal "--force" or "--dry"
# as a FLAG no matter where it came from -- its session-id shape check
# (`^[a-f0-9][a-f0-9-]*$`) only ever sees whatever landed in the positional
# slot, never the raw argv. A conversationId of exactly "--force" would
# therefore silently flip FORCE=true, bypassing the cooldown and
# minimum-human-message gates -- a real, reachable route confirmed by
# reading save-session.sh's own arg loop, not a hypothetical one. The
# sibling hooks' own guard (session-end-hook.sh, post-tool-hook.sh,
# session-start-hook.sh: `''|.|..|*[!A-Za-z0-9._-]*`) does not by itself
# close this: "--force" and "--dry" both consist entirely of characters that
# guard already allows. Extended here with a leading-dash rejection (`-*`)
# so nothing shaped like an option can ever reach that argv position.
case "$_CONVERSATION_ID" in
    ''|.|..|-*|*[!A-Za-z0-9._-]*) _CONVERSATION_ID="" ;;
esac

if [ -z "$_CONVERSATION_ID" ] || [ -z "$_TRANSCRIPT_PATH" ]; then
    if [ "$_PARSE_STATUS" != "ok" ]; then
        echo "[agy-stop-hook] WARNING: stdin payload could not be parsed as JSON (${_PARSE_STATUS:-python3 unavailable or produced no output}) -- capturing nothing for this Stop" >&2
    fi
    exit 0
fi

export REMEMBER_TRANSCRIPT_PATH="$_TRANSCRIPT_PATH"
# save-session.sh is not itself registered as a hook here (only this
# adapter is), so it has no stdin payload of its own to resolve PROJECT_DIR
# from -- resolve-paths.sh needs CLAUDE_PROJECT_DIR (or REMEMBER_HOOK_CWD)
# set some other way, or it FATALs and loud-exits (caught live, #563: the
# background call below was silently losing that FATAL to its own
# /dev/null redirect, so nothing ever appeared in the target workspace's
# memory log -- confirmed by running this hook by hand with output NOT
# redirected). Antigravity's own `workspacePaths` is the only project-root
# signal this payload carries, the same field agy-session-start-hook.sh
# already forwards as `cwd` -- forwarded here as CLAUDE_PROJECT_DIR
# directly, since this script calls save-session.sh, not a hook script that
# derives it from a stdin `cwd` field itself.
if [ -n "$_WORKSPACE_PATH" ]; then
    export CLAUDE_PROJECT_DIR="$_WORKSPACE_PATH"
fi
# nohup + disown, not a bare backgrounded subshell -- confirmed live (#563):
# a plain `( ... & )` never survived past this hook process exiting under a
# real `agy` run (no trace of save-session.sh ever having started, not even
# its own cooldown-skip log line, across repeated live probes), the same
# defence post-tool-hook.sh already applies for its own backgrounded save
# (scripts/post-tool-hook.sh's own `nohup "$SAVE_SCRIPT" ... &`) and
# session-end-hook.sh applies via its trailing `disown`. Antigravity's own
# hook executor appears to reap a command hook's process group more
# aggressively than Claude Code's did, which this failed silently against
# until caught by checking the target workspace's own memory log rather
# than trusting the hook's exit code alone.
nohup bash "$_SCRIPT_DIR/save-session.sh" "$_CONVERSATION_ID" >/dev/null 2>&1 &
disown 2>/dev/null || true
exit 0
