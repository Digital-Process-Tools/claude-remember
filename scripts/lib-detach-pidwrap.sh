#!/bin/sh
_pf="$1"
shift
if [ "$_pf" != "/dev/null" ]; then
    printf '%s' "$$" > "$_pf"
fi
exec "$@"
