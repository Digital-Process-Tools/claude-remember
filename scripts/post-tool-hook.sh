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
# Opt into resolve-paths.sh's soft-failure mode — see the comment in
# session-start-hook.sh. This hook must never block the agent, so a resolution
# failure (e.g. a nested/headless session with no CLAUDE_PROJECT_DIR) is a
# silent no-op, not a crash.
REMEMBER_PATHS_SOFT_FAIL=1 source "$(dirname "$0")/resolve-paths.sh" || exit 0
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/bootstrap-dirs.sh"
PLUGIN_ROOT="$PIPELINE_DIR"
PROJECT="$PROJECT_DIR"
source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
log "hook" "post-tool: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON REMEMBER_DIR=$REMEMBER_DIR"
SAVE_SCRIPT="$PLUGIN_ROOT/scripts/save-session.sh"
LAST_SAVE_FILE="$REMEMBER_DIR/tmp/last-save.json"
PID_FILE="$REMEMBER_DIR/tmp/save-session.pid"
SESSION_DIR="$HOME/.claude/projects/$(session_dir_slug "$PROJECT")"

[ -f "$SAVE_SCRIPT" ] || exit 0

# --- Count JSONL lines in the current session ---
# A slug that does not match the directory Claude Code actually created leaves
# nothing to read here, and the whole pipeline no-ops for the life of the
# session. That used to be a bare `exit 0`, so the only symptom was memory
# quietly never appearing (issue #144).
#
# Say so instead — but on a timer, not once ever. This runs on every tool call,
# so a line per call would bury the logs; a plain once-only sentinel is worse
# though, because the cause here is environmental (a mis-slugged path stays
# mis-slugged), so the warning would fire once and every later session would be
# silent again — the very failure being fixed. An hourly repeat keeps a
# persistent cause visible without flooding.
NOTICE_TTL=3600
LATEST_JSONL=$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)
if [ -z "$LATEST_JSONL" ]; then
    NOTICE_MARKER="$REMEMBER_DIR/tmp/no-transcript-notice"
    NOTICE_LAST=0
    if [ -f "$NOTICE_MARKER" ]; then
        NOTICE_LAST=$(cat "$NOTICE_MARKER" 2>/dev/null || echo 0)
        case "$NOTICE_LAST" in ''|*[!0-9]*) NOTICE_LAST=0 ;; esac
    fi
    if [ $(( $(_remember_date +%s) - NOTICE_LAST )) -ge "$NOTICE_TTL" ]; then
        mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null
        _remember_date +%s > "$NOTICE_MARKER" 2>/dev/null
        if [ -d "$SESSION_DIR" ]; then
            log "hook" "no .jsonl transcript in $SESSION_DIR — nothing to save yet"
        else
            log "hook" "WARNING: no session dir for this project: $SESSION_DIR (slug of $PROJECT). Memory cannot save until it matches a directory under $HOME/.claude/projects/"
        fi
    fi
    exit 0
fi
rm -f "$REMEMBER_DIR/tmp/no-transcript-notice" 2>/dev/null

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

# --- Don't fork a save that save-session.sh will only discard on cooldown ---
# The delta throttle above keys on save *position* (last-save.json), which is
# only written after a successful save. Until the first save lands LAST_LINE is
# 0, so DELTA is the whole transcript and always clears the threshold — and in
# an agentic session (many tool calls, few human turns) the min-human gate keeps
# that first save from ever landing, so the hook would fork a save-session.sh on
# every tool call for the whole session (issue #125). save-session.sh already
# rate-limits on the last-save-ts cooldown marker; consult it here so the fork
# is skipped entirely instead of spawned only to be discarded milliseconds later.
IN_COOLDOWN=false
COOLDOWN_MARKER="$REMEMBER_DIR/tmp/last-save-ts"
if [ -f "$COOLDOWN_MARKER" ]; then
    LAST_TS=$(cat "$COOLDOWN_MARKER" 2>/dev/null || echo 0)
    case "$LAST_TS" in ''|*[!0-9]*) LAST_TS=0 ;; esac
    SAVE_COOLDOWN=$(config ".cooldowns.save_seconds" 120)
    case "$SAVE_COOLDOWN" in ''|*[!0-9]*) SAVE_COOLDOWN=120 ;; esac
    [ $(( $(_remember_date +%s) - LAST_TS )) -lt "$SAVE_COOLDOWN" ] && IN_COOLDOWN=true
fi

# --- Fire save if delta exceeds threshold and no save already running ---
DELTA_THRESHOLD=$(config ".thresholds.delta_lines_trigger" 50)
if [ "$DELTA" -gt "$DELTA_THRESHOLD" ] && [ "$IN_COOLDOWN" = false ]; then
    ALREADY_RUNNING=false
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
        if kill -0 "$OLD_PID" 2>/dev/null; then
            ALREADY_RUNNING=true
        fi
    fi

    if [ "$ALREADY_RUNNING" = false ]; then
        mkdir -p "$REMEMBER_DIR/logs/autonomous"
        nohup "$SAVE_SCRIPT" "$SESSION_ID" > "$REMEMBER_DIR/logs/autonomous/save-$(_remember_date +%H%M%S).log" 2>&1 &
        echo $! > "$PID_FILE"
        SAVE_TRIGGERED="true"
    fi
fi

# --- Dispatch: after_post_tool ---
export REMEMBER_SAVE_TRIGGERED="$SAVE_TRIGGERED"
dispatch "after_post_tool"
