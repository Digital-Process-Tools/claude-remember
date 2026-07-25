#!/bin/bash
# ============================================================================
# save-session.sh — Extract and summarize a Claude Code session into daily memory
# ============================================================================
#
# DESCRIPTION
#   The main extraction pipeline. Reads the current session's JSONL transcript,
#   extracts human/assistant exchanges, sends them to Haiku for summarization,
#   and appends the result to now.md. Periodically compresses now.md into a
#   dated today-YYYY-MM-DD.md file via NDC (Now-Day Compression).
#
# USAGE
#   save-session.sh <session-id>        # normal (called by post-tool hook)
#   save-session.sh --force             # bypass cooldown + min message threshold
#   save-session.sh <id> --force        # recover a specific missed session
#   save-session.sh --dry               # preview extraction, skip Haiku call
#
# ARGUMENTS
#   <session-id>   UUID of the session JSONL file (auto-detected if omitted)
#   --force        Bypass cooldown timer and minimum human message threshold
#   --dry          Preview mode — show extracted exchanges, do not call Haiku
#
# ENVIRONMENT
#   REMEMBER_DEBUG   Set to "1" for verbose logging (default: 1)
#
# DEPENDENCIES
#   python3, claude CLI (Haiku), git, date, mktemp
#   Sources: log.sh (logging, safe_eval, config)
#   Python: pipeline.shell (extract, build-prompt, parse-haiku, save-position,
#           build-ndc-prompt)
#
# EXIT CODES
#   0   Success (or skip due to cooldown/threshold/SKIP response)
#   1   Lock held, invalid session ID, python3 not found, Haiku error,
#       or file write error
#
# ARCHITECTURE
#   Shell handles locks (noclobber), cooldowns, file I/O, and background NDC.
#   Python (pipeline/) handles JSONL extraction, prompt building, and response
#   parsing. Data flows via temp files — never shell-interpolated.
#
#   Pipeline steps:
#     1. Extract exchanges from session JSONL
#     2. Get last memory entry (for dedup context)
#     3. Build summarization prompt
#     4. Call Haiku via `pipeline.shell call-haiku` (sandbox + parse in one)
#     5. Parse response (detect SKIP vs. content)
#     6. Append to now.md + save position
#     7. NDC compression (hourly, background subshell)
#
# ============================================================================

set -e

trap 'log "error" "FAILED at line $LINENO (exit $?)"' ERR

source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/bootstrap-dirs.sh"
source "$(dirname "$0")/log.sh"
log "hook" "save-session: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON REMEMBER_DIR=$REMEMBER_DIR"

LOCK_FILE="${REMEMBER_DIR}/tmp/save.lock"
MEMORY_FILE="${REMEMBER_DIR}/now.md"
LAST_SAVE_FILE="${REMEMBER_DIR}/tmp/last-save.json"
COOLDOWN_MARKER="${REMEMBER_DIR}/tmp/last-save-ts"
TODAY_DATE=$(_remember_date +%Y-%m-%d)
CLEANUP_FILES=()
# The trap below REPLACES the cleanup trap lib-memory-dir.sh installed for
# $REMEMBER_CONFIG (bash keeps a single EXIT trap), so remove it here too.
CLEANUP_FILES+=("$REMEMBER_CONFIG")

# Remove lock file and all accumulated temp files on exit.
cleanup() { rm -f "$LOCK_FILE" "${CLEANUP_FILES[@]}"; }
trap cleanup EXIT

# --- Lock (atomic via noclobber) ---
if ! ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        [ "${REMEMBER_DEBUG:-1}" = "1" ] && log "lock" "locked by PID $LOCK_PID, skipping"
        exit 0
    fi
    log "lock" "stale lock (PID $LOCK_PID dead), taking over"
    echo $$ > "$LOCK_FILE"
fi

# --- Parse args ---
DRY_RUN=false
FORCE=false
SESSION_ID=""
for arg in "$@"; do
    case "$arg" in
        --dry)   DRY_RUN=true ;;
        --force) FORCE=true ;;
        *)       SESSION_ID="$arg" ;;
    esac
done

SESSION_DIR_PATH="$HOME/.claude/projects/$(session_dir_slug "$PROJECT_DIR")"
if [ -z "$SESSION_ID" ]; then
    LATEST_JSONL=$(ls -t "$SESSION_DIR_PATH"/*.jsonl 2>/dev/null | head -1)
    SESSION_ID=$(basename "$LATEST_JSONL" .jsonl)
fi

# --- Validate session ID (UUID format: hex + hyphens only) ---
if ! [[ "$SESSION_ID" =~ ^[a-f0-9-]+$ ]]; then
    log "save" "ERROR: invalid session ID: $(echo "$SESSION_ID" | head -c 40)"
    exit 1
fi

# --- Cooldown ---
[ "$FORCE" = true ] && log "force" "bypassing cooldown + min msgs"
if [ -f "$COOLDOWN_MARKER" ] && [ "$DRY_RUN" != true ] && [ "$FORCE" != true ]; then
    LAST_MOD=$(cat "$COOLDOWN_MARKER" 2>/dev/null || echo 0)
    ELAPSED=$(( $(date +%s) - LAST_MOD ))
    SAVE_COOLDOWN=$(config ".cooldowns.save_seconds" 120)
    if [ "$ELAPSED" -lt "$SAVE_COOLDOWN" ]; then
        [ "${REMEMBER_DEBUG:-1}" = "1" ] && log "cooldown" "${ELAPSED}s < ${SAVE_COOLDOWN}s, skip"
        exit 0
    fi
fi

# --- Dispatch: before_save ---
dispatch "before_save"

# --- Step 1: Extract ---
log "extract" "session $SESSION_ID"
safe_eval <<< "$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell extract "$SESSION_ID" "$PROJECT_DIR")"
CLEANUP_FILES+=("$EXTRACT_FILE")
date +%s > "$COOLDOWN_MARKER"
log "extract" "${EXCHANGE_COUNT} exchanges (${HUMAN_COUNT} human)"

# Nothing in this span at all: advance the saved position before leaving (#147).
# The position is only written after a *successful* save (:211), so an early exit
# here used to leave the cursor where it was — and since the cooldown marker at
# :131 *is* written, the next run re-extracted the identical span and exited
# identically, once per cooldown window, forever. There is nothing to summarize
# in an empty span, so advancing loses no memory and breaks the loop.
if [ "$EXCHANGE_COUNT" -eq 0 ]; then
    if [ "$DRY_RUN" = false ]; then
        log "extract" "0 exchanges, skip — position → $POSITION"
        cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION"
    else
        log "extract" "0 exchanges, skip (dry run — position unchanged)"
    fi
    exit 0
fi

# The min-human gate exists to keep greetings and one-liners out of memory. But
# an *agentic* session — many tool calls, few human turns — never clears it, so
# the plugin's core function silently never ran for the whole session (#147,
# #125). Substance is not only measured in human turns: a span with this many
# exchanges is real work, so let it through even when the human count is low.
# Set the threshold to 0 to disable the fallback and keep the strict gate.
MIN_HUMAN=$(config ".thresholds.min_human_messages" 3)
MIN_EXCHANGES=$(config ".thresholds.min_exchanges_without_human" 30)
# A non-numeric value in config.json would abort the script at the comparisons
# below (set -e + ERR trap), turning a typo into "memory silently stopped".
case "$MIN_HUMAN" in ''|*[!0-9]*) MIN_HUMAN=3 ;; esac
case "$MIN_EXCHANGES" in ''|*[!0-9]*) MIN_EXCHANGES=30 ;; esac
if [ "$HUMAN_COUNT" -lt "$MIN_HUMAN" ] && [ "$DRY_RUN" = false ] && [ "$FORCE" != true ]; then
    if [ "$MIN_EXCHANGES" -gt 0 ] && [ "$EXCHANGE_COUNT" -ge "$MIN_EXCHANGES" ]; then
        log "extract" "${HUMAN_COUNT} human < ${MIN_HUMAN} but ${EXCHANGE_COUNT} exchanges >= ${MIN_EXCHANGES}, saving (agentic session)"
    else
        # Deliberately does NOT advance the position: these exchanges are real
        # content, just not enough of it yet. Keeping the cursor lets them be
        # summarized together with the turns that follow. The cost of re-reading
        # is bounded by the save cooldown.
        log "extract" "${HUMAN_COUNT} human msgs < ${MIN_HUMAN}, skip"
        exit 0
    fi
fi

if [ "$DRY_RUN" = true ]; then
    echo ""; echo "=== DRY RUN ==="; echo ""; cat "$EXTRACT_FILE"; echo ""; exit 0
fi

# --- Step 2: Get last entry ---
TMP_LAST_ENTRY=$(mktemp "${TMPDIR:-/tmp}"/remember-last-entry-XXXXXX)
CLEANUP_FILES+=("$TMP_LAST_ENTRY")
if [ -f "$MEMORY_FILE" ]; then
    LAST_LINE=$(grep -n '^## ' "$MEMORY_FILE" | tail -1 | cut -d: -f1)
    [ -n "$LAST_LINE" ] && tail -n +"$LAST_LINE" "$MEMORY_FILE" > "$TMP_LAST_ENTRY" || echo "(no previous entry)" > "$TMP_LAST_ENTRY"
else
    echo "(no previous entry)" > "$TMP_LAST_ENTRY"
fi

# --- Step 3: Build prompt ---
# $REMEMBER_BRANCH wins when set, so users running Claude Code from a non-git
# directory (e.g., $HOME) can supply a meaningful identity for the today-*.md
# header instead of the literal "unknown" fallback. Empty string is treated as
# unset so an accidental `export REMEMBER_BRANCH=` doesn't propagate.
BRANCH="${REMEMBER_BRANCH:-$(cd "$PROJECT_DIR" && git branch --show-current 2>/dev/null || echo "unknown")}"
TIME_FORMAT=$(config ".time_format" "24h")
if [ "$TIME_FORMAT" = "12h" ]; then
    # Force uppercase AM/PM: %p is locale-dependent (lowercase on many Linux systems).
    CURRENT_TIME=$(_remember_date '+%-I:%M %p' | tr '[:lower:]' '[:upper:]')
else
    CURRENT_TIME=$(_remember_date '+%H:%M')
fi
TMP_PROMPT=$(mktemp "${TMPDIR:-/tmp}"/remember-prompt-XXXXXX)
CLEANUP_FILES+=("$TMP_PROMPT")

EXTRACT_MAX_BYTES=$(config ".thresholds.extract_max_bytes" 300000)
cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell build-prompt "$EXTRACT_FILE" "$TMP_LAST_ENTRY" "$CURRENT_TIME" "$BRANCH" "$TMP_PROMPT" "$EXTRACT_MAX_BYTES"

[ ! -s "$TMP_PROMPT" ] && { log "prompt" "ERROR: empty"; exit 1; }
grep -q '{{TIME}}\|{{BRANCH}}\|{{LAST_ENTRY}}\|{{EXTRACT}}' "$TMP_PROMPT" && { log "prompt" "ERROR: unsubstituted placeholders in prompt"; exit 1; }

# --- Step 4+5: Call Haiku (the claude -p invocation lives only in pipeline/haiku.py) ---
log "haiku" "calling (branch: $BRANCH)"
HAIKU_STDERR=$(mktemp "${TMPDIR:-/tmp}"/remember-haiku-err-XXXXXX)
CLEANUP_FILES+=("$HAIKU_STDERR")

# A summarization failure leaves the span unsummarized, so the position stays
# put and the span is retried on the next run — that is what we want for a
# transient error (rate limit, network blip). But a *persistent* failure on the
# same span then retries forever, once per cooldown window, and memory never
# advances again: @VictorVvdl hit exactly this for a month (#147). So count
# consecutive failures against the same (session, position) pair and, past
# thresholds.max_summary_failures, give up on that span — advance past it and
# say so loudly. Losing one span is bad; losing every future span is worse.
# Set the threshold to 0 to retry forever (the old behaviour).
FAILURE_MARKER="${REMEMBER_DIR}/tmp/last-summary-failure"
MAX_FAILURES=$(config ".thresholds.max_summary_failures" 3)
case "$MAX_FAILURES" in ''|*[!0-9]*) MAX_FAILURES=3 ;; esac

record_summary_failure() {
    [ "$MAX_FAILURES" -eq 0 ] && return 0
    _prev_key=""
    _prev_count=0
    if [ -f "$FAILURE_MARKER" ]; then
        read -r _prev_key _prev_count < "$FAILURE_MARKER" || true
        case "$_prev_count" in ''|*[!0-9]*) _prev_count=0 ;; esac
    fi
    _key="${SESSION_ID}:${POSITION}"
    if [ "$_prev_key" = "$_key" ]; then
        _count=$(( _prev_count + 1 ))
    else
        _count=1
    fi

    if [ "$_count" -ge "$MAX_FAILURES" ]; then
        log "haiku" "WARNING: ${_count} consecutive failures on this span — dropping it unsummarized and advancing position → $POSITION (see thresholds.max_summary_failures)"
        cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION"
        rm -f "$FAILURE_MARKER"
    else
        echo "$_key $_count" > "$FAILURE_MARKER"
        log "haiku" "failure ${_count}/${MAX_FAILURES} on this span — will retry next run"
    fi
}

# `|| { ... }` (not a bare `if [ $? ]`) so a failure is handled under set -e
# instead of tripping the ERR trap at the assignment.
HAIKU_VARS=$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell call-haiku "$TMP_PROMPT" 2>"$HAIKU_STDERR") || {
    log "haiku" "ERROR: $(head -1 "$HAIKU_STDERR")"; record_summary_failure; exit 1
}

safe_eval <<< "$HAIKU_VARS"
CLEANUP_FILES+=("$HAIKU_TEXT_FILE")
log_tokens "tokens" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"

HAIKU_TEXT=$(cat "$HAIKU_TEXT_FILE")
[ -z "$HAIKU_TEXT" ] && { log "haiku" "ERROR: empty response"; record_summary_failure; exit 1; }

# --- Step 5b: Validate format (warn, never discard) ---
if [ "$IS_SKIP" != "true" ]; then
    FIRST_LINE=$(head -1 "$HAIKU_TEXT_FILE")
    if ! echo "$FIRST_LINE" | grep -qE '^## ([0-9]{2}:[0-9]{2}|[0-9]{1,2}:[0-9]{2} (AM|PM)) \|'; then
        log "validate" "WARNING: unexpected format: $(echo "$FIRST_LINE" | head -c 80)"
    fi
fi

# --- Step 6: Handle SKIP ---
if [ "$IS_SKIP" = "true" ]; then
    log "haiku" "SKIP — position → $POSITION"
    cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION"
    # A SKIP is a successful summarization (the model judged the span not worth
    # recording) and it advances the position, so it must clear the failure
    # count too — otherwise a stale count survives and a later single failure
    # trips the give-up threshold early.
    rm -f "$FAILURE_MARKER"
    exit 0
fi

# --- Step 7: Append + save position ---
echo "" >> "$MEMORY_FILE" 2>/dev/null || { log "write" "ERROR: cannot write now.md"; exit 1; }
cat "$HAIKU_TEXT_FILE" >> "$MEMORY_FILE"
log "write" "appended: $(head -1 "$HAIKU_TEXT_FILE" | cut -c1-80)"
cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION"
log "write" "position → $POSITION"
rm -f "$FAILURE_MARKER"

# --- Dispatch: after_save ---
dispatch "after_save"

# --- Step 8: NDC compression (1h cooldown, background) ---
NDC_MARKER="${REMEMBER_DIR}/tmp/last-ndc.ts"
RUN_NDC=true
if [ -f "$NDC_MARKER" ]; then
    NDC_MOD=$(cat "$NDC_MARKER" 2>/dev/null || echo 0)
    NDC_COOLDOWN=$(config ".cooldowns.ndc_seconds" 3600)
    [ $(( $(date +%s) - NDC_MOD )) -lt "$NDC_COOLDOWN" ] && RUN_NDC=false
fi

TODAY_FILE="${REMEMBER_DIR}/today-${TODAY_DATE}.md"

if [ "$RUN_NDC" = true ]; then
    log "ndc" "now.md → today-${TODAY_DATE}.md"
    date +%s > "$NDC_MARKER"
    NDC_SRC_BYTES=$(wc -c < "$MEMORY_FILE" | tr -d ' ')
    NDC_PROMPT=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-XXXXXX)

    cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell build-ndc-prompt "$MEMORY_FILE" "$NDC_PROMPT"

    if [ -s "$NDC_PROMPT" ]; then
        (set +e  # don't inherit set -e — a haiku non-zero exit must not kill the subshell
            NDC_ERR=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-err-XXXXXX)
            # 180s (not the 120s default): NDC compresses a whole now.md.
            NDC_VARS=$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell call-haiku "$NDC_PROMPT" "" 180 2>"$NDC_ERR")
            NDC_EXIT=$?

            if [ "$NDC_EXIT" -ne 0 ]; then
                log "ndc" "ERROR: $(head -1 "$NDC_ERR" 2>/dev/null)"
            else
                safe_eval <<< "$NDC_VARS"
                NDC_TEXT=$(cat "$HAIKU_TEXT_FILE")
                if [ -n "$NDC_TEXT" ]; then
                    [ -s "$TODAY_FILE" ] && echo "" >> "$TODAY_FILE"
                    cat "$HAIKU_TEXT_FILE" >> "$TODAY_FILE"
                    # Drop exactly the bytes that were compressed, not the whole
                    # file (#142). now.md was snapshotted before the Haiku call
                    # above, which can take up to 180s — and by then the parent
                    # has released the save lock and exited, so a *newer* save
                    # may well have appended an entry. `: >` erased those
                    # entries, and their position had already been advanced, so
                    # they were unrecoverable and nothing was logged. Keep
                    # everything past the snapshot offset instead.
                    NDC_TAIL=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-tail-XXXXXX)
                    if tail -c +$(( NDC_SRC_BYTES + 1 )) "$MEMORY_FILE" > "$NDC_TAIL" 2>/dev/null; then
                        NDC_KEPT=$(wc -c < "$NDC_TAIL" | tr -d ' ')
                        mv "$NDC_TAIL" "$MEMORY_FILE"
                        [ "$NDC_KEPT" -gt 0 ] && log "ndc" "kept ${NDC_KEPT}b appended during compression"
                    else
                        rm -f "$NDC_TAIL"
                        : > "$MEMORY_FILE"
                    fi
                    log_tokens "ndc" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"
                    NDC_OUT_BYTES=$(wc -c < "$HAIKU_TEXT_FILE" | tr -d ' ')
                    [ "$NDC_SRC_BYTES" -gt 0 ] && log "ndc" "${NDC_SRC_BYTES}→${NDC_OUT_BYTES}b (-$(( (NDC_SRC_BYTES - NDC_OUT_BYTES) * 100 / NDC_SRC_BYTES ))%)"
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

    # Housekeeping: remove empty autonomous logs
    find "${REMEMBER_DIR}/logs/autonomous" -name "*.log" -empty -delete 2>/dev/null
fi
