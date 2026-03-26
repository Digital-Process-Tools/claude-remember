#!/bin/bash
# ============================================================================
# post-tool-hook.sh — PostToolUse hook for the Remember plugin
# ============================================================================
#
# DESCRIPTION
#   Fires after every Claude Code tool call. Counts new JSONL lines since the
#   last save, and when the delta exceeds a threshold (default 50 lines),
#   launches save-session.sh in the background. Also outputs a team memory
#   nudge as additionalContext to remind the agent to log team-worthy knowledge.
#
# USAGE
#   Called automatically by Claude Code's PostToolUse hook system.
#   Not intended for manual invocation.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT   Plugin install directory (set by Claude Code)
#   CLAUDE_PROJECT_DIR   Project root (default: .)
#
# DEPENDENCIES
#   python3 (for JSON parsing of last-save.json)
#   jq (for reading config.json threshold)
#   save-session.sh (launched in background when threshold met)
#
# EXIT CODES
#   0   Always (hook must not block the agent)
#
# ============================================================================

# --- Resolve paths ---
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${CLAUDE_PROJECT_DIR:-.}/.claude/remember}"
PROJECT="${CLAUDE_PROJECT_DIR:-.}"
PROJECT_DIR="$PROJECT"
export CLAUDE_PROJECT_DIR="$PROJECT"
export CLAUDE_PLUGIN_ROOT="$PLUGIN_ROOT"
source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
SAVE_SCRIPT="$PLUGIN_ROOT/scripts/save-session.sh"
LAST_SAVE_FILE="$PROJECT/.remember/tmp/last-save.json"
PID_FILE="$PROJECT/.remember/tmp/save-session.pid"
SESSION_DIR="$HOME/.claude/projects/$(echo "$PROJECT" | tr '/' '-')"

[ -f "$SAVE_SCRIPT" ] || exit 0

# --- Count JSONL lines in the current session ---
LATEST_JSONL=$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)
[ -n "$LATEST_JSONL" ] || exit 0

CURRENT_LINES=$(wc -l < "$LATEST_JSONL" | tr -d ' ')
SESSION_ID=$(basename "$LATEST_JSONL" .jsonl)

# --- Get last saved position (from last-save.json) ---
LAST_LINE=0
if [ -f "$LAST_SAVE_FILE" ]; then
    SAVED_SESSION=$(python3 - "$LAST_SAVE_FILE" <<'PYEOF'
import sys, json
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('session', ''))
except Exception:
    print('')
PYEOF
    )
    if [ "$SAVED_SESSION" = "$SESSION_ID" ]; then
        LAST_LINE=$(python3 - "$LAST_SAVE_FILE" <<'PYEOF'
import sys, json
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('line', 0))
except Exception:
    print(0)
PYEOF
        )
    fi
fi

DELTA=$((CURRENT_LINES - LAST_LINE))
SAVE_TRIGGERED=""

# --- Fire save if delta exceeds threshold and no save already running ---
DELTA_THRESHOLD=$(jq -r '.thresholds.delta_lines_trigger // 50' "$PLUGIN_ROOT/config.json" 2>/dev/null || echo 50)
if [ "$DELTA" -gt "$DELTA_THRESHOLD" ]; then
    ALREADY_RUNNING=false
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
        if kill -0 "$OLD_PID" 2>/dev/null; then
            ALREADY_RUNNING=true
        fi
    fi

    if [ "$ALREADY_RUNNING" = false ]; then
        mkdir -p "$PROJECT/.remember/logs/autonomous"
        nohup "$SAVE_SCRIPT" "$SESSION_ID" > "$PROJECT/.remember/logs/autonomous/save-$(date +%H%M%S).log" 2>&1 &
        echo $! > "$PID_FILE"
        SAVE_TRIGGERED="true"
    fi
fi

# --- Dispatch: after_post_tool ---
export REMEMBER_SAVE_TRIGGERED="$SAVE_TRIGGERED"
dispatch "after_post_tool"
