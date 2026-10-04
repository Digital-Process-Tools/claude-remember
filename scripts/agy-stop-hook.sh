#!/bin/bash

set -e

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="$(dirname "$_SCRIPT_DIR")"

_STDIN=$(cat)
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

_CONVERSATION_ID="${_CONVERSATION_ID%$'\r'}"
_TRANSCRIPT_PATH="${_TRANSCRIPT_PATH%$'\r'}"
_WORKSPACE_PATH="${_WORKSPACE_PATH%$'\r'}"
_PARSE_STATUS="${_PARSE_STATUS%$'\r'}"

if [ -z "${_CONVERSATION_ID#.}" ] || [ -z "${_CONVERSATION_ID#..}" ] \
    || [ "${_CONVERSATION_ID#-}" != "$_CONVERSATION_ID" ] \
    || [[ "$_CONVERSATION_ID" == *[!A-Za-z0-9._-]* ]]; then
    _CONVERSATION_ID=""
fi

if [ -z "$_CONVERSATION_ID" ] || [ -z "$_TRANSCRIPT_PATH" ]; then
    if [ "$_PARSE_STATUS" != "ok" ]; then
        echo "[agy-stop-hook] WARNING: stdin payload could not be parsed as JSON (${_PARSE_STATUS:-python3 unavailable or produced no output}) -- capturing nothing for this Stop" >&2
    fi
    exit 0
fi

export REMEMBER_TRANSCRIPT_PATH="$_TRANSCRIPT_PATH"
if [ -n "$_WORKSPACE_PATH" ]; then
    export CLAUDE_PROJECT_DIR="$_WORKSPACE_PATH"
fi
nohup bash "$_SCRIPT_DIR/save-session.sh" "$_CONVERSATION_ID" >/dev/null 2>&1 &
disown 2>/dev/null || true
exit 0
