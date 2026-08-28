#!/usr/bin/env bash
#
# lib-staging-lock.sh — the lock that owns today-*.md (#225).
#
# WHY A THIRD LOCK AND NOT ONE OF THE TWO THAT EXIST
#
#   today-<day>.md is written by save-session.sh's NDC step and read-then-
#   retired by run-consolidation.sh. Those two took save.lock and
#   consolidation.lock respectively — each internally sound (#182), neither
#   saying anything about the other. So an NDC append could land between
#   consolidation's `wc -c` of the staging file and its `mv` to .done.md, and
#   be sealed inside a file nothing globs again and session start never
#   injects: written to disk, then unreachable, with nothing logged.
#
#   Consolidating onto save.lock was the obvious move and is the wrong one.
#   save-session.sh holds save.lock for the WHOLE save, including its own
#   summarize Haiku call — which is why #226 exists at all. Making
#   consolidation's retire step wait on that means waiting behind a model
#   call, and a critical section that contains a model call is how #142
#   happened in the first place.
#
#   An ordering discipline over the two existing locks was the other option,
#   and it only holds while every path obeys it. A path that does not is
#   invisible until it deadlocks — and PR #173 proposes a third lock for
#   now.md, so "every path" is not a closed set.
#
#   So: give the shared resource its own lock, and hold it only for the
#   operations that touch the resource. Both critical sections are file ops
#   with no model call — an append of one Haiku summary, or N renames. Nothing
#   holds this lock while acquiring another, and nothing acquires it while
#   holding save.lock, so there is no lock ORDER to get wrong anywhere. That
#   property is what keeps #173 acceptable: a now.md lock is orthogonal to
#   this one, and no path can hold both.
#
# USAGE
#   source "$(dirname "$0")/lib-lock.sh"          # required: the primitive
#   source "$(dirname "$0")/log.sh"                # optional (#394): staging_append
#                                                   # calls config() and report_error(),
#                                                   # both defined only there. Absent, or
#                                                   # returned early on a store with no
#                                                   # writable logs/ (#361/#372), the
#                                                   # fallback guard below degrades to
#                                                   # stderr-only reporting rather than
#                                                   # an undefined-function crash.
#   source "$(dirname "$0")/lib-staging-lock.sh"
#   if staging_lock_acquire; then
#       staging_append "$TODAY_FILE" "$TEXT_FILE"
#       staging_lock_release
#   fi
#
# TIMEOUT
#   REMEMBER_STAGING_LOCK_TIMEOUT, default 10s. Unlike the 30s in #226 this
#   number has a bound behind it rather than an intuition: the only two
#   holders are the append above (one `cat` of a summary) and
#   run-consolidation.sh's retire loop (a `wc`/`head`/`tail`/`mv` per staging
#   file). Measured by polling the lock directory at 1ms while the real
#   scripts run: the append holds it ~30ms, and the retire loop ~200ms for a
#   3-file backlog — ~65ms per file, which is fork cost, not I/O. So the
#   longest hold scales with the backlog: 10s covers ~150 staging files,
#   about five months of unconsolidated days. Losing the wait is safe on both
#   sides and logged on both sides (see the callers), so a backlog past that
#   degrades to "retire it next run", not to data loss. Re-measure before
#   changing this number — that is the whole point of #226.
#

[ -n "${_REMEMBER_LIB_STAGING_LOCK_SOURCED:-}" ] && return 0
_REMEMBER_LIB_STAGING_LOCK_SOURCED=1

# Fallback for the case log.sh was never sourced, or was sourced but
# returned early -- the #361/#372 case, a store where $REMEMBER_DIR/logs
# cannot be created, in which log.sh returns before defining log(),
# report_error() or config() at all (#394). staging_append below calls both
# report_error() and config() unconditionally, so without this guard either
# case is an undefined-function crash, not a degraded read.
#
# `declare -F`, NOT `type` or `command -v`: the same reasoning
# session-end-hook.sh and post-tool-hook.sh already carry for the identical
# guard -- macOS ships /usr/bin/log as the system logging CLI, so `type log`
# is true whether or not a shell function was ever defined.
#
# Unlike user-prompt-hook.sh's `dispatch() { :; }` no-op stub, neither
# fallback here is silent. #349 exists specifically so a persistently
# growing staging file gets reported; a no-op report_error() would turn that
# warning into exactly the silent failure #349 was written to end, on
# precisely the broken stores (#361/#372) where surfacing it matters most.
#
# report_error()'s fallback mirrors log.sh's own report_error()
# (scripts/log.sh:765-770): it still writes to
# $REMEMBER_DIR/logs/hook-errors.log when that directory exists and is
# writable. That covers the (more common) case of this same fallback --
# log.sh was simply never sourced -- where logs/ is perfectly writable and
# only the missing `source` line stands between this file and the real
# functions. Falling straight to stderr in that case too would make the
# growth warning as invisible to /remember:doctor's "Recent errors" section
# as the no-op stub this design explicitly rejects. stderr is still the
# fallback of last resort, exactly as log.sh's own report_error() falls
# back to it when logs/ cannot be created at all -- the #361/#372 case.
#
# lib-clock.sh is self-contained (no log.sh dependency of its own, and its
# own SOURCED guard) and is what session-end-hook.sh's fallback sources for
# the identical reason, so it is pulled in here too, rather than a raw
# `date` call that would silently ignore REMEMBER_TZ -- unlike every other
# timestamp in this pipeline.
_REMEMBER_STAGING_LOCK_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_STAGING_LOCK_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_STAGING_LOCK_SRC_DIR="."
source "$_REMEMBER_STAGING_LOCK_SRC_DIR/lib-clock.sh"
unset _REMEMBER_STAGING_LOCK_SRC_DIR

declare -F log >/dev/null 2>&1 || log() {
    printf '%s [%s] %s\n' "$(_remember_date +%H:%M:%S)" "$1" "$2" >&2
}
declare -F report_error >/dev/null 2>&1 || report_error() {
    log "$1" "$2"
    [ -d "${REMEMBER_DIR:-}/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [$1] $2" \
        >> "${REMEMBER_DIR}/logs/hook-errors.log" 2>/dev/null || true
    return 0
}
# config()'s fallback answers every key with its caller-supplied default,
# same as log.sh's own `[ ! -f "$REMEMBER_CONFIG" ]` branch -- an honest
# answer when REMEMBER_CONFIG is genuinely absent, though NOT when it exists
# but log.sh returned early before reading it (#361/#372): that narrower gap
# -- a real, non-default threshold silently read back as the default -- is
# filed rather than fixed here (#394 follow-up), since closing it means
# duplicating log.sh's own multi-branch config-reading logic (jq / Python
# fallback / one-pass cache) inside this file.
declare -F config >/dev/null 2>&1 || config() { printf '%s\n' "${2:-}"; }

STAGING_LOCK_TIMEOUT="${REMEMBER_STAGING_LOCK_TIMEOUT:-10}"

# One lock for the whole staging set, not one per day file. Consolidation
# retires several files in one loop and the NDC round does not know which of
# them it is about to append to, so per-file locks would need an ordering
# between them — the thing this design exists to avoid.
staging_lock_dir() {
    printf '%s\n' "${REMEMBER_DIR}/tmp/staging.lock"
}

# Safe in $( ): it only prints. Unlike lib-lock.sh's _lock_self_set, which
# assigns and must never be called that way.
staging_lock_acquire() {
    lock_acquire "$(staging_lock_dir)" "${1:-$STAGING_LOCK_TIMEOUT}"
}

# `|| true`: release returns 1 when the lock is not ours, and every caller
# runs under `set -e` or inside a trap where that would rewrite the exit
# status of the work that just succeeded.
staging_lock_release() {
    lock_release "$(staging_lock_dir)" || true
}

# staging_append <today_file> <text_file>
# The NDC append, verbatim from what save-session.sh did inline: a blank-line
# separator when the file already has content, then the summary. Two appends,
# which is precisely why they belong inside a critical section — a reader
# landing between them sees a file ending in a blank line with no summary.
# Call only while holding the lock.
#
# THE GROWTH WARNING (#349): this file is genuinely append-only — every
# caller that appends here does so *because* rolling the span back risks
# erasing a concurrent write, and that trade is correct (see the callers'
# own comments). What nobody costed is a PERSISTENT cause: sustained lock
# contention, a full disk, or a consolidation round that has stopped for
# good (#346's skip-forever state, or features.ndc_compression turned off
# by a typo) all leave the same span landing here every round, forever,
# with nothing to retire it. A cap that silently dropped bytes would be
# worse than the growth it is guarding against — so this does not cap
# anything. It only makes the growth visible once, the same way #180 made
# a silently-failing NDC call visible: report_error() so it reaches both
# the daily log and hook-errors.log, surfaced by /remember:doctor, without
# touching the file's contents at all.
#
# Fires once per crossing rather than once per append past the line — the
# before/after byte count brackets the threshold with no marker file and no
# state of its own, so a store that later shrinks (a rotation, a manual
# edit) and grows past the line again warns again rather than staying mute
# forever after its first crossing.
staging_append() {
    local _today="$1" _text="$2"
    local _before=0
    [ -f "$_today" ] && _before=$(wc -c < "$_today" 2>/dev/null | tr -d ' ')
    case "$_before" in (''|*[!0-9]*) _before=0 ;; esac
    [ -s "$_today" ] && echo "" >> "$_today"
    cat "$_text" >> "$_today"
    local _warn_bytes
    _warn_bytes=$(config ".thresholds.staging_warn_bytes" 2000000)
    case "$_warn_bytes" in (''|*[!0-9]*) _warn_bytes=2000000 ;; esac
    if [ "$_warn_bytes" -gt 0 ] && [ "$_before" -lt "$_warn_bytes" ]; then
        local _after
        _after=$(wc -c < "$_today" 2>/dev/null | tr -d ' ')
        case "$_after" in (''|*[!0-9]*) _after=0 ;; esac
        if [ "$_after" -ge "$_warn_bytes" ]; then
            report_error "staging" "WARNING: ${_today} has grown past ${_warn_bytes}b — this file is append-only and only a SUCCESSFUL consolidation round retires it. Sustained lock contention, a full disk, or consolidation having stopped (check features.ndc_compression and hook-errors.log for consolidation failures) will keep appending the same kind of span here without bound. Nothing was dropped or truncated."
        fi
    fi
}
