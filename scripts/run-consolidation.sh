#!/bin/bash

set -e

source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/bootstrap-dirs.sh"
source "$(dirname "$0")/log.sh"
source "$(dirname "$0")/lib-lock.sh"
source "$(dirname "$0")/lib-staging-lock.sh"
log "hook" "run-consolidation: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON REMEMBER_DIR=$REMEMBER_DIR"
rotate_logs || true

LOCK_DIR="${REMEMBER_DIR}/tmp/consolidation.lock"
if ! lock_acquire "$LOCK_DIR" 0; then
    log "consolidation" "another consolidation holds the lock, skip"; exit 0
fi
SNAPSHOT_DIR=""
trap 'staging_lock_release; lock_release "$LOCK_DIR" || true; if [ -n "$SNAPSHOT_DIR" ]; then rm -rf "$SNAPSHOT_DIR"; fi; rm -f "$REMEMBER_CONFIG"' EXIT

STAGING_DIR="${REMEMBER_DIR}"
RECENT_FILE="${STAGING_DIR}/recent.md"
ARCHIVE_FILE="${STAGING_DIR}/archive.md"

dispatch "before_consolidate"

_remember_consolidate_glob_dir=$(_remember_forward_slash "$REMEMBER_DIR")
rm -rf "${_remember_consolidate_glob_dir}"/tmp/consolidate-snapshot-* 2>/dev/null || true
SNAPSHOT_DIR=$(mktemp -d "${REMEMBER_DIR}/tmp/consolidate-snapshot-XXXXXX")
if ! staging_lock_acquire "$STAGING_LOCK_TIMEOUT"; then
    log "consolidation" "staging.lock held for the whole ${STAGING_LOCK_TIMEOUT}s wait -- nothing read and nothing consolidated; staging, recent.md and archive.md are untouched and the next run picks up the same span (an NDC append may be half applied right now, and consuming its separator without its summary retires a blank line and defers the entry to a later day)"
    exit 0
fi
if ! SNAPSHOT_OUT=$(cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell consolidate-snapshot "$STAGING_DIR" "$SNAPSHOT_DIR" 2>&1); then
    staging_lock_release
    log "consolidation" "ERROR: staging snapshot failed -- $SNAPSHOT_OUT"
    exit 1
fi
staging_lock_release

CONSOLIDATE_MAX_BYTES=$(config ".thresholds.consolidate_max_bytes" 600000)
case "$CONSOLIDATE_MAX_BYTES" in
    ''|*[!0-9]*)
        log "consolidation" "WARNING: thresholds.consolidate_max_bytes is not a valid non-negative integer (got '$CONSOLIDATE_MAX_BYTES') -- using default 600000"
        CONSOLIDATE_MAX_BYTES=600000
        ;;
esac
CONSOLIDATE_TIMEOUT_SECONDS=$(config ".thresholds.consolidate_timeout_seconds" 180)
case "$CONSOLIDATE_TIMEOUT_SECONDS" in
    ''|*[!0-9]*)
        log "consolidation" "WARNING: thresholds.consolidate_timeout_seconds is not a valid non-negative integer (got '$CONSOLIDATE_TIMEOUT_SECONDS') -- using default 180"
        CONSOLIDATE_TIMEOUT_SECONDS=180
        ;;
    *)
        if [ "${#CONSOLIDATE_TIMEOUT_SECONDS}" -gt 9 ]; then
            log "consolidation" "WARNING: thresholds.consolidate_timeout_seconds ($CONSOLIDATE_TIMEOUT_SECONDS) is too large and would crash the consolidation call with an OverflowError -- using default 180"
            CONSOLIDATE_TIMEOUT_SECONDS=180
        elif [ "$CONSOLIDATE_TIMEOUT_SECONDS" -eq 0 ]; then
            log "consolidation" "WARNING: thresholds.consolidate_timeout_seconds is 0, which times out the consolidation call immediately on every run -- using default 180"
            CONSOLIDATE_TIMEOUT_SECONDS=180
        fi
        ;;
esac
log "consolidation" "start"
RESULT=$(cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell consolidate "$STAGING_DIR" "$RECENT_FILE" "$ARCHIVE_FILE" "$CONSOLIDATE_MAX_BYTES" "$SNAPSHOT_DIR" "$CONSOLIDATE_TIMEOUT_SECONDS" 2>&1) || {
    CONSOLIDATE_EXIT=$?
    if [ "$CONSOLIDATE_EXIT" -eq 3 ]; then
        log "consolidation" "DECLINED: $RESULT"
        exit 0
    fi
    log "consolidation" "ERROR: pipeline failed -- $RESULT"
    exit 1
}

assign_kv <<< "$RESULT"

if [ "${STAGING_COUNT:-0}" -eq 0 ]; then
    log "consolidation" "no staging files"; exit 0
fi

if [ "${CONSOLIDATION_STATUS:-ok}" != "ok" ]; then
    log "consolidation" "skip: status=${CONSOLIDATION_STATUS} -- memory + staging files left untouched"
    exit 0
fi

cp "$RECENT_OUT" "$RECENT_FILE"
cp "$ARCHIVE_OUT" "$ARCHIVE_FILE"
rm -f "$RECENT_OUT" "$ARCHIVE_OUT"

log_usage "consolidation" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"

if ! staging_lock_acquire "$STAGING_LOCK_TIMEOUT"; then
    log "consolidation" "ERROR: staging.lock held for the whole ${STAGING_LOCK_TIMEOUT}s wait -- staging files NOT retired; recent.md/archive.md already hold this span so the next run re-consolidates it (a duplicate the merge dedupes, chosen over sealing a concurrent append inside .done.md)"
    rm -f "$STAGING_PATHS_FILE"
    exit 0
fi

retire_whole_into() {
    local src="$1" dst="$2"
    if [ -e "$dst" ]; then
        cat "$src" >> "$dst" && rm -f "$src"
    else
        mv "$src" "$dst"
    fi
}

while IFS= read -r -d '' staging_path && IFS= read -r -d '' staging_consumed; do
    if [ ! -f "$staging_path" ]; then
        log "consolidation" "WARN: $(basename "$staging_path") disappeared"
        continue
    fi
    if [ -z "$staging_consumed" ] || [ "${staging_consumed#*[!0-9]}" != "$staging_consumed" ]; then staging_consumed=0; fi
    staging_now=$(wc -c < "$staging_path" | tr -d ' ')
    staging_done="${staging_path%.md}.done.md"

    if [ "$staging_consumed" -gt 0 ] && [ "$staging_now" -gt "$staging_consumed" ]; then
        _remember_staging_rm_glob=$(_remember_forward_slash "$staging_path")
        rm -f "${_remember_staging_rm_glob}".tail-* "${_remember_staging_rm_glob}".prefix-* 2>/dev/null
        staging_tail=$(mktemp "${staging_path}.tail-XXXXXX")
        staging_prefix=$(mktemp "${staging_path}.prefix-XXXXXX")
        if head -c "$staging_consumed" "$staging_path" > "$staging_prefix" 2>/dev/null &&
           tail -c +$(( 10#$staging_consumed + 1 )) "$staging_path" > "$staging_tail" 2>/dev/null; then
            if STAGING_MV_ERR=$(retire_whole_into "$staging_prefix" "$staging_done" 2>&1); then
                if STAGING_MV_ERR=$(mv "$staging_tail" "$staging_path" 2>&1); then
                    log "consolidation" "kept $(( staging_now - 10#$staging_consumed ))b appended to $(basename "$staging_path") during consolidation"
                else
                    rm -f "$staging_tail"
                    log "consolidation" "ERROR: could not keep the tail of $(basename "$staging_path") appended during consolidation -- staging_done already gained the consumed prefix above, so a retry will duplicate it (accepted, see the comment on retire_whole_into) -- staging_path left in place for the next run to retry: ${STAGING_MV_ERR}"
                fi
            else
                rm -f "$staging_tail"
                log "consolidation" "ERROR: could not commit the consumed prefix of $(basename "$staging_path") into .done.md -- staging_path left in place for the next run to retry: ${STAGING_MV_ERR}"
            fi
        else
            rm -f "$staging_tail" "$staging_prefix"
            if ! STAGING_MV_ERR=$(retire_whole_into "$staging_path" "$staging_done" 2>&1); then
                log "consolidation" "ERROR: could not retire $(basename "$staging_path") to .done.md -- staging_path left in place, but if .done.md already existed the append itself may have already landed and only cleanup failed, so the next run may duplicate rather than freshly retire: ${STAGING_MV_ERR}"
            fi
        fi
    else
        if ! STAGING_MV_ERR=$(retire_whole_into "$staging_path" "$staging_done" 2>&1); then
            log "consolidation" "ERROR: could not retire $(basename "$staging_path") to .done.md -- staging_path left in place, but if .done.md already existed the append itself may have already landed and only cleanup failed, so the next run may duplicate rather than freshly retire: ${STAGING_MV_ERR}"
        fi
    fi
done < "$STAGING_PATHS_FILE"
staging_lock_release
rm -f "$STAGING_PATHS_FILE"

log "consolidation" "done: ${STAGING_COUNT} files consolidated"

[ -n "${PLUGIN_ROOT:-}" ] || PLUGIN_ROOT="$PIPELINE_DIR"
if source "$(dirname "$0")/lib-memory-context.sh" 2>/dev/null; then
    _remember_memory_paths
    _remember_start_cache_context_publish
fi

dispatch "after_consolidate"
