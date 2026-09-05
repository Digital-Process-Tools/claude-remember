#!/bin/bash
# ============================================================================
# agy-session-start-hook.sh — Antigravity CLI (agy) SessionStart adapter (#563)
# ============================================================================
#
# DESCRIPTION
#   Antigravity's SessionStart stdin payload uses different field names from
#   Claude Code's -- {"conversationId", "transcriptPath", "modelName",
#   "artifactDirectoryPath", "workspacePaths"} vs {"session_id",
#   "transcript_path", "cwd", "source", ...} -- so this rewrites the payload
#   into the shape session-start-hook.sh already understands and delegates to
#   it unchanged. #563's own scope is a port, not new hook code: the same
#   "existing scripts only" discipline #410 held the Codex manifest to.
#
#   `source` is synthesized as "startup": Antigravity's own SessionStart
#   payload carries nothing equivalent to Claude Code's resume/clear/compact/
#   fork distinction (confirmed against a live capture, #563), and
#   session-start-hook.sh already treats an unrecognised/empty `source` as a
#   safe default rather than a special case, so "startup" costs nothing here
#   and is the closest real match: an Antigravity SessionStart fires once
#   per conversation, at its beginning.
#
# STDIN
#   The SessionStart JSON payload agy hands the hook, consumed here in full
#   and re-emitted (renamed, not reinterpreted) to session-start-hook.sh's
#   own stdin.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT is exported here, not read from the process
#   environment: agy does not set it, nor any plugin-root-shaped variable
#   (pipeline/host.py's ANTIGRAVITY host declares none, confirmed by dumping
#   a live hook process's own environment, #563), and the shared
#   ~/.gemini/config/hooks.json this hook installs into has no
#   extension-scoped ${extensionPath} the way a linked Gemini extension
#   does -- that substitution is documented as extension-local only. This
#   script's own location on disk IS one directory below the plugin root, so
#   it derives CLAUDE_PLUGIN_ROOT from itself (BASH_SOURCE) rather than
#   depend on a variable no host is known to supply here.
#
# DEPENDENCIES
#   python3 (renaming the payload; already a hard dependency of every other
#   hook script in this plugin), session-start-hook.sh
#
# EXIT CODES
#   0   Always -- see session-start-hook.sh's own contract; this adapter
#       never blocks a session on a rename failure, it just hands through an
#       empty object and lets the delegate's own defaults take over.
#
# ============================================================================

set -e

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="$(dirname "$_SCRIPT_DIR")"

_STDIN=$(cat)
# #568: a malformed payload (bad JSON, or no python3 at all) used to
# normalise to the exact same {} this delegate call already sends for a
# genuinely empty payload, with no receipt distinguishing "there was
# nothing to say" from "the payload could not even be read". Settled by
# #568 itself: a log line on this arm is safe -- agy parses these hooks'
# STDOUT as protojson, but the delegate's own stdout is discarded a few
# lines down regardless, so stderr is free to use.
# mktemp's own failure (unwritable/full TMPDIR) must not abort this script
# under `set -e` -- self-review caught an earlier draft that called mktemp
# unguarded here, which turned a broken TMPDIR into an interpreter-level
# abort instead of this hook's own documented "0 Always" exit contract.
# `_PARSE_ERR_FILE` (empty on mktemp failure) and `_PARSE_ERR_TARGET`
# (falls back to /dev/null) are kept as two separate variables so the
# `rm -f` below can never be handed `/dev/null` itself.
_PARSE_ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/remember-agy-session-start-parse-XXXXXX" 2>/dev/null) || _PARSE_ERR_FILE=""
_PARSE_ERR_TARGET="${_PARSE_ERR_FILE:-/dev/null}"
_NORMALIZED=$(printf '%s' "$_STDIN" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print(f"malformed: {exc}", file=sys.stderr)
    d = {}
workspace_paths = d.get("workspacePaths") or []
out = {
    "session_id": d.get("conversationId", ""),
    "transcript_path": d.get("transcriptPath", ""),
    "cwd": workspace_paths[0] if workspace_paths else "",
    "source": "startup",
}
print(json.dumps(out))
' 2>"$_PARSE_ERR_TARGET") || _NORMALIZED="{}"
if [ -n "$_PARSE_ERR_FILE" ]; then
    if [ -s "$_PARSE_ERR_FILE" ]; then
        echo "[agy-session-start-hook] WARNING: stdin payload could not be parsed as JSON ($(cat "$_PARSE_ERR_FILE")) -- forwarding an empty SessionStart" >&2
    fi
    rm -f "$_PARSE_ERR_FILE"
fi

# Antigravity's hook executor parses a command hook's STDOUT as protojson
# against its OWN structured-output schema, not Claude Code's -- confirmed
# live (#563): session-start-hook.sh's plain-text "=== REMEMBER ===" context
# injection made agy log "failed to unmarshal result ... via protojson:
# proto: syntax error (line 1:1): invalid value =" for every SessionStart.
# Context injection through hook stdout is a Claude Code-specific mechanism
# this issue does not attempt to port (#563's own scope is capture, not
# context injection) and Antigravity's own docs describe no equivalent
# contract this plugin could target instead, so the delegate's stdout is
# discarded here rather than guessed at.
printf '%s' "$_NORMALIZED" | bash "$_SCRIPT_DIR/session-start-hook.sh" >/dev/null
