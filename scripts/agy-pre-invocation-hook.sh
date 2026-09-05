#!/bin/bash
# ============================================================================
# agy-pre-invocation-hook.sh — Antigravity CLI (agy) PreInvocation adapter (#563)
# ============================================================================
#
# DESCRIPTION
#   Antigravity's PreInvocation is this plugin's SessionStart-adjacent
#   mid-session bookkeeping point -- see the #563 issue body's own mapping
#   table (UserPromptSubmit -> PreInvocation). It fires per MODEL
#   INVOCATION, not per user prompt: a turn that makes several invocations
#   fires this several times. user-prompt-hook.sh already tolerates being
#   called more than its Claude Code name implies (Codex's own
#   UserPromptSubmit binding shares this script unchanged, #410), so no new
#   throttling was added here -- if the extra firings prove too frequent in
#   practice that is a follow-up, not something to guess at now.
#
#   user-prompt-hook.sh reads only `cwd` from its own stdin (everything else
#   it needs comes from cached environment or is deliberately cleared, see
#   its own "REMEMBER_TRANSCRIPT_PATH" section) -- so the rename this adapter
#   does is intentionally the smallest one in this port: `workspacePaths[0]`
#   (Antigravity) -> `cwd` (Claude Code shape), nothing else.
#
# STDIN
#   The PreInvocation JSON payload agy hands the hook, consumed here in full
#   and re-emitted (renamed, not reinterpreted) to user-prompt-hook.sh's own
#   stdin.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT is exported here for the same reason
#   agy-session-start-hook.sh exports it -- see that script's own comment.
#
# DEPENDENCIES
#   python3, user-prompt-hook.sh
#
# EXIT CODES
#   0   Always -- user-prompt-hook.sh's own contract; see its own comment on
#       why a hook that runs on every invocation must never block one.
#
# ============================================================================

set -e

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="$(dirname "$_SCRIPT_DIR")"

_STDIN=$(cat)
_NORMALIZED=$(printf '%s' "$_STDIN" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
workspace_paths = d.get("workspacePaths") or []
out = {"cwd": workspace_paths[0] if workspace_paths else ""}
print(json.dumps(out))
' 2>/dev/null) || _NORMALIZED="{}"

# Discard the delegate's stdout for the same reason
# agy-session-start-hook.sh does -- confirmed live (#563):
# user-prompt-hook.sh's Claude Code-shaped `{"hookSpecificOutput": ...}`
# stdout envelope made agy log "failed to unmarshal result ... via protojson:
# ... unknown field \"hookSpecificOutput\"" for every PreInvocation.
printf '%s' "$_NORMALIZED" | bash "$_SCRIPT_DIR/user-prompt-hook.sh" >/dev/null
