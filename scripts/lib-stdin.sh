#!/bin/bash

[ -n "${_REMEMBER_LIB_STDIN_LOADED:-}" ] && return 0
_REMEMBER_LIB_STDIN_LOADED=1

_remember_stdin_join_lines() {
    local IFS=$'\n' _rs_noglob=""
    [[ "$-" == *f* ]] && _rs_noglob=1
    set -f
    set -- $_rs_buf
    [ -n "$_rs_noglob" ] || set +f
    printf -v _rs_buf '%s' "$@"
}

_remember_read_stdin_into() {
    local _rs_var="$1" _rs_buf="" _rs_part="" _rs_chunked=""
    if [ ! -t 0 ]; then
        if [ -z "${REMEMBER_NO_READ_N:-}" ]; then
            if [ "${BASH_VERSINFO[0]:-0}" -gt 4 ] 2>/dev/null; then
                _rs_chunked=1
            elif [ "${BASH_VERSINFO[0]:-0}" -eq 4 ] 2>/dev/null \
                && [ "${BASH_VERSINFO[1]:-0}" -ge 1 ] 2>/dev/null; then
                _rs_chunked=1
            fi
        fi
        if [ -n "$_rs_chunked" ]; then
            while IFS= read -r -d '' -N 1048576 -t 1 _rs_part || [ -n "$_rs_part" ]; do
                _rs_buf="$_rs_buf$_rs_part"
                _rs_part=""
            done
            if [[ "$_rs_buf" == *$'\n'* ]]; then
                _remember_stdin_join_lines
            fi
        else
            while IFS= read -r -t 1 _rs_part || [ -n "$_rs_part" ]; do
                _rs_buf="$_rs_buf$_rs_part"
                _rs_part=""
            done
        fi
    fi
    printf -v "$_rs_var" '%s' "$_rs_buf"
}
