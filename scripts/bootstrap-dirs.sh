#!/bin/bash

_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
source "$_REMEMBER_SRC_DIR/lib-memory-dir.sh"
unset _REMEMBER_SRC_DIR

SYS_TMPDIR="${TMPDIR:-/tmp}"

_mem_proj="${MEMORY_PROJECT_DIR:-}"
[ -n "$_mem_proj" ] || _mem_proj="$PROJECT_DIR"
_legacy_dir="${_mem_proj}/.remember"
_legacy_rd="$REMEMBER_DIR"
if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
    _legacy_dir="${_legacy_dir//\\//}"
    _legacy_rd="${_legacy_rd//\\//}"
fi
if [ "$_legacy_rd" != "$_legacy_dir" ] && [ "${_legacy_rd#"$_legacy_dir"/}" = "$_legacy_rd" ] \
    && [ ! -e "$REMEMBER_DIR" ] && [ -d "$_legacy_dir" ]; then
    for _legacy_f in now.md recent.md archive.md core-memories.md remember.md; do
        if [ -f "$_legacy_dir/$_legacy_f" ]; then
            printf 'remember: %s holds memory data but data_dir points to %s -- move it by hand (see /remember:doctor)\n' \
                "$_legacy_dir" "$REMEMBER_DIR" >&2
            break
        fi
    done
    unset _legacy_f
fi
unset _legacy_dir _legacy_rd

if [ ! -d "$REMEMBER_DIR/logs/autonomous" ] || [ ! -d "$REMEMBER_DIR/tmp" ]; then
    mkdir -p \
        "$REMEMBER_DIR/tmp" \
        "$REMEMBER_DIR/logs" \
        "$REMEMBER_DIR/logs/autonomous" \
        2>/dev/null
fi

if [ -d "$REMEMBER_DIR/tmp" ]; then
    for _remember_stale_cfg in "$REMEMBER_DIR/tmp"/remember-config-*.json; do
        [ -e "$_remember_stale_cfg" ] || [ -L "$_remember_stale_cfg" ] || continue
        find "$REMEMBER_DIR/tmp" -maxdepth 1 -name 'remember-config-*.json' \
            -mmin +30 -exec rm -f {} + 2>/dev/null || true
        break
    done
    unset _remember_stale_cfg

    _remember_relocated_cfg="$REMEMBER_DIR/tmp/remember-config-$$.json"
    if [ -n "${REMEMBER_CONFIG:-}" ] && [ -f "$REMEMBER_CONFIG" ] \
        && mv -f "$REMEMBER_CONFIG" "$_remember_relocated_cfg" 2>/dev/null; then
        REMEMBER_CONFIG="$_remember_relocated_cfg"
        export REMEMBER_CONFIG
        _remember_relocated_cfg_q=$(printf %q "$_remember_relocated_cfg")
        _remember_trap_raw=$(trap -p EXIT 2>/dev/null)
        _remember_existing_trap="${_remember_trap_raw#trap -- \'}"
        _remember_existing_trap="${_remember_existing_trap%\' EXIT}"
        _remember_existing_trap="${_remember_existing_trap//\'\\\'\'/\'}"
        unset _remember_trap_raw
        if [ -n "$_remember_existing_trap" ]; then
            trap "${_remember_existing_trap}; rm -f ${_remember_relocated_cfg_q}" EXIT
        else
            trap "rm -f ${_remember_relocated_cfg_q}" EXIT
        fi
        unset _remember_existing_trap
        unset _remember_relocated_cfg_q
    fi
    unset _remember_relocated_cfg
fi

if [ -d "$REMEMBER_DIR" ]; then
    [ -f "$REMEMBER_DIR/.install-marker" ] \
        || { echo 'This file marks when remember was first bootstrapped here. Read only by /remember:doctor (#401); do not delete it.' \
            > "$REMEMBER_DIR/.install-marker"; } 2>/dev/null
fi

if [ -d "$REMEMBER_DIR" ]; then
    _mem_bd_glob_dir="$REMEMBER_DIR"
    _mem_bd_glob_proj="$_mem_proj"
    if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
        _mem_bd_glob_dir="${_mem_bd_glob_dir//\\//}"
        _mem_bd_glob_proj="${_mem_bd_glob_proj//\\//}"
    fi
    if [ "${_mem_bd_glob_dir#"$_mem_bd_glob_proj"/}" != "$_mem_bd_glob_dir" ]; then
        [ -f "$REMEMBER_DIR/.gitignore" ] || { echo '*' > "$REMEMBER_DIR/.gitignore"; } 2>/dev/null
    fi
fi
unset _mem_proj _mem_bd_glob_dir _mem_bd_glob_proj

if [ -d "$REMEMBER_DIR/logs" ]; then
    _remember_bd_keep_fd2=""
    if [[ "$-" == *x* ]]; then
        if ! { [ "${BASH_VERSINFO[0]:-0}" -gt 4 ] || { [ "${BASH_VERSINFO[0]:-0}" -eq 4 ] && [ "${BASH_VERSINFO[1]:-0}" -ge 1 ]; }; } 2>/dev/null; then
            _remember_bd_keep_fd2="an xtrace is running and this bash (< 4.1) has no BASH_XTRACEFD, so it stays on fd 2"
        elif [ "${BASH_XTRACEFD:-2}" = "2" ]; then
            _remember_bd_keep_fd2="an xtrace is running on fd 2"
        fi
    fi
    [ "${REMEMBER_TRACE:-}" = "1" ] && _remember_bd_keep_fd2="REMEMBER_TRACE=1"
    if [ -n "$_remember_bd_keep_fd2" ]; then
        printf 'remember: %s, so stderr is NOT being redirected to %s -- point BASH_XTRACEFD at its own fd to get both (#690)\n' \
            "$_remember_bd_keep_fd2" "$REMEMBER_DIR/logs/hook-errors.log" >&2
    else
        exec 2>> "$REMEMBER_DIR/logs/hook-errors.log"
    fi
    unset _remember_bd_keep_fd2
fi
