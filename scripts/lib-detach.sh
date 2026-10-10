#!/bin/bash

_remember_is_windows() {
    local sys
    sys="${OS:-}"
    [ "$sys" = "Windows_NT" ] && return 0
    sys="$(uname -s 2>/dev/null)"
    [ "${sys#MINGW}" != "$sys" ] || [ "${sys#MSYS}" != "$sys" ] \
        || [ "${sys#CYGWIN}" != "$sys" ]
}

_remember_detach_windows() {
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

    local outfile_q
    outfile_q="$(printf '%q' "$outfile")"
    local c_script
    c_script='exec bash "$0" "$@" >>'"${outfile_q}"' 2>&1'

    local real_cmd=("$@")
    if [ "${real_cmd[0]}" = "bash" ]; then
        real_cmd[0]="$bash_path"
    fi

    wscript.exe //B "$vbs_win" "$bash_win" -c "$c_script" "$pidwrap" "$pidfile" "${real_cmd[@]}" >/dev/null 2>&1 &
    disown 2>/dev/null || true
    return 0
}
