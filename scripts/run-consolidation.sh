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
source "$(dirname "$0")/bootstrap-dirs.sh"
source "$(dirname "$0")/log.sh"
source "$(dirname "$0")/lib-lock.sh"
log "hook" "run-consolidation: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON REMEMBER_DIR=$REMEMBER_DIR"
rotate_logs

# --- Lock (mkdir acquisition, rename-based stale takeover — see lib-lock.sh) ---
# Third call site of the same rule (#182): the noclobber pidfile this replaced
# had the same non-atomic `dead PID -> overwrite` takeover as save.lock, so
# several consolidations could each declare themselves the new holder.
LOCK_DIR="${REMEMBER_DIR}/tmp/consolidation.lock"
if ! lock_acquire "$LOCK_DIR" 0; then
    log "consolidation" "another consolidation holds the lock, skip"; exit 0
fi
# This trap REPLACES the cleanup trap lib-memory-dir.sh installed for
# $REMEMBER_CONFIG (bash keeps a single EXIT trap), so remove it here too.
# Installed only after acquisition, so it can only ever release our own lock.
# `|| true` on the release: it can return 1, and under `set -e` that would
# abort the trap before $REMEMBER_CONFIG is cleaned and rewrite the exit status.
trap 'lock_release "$LOCK_DIR" || true; rm -f "$REMEMBER_CONFIG"' EXIT

STAGING_DIR="${REMEMBER_DIR}"
RECENT_FILE="${STAGING_DIR}/recent.md"
ARCHIVE_FILE="${STAGING_DIR}/archive.md"

# --- Dispatch: before_consolidate ---
dispatch "before_consolidate"

# --- Consolidate ---
# Python does: find staging files, read all content, build prompt,
# call Haiku (text-only), parse structured response into recent/archive.
# Oversized-prompt skip-guard: cap the assembled prompt so a runaway staging/
# archive never overflows Haiku's window (skips cleanly instead of crashing).
CONSOLIDATE_MAX_BYTES=$(config ".thresholds.consolidate_max_bytes" 600000)
log "consolidation" "start"
RESULT=$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell consolidate "$STAGING_DIR" "$RECENT_FILE" "$ARCHIVE_FILE" "$CONSOLIDATE_MAX_BYTES" 2>&1) || {
    # 3 is the spawn guard declining, not a broken pipeline (#204). Staging is
    # untouched in both cases and the next run picks it up, but "declined" and
    # "failed" send an operator looking in different places.
    CONSOLIDATE_EXIT=$?
    if [ "$CONSOLIDATE_EXIT" -eq 3 ]; then
        log "consolidation" "DECLINED: $RESULT"
        exit 0
    fi
    log "consolidation" "ERROR: pipeline failed — $RESULT"
    exit 1
}

# eval sets: STAGING_COUNT, CONSOLIDATION_STATUS, RECENT_OUT, ARCHIVE_OUT, TK_IN/OUT/CACHE/COST, STAGING_PATHS_FILE
safe_eval <<< "$RESULT"

if [ "${STAGING_COUNT:-0}" -eq 0 ]; then
    log "consolidation" "no staging files"; exit 0
fi

# Skip guard: if the model declined or returned non-conforming output, the
# pipeline emits CONSOLIDATION_STATUS=skip and no RECENT_OUT/ARCHIVE_OUT.
# Do NOT overwrite memory and do NOT retire staging files — leave everything
# in place so the next run retries. (Default to ok for backward compatibility
# with any caller that does not emit the status.)
if [ "${CONSOLIDATION_STATUS:-ok}" != "ok" ]; then
    log "consolidation" "skip: status=${CONSOLIDATION_STATUS} — memory + staging files left untouched"
    exit 0
fi

# --- Write output ---
cp "$RECENT_OUT" "$RECENT_FILE"
cp "$ARCHIVE_OUT" "$ARCHIVE_FILE"
rm -f "$RECENT_OUT" "$ARCHIVE_OUT"

log_tokens "consolidation" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"

# --- Rename processed staging files → .done.md ---
# Paths are NUL-separated in STAGING_PATHS_FILE, safe for any filename.
# Records are path\0consumed_bytes\0. Retire exactly the span that was
# consolidated and keep anything appended past it — a save can land while the
# Haiku call runs (180s budget, and this script is disowned so it runs
# alongside live sessions). A blind rename sealed those bytes inside the
# .done.md, which the next run's glob skips and session start never injects.
while IFS= read -r -d '' staging_path && IFS= read -r -d '' staging_consumed; do
    if [ ! -f "$staging_path" ]; then
        log "consolidation" "WARN: $(basename "$staging_path") disappeared"
        continue
    fi
    case "$staging_consumed" in (''|*[!0-9]*) staging_consumed=0 ;; esac
    staging_now=$(wc -c < "$staging_path" | tr -d ' ')
    staging_done="${staging_path%.md}.done.md"

    if [ "$staging_consumed" -gt 0 ] && [ "$staging_now" -gt "$staging_consumed" ]; then
        staging_tail=$(mktemp "${TMPDIR:-/tmp}"/remember-staging-tail-XXXXXX)
        if head -c "$staging_consumed" "$staging_path" > "$staging_done" 2>/dev/null &&
           tail -c +$(( staging_consumed + 1 )) "$staging_path" > "$staging_tail" 2>/dev/null; then
            mv "$staging_tail" "$staging_path"
            log "consolidation" "kept $(( staging_now - staging_consumed ))b appended to $(basename "$staging_path") during consolidation"
        else
            rm -f "$staging_tail"
            mv "$staging_path" "$staging_done"
        fi
    else
        mv "$staging_path" "$staging_done"
    fi
done < "$STAGING_PATHS_FILE"
rm -f "$STAGING_PATHS_FILE"

log "consolidation" "done: ${STAGING_COUNT} files consolidated"

# --- Dispatch: after_consolidate ---
dispatch "after_consolidate"
