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
source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/bootstrap-dirs.sh"
source "$(dirname "$0")/detect-tools.sh"
PLUGIN_ROOT="$PIPELINE_DIR"
PROJECT="$PROJECT_DIR"
source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
log "hook" "post-tool: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON"
SAVE_SCRIPT="$PLUGIN_ROOT/scripts/save-session.sh"
LAST_SAVE_FILE="$PROJECT/.remember/tmp/last-save.json"
PID_FILE="$PROJECT/.remember/tmp/save-session.pid"
SESSION_DIR="$HOME/.claude/projects/$(session_dir_slug "$PROJECT")"

[ -f "$SAVE_SCRIPT" ] || exit 0

# --- Count JSONL lines in the current session ---
LATEST_JSONL=$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)
[ -n "$LATEST_JSONL" ] || exit 0

CURRENT_LINES=$(wc -l < "$LATEST_JSONL" | tr -d ' ')
SESSION_ID=$(basename "$LATEST_JSONL" .jsonl)

# --- Get last saved position (from last-save.json) ---
LAST_LINE=0
if [ -f "$LAST_SAVE_FILE" ]; then
    SAVED_SESSION=$($PYTHON - "$LAST_SAVE_FILE" <<'PYEOF'
import sys, json
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('session', ''))
except Exception:
    print('')
PYEOF
    )
    if [ "$SAVED_SESSION" = "$SESSION_ID" ]; then
        LAST_LINE=$($PYTHON - "$LAST_SAVE_FILE" <<'PYEOF'
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
DELTA_THRESHOLD=$(config ".thresholds.delta_lines_trigger" 50)
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
        nohup "$SAVE_SCRIPT" "$SESSION_ID" > "$PROJECT/.remember/logs/autonomous/save-$(_remember_date +%H%M%S).log" 2>&1 &
        echo $! > "$PID_FILE"
        SAVE_TRIGGERED="true"
    fi
fi

# --- Dispatch: after_post_tool ---
export REMEMBER_SAVE_TRIGGERED="$SAVE_TRIGGERED"
dispatch "after_post_tool"
