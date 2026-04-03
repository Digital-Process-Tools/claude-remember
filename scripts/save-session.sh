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
#     4. Call Haiku (sandboxed: cwd=/tmp, no tools, max-turns 1)
#     5. Parse response (detect SKIP vs. content)
#     6. Append to now.md + save position
#     7. NDC compression (hourly, background subshell)
#
# ============================================================================

set -e

trap 'log "error" "FAILED at line $LINENO (exit $?)"' ERR

source "$(dirname "$0")/resolve-paths.sh"
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/log.sh"
log "hook" "save-session: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON"

REMEMBER_TZ=$(config ".timezone" "Europe/Paris")

REMEMBER_DATA="${PROJECT_DIR}/.remember"
LOCK_FILE="${REMEMBER_DATA}/tmp/save.lock"
MEMORY_FILE="${REMEMBER_DATA}/now.md"
LAST_SAVE_FILE="${REMEMBER_DATA}/tmp/last-save.json"
COOLDOWN_MARKER="${REMEMBER_DATA}/tmp/last-save-ts"
TODAY_DATE=$(TZ="$REMEMBER_TZ" date +%Y-%m-%d)
CLEANUP_FILES=()

# Auto-create data dir + gitignore on first run
mkdir -p "${REMEMBER_DATA}/tmp"
[ -f "${REMEMBER_DATA}/.gitignore" ] || echo '*' > "${REMEMBER_DATA}/.gitignore"

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

SESSION_DIR_PATH="$HOME/.claude/projects/$(echo "$PROJECT_DIR" | sed 's/[^a-zA-Z0-9]/-/g')"
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

[ "$EXCHANGE_COUNT" -eq 0 ] && { log "extract" "0 exchanges, skip"; exit 0; }
MIN_HUMAN=$(config ".thresholds.min_human_messages" 3)
[ "$HUMAN_COUNT" -lt "$MIN_HUMAN" ] && [ "$DRY_RUN" = false ] && [ "$FORCE" != true ] && { log "extract" "${HUMAN_COUNT} human msgs < ${MIN_HUMAN}, skip"; exit 0; }

if [ "$DRY_RUN" = true ]; then
    echo ""; echo "=== DRY RUN ==="; echo ""; cat "$EXTRACT_FILE"; echo ""; exit 0
fi

# --- Step 2: Get last entry ---
TMP_LAST_ENTRY=$(mktemp "${TMPDIR:-/tmp}"/remember-last-entry-XXXXXX.txt)
CLEANUP_FILES+=("$TMP_LAST_ENTRY")
if [ -f "$MEMORY_FILE" ]; then
    LAST_LINE=$(grep -n '^## ' "$MEMORY_FILE" | tail -1 | cut -d: -f1)
    [ -n "$LAST_LINE" ] && tail -n +"$LAST_LINE" "$MEMORY_FILE" > "$TMP_LAST_ENTRY" || echo "(no previous entry)" > "$TMP_LAST_ENTRY"
else
    echo "(no previous entry)" > "$TMP_LAST_ENTRY"
fi

# --- Step 3: Build prompt ---
BRANCH=$(cd "$PROJECT_DIR" && git branch --show-current 2>/dev/null || echo "unknown")
CURRENT_TIME=$(TZ="$REMEMBER_TZ" date +%H:%M)
TMP_PROMPT=$(mktemp "${TMPDIR:-/tmp}"/remember-prompt-XXXXXX.txt)
CLEANUP_FILES+=("$TMP_PROMPT")

cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell build-prompt "$EXTRACT_FILE" "$TMP_LAST_ENTRY" "$CURRENT_TIME" "$BRANCH" "$TMP_PROMPT"

[ ! -s "$TMP_PROMPT" ] && { log "prompt" "ERROR: empty"; exit 1; }
head -1 "$TMP_PROMPT" | grep -q '{{TIME}}\|{{BRANCH}}' && { log "prompt" "ERROR: unsubstituted placeholders in prompt header"; exit 1; }

# --- Step 4: Call Haiku ---
log "haiku" "calling (branch: $BRANCH)"
HAIKU_STDERR=$(mktemp "${TMPDIR:-/tmp}"/remember-haiku-err-XXXXXX.txt)
CLEANUP_FILES+=("$HAIKU_STDERR")

HAIKU_JSON=$(cd /tmp && env -u CLAUDECODE claude -p \
    --model haiku --allowedTools "" --max-turns 1 \
    --output-format json \
    --mcp-config '{"mcpServers":{}}' --strict-mcp-config \
    2>"$HAIKU_STDERR" < "$TMP_PROMPT")
HAIKU_EXIT=$?

if [ "$HAIKU_EXIT" -ne 0 ]; then
    log "haiku" "ERROR: exit $HAIKU_EXIT — $(head -1 "$HAIKU_STDERR")"; exit 1
fi

# --- Step 5: Parse response ---
safe_eval <<< "$(echo "$HAIKU_JSON" | (cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell parse-haiku))"
CLEANUP_FILES+=("$HAIKU_TEXT_FILE")
log_tokens "tokens" "$TK_IN" "$TK_OUT" "$TK_CACHE" "$TK_COST"

HAIKU_TEXT=$(cat "$HAIKU_TEXT_FILE")
[ -z "$HAIKU_TEXT" ] && { log "haiku" "ERROR: empty response"; exit 1; }

# --- Step 5b: Validate format (warn, never discard) ---
if [ "$IS_SKIP" != "true" ]; then
    FIRST_LINE=$(head -1 "$HAIKU_TEXT_FILE")
    if ! echo "$FIRST_LINE" | grep -qE '^## [0-9]{2}:[0-9]{2} \|'; then
        log "validate" "WARNING: unexpected format: $(echo "$FIRST_LINE" | head -c 80)"
    fi
fi

# --- Step 6: Handle SKIP ---
if [ "$IS_SKIP" = "true" ]; then
    log "haiku" "SKIP — position → $POSITION"
    cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION"
    exit 0
fi

# --- Step 7: Append + save position ---
echo "" >> "$MEMORY_FILE" 2>/dev/null || { log "write" "ERROR: cannot write now.md"; exit 1; }
cat "$HAIKU_TEXT_FILE" >> "$MEMORY_FILE"
log "write" "appended: $(head -1 "$HAIKU_TEXT_FILE" | cut -c1-80)"
cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell save-position "$LAST_SAVE_FILE" "$SESSION_ID" "$POSITION"
log "write" "position → $POSITION"

# --- Dispatch: after_save ---
dispatch "after_save"

# --- Step 8: NDC compression (1h cooldown, background) ---
NDC_MARKER="${PROJECT_DIR}/.remember/tmp/last-ndc.ts"
RUN_NDC=true
if [ -f "$NDC_MARKER" ]; then
    NDC_MOD=$(cat "$NDC_MARKER" 2>/dev/null || echo 0)
    NDC_COOLDOWN=$(config ".cooldowns.ndc_seconds" 3600)
    [ $(( $(date +%s) - NDC_MOD )) -lt "$NDC_COOLDOWN" ] && RUN_NDC=false
fi

TODAY_FILE="${PROJECT_DIR}/.remember/today-${TODAY_DATE}.md"

if [ "$RUN_NDC" = true ]; then
    log "ndc" "now.md → today-${TODAY_DATE}.md"
    date +%s > "$NDC_MARKER"
    NDC_SRC_BYTES=$(wc -c < "$MEMORY_FILE" | tr -d ' ')
    NDC_PROMPT=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-XXXXXX.txt)

    cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell build-ndc-prompt "$MEMORY_FILE" "$NDC_PROMPT"

    if [ -s "$NDC_PROMPT" ]; then
        (NDC_ERR=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-err-XXXXXX.txt)
            NDC_JSON=$(cd /tmp && env -u CLAUDECODE claude -p \
                --allowedTools "" --model haiku --max-turns 1 \
                --output-format json \
                --mcp-config '{"mcpServers":{}}' --strict-mcp-config \
                2>"$NDC_ERR" < "$NDC_PROMPT")

            if [ $? -ne 0 ]; then
                log "ndc" "ERROR: haiku exit $? — $(head -1 "$NDC_ERR" 2>/dev/null)"
            else
                safe_eval <<< "$(echo "$NDC_JSON" | (cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell parse-haiku))"
                NDC_TEXT=$(cat "$HAIKU_TEXT_FILE")
                if [ -n "$NDC_TEXT" ]; then
                    [ -s "$TODAY_FILE" ] && echo "" >> "$TODAY_FILE"
                    cat "$HAIKU_TEXT_FILE" >> "$TODAY_FILE"
                    : > "$MEMORY_FILE"
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
    find "${PROJECT_DIR}/.remember/logs/autonomous" -name "*.log" -empty -delete 2>/dev/null
fi
