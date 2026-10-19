#!/bin/bash
# ============================================================================
# lib-stdin.sh -- read a hook's stdin payload in chunks, not a byte at a time
# ============================================================================
#
# USAGE
#   source "$_HOOK_DIR/lib-stdin.sh"
#   _remember_read_stdin_into HOOK_STDIN
#
#   Sets the named variable to the host's payload with every newline removed,
#   or to "" when stdin is a terminal or empty. Never blocks for long: each
#   read gives up after 1 s of silence, so a host that holds the pipe open and
#   writes nothing costs a second, not a hung agent. bash 3.2 has no
#   sub-second -t, hence 1.
#
#   Used by post-tool-hook.sh, user-prompt-hook.sh and session-end-hook.sh.
#   session-start-hook.sh keeps the line loop below, and says why at its read.
#
# WHY CHUNKS (#1040)
#   All four hooks used to read with
#
#     while IFS= read -r -t 1 _line || [ -n "$_line" ]; do X="$X$_line"; done
#
#   bash reads a pipe unbuffered, since it cannot push back what it
#   over-read, so that is one read() per payload byte, and with -t one
#   select() per byte as well. On Cygwin/MSYS every fd that is not a terminal
#   counts as a pipe, and each of those calls goes through the Cygwin layer:
#   a 1 MB tool result took 24.7 s to read on Windows (MSYS bash 5.2.37), and
#   34-43 s for the whole PostToolUse call, measured by @gwangyong-kim.
#   read -N (bash >= 4.1) reads a fixed count with buffered refills, one
#   select() per refill: 0.45 s for the same 1 MB, byte-identical. A regular
#   file on stdin hides all of this (bash turns the timeout off for one),
#   which is why a measurement with the payload redirected from a file looked
#   fine. Hooks get a pipe.
#
# WHAT THE EXTRACTORS SEE IS UNCHANGED
#   read drops each line's newline and the old loop joined the lines bare,
#   so the payload the JSON field extraction saw never had a newline in it:
#   not the trailing one, not the line breaks of a pretty-printed payload.
#   read -N keeps them, so the chunked path removes them again, all of them,
#   trailing or not, in one pass that runs only when a scan finds one (Claude
#   Code sends a single line, often with a trailing newline). A newline in JSON
#   is whitespace between tokens, never part of a string value, so removing
#   it changes no value, and the string every hook and every hooks.d
#   listener receives is the one it received before #1040.
#
# FALLBACK
#   bash 3.2 (stock macOS /bin/bash) has no -N, so it keeps the line loop. On
#   macOS and Linux that loop costs about a second per MB, not twenty.
#   REMEMBER_NO_READ_N=1 forces the line loop on any bash, so CI on bash 5
#   still exercises the path a stock Mac runs (the same reason lib-clock.sh
#   has REMEMBER_NO_PRINTF_T). Neither path forks: no external reader and no
#   command substitution, because the hooks that source this have their
#   processes counted (#1011, #1029).
#
# ============================================================================

[ -n "${_REMEMBER_LIB_STDIN_LOADED:-}" ] && return 0
_REMEMBER_LIB_STDIN_LOADED=1

# Remove every newline from _rs_buf, the variable of the caller below (bash
# scoping is dynamic). Not a pattern substitution of the newline: bash runs
# that in quadratic time here, 8 s for an 840 KB pretty-printed payload under
# a UTF-8 locale, against 0.03 s for splitting on IFS and joining back
# (bash 5.3, macOS, measured). Globbing is off for the split and is put back
# the way it was found; the split lands in the positional parameters of this
# function only.
_remember_stdin_join_lines() {
    local IFS=$'\n' _rs_noglob=""
    [[ "$-" == *f* ]] && _rs_noglob=1
    set -f
    # shellcheck disable=SC2086 # the split is the point; globbing is off
    set -- $_rs_buf
    [ -n "$_rs_noglob" ] || set +f
    printf -v _rs_buf '%s' "$@"
}

_remember_read_stdin_into() {
    local _rs_var="$1" _rs_buf="" _rs_part="" _rs_chunked=""
    if [ ! -t 0 ]; then
        if [ -z "${REMEMBER_NO_READ_N:-}" ]; then
            if [ "${BASH_VERSINFO[0]:-0}" -gt 4 ] 2>/dev/null; then
                _rs_chunked=1
            elif [ "${BASH_VERSINFO[0]:-0}" -eq 4 ] 2>/dev/null \
                && [ "${BASH_VERSINFO[1]:-0}" -ge 1 ] 2>/dev/null; then
                _rs_chunked=1
            fi
        fi
        if [ -n "$_rs_chunked" ]; then
            # The last chunk ends at EOF short of the count: read reports
            # failure with the data still in the variable, hence the -n test.
            while IFS= read -r -d '' -N 1048576 -t 1 _rs_part || [ -n "$_rs_part" ]; do
                _rs_buf="$_rs_buf$_rs_part"
                _rs_part=""
            done
            # One linear scan, and the join only when it finds a newline. Not
            # a suffix strip for the trailing one: on a payload that has no
            # trailing newline, bash tries every suffix before giving up, which
            # took 165 s for 1 MB (bash 5.3, macOS, measured).
            if [[ "$_rs_buf" == *$'\n'* ]]; then
                _remember_stdin_join_lines
            fi
        else
            while IFS= read -r -t 1 _rs_part || [ -n "$_rs_part" ]; do
                _rs_buf="$_rs_buf$_rs_part"
                _rs_part=""
            done
        fi
    fi
    printf -v "$_rs_var" '%s' "$_rs_buf"
}
