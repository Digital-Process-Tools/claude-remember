#!/bin/bash
# ============================================================================
# session-start-hook.sh — SessionStart hook for the Remember plugin
# ============================================================================
#
# DESCRIPTION
#   Runs at the beginning of every Claude Code session. Performs three jobs:
#   1. Injects memory files (identity, core memories, today, now, recent,
#      archive) into the session context via stdout.
#   2. Recovers the most recent missed session by launching save-session.sh
#      with --force in the background.
#   3. Triggers background maintenance: consolidation of past-day staging
#      files and team memory digest refresh.
#   4. Dispatches before_session_start / after_session_start via hooks.d/.
#
# USAGE
#   Called automatically by Claude Code's SessionStart hook system.
#   Not intended for manual invocation.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT   Plugin install directory (set by Claude Code)
#   CLAUDE_PROJECT_DIR   Project root (default: .)
#
# DEPENDENCIES
#   jq (for config.json reading)
#   save-session.sh (for session recovery)
#   run-consolidation.sh (for staging compression)
#   log.sh (for dispatch via hooks.d/)
#
# EXIT CODES
#   0   Always (hook must not block session startup)
#
# OUTPUT
#   Prints memory content to stdout for injection into session context.
#   Sections: === MEMORY ===, === MEMORY CONSOLIDATION ===
#   hooks.d/ listeners may add their own (e.g., === TEAM ===).
#
# ============================================================================

source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/detect-tools.sh"
PLUGIN_ROOT="$PIPELINE_DIR"
PROJECT="$PROJECT_DIR"
CONFIG="$PLUGIN_ROOT/config.json"
# Read a config value from config.json. Falls back to $2 if missing.
cfg() { $JQ -r "$1 // empty" "$CONFIG" 2>/dev/null || echo "$2"; }
source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
REMEMBER_TZ=$(cfg ".timezone" "Europe/Paris")
TODAY=$(TZ="$REMEMBER_TZ" date '+%Y-%m-%d')
log "hook" "session-start: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR"

# ── Dispatch: before_session_start ────────────────────────────────────────
dispatch "before_session_start"

# ── Cleanup + health check ─────────────────────────────────────────────────
rm -f "$PROJECT/.remember/tmp/save-session.pid"
for DIR in "$PROJECT/.remember/tmp" "$PROJECT/.remember/logs" "$PROJECT/.remember/logs/autonomous"; do
    mkdir -p "$DIR" 2>/dev/null
done
[ -f "$PROJECT/.remember/.gitignore" ] || echo '*' > "$PROJECT/.remember/.gitignore"

# ── Recovery: save the most recent missed session ──────────────────────────
if [ "$(cfg '.features.recovery' true)" = "true" ]; then
PROJECT_PATH_SLUG="$(session_dir_slug "$PROJECT")"
SESSIONS_DIR="$HOME/.claude/projects/${PROJECT_PATH_SLUG}"
LAST_SAVE_FILE="$PROJECT/.remember/tmp/last-save.json"

if [ -d "$SESSIONS_DIR" ] && [ -f "$LAST_SAVE_FILE" ]; then
    SAVED_ID=$($JQ -r '.session // ""' "$LAST_SAVE_FILE" 2>/dev/null)
    LAST_JSONL=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null | tail -n +2 | head -1)
    if [ -n "$LAST_JSONL" ]; then
        LAST_ID=$(basename "$LAST_JSONL" .jsonl)
        if [ "$LAST_ID" != "$SAVED_ID" ]; then
            "$PLUGIN_ROOT/scripts/save-session.sh" "$LAST_ID" --force 2>/dev/null &
        fi
    fi
fi
fi

IDENTITY_FILE="$PLUGIN_ROOT/identity.md"
CORE_MEMORIES="$PROJECT/.remember/core-memories.md"
REMEMBER_RECENT="$PROJECT/.remember/recent.md"
REMEMBER_ARCHIVE="$PROJECT/.remember/archive.md"
REMEMBER_HANDOFF="$PROJECT/.remember/remember.md"
REMEMBER_NOW="$PROJECT/.remember/now.md"
REMEMBER_TODAY_FILE="$PROJECT/.remember/today-${TODAY}.md"

# ── History hint ───────────────────────────────────────────────────────────
cat "$PLUGIN_ROOT/prompts/session-history-hint.txt" 2>/dev/null
echo ""

# ── Inject memory into context ────────────────────────────────────────────
HAS_MEMORY=""
for MFILE in "$IDENTITY_FILE" "$CORE_MEMORIES" "$REMEMBER_HANDOFF" "$REMEMBER_TODAY_FILE" "$REMEMBER_NOW" "$REMEMBER_RECENT" "$REMEMBER_ARCHIVE"; do
    if [ -f "$MFILE" ]; then
        HAS_MEMORY="true"
    fi
done

if [ -n "$HAS_MEMORY" ]; then
    echo "=== MEMORY ==="
    for MFILE in "$IDENTITY_FILE" "$CORE_MEMORIES" "$REMEMBER_HANDOFF" "$REMEMBER_TODAY_FILE" "$REMEMBER_NOW" "$REMEMBER_RECENT" "$REMEMBER_ARCHIVE"; do
        if [ -f "$MFILE" ] && [ -s "$MFILE" ]; then
            BASENAME=$(basename "$MFILE")
            echo "--- $BASENAME ---"
            cat "$MFILE"
            echo ""
        fi
    done
    echo ""
    # Consume handoff — one-shot briefing, read once then cleared
    if [ -f "$REMEMBER_HANDOFF" ] && [ -s "$REMEMBER_HANDOFF" ]; then
        : > "$REMEMBER_HANDOFF"
    fi
fi

# ── Consolidation trigger ─────────────────────────────────────────────────
# If past-day staging files exist, compress them in the background.
STAGING_COUNT=$(ls "$PROJECT/.remember/today-"*.md 2>/dev/null | grep -v "today-${TODAY}.md" | grep -v "\.done\.md" | wc -l | tr -d ' ')
if [ "$STAGING_COUNT" -gt 0 ]; then
    echo "=== MEMORY CONSOLIDATION ==="
    echo "$STAGING_COUNT day(s) of memory to compress. Running consolidation in background..."
    nohup "$PLUGIN_ROOT/scripts/run-consolidation.sh" > /dev/null 2>&1 &
    echo ""
fi

# ── Dispatch: after_session_start ────────────────────────────────────────
# Plugins register here via hooks.d/after_session_start/
# e.g., team-memory hook injects === TEAM === section
dispatch "after_session_start"
