#!/bin/bash

set -e

trap 'log "error" "FAILED at line $LINENO (exit $?)"' ERR

source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/bootstrap-dirs.sh"
source "$(dirname "$0")/log.sh"
source "$(dirname "$0")/lib-lock.sh"
source "$(dirname "$0")/lib-staging-lock.sh"
log "hook" "save-session: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON REMEMBER_DIR=$REMEMBER_DIR"

LOCK_DIR="${REMEMBER_DIR}/tmp/save.lock"
NDC_COMMIT_LOCK_TIMEOUT="${REMEMBER_NDC_COMMIT_LOCK_TIMEOUT:-30}"
MEMORY_FILE="${REMEMBER_DIR}/now.md"
NDC_GEN_FILE="${REMEMBER_DIR}/tmp/ndc-generation"
ndc_read_gen() {
    if [ ! -e "$NDC_GEN_FILE" ] && [ ! -L "$NDC_GEN_FILE" ]; then
        echo 0
        return 0
    fi
    if [ ! -f "$NDC_GEN_FILE" ]; then
        echo unreadable
        return 0
    fi
    local _ndc_gen
    _ndc_gen=$(cat "$NDC_GEN_FILE" 2>/dev/null)
    if [ -z "$_ndc_gen" ] || [[ "$_ndc_gen" == *[!0-9]* ]]; then
        echo unreadable
    else
        echo "$_ndc_gen"
    fi
}
ts_marker_read() {
    local _marker="$1"
    if [ ! -e "$_marker" ] && [ ! -L "$_marker" ]; then
        echo 0
        return 0
    fi
    if [ ! -f "$_marker" ]; then
        echo unreadable
        return 0
    fi
    local _val
    _val=$(cat "$_marker" 2>/dev/null)
    if [ -z "$_val" ] || [[ "$_val" == *[!0-9]* ]]; then
        echo unreadable
    else
        echo "$_val"
    fi
}
marker_write_ok() {
    local _path="$1" _tag="$2"
    if [ -e "$_path" ] && [ ! -f "$_path" ]; then
        report_error "$_tag" "WARNING: could not write $_path -- it exists but is not a regular file, and opening it is refused (a FIFO here would block this process forever). Remove or replace it."
        return 1
    fi
    return 0
}
NOW_DAY_FILE="${REMEMBER_DIR}/tmp/now-day"
LAST_SAVE_FILE="${REMEMBER_DIR}/tmp/last-save.json"
COOLDOWN_MARKER="${REMEMBER_DIR}/tmp/last-save-ts"
TODAY_DATE=$(_remember_date +%Y-%m-%d)
CLEANUP_FILES=()
CLEANUP_FILES+=("$REMEMBER_CONFIG")

HAVE_LOCK=false

cleanup() {
    [ "$HAVE_LOCK" = true ] && { lock_release "$LOCK_DIR" || true; }
    rm -f "${CLEANUP_FILES[@]}"
}
trap cleanup EXIT

DRY_RUN=false
FORCE=false
SESSION_ID=""
for arg in "$@"; do
    if [ "$arg" = "--dry" ]; then
        DRY_RUN=true
    elif [ "$arg" = "--force" ]; then
        FORCE=true
    else
        SESSION_ID="$arg"
    fi
done

FORCE_LOCK_TIMEOUT="${REMEMBER_FORCE_LOCK_TIMEOUT:-30}"
if [ "$FORCE" = true ]; then
    if lock_acquire "$LOCK_DIR" "$FORCE_LOCK_TIMEOUT"; then
        HAVE_LOCK=true
    else
        log "lock" "ERROR: --force waited ${FORCE_LOCK_TIMEOUT}s for save.lock and another save still held it -- nothing was flushed this call"
        exit 1
    fi
else
    if lock_acquire "$LOCK_DIR" 0; then
        HAVE_LOCK=true
    else
        debug_enabled 1 && log "lock" "another save holds the lock, skipping"
        exit 0
    fi
fi

SESSION_DIR_PATH="$(claude_projects_dir)/$(session_dir_slug "$PROJECT_DIR")"
if [ -z "$SESSION_ID" ]; then
    LATEST_JSONL=$(ls -t "$SESSION_DIR_PATH"/*.jsonl 2>/dev/null | head -1)
    SESSION_ID=$(basename "$LATEST_JSONL" .jsonl)
fi

if ! [[ "$SESSION_ID" =~ ^[a-f0-9][a-f0-9-]*$ ]]; then
    log "save" "ERROR: invalid session ID: $(echo "$SESSION_ID" | head -c 40)"
    exit 1
fi

[ "$FORCE" = true ] && log "force" "bypassing cooldown + min msgs"
if [[ ( -e "$COOLDOWN_MARKER" || -L "$COOLDOWN_MARKER" ) && "$DRY_RUN" != true && "$FORCE" != true ]]; then
    LAST_MOD=$(ts_marker_read "$COOLDOWN_MARKER")
    if [ "$LAST_MOD" = "unreadable" ]; then
        report_error "cooldown" "WARNING: $COOLDOWN_MARKER exists but its value could not be used (a read failure, or content that is not a plain timestamp) -- treating the cooldown as expired and saving now. This will recur on every save until the marker holds a valid timestamp again, or is removed."
        LAST_MOD=0
    fi
    if [ -z "$LAST_MOD" ] || [ "${LAST_MOD#*[!0-9]}" != "$LAST_MOD" ]; then
        LAST_MOD=0
    fi
    ELAPSED=$(( $(date +%s) - 10#$LAST_MOD ))
    SAVE_COOLDOWN=$(config ".cooldowns.save_seconds" 120)
    if [ "$ELAPSED" -lt 0 ]; then
        report_error "cooldown" "WARNING: $COOLDOWN_MARKER is $(( 0 - ELAPSED ))s ahead of now -- the clock moved back, or the marker is corrupt in a way a digits-only check cannot see. Resetting it and saving; the cooldown resumes from now."
        if marker_write_ok "$COOLDOWN_MARKER" cooldown; then
            { date +%s > "$COOLDOWN_MARKER"; } 2>/dev/null || true
        fi
    elif [ "$ELAPSED" -lt "$SAVE_COOLDOWN" ]; then
        debug_enabled 1 && log "cooldown" "${ELAPSED}s < ${SAVE_COOLDOWN}s, skip"
        exit 0
    fi
fi

dispatch "before_save"

log "extract" "session $SESSION_ID"
assign_kv <<< "$(cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell extract "$SESSION_ID" "$PROJECT_DIR")"
if [ -z "${EXTRACT_FILE:-}" ]; then
    report_error "extract" "FAILED: the extract step produced no EXTRACT_FILE. Every variable it prints crosses into this script through assign_kv, so an absent one means the bridge lost it, not that there was nothing to extract. Stopping here rather than calling build-prompt with an empty path (#695)."
    exit 1
fi
CLEANUP_FILES+=("$EXTRACT_FILE")
if marker_write_ok "$COOLDOWN_MARKER" cooldown; then
    { date +%s > "$COOLDOWN_MARKER"; } 2>/dev/null \
        || report_error "cooldown" "WARNING: could not write $COOLDOWN_MARKER after this save -- the cooldown will not reflect it, and every future save will hit the same unreadable/unwritable marker until it is fixed or removed."
fi
if [ "$ENVELOPE" = "unrecognised" ]; then
    log "extract" "unrecognised transcript envelope, 0 exchanges read (not a quiet session -- see pipeline/host.sniff_envelope)"
else
    log "extract" "${EXCHANGE_COUNT} exchanges (${HUMAN_COUNT} human)"
fi

if [ "$EXCHANGE_COUNT" -eq 0 ]; then
    if [ "$DRY_RUN" = false ]; then
        if [ "$ENVELOPE" = "unrecognised" ]; then
            log "extract" "unrecognised envelope, skip -- position -> $POSITION (span quarantined from line $SKIP_LINES for a future build)"
            SAVE_ENVELOPE="$ENVELOPE"
        elif [ "$ENVELOPE_HAS_UNMAPPED_STEP" = "1" ]; then
            log "extract" "$ENVELOPE envelope with an unmapped step type, skip -- position -> $POSITION (span quarantined from line $SKIP_LINES for a future build)"
            SAVE_ENVELOPE="unrecognised"
        else
            log "extract" "0 exchanges, skip -- position -> $POSITION"
            SAVE_ENVELOPE="$ENVELOPE"
        fi
        cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION" "$SAVE_ENVELOPE" "$SKIP_LINES"
    else
        log "extract" "0 exchanges, skip (dry run -- position unchanged)"
    fi
    exit 0
fi

MIN_HUMAN=$(config ".thresholds.min_human_messages" 3)
MIN_EXCHANGES=$(config ".thresholds.min_exchanges_without_human" 30)
if [ -z "$MIN_HUMAN" ] || [ "${MIN_HUMAN#*[!0-9]}" != "$MIN_HUMAN" ]; then MIN_HUMAN=3; fi
if [ -z "$MIN_EXCHANGES" ] || [ "${MIN_EXCHANGES#*[!0-9]}" != "$MIN_EXCHANGES" ]; then MIN_EXCHANGES=30; fi
if [ "$HUMAN_COUNT" -lt "$MIN_HUMAN" ] && [ "$DRY_RUN" = false ] && [ "$FORCE" != true ]; then
    if [ "$MIN_EXCHANGES" -gt 0 ] && [ "$EXCHANGE_COUNT" -ge "$MIN_EXCHANGES" ]; then
        log "extract" "${HUMAN_COUNT} human < ${MIN_HUMAN} but ${EXCHANGE_COUNT} exchanges >= ${MIN_EXCHANGES}, saving (agentic session)"
    else
        log "extract" "${HUMAN_COUNT} human msgs < ${MIN_HUMAN}, skip"
        exit 0
    fi
fi

if [ "$DRY_RUN" = true ]; then
    echo ""; echo "=== DRY RUN ==="; echo ""; cat "$EXTRACT_FILE"; echo ""; exit 0
fi

NO_PREVIOUS_ENTRY="(no previous entry)"
LAST_ENTRY_UNAVAILABLE="(previous entry unavailable -- now.md could not be read; earlier work may already be recorded)"
TMP_LAST_ENTRY=$(mktemp "${TMPDIR:-/tmp}"/remember-last-entry-XXXXXX)
CLEANUP_FILES+=("$TMP_LAST_ENTRY")
if [ ! -f "$MEMORY_FILE" ]; then
    printf '%s\n' "$NO_PREVIOUS_ENTRY" > "$TMP_LAST_ENTRY"
elif [ ! -r "$MEMORY_FILE" ]; then
    printf '%s\n' "$LAST_ENTRY_UNAVAILABLE" > "$TMP_LAST_ENTRY"
    log "prompt" "ERROR: now.md exists but is not readable -- last entry sent as unavailable, not as absent"
else
    LAST_ENTRY_HEADERS=""
    HEADER_GREP_RC=0
    LAST_ENTRY_HEADERS=$(grep -n '^## ' "$MEMORY_FILE") || HEADER_GREP_RC=$?
    if [ "$HEADER_GREP_RC" -gt 1 ]; then
        printf '%s\n' "$LAST_ENTRY_UNAVAILABLE" > "$TMP_LAST_ENTRY"
        log "prompt" "ERROR: header search over now.md failed (grep rc ${HEADER_GREP_RC}) -- last entry sent as unavailable, not as absent"
    else
        LAST_LINE="${LAST_ENTRY_HEADERS##*$'\n'}"
        LAST_LINE="${LAST_LINE%%:*}"
        if [ -z "$LAST_LINE" ] || [ "${LAST_LINE#*[!0-9]}" != "$LAST_LINE" ]; then LAST_LINE=""; fi
        if [ -z "$LAST_LINE" ]; then
            printf '%s\n' "$NO_PREVIOUS_ENTRY" > "$TMP_LAST_ENTRY"
        elif tail -n +"$LAST_LINE" "$MEMORY_FILE" > "$TMP_LAST_ENTRY"; then
            :
        else
            printf '%s\n' "$LAST_ENTRY_UNAVAILABLE" > "$TMP_LAST_ENTRY"
            log "prompt" "ERROR: reading the last entry from now.md failed -- sent as unavailable, not as absent (this save may duplicate work already recorded)"
        fi
    fi
fi

if [ -n "${REMEMBER_BRANCH:-}" ]; then
    BRANCH="$REMEMBER_BRANCH"
else
    BRANCH=""
    if [ -n "${REMEMBER_BRANCH_CMD:-}" ]; then
        if CMD_BRANCH=$("$REMEMBER_BRANCH_CMD" "$SESSION_ID" 2>/dev/null) && [ -n "$CMD_BRANCH" ]; then
            if [[ "$CMD_BRANCH" == *$'\n'* ]] || [[ "$CMD_BRANCH" == *$'\r'* ]]; then
                log "branch" "WARNING: REMEMBER_BRANCH_CMD ($REMEMBER_BRANCH_CMD) printed multi-line (or carriage-return-bearing) output for session $SESSION_ID -- refusing to use it unbounded, falling back to git branch lookup"
            else
                BRANCH="$CMD_BRANCH"
            fi
        else
            log "branch" "WARNING: REMEMBER_BRANCH_CMD ($REMEMBER_BRANCH_CMD) exited non-zero or printed nothing for session $SESSION_ID -- falling back to git branch lookup"
        fi
    fi
    [ -z "$BRANCH" ] && BRANCH="$(cd "$PROJECT_DIR" && git branch --show-current 2>/dev/null || echo "unknown")"
fi
TIME_FORMAT=$(config ".time_format" "24h")
if [ "$TIME_FORMAT" = "12h" ]; then
    CURRENT_TIME=$(_remember_date '+%-I:%M %p' | tr '[:lower:]' '[:upper:]')
else
    CURRENT_TIME=$(_remember_date '+%H:%M')
fi
TMP_PROMPT=$(mktemp "${TMPDIR:-/tmp}"/remember-prompt-XXXXXX)
CLEANUP_FILES+=("$TMP_PROMPT")

EXTRACT_MAX_BYTES=$(config ".thresholds.extract_max_bytes" 300000)
if [ -z "$EXTRACT_MAX_BYTES" ] || [[ "$EXTRACT_MAX_BYTES" == *[!0-9]* ]]; then
    log "prompt" "WARNING: thresholds.extract_max_bytes is not a valid non-negative integer (got '$EXTRACT_MAX_BYTES') -- using default 300000"
    EXTRACT_MAX_BYTES=300000
fi
cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell build-prompt "$EXTRACT_FILE" "$TMP_LAST_ENTRY" "$CURRENT_TIME" "$BRANCH" "$TMP_PROMPT" "$EXTRACT_MAX_BYTES"

[ ! -s "$TMP_PROMPT" ] && { log "prompt" "ERROR: empty"; exit 1; }
grep -q '{{TIME}}\|{{BRANCH}}\|{{LAST_ENTRY}}\|{{EXTRACT}}' "$TMP_PROMPT" && { log "prompt" "ERROR: unsubstituted placeholders in prompt"; exit 1; }

log "haiku" "calling (branch: $BRANCH)"
HAIKU_STDERR=$(mktemp "${TMPDIR:-/tmp}"/remember-haiku-err-XXXXXX)
CLEANUP_FILES+=("$HAIKU_STDERR")

FAILURE_MARKER="${REMEMBER_DIR}/tmp/last-summary-failure"
MAX_FAILURES=$(config ".thresholds.max_summary_failures" 3)
if [ -z "$MAX_FAILURES" ] || [ "${MAX_FAILURES#*[!0-9]}" != "$MAX_FAILURES" ]; then MAX_FAILURES=3; fi

save_position_span() {
    if [ "$ENVELOPE" != "unrecognised" ] && [ "$ENVELOPE_HAS_UNMAPPED_STEP" = "1" ]; then
        log "extract" "$ENVELOPE envelope with an unmapped step type, skip -- position -> $POSITION (span quarantined from line $SKIP_LINES for a future build)"
        cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION" "unrecognised" "$SKIP_LINES"
    else
        cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION" "$ENVELOPE"
    fi
}

record_summary_failure() {
    [ "$MAX_FAILURES" -eq 0 ] && return 0
    _prev_span_id=""
    _prev_count=0
    if [ -f "$FAILURE_MARKER" ]; then
        read -r _prev_span_id _prev_count < "$FAILURE_MARKER" || true
        if [ -z "$_prev_count" ] || [ "${_prev_count#*[!0-9]}" != "$_prev_count" ]; then _prev_count=0; fi
    fi
    _span_id="${SESSION_ID}:${POSITION}"
    if [ "$_prev_span_id" = "$_span_id" ]; then
        _count=$(( 10#$_prev_count + 1 ))
    else
        _count=1
    fi

    if [ "$_count" -ge "$MAX_FAILURES" ]; then
        log "haiku" "WARNING: ${_count} consecutive failures on this span -- dropping it unsummarized and advancing position -> $POSITION (see thresholds.max_summary_failures)"
        save_position_span
        rm -f "$FAILURE_MARKER"
    elif marker_write_ok "$FAILURE_MARKER" summary; then
        echo "$_span_id $_count" > "$FAILURE_MARKER"
        log "haiku" "failure ${_count}/${MAX_FAILURES} on this span -- will retry next run"
    fi
}

SPAWN_DECLINED_EXIT=3

HAIKU_VARS=$(cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell call-haiku "$TMP_PROMPT" 2>"$HAIKU_STDERR") || {
    HAIKU_EXIT=$?
    if [ "$HAIKU_EXIT" -eq "$SPAWN_DECLINED_EXIT" ]; then
        log "haiku" "DECLINED: $(head -1 "$HAIKU_STDERR")"
        exit 0
    fi
    report_error "haiku" "ERROR: $(head -1 "$HAIKU_STDERR")"; record_summary_failure; exit 1
}

assign_kv <<< "$HAIKU_VARS"
CLEANUP_FILES+=("$HAIKU_TEXT_FILE")
log_usage "tokens" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"

HAIKU_TEXT=$(cat "$HAIKU_TEXT_FILE")
[ -z "$HAIKU_TEXT" ] && { report_error "haiku" "ERROR: empty response"; record_summary_failure; exit 1; }

keep_rejected_text() {
    local _src="$1" _tag="$2"
    local _dir="${REMEMBER_DIR}/tmp"
    local _file="${_dir}/rejected-$(_remember_date +%Y%m%d-%H%M%S)-$$.md"
    mkdir -p "$_dir" 2>/dev/null
    if cp "$_src" "$_file" 2>/dev/null; then
        log "$_tag" "rejected text kept at $_file"
    else
        log "$_tag" "WARNING: could not keep rejected text at $_file"
    fi
    ls -t "${_dir}"/rejected-*.md 2>/dev/null | tail -n +21 | while read -r _old; do
        rm -f "$_old"
    done
}

ENTRY_HEADER_ERE=$(cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.entry_header --ere entry 2>/dev/null) \
    || ENTRY_HEADER_ERE=''
[ -n "$ENTRY_HEADER_ERE" ] || ENTRY_HEADER_ERE='^## ([0-9]{2}:[0-9]{2}|[0-9]{1,2}:[0-9]{2} (AM|PM)) \|'

if [ "$IS_SKIP" != "true" ]; then
    FIRST_LINE=$(head -1 "$HAIKU_TEXT_FILE")
    if ! echo "$FIRST_LINE" | grep -qE "$ENTRY_HEADER_ERE"; then
        log "validate" "REJECTED (not an entry header): $(echo "$FIRST_LINE" | head -c 80)"
        keep_rejected_text "$HAIKU_TEXT_FILE" "validate"
        save_position_span
        log "validate" "position -> $POSITION"
        rm -f "$FAILURE_MARKER"
        exit 0
    else
        HEADER_REST="${FIRST_LINE#*|}"
        HEADER_REST="${HEADER_REST# }"
        NEW_FIRST_LINE="## ${CURRENT_TIME} | ${HEADER_REST}"
        if ! echo "$NEW_FIRST_LINE" | grep -qE "$ENTRY_HEADER_ERE"; then
            log "validate" "WARNING: refusing to rewrite header, result malformed: $(echo "$NEW_FIRST_LINE" | head -c 60)"
            NEW_FIRST_LINE="$FIRST_LINE"
        fi
        if [ "$NEW_FIRST_LINE" != "$FIRST_LINE" ]; then
            NORMALIZED=$(mktemp "${TMPDIR:-/tmp}"/remember-header-XXXXXX)
            { printf '%s\n' "$NEW_FIRST_LINE"; tail -n +2 "$HAIKU_TEXT_FILE"; } > "$NORMALIZED"
            mv "$NORMALIZED" "$HAIKU_TEXT_FILE"
            log "validate" "header time corrected to $CURRENT_TIME (model wrote: $(echo "$FIRST_LINE" | head -c 40))"
        fi
    fi
fi

if [ "$IS_SKIP" = "true" ]; then
    if [ "${IS_REJECTED:-false}" = "true" ]; then
        log "haiku" "REJECTED (provider: ${PROVIDER:-claude}; not a summary -- refusal or clarification): $(head -c 80 "$HAIKU_TEXT_FILE" 2>/dev/null)"
        keep_rejected_text "$HAIKU_TEXT_FILE" "haiku"
    fi
    log "haiku" "SKIP (provider: ${PROVIDER:-claude}) -- position -> $POSITION"
    save_position_span
    rm -f "$FAILURE_MARKER"
    exit 0
fi

if [ ! -s "$MEMORY_FILE" ]; then
    if marker_write_ok "$NOW_DAY_FILE" now-day; then
        { printf '%s\n' "$TODAY_DATE" > "$NOW_DAY_FILE"; } 2>/dev/null || true
    fi
fi
append_failed() {
    rm -f "$APPEND_TMP" 2>/dev/null
    log "write" "ERROR: cannot write now.md -- $1"
    exit 1
}
rm -f "${MEMORY_FILE}".append-* 2>/dev/null
APPEND_TMP=$(mktemp "${MEMORY_FILE}.append-XXXXXX") || {
    log "write" "ERROR: cannot write now.md -- no temp could be created beside it"
    exit 1
}
if [ -f "$MEMORY_FILE" ]; then
    APPEND_ERR=$(cat "$MEMORY_FILE" 2>&1 > "$APPEND_TMP") \
        || append_failed "could not copy the existing now.md: ${APPEND_ERR:-unknown error}"
fi
APPEND_ERR=$({ printf '\n' && cat "$HAIKU_TEXT_FILE"; } 2>&1 >> "$APPEND_TMP") \
    || append_failed "could not stage the entry: ${APPEND_ERR:-unknown error}"
APPEND_ERR=$(mv "$APPEND_TMP" "$MEMORY_FILE" 2>&1) \
    || append_failed "commit failed: ${APPEND_ERR:-unknown error}"
log "write" "appended (provider: ${PROVIDER:-claude}): $(head -1 "$HAIKU_TEXT_FILE" | cut -c1-80)"
save_position_span
log "write" "position -> $POSITION"
rm -f "$FAILURE_MARKER"

dispatch "after_save"

NDC_MARKER="${REMEMBER_DIR}/tmp/last-ndc.ts"
RUN_NDC=true
if [ "$(config '.features.ndc_compression' true)" != "true" ]; then
    RUN_NDC=false
    log "ndc" "disabled by features.ndc_compression"
fi
if [[ "$RUN_NDC" = true && ( -e "$NDC_MARKER" || -L "$NDC_MARKER" ) ]]; then
    NDC_MOD=$(ts_marker_read "$NDC_MARKER")
    if [ "$NDC_MOD" = "unreadable" ]; then
        report_error "ndc" "WARNING: $NDC_MARKER exists but its value could not be used (a read failure, or content that is not a plain timestamp) -- treating the cooldown as expired and compressing now. This will recur on every save until the marker holds a valid timestamp again, or is removed."
        NDC_MOD=0
    fi
    if [ -z "$NDC_MOD" ] || [ "${NDC_MOD#*[!0-9]}" != "$NDC_MOD" ]; then
        NDC_MOD=0
    fi
    NDC_COOLDOWN=$(config ".cooldowns.ndc_seconds" 3600)
    NDC_ELAPSED=$(( $(date +%s) - 10#$NDC_MOD ))
    if [ "$NDC_ELAPSED" -lt 0 ]; then
        report_error "ndc" "WARNING: $NDC_MARKER is $(( 0 - NDC_ELAPSED ))s ahead of now -- the clock moved back, or the marker is corrupt in a way a digits-only check cannot see. Resetting it and compressing; the cooldown resumes from now."
        if marker_write_ok "$NDC_MARKER" ndc; then
            { date +%s > "$NDC_MARKER"; } 2>/dev/null || true
        fi
    elif [ "$NDC_ELAPSED" -lt "$NDC_COOLDOWN" ]; then
        RUN_NDC=false
    fi
fi

if [ -f "$NOW_DAY_FILE" ]; then
    NDC_DAY=$(cat "$NOW_DAY_FILE" 2>/dev/null | tr -d '[:space:]')
elif [ -e "$NOW_DAY_FILE" ]; then
    report_error "now-day" "WARNING: $NOW_DAY_FILE exists but is not a regular file -- treating it as absent, so this round's entries are attributed to today ($TODAY_DATE). Remove or replace it."
    NDC_DAY=""
else
    NDC_DAY=""
fi
if [ -z "$NDC_DAY" ] || [ -n "${NDC_DAY#[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]}" ]; then
    NDC_DAY="$TODAY_DATE"
fi
TODAY_FILE="${REMEMBER_DIR}/today-${NDC_DAY}.md"

if [ "$RUN_NDC" = true ]; then
    log "ndc" "now.md -> today-${NDC_DAY}.md"
    if marker_write_ok "$NDC_MARKER" ndc; then
        { date +%s > "$NDC_MARKER"; } 2>/dev/null \
            || report_error "ndc" "WARNING: could not write $NDC_MARKER after this compression -- the cooldown will not reflect it, and every future save will hit the same unreadable/unwritable marker until it is fixed or removed."
    fi
    NDC_SRC_BYTES=$(wc -c < "$MEMORY_FILE" | tr -d ' ')
    NDC_SRC_GEN=$(ndc_read_gen)
    NDC_PROMPT=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-XXXXXX)

    cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell build-ndc-prompt "$MEMORY_FILE" "$NDC_PROMPT"

    if [ -s "$NDC_PROMPT" ]; then
        (set +e  # don't inherit set -e -- a haiku non-zero exit must not kill the subshell
            NDC_ERR=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-err-XXXXXX)
            NDC_TIMEOUT_SECONDS=$(config ".thresholds.ndc_timeout_seconds" 180)
            if [ -z "$NDC_TIMEOUT_SECONDS" ] || [[ "$NDC_TIMEOUT_SECONDS" == *[!0-9]* ]]; then
                log "ndc" "WARNING: thresholds.ndc_timeout_seconds is not a valid non-negative integer (got '$NDC_TIMEOUT_SECONDS') -- using default 180"
                NDC_TIMEOUT_SECONDS=180
            else
                if [ "${#NDC_TIMEOUT_SECONDS}" -gt 9 ]; then
                    log "ndc" "WARNING: thresholds.ndc_timeout_seconds ($NDC_TIMEOUT_SECONDS) is too large and would crash the NDC call with an OverflowError -- using default 180"
                    NDC_TIMEOUT_SECONDS=180
                elif [ "$NDC_TIMEOUT_SECONDS" -eq 0 ]; then
                    log "ndc" "WARNING: thresholds.ndc_timeout_seconds is 0, which times out the NDC call immediately on every run -- using default 180"
                    NDC_TIMEOUT_SECONDS=180
                fi
            fi
            NDC_VARS=$(cd "$PIPELINE_DIR" && _remember_run_python -m pipeline.shell call-haiku "$NDC_PROMPT" "" "$NDC_TIMEOUT_SECONDS" 2>"$NDC_ERR")
            NDC_EXIT=$?

            if [ "$NDC_EXIT" -eq "$SPAWN_DECLINED_EXIT" ]; then
                log "ndc" "DECLINED: $(head -1 "$NDC_ERR" 2>/dev/null)"
            elif [ "$NDC_EXIT" -ne 0 ]; then
                log "ndc" "ERROR: $(head -1 "$NDC_ERR" 2>/dev/null)"
            else
                IS_SKIP=false
                IS_REJECTED=false
                PROVIDER=claude
                assign_kv <<< "$NDC_VARS"
                NDC_TEXT=$(cat "$HAIKU_TEXT_FILE")
                log_usage "ndc" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"
                if [ "$IS_SKIP" != "true" ] && [ "${IS_REJECTED:-false}" != "true" ]; then
                    NDC_HEADER_LINE=$(grep -n -m1 '^## ' "$HAIKU_TEXT_FILE" 2>/dev/null | cut -d: -f1)
                    if [ "$NDC_HEADER_LINE" = 1 ]; then
                        NDC_LOOKS_LIKE_HEADER=true
                    elif [ "$NDC_HEADER_LINE" = 2 ] || [ "$NDC_HEADER_LINE" = 3 ] || [ "$NDC_HEADER_LINE" = 4 ]; then
                        NDC_STRIPPED_FILE=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-stripped-XXXXXX)
                        if [ -n "$NDC_STRIPPED_FILE" ] \
                            && tail -n "+$NDC_HEADER_LINE" "$HAIKU_TEXT_FILE" > "$NDC_STRIPPED_FILE" \
                            && mv "$NDC_STRIPPED_FILE" "$HAIKU_TEXT_FILE"; then
                            NDC_LOOKS_LIKE_HEADER=true
                            NDC_TEXT=$(cat "$HAIKU_TEXT_FILE")
                            log "ndc" "preamble stripped ($((NDC_HEADER_LINE - 1)) line(s) before the first '## ')"
                        else
                            rm -f "$NDC_STRIPPED_FILE" 2>/dev/null
                            NDC_LOOKS_LIKE_HEADER=false
                            report_error "ndc" "WARNING: could not strip a short preamble from the NDC reply -- treating it as rejected instead of risking a partially-written file"
                        fi
                    else
                        NDC_LOOKS_LIKE_HEADER=false
                    fi
                fi
                if [ "$IS_SKIP" = "true" ] || [ "${IS_REJECTED:-false}" = "true" ] || [ "$NDC_LOOKS_LIKE_HEADER" = "false" ]; then
                    if [ "$IS_SKIP" = "true" ] || [ "${IS_REJECTED:-false}" = "true" ]; then
                        log "ndc" "REJECTED (provider: ${PROVIDER:-claude}; not a summary -- refusal or clarification): $(head -c 80 "$HAIKU_TEXT_FILE" 2>/dev/null)"
                    else
                        log "ndc" "REJECTED (not a header -- reply does not open with '## '): $(head -c 80 "$HAIKU_TEXT_FILE" 2>/dev/null)"
                    fi
                    keep_rejected_text "$HAIKU_TEXT_FILE" "ndc"
                elif [ -n "$NDC_TEXT" ]; then
                    if ! staging_lock_acquire "$STAGING_LOCK_TIMEOUT"; then
                        NDC_STAGED=false
                        log "ndc" "SKIPPED: staging.lock held for the whole ${STAGING_LOCK_TIMEOUT}s wait (a consolidation is retiring staging files) -- today-${NDC_DAY}.md not appended and now.md left untouched, so the next round re-summarizes this span with no duplicate"
                    else
                        NDC_STAGED=true
                        staging_append "$TODAY_FILE" "$HAIKU_TEXT_FILE"
                        staging_lock_release
                    fi
                    if [ "$NDC_STAGED" = true ] && lock_acquire "$LOCK_DIR" "$NDC_COMMIT_LOCK_TIMEOUT"; then
                        NDC_LIVE_BYTES=$(wc -c < "$MEMORY_FILE" 2>/dev/null | tr -d ' ')
                        if [ -z "$NDC_LIVE_BYTES" ] || [ "${NDC_LIVE_BYTES#*[!0-9]}" != "$NDC_LIVE_BYTES" ]; then
                            NDC_LIVE_BYTES=0
                        fi
                        NDC_LIVE_GEN=$(ndc_read_gen)
                        if [ "$NDC_LIVE_BYTES" -lt "$NDC_SRC_BYTES" ]; then
                            log "ndc" "SKIPPED commit: now.md is ${NDC_LIVE_BYTES}b, below the ${NDC_SRC_BYTES}b snapshot this offset was taken from -- left untouched (today-${NDC_DAY}.md may now hold a duplicate of this span)"
                        elif [ "$NDC_SRC_GEN" = "unreadable" ] || [ "$NDC_LIVE_GEN" = "unreadable" ]; then
                            log "ndc" "SKIPPED commit: could not read ${NDC_GEN_FILE} (src=${NDC_SRC_GEN}, live=${NDC_LIVE_GEN}) -- now.md left untouched, this round cannot tell whether another round committed since its snapshot (today-${NDC_DAY}.md may now hold a duplicate of this span). If this recurs, ${NDC_GEN_FILE} is likely durably unreadable rather than merely racing a writer -- delete it to reset generation tracking to 0 and unblock future commits."
                        elif [ "$NDC_LIVE_GEN" != "$NDC_SRC_GEN" ]; then
                            log "ndc" "SKIPPED commit: another NDC round already committed since this round's snapshot (generation ${NDC_SRC_GEN} -> ${NDC_LIVE_GEN}) -- now.md left untouched, this round's offset no longer describes a real boundary (today-${NDC_DAY}.md may now hold a duplicate of this span)"
                        else
                            rm -f "${MEMORY_FILE}".ndc-* 2>/dev/null
                            NDC_TAIL=$(mktemp "${MEMORY_FILE}.ndc-XXXXXX")
                            if { tail -c +$(( NDC_SRC_BYTES + 1 )) "$MEMORY_FILE" > "$NDC_TAIL"; } 2>/dev/null; then
                                NDC_KEPT=$(wc -c < "$NDC_TAIL" | tr -d ' ')
                                if [ -z "$NDC_KEPT" ] || [ "${NDC_KEPT#*[!0-9]}" != "$NDC_KEPT" ]; then
                                    NDC_KEPT=0
                                fi
                                if NDC_MV_ERR=$(mv "$NDC_TAIL" "$MEMORY_FILE" 2>&1); then
                                    if [ "$NDC_KEPT" -gt 0 ]; then
                                        if marker_write_ok "$NOW_DAY_FILE" now-day; then
                                            { printf '%s\n' "$(_remember_date +%Y-%m-%d)" > "$NOW_DAY_FILE"; } 2>/dev/null || true
                                        fi
                                    else
                                        rm -f "$NOW_DAY_FILE"
                                    fi
                                    [ "$NDC_KEPT" -gt 0 ] && log "ndc" "kept ${NDC_KEPT}b appended during compression"
                                    if marker_write_ok "$NDC_GEN_FILE" ndc && ! NDC_GEN_ERR=$(echo $(( 10#$NDC_SRC_GEN + 1 )) > "$NDC_GEN_FILE" 2>&1); then
                                        log "ndc" "WARNING: could not bump ${NDC_GEN_FILE} past ${NDC_SRC_GEN} -- a later round that started from this same generation will not detect that this commit already landed: ${NDC_GEN_ERR:-unknown error}"
                                    fi
                                else
                                    rm -f "$NDC_TAIL"
                                    log "ndc" "ERROR: commit failed, now.md left untouched and the day stamp not changed (today-${NDC_DAY}.md may now hold a duplicate of this span): ${NDC_MV_ERR}"
                                fi
                            else
                                rm -f "$NDC_TAIL"
                                log "ndc" "ERROR: tail failed, now.md left untouched (today-${NDC_DAY}.md may now hold a duplicate of this span)"
                            fi
                        fi
                        lock_release "$LOCK_DIR" || true
                    elif [ "$NDC_STAGED" = true ]; then
                        log "ndc" "SKIPPED commit: another save held the lock for the whole ${NDC_COMMIT_LOCK_TIMEOUT}s wait, now.md left untouched (today-${NDC_DAY}.md now holds a duplicate of this span -- the routine outcome of losing this race, not an error)"
                    fi
                    NDC_OUT_BYTES=$(wc -c < "$HAIKU_TEXT_FILE" | tr -d ' ')
                    [ "$NDC_SRC_BYTES" -gt 0 ] && log "ndc" "${NDC_SRC_BYTES}->${NDC_OUT_BYTES}b (-$(( (NDC_SRC_BYTES - NDC_OUT_BYTES) * 100 / NDC_SRC_BYTES ))%)"
                else
                    log "ndc" "ERROR: produced empty result"
                fi
                rm -f "$HAIKU_TEXT_FILE"
            fi
            rm -f "$NDC_PROMPT" "$NDC_ERR"
        ) &
        log "ndc" "running (PID $!)"
    else
        log "ndc" "ERROR: prompt empty"
        rm -f "$NDC_PROMPT"
    fi
fi

if [ "$HAVE_LOCK" = true ]; then
    lock_release "$LOCK_DIR" || true
    HAVE_LOCK=false
fi

_AUTONOMOUS_LOG_RETENTION_DAYS=$(config ".thresholds.autonomous_log_retention_days" 7)
if [ -z "$_AUTONOMOUS_LOG_RETENTION_DAYS" ] || [ "${_AUTONOMOUS_LOG_RETENTION_DAYS#*[!0-9]}" != "$_AUTONOMOUS_LOG_RETENTION_DAYS" ]; then _AUTONOMOUS_LOG_RETENTION_DAYS=7; fi
if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
    _remember_auto_dir="${REMEMBER_DIR//\\//}"
else
    _remember_auto_dir="$REMEMBER_DIR"
fi
for _remember_auto_log in "${_remember_auto_dir}/logs/autonomous"/*.log; do
    [ -f "$_remember_auto_log" ] || continue
    if [ ! -s "$_remember_auto_log" ]; then
        rm -f "$_remember_auto_log" 2>/dev/null \
            || log "housekeeping" "WARNING: could not remove empty $_remember_auto_log"
        continue
    fi
    _remember_auto_mtime=$(stat -c %Y "$_remember_auto_log" 2>/dev/null) \
        || _remember_auto_mtime=$(stat -f %m "$_remember_auto_log" 2>/dev/null) \
        || _remember_auto_mtime=""
    if [ -z "$_remember_auto_mtime" ] || [[ "$_remember_auto_mtime" == *[!0-9]* ]]; then
        log "housekeeping" "WARNING: could not read mtime of $_remember_auto_log -- leaving it in place"
        continue
    fi
    _remember_auto_now=$(_remember_date +%s)
    if [ -z "$_remember_auto_now" ] || [[ "$_remember_auto_now" == *[!0-9]* ]]; then
        log "housekeeping" "WARNING: could not read the clock -- skipping the retention sweep for $_remember_auto_log"
        continue
    fi
    _remember_auto_age_days=$(( (10#$_remember_auto_now - 10#$_remember_auto_mtime) / 86400 ))
    if [ "$_remember_auto_age_days" -gt "$_AUTONOMOUS_LOG_RETENTION_DAYS" ]; then
        rm -f "$_remember_auto_log" 2>/dev/null \
            || log "housekeeping" "WARNING: could not remove aged (${_remember_auto_age_days}d) $_remember_auto_log"
    fi
done
unset _remember_auto_dir _remember_auto_log _remember_auto_mtime _remember_auto_now _remember_auto_age_days

[ -n "${PLUGIN_ROOT:-}" ] || PLUGIN_ROOT="$PIPELINE_DIR"
if source "$(dirname "$0")/lib-memory-context.sh" 2>/dev/null; then
    _remember_memory_paths
    _remember_start_cache_context_publish
fi
