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
#   REMEMBER_DEBUG   Set to "1"/"0" for verbose logging. Unset, the `debug`
#                    config option decides; unset there too, this script is
#                    verbose (its long-standing default) while the git-backup
#                    hook is quiet.
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
source "$(dirname "$0")/lib-lock.sh"
log "hook" "save-session: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON REMEMBER_DIR=$REMEMBER_DIR"

LOCK_DIR="${REMEMBER_DIR}/tmp/save.lock"
# How long the backgrounded NDC commit waits for the save lock before giving up
# and leaving now.md alone (#223). See Step 8 for why this is not the parent's
# timeout 0. Env-overridable so the concurrency tests can drive the timeout path
# without pinning a lock for half a minute — same shape as _LOCK_ADOPT_AFTER.
NDC_COMMIT_LOCK_TIMEOUT="${REMEMBER_NDC_COMMIT_LOCK_TIMEOUT:-30}"
MEMORY_FILE="${REMEMBER_DIR}/now.md"
# Which day now.md's contents belong to (#141) — see Step 7.
NOW_DAY_FILE="${REMEMBER_DIR}/tmp/now-day"
LAST_SAVE_FILE="${REMEMBER_DIR}/tmp/last-save.json"
COOLDOWN_MARKER="${REMEMBER_DIR}/tmp/last-save-ts"
TODAY_DATE=$(_remember_date +%Y-%m-%d)
CLEANUP_FILES=()
# The trap below REPLACES the cleanup trap lib-memory-dir.sh installed for
# $REMEMBER_CONFIG (bash keeps a single EXIT trap), so remove it here too.
CLEANUP_FILES+=("$REMEMBER_CONFIG")

# HAVE_LOCK: only release LOCK_DIR if THIS process actually owns it. The trap
# is installed before acquisition — so it also fires on the "someone else holds
# it, skipping" exit — and cleanup() used to unlink the lock unconditionally.
# That let a session which correctly skipped delete the *holder's* lock, so a
# third process could then acquire while the first was still running: two
# concurrent save-session.sh, two Haiku calls, two position writes (#168).
HAVE_LOCK=false

# Release the lock (only if we own it) and remove all temp files on exit.
cleanup() {
    # `|| true`: lock_release legitimately returns 1 (it refuses to release a
    # lock it does not own), and under `set -e` a bare failing command aborts
    # the trap — so the `rm -f` below would never run and the script's exit
    # status would be silently rewritten to 1.
    [ "$HAVE_LOCK" = true ] && { lock_release "$LOCK_DIR" || true; }
    rm -f "${CLEANUP_FILES[@]}"
}
trap cleanup EXIT

# --- Lock (mkdir acquisition, rename-based stale takeover — see lib-lock.sh) ---
# Timeout 0: a save that loses the race skips rather than queues. Two saves of
# the same session back to back have nothing to add to each other, and the next
# tool call brings another one along in seconds.
if lock_acquire "$LOCK_DIR" 0; then
    HAVE_LOCK=true
else
    debug_enabled 1 && log "lock" "another save holds the lock, skipping"
    exit 0
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

SESSION_DIR_PATH="$(claude_projects_dir)/$(session_dir_slug "$PROJECT_DIR")"
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
        debug_enabled 1 && log "cooldown" "${ELAPSED}s < ${SAVE_COOLDOWN}s, skip"
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

# Exit code the spawn guard uses to say "declined", as opposed to 1 for
# "failed" (#204). See pipeline/spawn_guard.py — the two must not be conflated,
# because record_summary_failure counts failures against this span and drops it
# after thresholds.max_summary_failures. A cap that is working correctly would
# then destroy the span it protected.
SPAWN_DECLINED_EXIT=3

# `|| { ... }` (not a bare `if [ $? ]`) so a failure is handled under set -e
# instead of tripping the ERR trap at the assignment.
HAIKU_VARS=$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell call-haiku "$TMP_PROMPT" 2>"$HAIKU_STDERR") || {
    HAIKU_EXIT=$?
    if [ "$HAIKU_EXIT" -eq "$SPAWN_DECLINED_EXIT" ]; then
        # Declined, not failed: no failure is recorded and the position stays
        # put, so this span is summarized by a later run. Logged as its own word
        # so `grep DECLINED` finds it — a cap that fires has to be visible.
        log "haiku" "DECLINED: $(head -1 "$HAIKU_STDERR")"
        exit 0
    fi
    log "haiku" "ERROR: $(head -1 "$HAIKU_STDERR")"; record_summary_failure; exit 1
}

safe_eval <<< "$HAIKU_VARS"
CLEANUP_FILES+=("$HAIKU_TEXT_FILE")
log_tokens "tokens" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"

HAIKU_TEXT=$(cat "$HAIKU_TEXT_FILE")
[ -z "$HAIKU_TEXT" ] && { log "haiku" "ERROR: empty response"; record_summary_failure; exit 1; }

# Park a discarded reply where it can be read later. Three callers now (format
# validator, reject gate, NDC compression) — one copy each drifts, so it lives
# here once.
#
# The timestamp carries $$ because seconds-resolution alone collides: two
# rejections in the same second overwrote each other, and the second one was
# the evidence you actually wanted.
keep_rejected_text() {
    local _src="$1" _tag="$2"
    local _dir="${REMEMBER_DIR}/tmp"
    local _file="${_dir}/rejected-$(_remember_date +%Y%m%d-%H%M%S)-$$.md"
    mkdir -p "$_dir" 2>/dev/null
    # Report what actually happened. The copy used to be silenced with 2>/dev/null
    # and the success line logged unconditionally, so a full disk or a bad
    # permission produced a log entry pointing at a file that was never written.
    if cp "$_src" "$_file" 2>/dev/null; then
        log "$_tag" "rejected text kept at $_file"
    else
        log "$_tag" "WARNING: could not keep rejected text at $_file"
    fi
    # Keep the last 20; these are diagnostic, not memory.
    ls -t "${_dir}"/rejected-*.md 2>/dev/null | tail -n +21 | while read -r _old; do
        rm -f "$_old"
    done
}

# --- Step 5b: Validate format (warn, never discard) ---
# The pattern comes from pipeline/entry_header.py rather than being written out
# here. It used to be spelled once here and differently in consolidate.py — 12h
# accepted here, 24h-only there — and the two only agreed because the header is
# normally rewritten to 24h. The #139 fallback below keeps the model's ORIGINAL
# line when a rewrite would malform it, so an AM/PM header can reach memory that
# consolidation then does not recognise as an entry at all (#177).
ENTRY_HEADER_ERE=$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.entry_header --ere entry 2>/dev/null) \
    || ENTRY_HEADER_ERE=''
# A failed lookup must not silently accept everything: fall back to the literal
# pattern rather than to an empty regex, which grep matches against anything.
[ -n "$ENTRY_HEADER_ERE" ] || ENTRY_HEADER_ERE='^## ([0-9]{2}:[0-9]{2}|[0-9]{1,2}:[0-9]{2} (AM|PM)) \|'

if [ "$IS_SKIP" != "true" ]; then
    FIRST_LINE=$(head -1 "$HAIKU_TEXT_FILE")
    if ! echo "$FIRST_LINE" | grep -qE "$ENTRY_HEADER_ERE"; then
        # Not an entry header, so it is not an entry. It used to be logged as a
        # warning and appended anyway (#136), which put a permission prompt in
        # one reporter's now.md — and memory is a summary of summaries, so a
        # bad line does not fade: now.md rolls into today-*.md, then recent.md,
        # then archive.md, each hop compressing it as though it were real work.
        #
        # Dropped rather than kept, but never destroyed: the text goes to
        # tmp/rejected-*.md, so nothing is lost, it simply does not enter the
        # compression chain. The position still advances, exactly as a SKIP
        # does, or this span would be re-summarized on every run forever.
        log "validate" "REJECTED (not an entry header): $(echo "$FIRST_LINE" | head -c 80)"
        keep_rejected_text "$HAIKU_TEXT_FILE" "validate"
        cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION"
        log "validate" "position → $POSITION"
        rm -f "$FAILURE_MARKER"
        exit 0
    else
        # --- Step 5c: take the header time back off the model (#139) ---
        # The prompt injects {{TIME}} and says to copy it verbatim, but the
        # model reads a transcript full of other timestamps and sometimes
        # stamps one of those instead. The check above cannot see it — a wrong
        # time is still a well-formed one — so entries landed hours out of
        # order with nothing logged. This is the one field the pipeline knows
        # better than the model, so overwrite rather than validate. A no-op
        # when the model copied it correctly.
        # Split on the pipe itself, not on " | ". The check above only demands a
        # space BEFORE the pipe, so "## 18:30 |main" passes it — and a literal
        # " | " match then finds nothing to strip, leaving the whole original
        # line as the "rest" and writing "## 00:00 | ## 18:30 |main". That is
        # worse than the wrong time it was meant to fix. Any pipes after the
        # first belong to the branch name and are left alone.
        HEADER_REST="${FIRST_LINE#*|}"
        HEADER_REST="${HEADER_REST# }"
        NEW_FIRST_LINE="## ${CURRENT_TIME} | ${HEADER_REST}"
        # Never write a header that would not pass the check above.
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

# --- Step 6: Handle SKIP ---
if [ "$IS_SKIP" = "true" ]; then
    # A model SKIP and a reject-gate match both stay out of memory, but they are
    # not the same event. SKIP means the span held nothing worth recording; a
    # rejection means a span of real work was discarded because the reply came
    # back as a refusal or a clarifying question. Reported identically, the
    # second is invisible: the log reads like a quiet session, memory simply
    # stops growing, and max_summary_failures never notices because that counts
    # hard errors and this call succeeded.
    if [ "${IS_REJECTED:-false}" = "true" ]; then
        log "haiku" "REJECTED (not a summary — refusal or clarification): $(head -c 80 "$HAIKU_TEXT_FILE" 2>/dev/null)"
        keep_rejected_text "$HAIKU_TEXT_FILE" "haiku"
    fi
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
# Record which day now.md belongs to, when it starts. Entries carry `## HH:MM`
# and nothing else, so once now.md crosses midnight uncompressed there is no
# way to tell from the content which day it came from — and NDC filed it by the
# date it happened to RUN, putting the evening's work at the top of tomorrow's
# file (issue #141).
#
# Kept beside now.md rather than inside it. A marker line in the file would be
# the first thing session-start injects into context and the first thing the
# summarizer reads, for a fact only the pipeline needs.
if [ ! -s "$MEMORY_FILE" ]; then
    printf '%s\n' "$TODAY_DATE" > "$NOW_DAY_FILE" 2>/dev/null || true
fi
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
# features.ndc_compression has been documented in the README since the option
# existed, and was read nowhere: setting it false did nothing and compression
# ran anyway (#159). The only working brake was an unreachable combination of
# cooldowns.ndc_seconds and a hand-written marker file.
if [ "$(config '.features.ndc_compression' true)" != "true" ]; then
    RUN_NDC=false
    log "ndc" "disabled by features.ndc_compression"
fi
if [ "$RUN_NDC" = true ] && [ -f "$NDC_MARKER" ]; then
    NDC_MOD=$(cat "$NDC_MARKER" 2>/dev/null || echo 0)
    NDC_COOLDOWN=$(config ".cooldowns.ndc_seconds" 3600)
    [ $(( $(date +%s) - NDC_MOD )) -lt "$NDC_COOLDOWN" ] && RUN_NDC=false
fi

# The day the content belongs to, not the day this run happens to fall on.
# NDC only fires on a save and carries a cooldown, so now.md routinely holds
# evening entries when the first save after midnight arrives; filing by run
# date put them in the new day's file and downstream consolidation then
# attributed them to the wrong day (#141). Falls back to today for a now.md
# written before this marker existed, or left behind by an interrupted run.
NDC_DAY=$(cat "$NOW_DAY_FILE" 2>/dev/null | tr -d '[:space:]')
case "$NDC_DAY" in
    ([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    (*) NDC_DAY="$TODAY_DATE" ;;
esac
TODAY_FILE="${REMEMBER_DIR}/today-${NDC_DAY}.md"

if [ "$RUN_NDC" = true ]; then
    log "ndc" "now.md → today-${NDC_DAY}.md"
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

            if [ "$NDC_EXIT" -eq "$SPAWN_DECLINED_EXIT" ]; then
                # Same distinction as the save call above (#204). now.md is left
                # untouched either way; only the word in the log differs, and it
                # is the word that tells an operator whether to look for a
                # broken summarizer or a busy machine.
                log "ndc" "DECLINED: $(head -1 "$NDC_ERR" 2>/dev/null)"
            elif [ "$NDC_EXIT" -ne 0 ]; then
                log "ndc" "ERROR: $(head -1 "$NDC_ERR" 2>/dev/null)"
            else
                # Defensive, and not load-bearing today: Step 6 exits before
                # this block whenever either flag is true, and safe_eval rewrites
                # both from NDC_VARS on every successful call, so no reachable
                # path currently carries a stale value in here. Kept so the gate
                # below cannot quietly start reading an inherited value if either
                # invariant changes — a reset that costs nothing, guarding a
                # failure mode that writes into permanent memory.
                IS_SKIP=false
                IS_REJECTED=false
                safe_eval <<< "$NDC_VARS"
                NDC_TEXT=$(cat "$HAIKU_TEXT_FILE")
                # Before the branch, not inside the success arm: the call has
                # already gone out and already cost money by this point, and
                # what it returned does not change that. Logged only on success,
                # a model that starts refusing compression produced a run where
                # memory stopped growing AND reported cost fell to zero — which
                # reads as "nothing happened" rather than "this failed
                # repeatedly and was paid for" (#180).
                log_tokens "ndc" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"
                # Compression runs through the same reject gate as the summarize
                # call, but nothing here consumed the verdict: a refusal came
                # back non-empty, passed `[ -n ... ]`, and was appended to
                # today-*.md as if it were a day summary. Not lost memory —
                # corrupted memory, written into the permanent record with no
                # log line. now.md is left intact so the next round retries.
                if [ "$IS_SKIP" = "true" ] || [ "${IS_REJECTED:-false}" = "true" ]; then
                    log "ndc" "REJECTED (not a summary — refusal or clarification): $(head -c 80 "$HAIKU_TEXT_FILE" 2>/dev/null)"
                    keep_rejected_text "$HAIKU_TEXT_FILE" "ndc"
                elif [ -n "$NDC_TEXT" ]; then
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
                    #
                    # The offset arithmetic closes the 180s window. It does NOT
                    # close the commit itself (#223, @Jutiphan's defect (b) in
                    # #173): `tail` reads to EOF, a concurrent save appends —
                    # entitled to, since nothing was held here — and `mv` puts
                    # back a copy predating that append. Same erasure as #142,
                    # in milliseconds instead of minutes. So take the lock back
                    # for the read-and-replace. The parent released it, and this
                    # subshell is a different process: `lock_release`'s ownership
                    # check compares pids, and the EXIT trap that would have
                    # released it early is not inherited by a backgrounded
                    # subshell, so there is nothing to unwind here.
                    #
                    # Not the parent's timeout 0. The parent releases this very
                    # lock a handful of instructions after backgrounding us, so
                    # with 0 the first contender we lose to is our own parent
                    # whenever the Haiku call returns quickly — every such round
                    # would skip and duplicate its span. A bounded wait cannot
                    # deadlock (lock_acquire spins to a deadline and returns 1)
                    # and costs nothing: nothing waits on this subshell.
                    #
                    # It cannot steal the lock from a live save either — steal
                    # needs a dead pid, adoption needs no pid at all — so losing
                    # the wait means someone is genuinely holding it.
                    if lock_acquire "$LOCK_DIR" "$NDC_COMMIT_LOCK_TIMEOUT"; then
                        # Re-read the size under the lock. The offset stays valid as
                        # the START of the kept range no matter what happened during
                        # the Haiku call, but only while now.md still HAS that many
                        # bytes: a file that shrank underneath us (a rotation, or an
                        # earlier NDC round that committed its own tail first) is not
                        # the file the offset describes, and tailing past its end
                        # yields either nothing or a fragment cut mid-line, which the
                        # `mv` would then install over live content.
                        NDC_LIVE_BYTES=$(wc -c < "$MEMORY_FILE" 2>/dev/null | tr -d ' ')
                        case "$NDC_LIVE_BYTES" in
                            (''|*[!0-9]*) NDC_LIVE_BYTES=0 ;;
                        esac
                        if [ "$NDC_LIVE_BYTES" -lt "$NDC_SRC_BYTES" ]; then
                            log "ndc" "SKIPPED commit: now.md is ${NDC_LIVE_BYTES}b, below the ${NDC_SRC_BYTES}b snapshot this offset was taken from — left untouched (today-${NDC_DAY}.md may now hold a duplicate of this span)"
                        else
                            NDC_TAIL=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-tail-XXXXXX)
                            if tail -c +$(( NDC_SRC_BYTES + 1 )) "$MEMORY_FILE" > "$NDC_TAIL" 2>/dev/null; then
                                NDC_KEPT=$(wc -c < "$NDC_TAIL" | tr -d ' ')
                                mv "$NDC_TAIL" "$MEMORY_FILE"
                                # The stamped day is spent with the bytes it covered.
                                # Anything kept was appended during this compression,
                                # so stamp it with the day it is NOW — not $TODAY_DATE,
                                # which was computed once in the parent before a Haiku
                                # call that can run 180s. A compression that started
                                # before midnight would otherwise stamp those newer
                                # bytes with the previous day: not the day they were
                                # appended, not their own day, but a third stale one
                                # belonging to an earlier run. That is the #142-shaped
                                # window, and misfiling is exactly what it costs here.
                                #
                                # Still one stamp for the whole kept range. If two saves
                                # land inside this window on opposite sides of midnight,
                                # the earlier one is filed with the later one's day.
                                # Splitting the range would need each entry to carry its
                                # own day, which is the thing now.md does not have and
                                # the reason this stamp exists — see #141 for the flush
                                # design that would close it.
                                if [ "$NDC_KEPT" -gt 0 ]; then
                                    printf '%s\n' "$(_remember_date +%Y-%m-%d)" > "$NOW_DAY_FILE" 2>/dev/null || true
                                else
                                    rm -f "$NOW_DAY_FILE"
                                fi
                                [ "$NDC_KEPT" -gt 0 ] && log "ndc" "kept ${NDC_KEPT}b appended during compression"
                            else
                                # tail failed (disk, permissions, a full $TMPDIR — #173).
                                # This branch used to be `: > "$MEMORY_FILE"`, the exact
                                # blind truncate #142 removed from the success path above,
                                # reintroduced here as the failure path. now.md is left
                                # completely untouched instead: whatever tail could not
                                # read is safer sitting in now.md than erased.
                                #
                                # Cost: the span already appended to $TODAY_FILE above
                                # (line ~502) stays there, and since now.md keeps every
                                # byte, the next NDC round can summarize the same span
                                # again — a duplicated entry in today-*.md. That trade is
                                # deliberate: a duplicated summary is visible and
                                # recoverable; erased entries are neither. Rolling back
                                # the TODAY_FILE append instead was considered and
                                # rejected — TODAY_FILE can itself receive concurrent
                                # appends during this same 180s window (any other save's
                                # own NDC round, or a future flush), and truncating it
                                # back to a byte count captured earlier would risk
                                # erasing exactly that concurrent write: the same #142
                                # class of bug, moved one file over.
                                rm -f "$NDC_TAIL"
                                log "ndc" "ERROR: tail failed, now.md left untouched (today-${NDC_DAY}.md may now hold a duplicate of this span)"
                            fi
                        fi
                        # Released before the summary log line below —
                        # nothing past this point touches now.md.
                        lock_release "$LOCK_DIR" || true
                    else
                        log "ndc" "SKIPPED commit: another save held the lock for the whole ${NDC_COMMIT_LOCK_TIMEOUT}s wait, now.md left untouched (today-${NDC_DAY}.md now holds a duplicate of this span — the routine outcome of losing this race, not an error)"
                    fi
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
