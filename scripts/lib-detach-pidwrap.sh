#!/bin/sh
_remember_pidwrap_is_null() {
    [ -z "$1" ] && return 0
    [ "$1" = "/dev/null" ] && return 0
    [ "${OS:-}" = "Windows_NT" ] || return 1
    [ "$1" = "nul" ] || [ "$1" = "NUL" ]
}
_of="$1"
shift
if ! _remember_pidwrap_is_null "$_of"; then
    exec >>"$_of" 2>&1
fi
_pf="$1"
shift
if ! _remember_pidwrap_is_null "$_pf"; then
    printf '%s' "$$" > "$_pf"
fi
exec "$@"
