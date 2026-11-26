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
# #1002 round 2 (release-audit v0.42.3 gate 3, trap A): this script now
# also owns the OUTFILE redirect that used to be a separate bash -c
# "exec bash \"$0\" \"$@\" >>OUTFILE 2>&1" wrapper lib-detach.sh built as
# one argv entry with embedded literal quotes -- whether those quotes
# survived the exec->CreateProcess->WSH argv hop unmangled was never
# settled on a real Windows host. OUTFILE is now its own separate argv
# entry (no embedded quotes anywhere on this call), and the redirect
# happens here, inside a real shell, instead of being spelled out as a
# string for another shell to re-parse.
#
# Usage: lib-detach-pidwrap.sh OUTFILE PIDFILE CMD...
# OUTFILE and PIDFILE may each independently be empty or /dev/null (on Windows also MSYS's converted nul) to skip that
# step (no redirect, or no liveness guard, at that call site).
# #1132: lib-detach.sh now hands wscript an EMPTY string for a /dev/null
# placeholder (MSYS rewrites a literal /dev/null argv entry to "nul", but
# never touches an empty one), so empty and /dev/null both mean skip. A
# "nul"/"NUL" arriving anyway (an older caller) is the MSYS-converted form:
# skipped ONLY on Windows (OS=Windows_NT, set by Windows itself), because on
# Linux/macOS NUL is a legitimate filename. Plain [ ] tests, no case.
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
