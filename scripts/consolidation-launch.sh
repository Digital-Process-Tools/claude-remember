#!/bin/bash
set -u
_lib_dir="${BASH_SOURCE[0]%/*}"
[ "$_lib_dir" = "${BASH_SOURCE[0]}" ] && _lib_dir="$(pwd)"
source "$_lib_dir/lib-detach.sh"

_detach_rc=1
if _remember_is_windows; then
    if [ -f ./nul ] && rm -f ./nul 2>/dev/null; then
        echo "[remember] removed stray 'nul' file (reserved Windows name, #1132)" >&2
    fi
    _remember_detach_windows /dev/null /dev/null "$@"; _detach_rc=$?
fi
if [ "$_detach_rc" -ne 0 ]; then
    nohup "$@" </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
fi
