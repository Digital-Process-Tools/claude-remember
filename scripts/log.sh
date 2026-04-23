#!/bin/bash
# ============================================================================
# log.sh — Shared logging and utility functions for memory pipeline scripts
# ============================================================================
#
# DESCRIPTION
#   Provides timestamped logging, token usage tracking, safe shell evaluation,
#   config reading, and log rotation. Sourced by every other script in the
#   memory pipeline — never executed directly.
#
# USAGE
#   source "$(dirname "$0")/log.sh"
#   log "save" "5 exchanges extracted"
#   log_tokens "save" 1247 342
#   config ".cooldowns.save_seconds" 120
#
# ENVIRONMENT
#   PROJECT_DIR   Project root (default: .)
#
# OUTPUT
#   .remember/logs/memory-YYYY-MM-DD.log
#   Format: HH:MM:SS [component] message
#
# DEPENDENCIES
#   jq (optional, for config reading)
#   date, find, tar (for log rotation)
#
# FUNCTIONS
#   log             Log a timestamped message
#   log_tokens      Log token usage with optional cost
#   safe_eval       Evaluate only valid shell variable assignments from stdin
#   config          Read a value from config.json with jq, with fallback default
#   rotate_logs     Archive log files older than 7 days into monthly tarballs
#
# ============================================================================

REMEMBER_LOG_DIR="${PROJECT_DIR:-.}/.remember/logs"
if ! mkdir -p "$REMEMBER_LOG_DIR" 2>/dev/null; then
    echo "FATAL: cannot create $REMEMBER_LOG_DIR" >&2
    return 1 2>/dev/null || true
fi

# Read .timezone from config.json BEFORE computing MEMORY_LOG_DATE — otherwise
# TZ="" falls back to UTC on macOS/BSD and produces next-day filenames after
# ~20:00 local in zones west of UTC.
REMEMBER_CONFIG="${PIPELINE_DIR:-${PROJECT_DIR:-.}/.claude/remember}/config.json"
config() {
    local key="$1"
    local default="$2"
    if [ -f "$REMEMBER_CONFIG" ] && command -v jq >/dev/null 2>&1; then
        local val
        val=$(jq -r "$key // empty" "$REMEMBER_CONFIG" 2>/dev/null)
        [ -n "$val" ] && echo "$val" || echo "$default"
    else
        echo "$default"
    fi
}

REMEMBER_TZ=$(config ".timezone" "")
export REMEMBER_TZ

# Resolve "today" / "now" using REMEMBER_TZ when set, else system local.
# Crucially, an empty REMEMBER_TZ must NOT produce `TZ="" date` — that's UTC.
_remember_date() {
    if [ -n "$REMEMBER_TZ" ]; then
        TZ="$REMEMBER_TZ" date "$@"
    else
        date "$@"
    fi
}

MEMORY_LOG_DATE=$(_remember_date +%Y-%m-%d)
MEMORY_LOG_FILE="${REMEMBER_LOG_DIR}/memory-${MEMORY_LOG_DATE}.log"

# Log a timestamped message to the daily pipeline log file.
#
# Args:
#   $1 — component name (e.g., "save", "consolidate", "team")
#   $2 — message text
#
# Output:
#   Appends "HH:MM:SS [component] message" to daily log file.
#   Falls back to stderr if log file is unwritable.
log() {
    local component="$1"
    local message="$2"
    local timestamp
    timestamp=$(_remember_date +%H:%M:%S)
    echo "${timestamp} [${component}] ${message}" >> "$MEMORY_LOG_FILE" 2>/dev/null \
        || echo "${timestamp} [${component}] ${message}" >&2
}

# Log token usage for a Haiku API call.
#
# Args:
#   $1 — component name (e.g., "save", "ndc", "team")
#   $2 — input token count (default: 0)
#   $3 — output token count (default: 0)
#   $4 — cache read token count (optional, default: 0)
#   $5 — cost in USD (optional, appended if provided)
#
# Output:
#   Logs "tokens: {in}+{cache}cache->{out}out ($cost)" via log()
log_tokens() {
    local component="$1"
    local input="${2:-0}"
    local output="${3:-0}"
    local cache="${4:-0}"
    local cost="${5:-}"
    local msg="tokens: ${input}+${cache}cache→${output}out"
    [ -n "$cost" ] && msg="${msg} (\$${cost})"
    log "$component" "$msg"
}

# Safely evaluate shell variable assignments from stdin.
#
# Reads lines from stdin and only eval's lines matching the pattern
# UPPER_CASE_VAR=... — rejects everything else (Python warnings,
# tracebacks, debug prints, or injected commands).
#
# Args:
#   (none — reads from stdin)
#
# Usage:
#   safe_eval <<< "$(python3 -m pipeline.shell extract ...)"
safe_eval() {
    while IFS= read -r line; do
        if [[ "$line" =~ ^[A-Z_][A-Z0-9_]*= ]]; then
            eval "$line"
        fi
    done
}
# Dispatch a lifecycle event to all registered hooks.
#
# Runs every executable in hooks.d/<event>/, passing the project path
# as REMEMBER_PROJECT. Hooks run sequentially, failures are logged
# but don't stop the pipeline.
#
# Args:
#   $1 — event name (e.g., "after_save", "before_consolidate")
#
# Usage:
#   dispatch "after_save"
REMEMBER_HOOKS_DIR="${PIPELINE_DIR:-${PROJECT_DIR:-.}/.claude/remember}/hooks.d"
dispatch() {
    local event="$1"
    local event_dir="$REMEMBER_HOOKS_DIR/$event"
    [ -d "$event_dir" ] || return 0
    for hook in "$event_dir"/*; do
        [ -x "$hook" ] || continue
        REMEMBER_PROJECT="${PROJECT_DIR:-.}" "$hook" 2>/dev/null \
            || log "dispatch" "hook failed: $event/$(basename "$hook")"
    done
}

# Archive log files older than 7 days into monthly tar.gz bundles.
#
# Finds memory-*.log files with mtime > 7 days, compresses them into
# logs-YYYY-MM.tar.gz, and removes the originals on success.
# No-op if no old logs exist.
#
# Args:
#   (none — operates on REMEMBER_LOG_DIR)
#
# Side effects:
#   Creates logs-YYYY-MM.tar.gz in the log directory.
#   Deletes archived .log files.
rotate_logs() {
    local old_logs
    old_logs=$(find "$REMEMBER_LOG_DIR" -name "memory-*.log" -mtime +7 2>/dev/null)
    [ -z "$old_logs" ] && return 0

    local archive_month
    archive_month=$(date -v-7d +%Y-%m 2>/dev/null || date -d '7 days ago' +%Y-%m)
    local archive="${REMEMBER_LOG_DIR}/logs-${archive_month}.tar.gz"
    local count
    count=$(echo "$old_logs" | wc -l | tr -d ' ')

    local basenames=()
    while IFS= read -r f; do
        basenames+=("$(basename "$f")")
    done <<< "$old_logs"

    if tar -czf "$archive" -C "$REMEMBER_LOG_DIR" "${basenames[@]}" 2>/dev/null; then
        while IFS= read -r f; do rm -f "$f"; done <<< "$old_logs"
        log "rotate" "archived ${count} logs → $(basename "$archive")"
    else
        log "rotate" "ERROR: tar failed for ${count} logs"
    fi
}
