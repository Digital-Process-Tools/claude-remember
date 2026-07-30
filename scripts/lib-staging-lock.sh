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
staging_append() {
    local _today="$1" _text="$2"
    [ -s "$_today" ] && echo "" >> "$_today"
    cat "$_text" >> "$_today"
}
