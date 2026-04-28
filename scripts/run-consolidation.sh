#!/bin/bash
# ============================================================================
# run-consolidation.sh — Compress staging memory into recent + archive
# ============================================================================
#
# DESCRIPTION
#   Merges past-day staging files (today-YYYY-MM-DD.md) into two long-lived
#   memory files: recent.md (last ~7 days, detailed) and archive.md (older,
#   compressed). Uses Haiku to intelligently merge and deduplicate entries.
#   Staging files are renamed to .done.md after successful processing.
#
# USAGE
#   run-consolidation.sh    # no arguments needed
#
# ENVIRONMENT
#   CLAUDE_PROJECT_DIR   Project root (set by Claude Code; falls back to path traversal)
#   CLAUDE_PLUGIN_ROOT   Plugin install directory (set by Claude Code)
#
# DEPENDENCIES
#   python3, claude CLI (Haiku)
#   Sources: log.sh (logging, safe_eval, rotate_logs)
#   Python: pipeline.shell (consolidate)
#
# EXIT CODES
#   0   Success, or no staging files to process, or lock held by another process
#   1   python3 not found or pipeline error
#
# ARCHITECTURE
#   Shell handles atomic locking (noclobber), log rotation, and file renames.
#   Python (pipeline/) reads staging files, builds a consolidation prompt,
#   calls Haiku (text-only, no file tools), and parses the structured
#   response into separate recent/archive sections.
#
# ============================================================================

set -e

source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/log.sh"
log "hook" "run-consolidation: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON"
rotate_logs

# --- Lock (atomic via noclobber) ---
LOCK_FILE="${PROJECT_DIR}/.remember/tmp/consolidation.lock"
if ! ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        log "consolidation" "locked by PID $LOCK_PID, skip"; exit 0
    fi
    log "consolidation" "stale lock (PID $LOCK_PID dead), taking over"
    echo $$ > "$LOCK_FILE"
fi
trap 'rm -f "$LOCK_FILE"' EXIT

STAGING_DIR="${PROJECT_DIR}/.remember"
RECENT_FILE="${STAGING_DIR}/recent.md"
ARCHIVE_FILE="${STAGING_DIR}/archive.md"

# --- Dispatch: before_consolidate ---
dispatch "before_consolidate"

# --- Consolidate ---
# Python does: find staging files, read all content, build prompt,
# call Haiku (text-only), parse structured response into recent/archive.
log "consolidation" "start"
RESULT=$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell consolidate "$STAGING_DIR" "$RECENT_FILE" "$ARCHIVE_FILE" 2>&1) || {
    log "consolidation" "ERROR: pipeline failed — $RESULT"
    exit 1
}

# eval sets: STAGING_COUNT, RECENT_OUT, ARCHIVE_OUT, TK_IN/OUT/CACHE/COST
safe_eval <<< "$(echo "$RESULT" | grep -v '^STAGING=')"

if [ "${STAGING_COUNT:-0}" -eq 0 ]; then
    log "consolidation" "no staging files"; exit 0
fi

# --- Write output ---
cp "$RECENT_OUT" "$RECENT_FILE"
cp "$ARCHIVE_OUT" "$ARCHIVE_FILE"
rm -f "$RECENT_OUT" "$ARCHIVE_OUT"

log_tokens "consolidation" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"

# --- Rename processed staging files → .done.md ---
while IFS= read -r line; do
    staging_path=$(echo "$line" | sed "s/^STAGING=//;s/^'//;s/'$//")
    if [ -f "$staging_path" ]; then
        mv "$staging_path" "${staging_path%.md}.done.md"
    else
        log "consolidation" "WARN: $(basename "$staging_path") disappeared"
    fi
done <<< "$(echo "$RESULT" | grep '^STAGING=')"

log "consolidation" "done: ${STAGING_COUNT} files consolidated"

# --- Dispatch: after_consolidate ---
dispatch "after_consolidate"
