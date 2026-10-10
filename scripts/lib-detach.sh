#!/bin/bash

_remember_is_windows() {
    local sys
    sys="${OS:-}"
    [ "$sys" = "Windows_NT" ] && return 0
    sys="$(uname -s 2>/dev/null)"
    [ "${sys#MINGW}" != "$sys" ] || [ "${sys#MSYS}" != "$sys" ] \
        || [ "${sys#CYGWIN}" != "$sys" ]
}

_REMEMBER_DETACH_WIN_CACHE="${TMPDIR:-/tmp}/remember-detach-windows-cache"

_REMEMBER_DETACH_WIN_CACHE_TTL=3600

_REMEMBER_DETACH_WIN_SLOW_TTL=300

_remember_detach_windows_cache_load() {
    [ "${REMEMBER_DETACH_WIN_CACHE:-1}" = "1" ] || return 1
    local _f="$_REMEMBER_DETACH_WIN_CACHE"
    [ -f "$_f" ] && [ ! -L "$_f" ] && [ -O "$_f" ] && [ -r "$_f" ] || return 1
    local _line _path="" _verdict="" _ts=""
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        if [ "${_line#CACHE_PATH=}" != "$_line" ]; then
            _path="${_line#*=}"
        elif [ "${_line#VERDICT=}" != "$_line" ]; then
            _verdict="${_line#*=}"
        elif [ "${_line#CACHE_TS=}" != "$_line" ]; then
            _ts="${_line#*=}"
        else
            return 1
        fi
    done < "$_f"
    [ -n "$_path" ] && [ "$_path" = "$PATH" ] || return 1
    local _ttl
    if [ "$_verdict" = "unusable" ]; then
        _ttl="$_REMEMBER_DETACH_WIN_CACHE_TTL"
    elif [ "$_verdict" = "slow" ]; then
        _ttl="$_REMEMBER_DETACH_WIN_SLOW_TTL"
    else
        return 1
    fi
    [[ "$_ts" =~ ^[0-9]+$ ]] || return 1
    local _now
    _now=$(date +%s 2>/dev/null) || return 1
    (( _now - _ts < _ttl )) || return 1
    return 0
}

_remember_detach_windows_cache_publish_unusable() {
    [ "${REMEMBER_DETACH_WIN_CACHE:-1}" = "1" ] || return 0
    local _f="$_REMEMBER_DETACH_WIN_CACHE" _t _now
    _now=$(date +%s 2>/dev/null) || return 0
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    printf '%s=%s\n' CACHE_PATH "$PATH" VERDICT unusable CACHE_TS "$_now" \
        > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t" 2>/dev/null
    return 0
}

_remember_detach_windows_cache_publish_slow() {
    [ "${REMEMBER_DETACH_WIN_CACHE:-1}" = "1" ] || return 0
    local _f="$_REMEMBER_DETACH_WIN_CACHE" _t _now
    _now=$(date +%s 2>/dev/null) || return 0
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    printf '%s=%s\n' CACHE_PATH "$PATH" VERDICT slow CACHE_TS "$_now" \
        > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t" 2>/dev/null
    return 0
}

_remember_detach_windows() {
    _remember_detach_windows_cache_load && return 1
    _remember_detach_windows_impl "$@"
    local _rc=$?
    if [ "$_rc" -eq 137 ]; then
        _remember_detach_windows_cache_publish_slow
    elif [ "$_rc" -ne 0 ]; then
        _remember_detach_windows_cache_publish_unusable
    fi
    return "$_rc"
}

_remember_detach_windows_impl() {
    local outfile pidfile
    outfile="$1"
    pidfile="$2"
    shift 2

    local lib_dir
    lib_dir="${BASH_SOURCE[0]%/*}"
    [ "$lib_dir" = "${BASH_SOURCE[0]}" ] && lib_dir="$(pwd)"

    local vbs pidwrap
    vbs="$lib_dir/windows-hidden-run.vbs"
    pidwrap="$lib_dir/lib-detach-pidwrap.sh"
    [ -f "$vbs" ] || return 1
    [ -f "$pidwrap" ] || return 1
    command -v wscript.exe >/dev/null 2>&1 || return 1
    command -v cygpath >/dev/null 2>&1 || return 1

    local vbs_win
    vbs_win="$(cygpath -w "$vbs" 2>/dev/null)" || return 1
    [ -n "$vbs_win" ] || return 1

    local bash_path bash_win
    bash_path="$(command -v bash)" || return 1
    bash_win="$(cygpath -w "$bash_path" 2>/dev/null)" || return 1
    [ -n "$bash_win" ] || return 1

    local real_cmd=("$@")
    if [ "${real_cmd[0]}" = "bash" ]; then
        real_cmd[0]="$bash_path"
    fi

    local _watchdog_secs="${_REMEMBER_DETACH_WIN_WATCHDOG_SECS:-15}"
    wscript.exe //B "$vbs_win" "$bash_win" "$pidwrap" "$outfile" "$pidfile" "${real_cmd[@]}" >/dev/null 2>&1 &
    local wscript_pid=$!
    ( sleep "$_watchdog_secs"; kill -9 "$wscript_pid" 2>/dev/null ) >/dev/null 2>&1 &
    local watchdog_pid=$!
    wait "$wscript_pid" 2>/dev/null
    local wscript_rc=$?
    kill "$watchdog_pid" 2>/dev/null
    wait "$watchdog_pid" 2>/dev/null
    return "$wscript_rc"
}
