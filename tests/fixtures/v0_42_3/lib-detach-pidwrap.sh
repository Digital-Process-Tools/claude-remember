#!/bin/sh
# lib-detach-pidwrap.sh (#1002) -- shipped plugin asset, never a generated
# temp file. The Windows hidden-launch route in lib-detach.sh adds a
# wscript.exe-then-cmd process in front of the real command, so the shell's
# own pid of THAT process is not the real command's pid. This script runs
# first instead: write its own pid to PIDFILE (its own, because exec below
# replaces this process image without changing the pid), then exec into
# the real command, keeping that same pid for the command's entire life --
# the same guarantee a plain "nohup CMD &" already gives "$!" for free on
# every other platform.
#
# Usage: lib-detach-pidwrap.sh PIDFILE CMD...
# PIDFILE may be /dev/null to skip writing (no liveness guard at that call
# site).
_pf="$1"
shift
if [ "$_pf" != "/dev/null" ]; then
    printf '%s' "$$" > "$_pf"
fi
exec "$@"
