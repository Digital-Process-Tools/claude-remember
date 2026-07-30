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
source "$(dirname "$0")/lib-staging-lock.sh"
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
# staging.lock is released explicitly after the retire loop; the trap covers
# the case where the script dies inside it. `lock_release` returns 1 when the
# lock is not ours, which is the normal case on every other exit path.
# SNAPSHOT_DIR is set below, after the trap, so it is declared here first —
# and removed with `if`, not `[ -n ] &&`, because a failing test as the last
# command of a trap rewrites the exit status under `set -e`.
SNAPSHOT_DIR=""
trap 'staging_lock_release; lock_release "$LOCK_DIR" || true; if [ -n "$SNAPSHOT_DIR" ]; then rm -rf "$SNAPSHOT_DIR"; fi; rm -f "$REMEMBER_CONFIG"' EXIT

STAGING_DIR="${REMEMBER_DIR}"
RECENT_FILE="${STAGING_DIR}/recent.md"
ARCHIVE_FILE="${STAGING_DIR}/archive.md"

# --- Dispatch: before_consolidate ---
dispatch "before_consolidate"

# --- Snapshot staging under staging.lock (#235) ---
# The read of today-*.md lived in the same Python process as the Haiku call, so
# it could not be put under staging.lock: a critical section containing a model
# call is how #142 happened, and is why save.lock was rejected as the staging
# lock in #225. But `staging_append` is two writes — a separator, then the
# summary — and an unlocked reader landing between them consumes the blank line
# as if it were the end of the day. The span retired into .done.md then ends
# with a separator whose entry is not there, and the entry is re-consolidated on
# a later round under a later timestamp, attributed to the day it was
# re-consumed rather than the day it was written, with nothing to say so.
#
# So end the critical section at a process boundary instead of widening it past
# the model call — #224's shape, one file over. Copy the eligible files under
# the lock, release, then read the copies. That copy IS the "snapshot" option
# the issue offers as an alternative: in this layout the two are the same
# change, because the lock lives in bash and the read lives in Python, so
# holding the lock across the read but not the model call requires the read to
# end at a process boundary and the bytes to cross it on disk.
#
# Cleanup: the directory is ours alone (consolidation.lock is held for the whole
# script) and the EXIT trap removes it on every path, including the two failure
# exits below. A SIGKILL leaves one behind holding a copy of bytes that also
# still exist in staging — no recovery is owed, so the sweep is tidiness.
rm -rf "${REMEMBER_DIR}"/tmp/consolidate-snapshot-* 2>/dev/null || true
SNAPSHOT_DIR=$(mktemp -d "${REMEMBER_DIR}/tmp/consolidate-snapshot-XXXXXX")
# Losing this wait costs a round and nothing else: nothing has been read, so
# staging, recent.md and archive.md are all exactly as they were and the next
# run consolidates the same span. That is strictly cheaper than the retire
# loop's lost-wait branch below, which has already rewritten memory and so
# leaves a duplicate for the merge prompt to dedupe.
if ! staging_lock_acquire "$STAGING_LOCK_TIMEOUT"; then
    log "consolidation" "staging.lock held for the whole ${STAGING_LOCK_TIMEOUT}s wait — nothing read and nothing consolidated; staging, recent.md and archive.md are untouched and the next run picks up the same span (an NDC append may be half applied right now, and consuming its separator without its summary retires a blank line and defers the entry to a later day)"
    exit 0
fi
if ! SNAPSHOT_OUT=$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell consolidate-snapshot "$STAGING_DIR" "$SNAPSHOT_DIR" 2>&1); then
    staging_lock_release
    log "consolidation" "ERROR: staging snapshot failed — $SNAPSHOT_OUT"
    exit 1
fi
staging_lock_release

# --- Consolidate ---
# Python does: read the snapshot taken above, build prompt,
# call Haiku (text-only), parse structured response into recent/archive.
# Oversized-prompt skip-guard: cap the assembled prompt so a runaway staging/
# archive never overflows Haiku's window (skips cleanly instead of crashing).
CONSOLIDATE_MAX_BYTES=$(config ".thresholds.consolidate_max_bytes" 600000)
log "consolidation" "start"
RESULT=$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell consolidate "$STAGING_DIR" "$RECENT_FILE" "$ARCHIVE_FILE" "$CONSOLIDATE_MAX_BYTES" "$SNAPSHOT_DIR" 2>&1) || {
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
# Under staging.lock for the whole loop (#225). The consumed-byte count above
# closes the 180s window around the Haiku call — an append landing there is
# kept as the tail. It does NOT close this loop: `wc -c`, `head`, `tail` and
# `mv` are four separate operations, and an NDC append landing among them goes
# into the inode this loop is about to rename to .done.md. Nothing globs those
# again and session start never injects them, so the entry is unreachable and
# no line is logged — #142's shape, in milliseconds instead of minutes, one
# file over. save.lock could not serve here: save-session.sh holds it across
# its own summarize call (#226), so waiting on it would mean waiting behind a
# model call. See lib-staging-lock.sh.
#
# Losing the wait leaves staging exactly as it is. recent.md/archive.md
# already hold this span, so the next run consolidates it again and the merge
# prompt dedupes it — a visible duplicate, chosen over sealing a concurrent
# append into a file nothing reads. Same trade as the NDC tail-failure branch.
if ! staging_lock_acquire "$STAGING_LOCK_TIMEOUT"; then
    log "consolidation" "ERROR: staging.lock held for the whole ${STAGING_LOCK_TIMEOUT}s wait — staging files NOT retired; recent.md/archive.md already hold this span so the next run re-consolidates it (a duplicate the merge dedupes, chosen over sealing a concurrent append inside .done.md)"
    rm -f "$STAGING_PATHS_FILE"
    exit 0
fi
while IFS= read -r -d '' staging_path && IFS= read -r -d '' staging_consumed; do
    if [ ! -f "$staging_path" ]; then
        log "consolidation" "WARN: $(basename "$staging_path") disappeared"
        continue
    fi
    case "$staging_consumed" in (''|*[!0-9]*) staging_consumed=0 ;; esac
    staging_now=$(wc -c < "$staging_path" | tr -d ' ')
    staging_done="${staging_path%.md}.done.md"

    if [ "$staging_consumed" -gt 0 ] && [ "$staging_now" -gt "$staging_consumed" ]; then
        # Sibling of staging_path (#246), not $TMPDIR — #242's class, one file
        # over. Across filesystems mv is copy-then-unlink, not rename(2), and a
        # failure partway leaves staging_path destroyed or truncated instead of
        # "old or new". $TMPDIR is a different filesystem in the ordinary cases
        # (tmpfs /tmp, devcontainers, WSL under /mnt/c, external data_dir). A
        # crash between mktemp and mv leaves a stray sibling; sweep it first,
        # same as #245 — inert (name does not end in .md, so nothing globs it)
        # but would accumulate one per failure otherwise.
        rm -f "${staging_path}".tail-* 2>/dev/null
        staging_tail=$(mktemp "${staging_path}.tail-XXXXXX")
        if head -c "$staging_consumed" "$staging_path" > "$staging_done" 2>/dev/null &&
           tail -c +$(( staging_consumed + 1 )) "$staging_path" > "$staging_tail" 2>/dev/null; then
            # Checked, not run bare under set -e: an unchecked failure here used
            # to kill the whole script mid-loop, abandoning every other staging
            # file in STAGING_PATHS_FILE rather than handling just this one.
            if STAGING_MV_ERR=$(mv "$staging_tail" "$staging_path" 2>&1); then
                log "consolidation" "kept $(( staging_now - staging_consumed ))b appended to $(basename "$staging_path") during consolidation"
            else
                # staging_path is untouched: same-directory mv is a real
                # rename, which cannot fail partway (#246). staging_done
                # already holds the consumed prefix — harmless, it is
                # overwritten identically when this file is retried. Leaving
                # staging_path in place means the next run re-reads it whole
                # and redoes the split; the consumed span becomes a duplicate
                # in recent.md/archive.md that the merge dedupes, chosen over
                # losing the tail appended during this consolidation.
                rm -f "$staging_tail"
                log "consolidation" "ERROR: could not keep the tail of $(basename "$staging_path") appended during consolidation — left in place for the next run to retry: ${STAGING_MV_ERR}"
            fi
        else
            rm -f "$staging_tail"
            if ! STAGING_MV_ERR=$(mv "$staging_path" "$staging_done" 2>&1); then
                log "consolidation" "ERROR: could not retire $(basename "$staging_path") to .done.md — left in place, the next run will see it as unconsolidated: ${STAGING_MV_ERR}"
            fi
        fi
    else
        if ! STAGING_MV_ERR=$(mv "$staging_path" "$staging_done" 2>&1); then
            log "consolidation" "ERROR: could not retire $(basename "$staging_path") to .done.md — left in place, the next run will see it as unconsolidated: ${STAGING_MV_ERR}"
        fi
    fi
done < "$STAGING_PATHS_FILE"
staging_lock_release
rm -f "$STAGING_PATHS_FILE"

log "consolidation" "done: ${STAGING_COUNT} files consolidated"

# --- Dispatch: after_consolidate ---
dispatch "after_consolidate"
