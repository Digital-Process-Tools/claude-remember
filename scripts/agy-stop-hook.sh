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
_FIELDS=$(printf '%s' "$_STDIN" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
workspace_paths = d.get("workspacePaths") or []
print(d.get("conversationId", ""))
print(d.get("transcriptPath", ""))
print(workspace_paths[0] if workspace_paths else "")
' 2>/dev/null) || _FIELDS=""

_CONVERSATION_ID=$(printf '%s\n' "$_FIELDS" | sed -n 1p)
_TRANSCRIPT_PATH=$(printf '%s\n' "$_FIELDS" | sed -n 2p)
_WORKSPACE_PATH=$(printf '%s\n' "$_FIELDS" | sed -n 3p)

if [ -z "$_CONVERSATION_ID" ] || [ -z "$_TRANSCRIPT_PATH" ]; then
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
