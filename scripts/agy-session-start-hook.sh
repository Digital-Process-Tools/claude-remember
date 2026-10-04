#!/bin/bash

set -e

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="$(dirname "$_SCRIPT_DIR")"

_STDIN=$(cat)
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

printf '%s' "$_NORMALIZED" \
    | REMEMBER_SUPPRESS_PROMO=1 bash "$_SCRIPT_DIR/session-start-hook.sh" >/dev/null
