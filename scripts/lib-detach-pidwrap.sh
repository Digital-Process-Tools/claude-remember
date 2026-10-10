#!/bin/sh
_of="$1"
shift
if [ "$_of" != "/dev/null" ]; then
    exec >>"$_of" 2>&1
fi
_pf="$1"
shift
if [ "$_pf" != "/dev/null" ]; then
    printf '%s' "$$" > "$_pf"
fi
exec "$@"
