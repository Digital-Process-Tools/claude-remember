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
#
# CALLERS: only post-tool-hook.sh's save wires into this file today -- the
# one site the reporter actually measured flashing a console every few
# minutes. The other three sites (SessionStart's consolidation,
# SessionEnd's self-redetach, the Antigravity Stop hook's save) are
# tracked in #1006 rather than wired here: SessionStart has no compiled
# release-tree budget left to absorb it, and wiring SessionEnd surfaced a
# real regression under review -- a wscript-launched child does not
# inherit the parent's stdin the way `nohup ... &` does, so SessionEnd
# would lose the hook's own JSON payload on the hidden route. Those three
# keep their unchanged nohup lines, so they still flash a console.

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
    # #898 round 19: written as prefix tests instead of a `case`, which the
    # directory's scanner mis-parses -- see _check_case_statement.
    [ "${sys#MINGW}" != "$sys" ] || [ "${sys#MSYS}" != "$sys" ] \
        || [ "${sys#CYGWIN}" != "$sys" ]
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

    # #1002 review: a call site may hand us a bare "bash" as the real
    # command's own argv[0] (session-end-hook.sh and agy-stop-hook.sh both
    # re-invoke themselves as `bash SCRIPT`, rather than exec'ing SCRIPT
    # directly the way post-tool-hook.sh does). That bareword then travels
    # through one more layer (lib-detach-pidwrap.sh's `exec "$@"`) before
    # it is ever resolved (#1002 round 2: the bash -c driver this comment
    # once described no longer exists, see the wscript.exe call below) --
    # an extra place a bare "bash" could resolve to the wrong
    # interpreter on a Windows host with WSL installed (its own bash.exe
    # launcher stub lives in System32, ahead of Git's own bin directories
    # on some PATH orderings). Resolve it ONCE, here, to the same absolute
    # path $bash_path already is, rather than trust three more PATH
    # lookups deep in a process chain this function cannot observe.
    local real_cmd=("$@")
    if [ "${real_cmd[0]}" = "bash" ]; then
        real_cmd[0]="$bash_path"
    fi

    # #1002 round 2 (release-audit v0.42.3 gate 3, trap A): the previous
    # shape built a bash -c SCRIPT whose script string embedded literal
    # double quotes (exec bash "$0" "$@" >>OUTFILE 2>&1) and handed that
    # whole string to wscript.exe as ONE argv entry. Whether a quote
    # embedded that way survives the exec->CreateProcess argv-to-command-
    # line conversion and WSH's own command-line parse was never settled
    # on a real Windows host -- CI only ever exercised a bash stub
    # standing in for wscript.exe (tests/test_windows_hidden_detach_1002.py),
    # which cannot see either side of that parse. Removing the embedded
    # quotes removes the question: OUTFILE and PIDFILE are now each their
    # own separate WScript.Arguments entry, quoted (if at all) by
    # windows-hidden-run.vbs's own QuoteArg -- the same tested,
    # CreateProcess-convention quoting every other argv entry already
    # gets -- and lib-detach-pidwrap.sh does its own redirection from
    # OUTFILE directly, rather than via a second nested bash -c. No
    # argument on this call ever carries a literal quote any more.
    #
    # #1002 round 2 (trap B): the previous shape backgrounded wscript.exe
    # itself with a trailing "&" and then unconditionally returned 0, so
    # a wscript.exe launch failure (e.g. Windows Script Host disabled)
    # was invisible to the caller -- post-tool-hook.sh's own nohup
    # fallback only fires on a non-zero return. WshShell.Run's own wait
    # flag is already False (async: it returns as soon as the real
    # command is spawned, not when it finishes), so wscript.exe's own
    # process exits almost immediately either way -- backgrounding it
    # here was never needed for responsiveness, only hid its exit
    # status. Run it in the FOREGROUND instead and let its exit status
    # become this function's own return value: non-zero when WSH itself
    # could not run the script at all, reaching the caller and
    # triggering the nohup fallback instead of a silent skip.
    wscript.exe //B "$vbs_win" "$bash_win" "$pidwrap" "$outfile" "$pidfile" "${real_cmd[@]}" >/dev/null 2>&1
}
