#!/bin/bash
# lib-detach.sh - shared "launch detached, no visible console" helpers (#1002)
#
# On Windows 11 with Windows Terminal as the default terminal, a plain
# nohup-and-background launch still allocates a brand-new console --
# Windows Terminal shows it as a visible window flashing for ~1s every time
# one of this plugin's four detached saves/consolidations fires (reporter
# fmatamala, #1002).
#
# This file is the ONE place that knows how to avoid that: _remember_is_windows
# (cheap, no I/O -- safe on every platform's hot path) and
# _remember_detach_windows (the hidden-launch mechanics, Windows-only).
# Every call site keeps its own existing nohup line completely unchanged as
# the fallback/non-Windows path -- this library only offers an alternative
# BEFORE it, so non-Windows behaviour is byte-for-byte what it was before
# #1002, and a Windows launcher that cannot run for any reason falls
# straight back to that same unchanged nohup line rather than skipping the
# launch -- a flashed console beats a silently skipped save.
#
# STATUS: the Windows branch is REASONED, not OBSERVED -- there is no
# Windows box in this project's own hands to confirm no console flashes
# with this route. docs/windows.md says so; #1002's own reporter is asked
# to confirm.

# _remember_is_windows: true under MSYS2 or Cygwin on Windows, matching how
# the rest of this plugin already detects Windows (docs/windows.md's
# OSTYPE-based gate, scripts/lib-slug.sh's cygpath probe). OS is checked
# first since Windows itself sets it regardless of which POSIX layer is
# running; uname -s covers MINGW/MSYS/CYGWIN.
_remember_is_windows() {
    local sys
    sys="${OS:-}"
    [ "$sys" = "Windows_NT" ] && return 0
    sys="$(uname -s 2>/dev/null)"
    case "$sys" in
        MINGW*|MSYS*|CYGWIN*) return 0 ;;
    esac
    return 1
}

# _remember_detach_windows OUTFILE PIDFILE CMD...
#
# Launches CMD... detached with stdout+stderr appended to OUTFILE (use
# /dev/null to discard) and stdin from /dev/null, via a hidden wscript.exe
# launcher so Windows Terminal never allocates a console for it. Returns 1
# (do nothing further -- caller falls back to its own nohup line) whenever
# the hidden route cannot be set up: wscript.exe or cygpath missing, or the
# shipped VBS/pidwrap assets are not where this file expects them.
#
# Because the hidden route adds a wscript.exe-then-cmd-level process in
# front of the real command, bash's own "$!" after launching it is NOT the
# real command's PID -- unlike the plain nohup fallback, where "$!" already
# is. PIDFILE is instead written by lib-detach-pidwrap.sh, which runs as
# the real command's own first act, writes its OWN pid to PIDFILE, then
# execs into the real command so the pid it just wrote stays correct for
# the command's entire life -- the same guarantee the existing
# "echo $! > PID_FILE" line gives today, just sourced from inside the
# child instead of from the parent. Pass PIDFILE as /dev/null at call
# sites with no PID-based liveness guard to read.
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
    c_script="exec bash \"\$0\" \"\$@\" >>${outfile_q} 2>&1"

    wscript.exe //B "$vbs_win" "$bash_win" -c "$c_script" "$pidwrap" "$pidfile" "$@" >/dev/null 2>&1 &
    disown 2>/dev/null || true
    return 0
}
