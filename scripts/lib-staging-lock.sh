#!/usr/bin/env bash

[ -n "${_REMEMBER_LIB_STAGING_LOCK_SOURCED:-}" ] && return 0
_REMEMBER_LIB_STAGING_LOCK_SOURCED=1

_REMEMBER_STAGING_LOCK_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_STAGING_LOCK_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_STAGING_LOCK_SRC_DIR="$(pwd)"
source "$_REMEMBER_STAGING_LOCK_SRC_DIR/lib-clock.sh"
unset _REMEMBER_STAGING_LOCK_SRC_DIR

declare -F log >/dev/null 2>&1 || log() {
    printf '%s [%s] %s\n' "$(_remember_date +%H:%M:%S)" "$1" "$2" >&2
}
declare -F report_error >/dev/null 2>&1 || report_error() {
    local _msg
    _msg="$(printf '%s' "$2" | LC_ALL=C tr '[:cntrl:]' ' ')"
    log "$1" "$_msg"
    [ -d "${REMEMBER_DIR:-}/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [$1] $_msg" \
        >> "${REMEMBER_DIR}/logs/hook-errors.log" 2>/dev/null || true
    return 0
}
if declare -F config >/dev/null 2>&1; then
    _REMEMBER_CONFIG_IS_FALLBACK=0
else
    _REMEMBER_CONFIG_IS_FALLBACK=1
    config() { printf '%s\n' "${2:-}"; }
fi

STAGING_LOCK_TIMEOUT="${REMEMBER_STAGING_LOCK_TIMEOUT:-10}"

staging_lock_dir() {
    printf '%s\n' "${REMEMBER_DIR}/tmp/staging.lock"
}

staging_lock_acquire() {
    local _sla_timeout="${1:-}"
    [ -n "$_sla_timeout" ] || _sla_timeout="$STAGING_LOCK_TIMEOUT"
    lock_acquire "$(staging_lock_dir)" "$_sla_timeout"
}

staging_lock_release() {
    lock_release "$(staging_lock_dir)" || true
}

staging_append() {
    local _today="$1" _text="$2"
    local _before=0
    [ -f "$_today" ] && _before=$(wc -c < "$_today" 2>/dev/null | tr -d ' ')
    if [ -z "$_before" ] || [ "${_before#*[!0-9]}" != "$_before" ]; then _before=0; fi
    [ -s "$_today" ] && echo "" >> "$_today"
    cat "$_text" >> "$_today"
    local _warn_bytes
    _warn_bytes=$(config ".thresholds.staging_warn_bytes" 2000000)
    if [ -z "$_warn_bytes" ] || [ "${_warn_bytes#*[!0-9]}" != "$_warn_bytes" ]; then _warn_bytes=2000000; fi
    if [ "${_REMEMBER_CONFIG_IS_FALLBACK:-0}" = 1 ] \
        && [ -n "${REMEMBER_CONFIG:-}" ] && [ -e "$REMEMBER_CONFIG" ]; then
        report_error "staging" "could not confirm .thresholds.staging_warn_bytes is genuinely unset -- REMEMBER_CONFIG (${REMEMBER_CONFIG}) exists, but log.sh never had the chance to parse it (#361/#372), so the ${_warn_bytes}b default below may not be the configured value"
    fi
    if [ "$_warn_bytes" -gt 0 ] && [ "$_before" -lt "$_warn_bytes" ]; then
        local _after
        _after=$(wc -c < "$_today" 2>/dev/null | tr -d ' ')
        if [ -z "$_after" ] || [ "${_after#*[!0-9]}" != "$_after" ]; then _after=0; fi
        if [ "$_after" -ge "$_warn_bytes" ]; then
            report_error "staging" "WARNING: ${_today} has grown past ${_warn_bytes}b -- this file is append-only and only a SUCCESSFUL consolidation round retires it. Sustained lock contention, a full disk, or consolidation having stopped (check features.ndc_compression and hook-errors.log for consolidation failures) will keep appending the same kind of span here without bound. Nothing was dropped or truncated."
        fi
    fi
}
