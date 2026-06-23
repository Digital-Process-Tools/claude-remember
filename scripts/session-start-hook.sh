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
#   Sections: === HANDOFF ===, === MEMORY ===, === MEMORY CONSOLIDATION ===
#   hooks.d/ listeners may add their own (e.g., === TEAM ===).
#
# ============================================================================

source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/bootstrap-dirs.sh"
PLUGIN_ROOT="$PIPELINE_DIR"
PROJECT="$PROJECT_DIR"
source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
# log.sh is sourced with stderr suppressed; a silent failure (e.g. read-only
# mount where log.sh `return 1`s) would leave _remember_date / log / dispatch
# undefined, crashing later with a cryptic `command not found`. Surface a clear
# diagnostic up front. Exit 127 (command-missing) to match the degraded-env
# contract that tolerates rc in (0, 127), not a bare 1.
if ! command -v _remember_date >/dev/null 2>&1; then
    echo "session-start-hook: ERROR — failed to source $PLUGIN_ROOT/scripts/log.sh" >&2
    exit 127
fi
TODAY=$(_remember_date '+%Y-%m-%d')
log "hook" "session-start: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR REMEMBER_DIR=$REMEMBER_DIR"

# ── Dispatch: before_session_start ────────────────────────────────────────
dispatch "before_session_start"

# ── Cleanup + health check ─────────────────────────────────────────────────
rm -f "$REMEMBER_DIR/tmp/save-session.pid"

# ── Recovery: save the most recent missed session ──────────────────────────
if [ "$(config '.features.recovery' true)" = "true" ]; then
PROJECT_PATH_SLUG="$(session_dir_slug "$PROJECT")"
SESSIONS_DIR="$HOME/.claude/projects/${PROJECT_PATH_SLUG}"
LAST_SAVE_FILE="$REMEMBER_DIR/tmp/last-save.json"

if [ -d "$SESSIONS_DIR" ] && [ -f "$LAST_SAVE_FILE" ]; then
    SAVED_ID=$($JQ -r '.session // ""' "$LAST_SAVE_FILE" 2>/dev/null)
    LAST_JSONL=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null | tail -n +2 | head -1)
    if [ -n "$LAST_JSONL" ]; then
        LAST_ID=$(basename "$LAST_JSONL" .jsonl)
        if [ "$LAST_ID" != "$SAVED_ID" ]; then
            "$PLUGIN_ROOT/scripts/save-session.sh" "$LAST_ID" --force </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true
        fi
    fi
fi
fi

# ── Identity: per-project → user-global → plugin-bundled ──────────────────
# User-global tier: <REMEMBER_ROOT>/identity.md (external mode only).
# In legacy mode REMEMBER_ROOT == PROJECT_DIR, so we skip it there.
REMEMBER_ROOT=$(dirname "$REMEMBER_DIR")
if [ -f "$REMEMBER_DIR/identity.md" ]; then
    IDENTITY_FILE="$REMEMBER_DIR/identity.md"
elif [ -f "$REMEMBER_ROOT/identity.md" ] && [ "$REMEMBER_ROOT" != "$PROJECT_DIR" ]; then
    IDENTITY_FILE="$REMEMBER_ROOT/identity.md"
else
    IDENTITY_FILE="$PLUGIN_ROOT/identity.md"
fi

CORE_MEMORIES="$REMEMBER_DIR/core-memories.md"
REMEMBER_RECENT="$REMEMBER_DIR/recent.md"
REMEMBER_ARCHIVE="$REMEMBER_DIR/archive.md"
REMEMBER_HANDOFF="$REMEMBER_DIR/remember.md"
REMEMBER_NOW="$REMEMBER_DIR/now.md"
REMEMBER_TODAY_FILE="$REMEMBER_DIR/today-${TODAY}.md"

# ── Handoff path hint (consumed by the /remember skill) ───────────────────
# Emitted only in external mode. In legacy mode REMEMBER_HANDOFF resolves to
# {project}/.remember/remember.md — the exact path the skill defaults to when
# no === HANDOFF === block is present, so the hint would be pure noise.
if [ "$REMEMBER_ROOT" != "$PROJECT_DIR" ]; then
    echo "=== HANDOFF ==="
    echo "Write next handoff to: $REMEMBER_HANDOFF"
    echo ""
fi

# ── Last handoff (injected FIRST so it survives context-preview truncation) ─
# The session-start output can be large; the harness may deliver only a leading
# preview to the agent. Emit the previous session's handoff up top — before
# identity/memory — so it always lands in context. Read once, then consume.
if [ -f "$REMEMBER_HANDOFF" ] && [ -s "$REMEMBER_HANDOFF" ]; then
    echo "=== LAST HANDOFF ==="
    cat "$REMEMBER_HANDOFF"
    echo ""
    : > "$REMEMBER_HANDOFF"
fi

# ── History hint ───────────────────────────────────────────────────────────
cat "$PLUGIN_ROOT/prompts/session-history-hint.txt" 2>/dev/null
echo ""

# ── Inject memory into context ────────────────────────────────────────────
HAS_MEMORY=""
for MFILE in "$IDENTITY_FILE" "$CORE_MEMORIES" "$REMEMBER_TODAY_FILE" "$REMEMBER_NOW" "$REMEMBER_RECENT" "$REMEMBER_ARCHIVE"; do
    if [ -f "$MFILE" ]; then
        HAS_MEMORY="true"
    fi
done

if [ -n "$HAS_MEMORY" ]; then
    echo "=== MEMORY ==="
    for MFILE in "$IDENTITY_FILE" "$CORE_MEMORIES" "$REMEMBER_TODAY_FILE" "$REMEMBER_NOW" "$REMEMBER_RECENT" "$REMEMBER_ARCHIVE"; do
        if [ -f "$MFILE" ] && [ -s "$MFILE" ]; then
            BASENAME=$(basename "$MFILE")
            echo "--- $BASENAME ---"
            cat "$MFILE"
            echo ""
        fi
    done
    echo ""
fi

# ── Consolidation trigger ─────────────────────────────────────────────────
# If past-day staging files exist, compress them in the background.
STAGING_COUNT=$(ls "$REMEMBER_DIR/today-"*.md 2>/dev/null | grep -v "today-${TODAY}.md" | grep -v "\.done\.md" | wc -l | tr -d ' ')
if [ "$STAGING_COUNT" -gt 0 ]; then
    echo "=== MEMORY CONSOLIDATION ==="
    echo "$STAGING_COUNT day(s) of memory to compress. Running consolidation in background..."
    nohup "$PLUGIN_ROOT/scripts/run-consolidation.sh" </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true
    echo ""
fi

# ── Dispatch: after_session_start ────────────────────────────────────────
# Plugins register here via hooks.d/after_session_start/
# e.g., team-memory hook injects === TEAM === section
dispatch "after_session_start"
