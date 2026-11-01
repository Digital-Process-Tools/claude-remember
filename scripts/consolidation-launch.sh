#!/bin/bash
set -u
_lib_dir="${BASH_SOURCE[0]%/*}"
[ "$_lib_dir" = "${BASH_SOURCE[0]}" ] && _lib_dir="$(pwd)"
source "$_lib_dir/lib-detach.sh"

_detach_rc=1
_remember_is_windows && { _remember_detach_windows /dev/null /dev/null "$@"; _detach_rc=$?; }
if [ "$_detach_rc" -ne 0 ]; then
    nohup "$@" </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
fi
