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
# --- Hidden-route verdict cache (#1002 round 3) ---
# With Windows Script Host disabled by policy, every _remember_detach_windows
# call used to pay the full watchdog bound (foreground wscript.exe, killed
# after the bound elapses) before falling back to nohup -- and PostToolUse
# calls this on every tool call, so an affected host stalled every single
# save by that amount, forever, never just once. The verdict (can this host
# reach the hidden route at all, right now) cannot change unless PATH
# changes -- same reasoning as detect-tools.sh's own #668 tool-verdict
# cache, which this mirrors: identity is the whole PATH string, byte for
# byte, since there is no single "source file" whose mtime could track a
# PATH change the way the other #668 caches key on file mtimes.
#
# Lives under the system temp dir (like detect-tools.sh's own cache file),
# machine/PATH-global rather than per-project -- whether this host's WSH is
# usable does not depend on which project a hook is running for.
#
# SECURITY: same convention as detect-tools.sh/lib-env-cache.sh -- read only
# when it is a regular file (never a symlink) owned by the current user; a
# pre-planted file from another user on a shared tmp dir simply fails that
# check and falls through to a real (re-)attempt, exactly as if no cache
# existed.
_REMEMBER_DETACH_WIN_CACHE="${TMPDIR:-/tmp}/remember-detach-windows-cache"

# Only the "unusable" verdict is ever cached. A "usable" verdict is cheap to
# re-earn (the wscript.exe call itself succeeds quickly on a healthy host --
# see the watchdog-bound comment below), and caching it would mean a host
# that becomes unusable mid-session (WSH disabled while a long session is
# open) keeps being told it is fine. Caching only the expensive failure is
# the asymmetry that actually matters: it is the repeated 10s-class stall
# this cache exists to remove, never the fast path.
#
# TTL, not a permanent verdict (self-review finding, both spawns independently):
# unlike detect-tools.sh's own #668 cache, where the thing being cached
# (which python/jq resolves on PATH) genuinely CANNOT change unless PATH
# itself changes, "is the hidden route usable" can change with NO PATH
# change at all -- the dominant real trigger is Windows Script Host being
# toggled on/off via registry policy (the same HKCU key
# test_windows_real_wscript_1002.py's _WSH_KEY names), which has nothing
# to do with PATH. Caching that verdict forever, keyed only on PATH, would
# mean a host whose WSH gets re-enabled stays stuck on the nohup fallback
# indefinitely -- silently defeating the console-flash fix #1002 exists to
# deliver, with nothing to notice or self-correct it. A TTL bounds the
# damage to "at most one watchdog-bound stall per TTL window" instead of
# "once per host, forever": the repeated-every-save stall this cache was
# built to remove is still removed, and a host whose condition changes
# recovers on its own within one TTL window rather than never.
_REMEMBER_DETACH_WIN_CACHE_TTL=3600

_remember_detach_windows_cache_load() {
    [ "${REMEMBER_DETACH_WIN_CACHE:-1}" = "1" ] || return 1
    local _f="$_REMEMBER_DETACH_WIN_CACHE"
    [ -f "$_f" ] && [ ! -L "$_f" ] && [ -O "$_f" ] && [ -r "$_f" ] || return 1
    local _line _path="" _verdict="" _ts=""
    # `[ ]` prefix tests, not a `case` with a catch-all `*)` arm inside this
    # loop (#898 round 7 -- that shape is one the plugin directory's
    # scanner holds a submission on; detect-tools.sh's own cache loader
    # uses the identical shape for the identical reason).
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        if [ "${_line#CACHE_PATH=}" != "$_line" ]; then
            _path="${_line#*=}"
        elif [ "${_line#VERDICT=}" != "$_line" ]; then
            _verdict="${_line#*=}"
        elif [ "${_line#CACHE_TS=}" != "$_line" ]; then
            _ts="${_line#*=}"
        else
            # Unknown line: not our file, or not our version of it --
            # distrust the whole thing rather than partially validate it.
            return 1
        fi
    done < "$_f"
    # An EMPTY PATH compares equal to itself just as readily as a real one --
    # never let a process that genuinely has no PATH short-circuit real
    # detection on that coincidence.
    [ -n "$_path" ] && [ "$_path" = "$PATH" ] || return 1
    [ "$_verdict" = "unusable" ] || return 1
    # A cache written before this TTL field existed carries no CACHE_TS at
    # all -- distrust it outright (same convention as detect-tools.sh's
    # PYFLOOR check) rather than treat a missing timestamp as "always
    # fresh", which would silently resurrect the no-TTL behaviour for
    # exactly the hosts most likely to still have an old cache file lying
    # around.
    [[ "$_ts" =~ ^[0-9]+$ ]] || return 1
    local _now
    _now=$(date +%s 2>/dev/null) || return 1
    (( _now - _ts < _REMEMBER_DETACH_WIN_CACHE_TTL )) || return 1
    return 0
}

_remember_detach_windows_cache_publish_unusable() {
    [ "${REMEMBER_DETACH_WIN_CACHE:-1}" = "1" ] || return 0
    local _f="$_REMEMBER_DETACH_WIN_CACHE" _t _now
    _now=$(date +%s 2>/dev/null) || return 0
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    printf '%s=%s\n' CACHE_PATH "$PATH" VERDICT unusable CACHE_TS "$_now" \
        > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t" 2>/dev/null
    return 0
}

# Public entry point: a cached "unusable" verdict short-circuits straight to
# the caller's own nohup fallback, paying none of the checks below and none
# of the watchdog bound. Everything that used to be the whole function body
# is now _remember_detach_windows_impl; any failure it returns gets cached
# here, in the one place every failing return path already funnels through,
# rather than touching each of its own early "return 1"s individually.
_remember_detach_windows() {
    _remember_detach_windows_cache_load && return 1
    _remember_detach_windows_impl "$@"
    local _rc=$?
    [ "$_rc" -ne 0 ] && _remember_detach_windows_cache_publish_unusable
    return "$_rc"
}

_remember_detach_windows_impl() {
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
    #
    # #1002 round 2, live CI finding: a bare foreground call hung this
    # exact CI job on windows-latest -- WSH disabled is not guaranteed to
    # fail wscript.exe silently; it can surface as a blocking UI prompt
    # that never resolves on a headless runner, and would hang the real
    # PostToolUse hook the same way. A hard bound closes that hole.
    #
    # Implemented as a plain bash background-job-plus-watchdog pair,
    # never the external `timeout` binary: that coreutils tool is on the
    # real Windows target's own PATH (it ships with every supported
    # Windows shell this doc names), but is NOT guaranteed on the hosts
    # this function's own stub-based test suite simulates Windows on --
    # observed absent on a bare macos-latest CI image, which turned
    # "WSH is fine" into "always return 1" for every stub test on that
    # platform. `wait` and `kill` are bash builtins with no such gap.
    #
    # Bound is 5s, not the original 10s (#1002 round 3, maintainer
    # instruction: "take the smallest value the healthy path reliably
    # beats"). tests/test_windows_real_wscript_1002.py's own
    # test_real_wscript_healthy_path_latency measured the real cost on
    # real windows-latest CI legs (PR #1010, run #38054376838, job
    # #114219715609 and siblings): hidden-route max 0.422s, nohup max
    # 0.250s, over 5 runs each -- roughly 10x margin under 5s. A disabled
    # WSH still stalls for the full bound regardless of its value (the
    # watchdog's job is to cap the worst case, not to represent any real
    # completion time), so halving the bound halves that worst case
    # without touching the margin a genuinely healthy call needs.
    wscript.exe //B "$vbs_win" "$bash_win" "$pidwrap" "$outfile" "$pidfile" "${real_cmd[@]}" >/dev/null 2>&1 &
    local wscript_pid=$!
    # The watchdog's own stdout/stderr must be redirected away from
    # whatever this function's own caller inherited (a pipe, in the test
    # suite's own subprocess.run) -- otherwise a caller reading that pipe
    # for EOF keeps blocking on it for as long as the watchdog's `sleep`
    # is still alive, even after wscript.exe itself has already exited
    # and this function has already returned the right value (observed:
    # a positive-control test timed out at exactly the watchdog's own
    # bound despite RC=0 already being in the buffered output).
    ( sleep 5; kill -9 "$wscript_pid" 2>/dev/null ) >/dev/null 2>&1 &
    local watchdog_pid=$!
    wait "$wscript_pid" 2>/dev/null
    local wscript_rc=$?
    kill "$watchdog_pid" 2>/dev/null
    wait "$watchdog_pid" 2>/dev/null
    return "$wscript_rc"
}
