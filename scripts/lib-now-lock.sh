#!/bin/bash
# ============================================================================
# lib-now-lock.sh — serialized read/modify/write access to now.md
# ============================================================================
#
# DESCRIPTION
#   Follow-up to #142. That fix replaced the blind `: > "$MEMORY_FILE"` NDC
#   truncate with a partial truncate (drop only the bytes that were actually
#   compressed, keep anything a concurrent save appended after the snapshot)
#   — the right idea, but every step around it is still unsynchronized:
#
#     1. The byte-count snapshot (`wc -c`) and the `build-ndc-prompt` read
#        that follows it are two separate, unlocked reads of now.md. A save
#        landing in between makes the snapshot disagree with what was
#        actually fed to the summarizer — the eventual truncate then drops
#        bytes that were never summarized.
#     2. The closing `tail`/`mv` that commits the truncate is itself
#        unserialized against a concurrent append landing at the same time.
#     3. The `tail`-failure branch still falls back to `: > "$MEMORY_FILE"`
#        — the original #142 bug, preserved as an error path.
#
#   This library gives every now.md writer (save's append, NDC's snapshot,
#   NDC's commit) a single flock-guarded critical section, so none of the
#   three windows above can open.
#
# USAGE
#   source "$(dirname "$0")/lib-now-lock.sh"
#   now_locked 30 now_append "$MEMORY_FILE" "$HAIKU_TEXT_FILE"
#
# EXIT CODES (now_locked)
#   rc of the wrapped command, or 99 if the lock could not be acquired
#   within the given timeout.
#
# ============================================================================

[ -n "${_LIB_NOW_LOCK_LOADED:-}" ] && return 0
_LIB_NOW_LOCK_LOADED=1

# now_locked <timeout_s> <command> [args...]
# Runs the command (function or external) inside a subshell holding an
# flock on now.lock, so it cannot interleave with any other now_locked
# caller. Returns the command's exit code, or 99 on lock-acquire timeout.
now_locked() {
    local _timeout="$1"; shift
    (
        flock -w "$_timeout" 9 || exit 99
        "$@"
    ) 9>>"${REMEMBER_DIR}/tmp/now.lock"
}

# now_append <memory_file> <content_file>
# Blank-line separator + content, same shape as the unlocked append it
# replaces. Call only via now_locked.
now_append() {
    local mem="$1" content="$2"
    echo "" >> "$mem" && cat "$content" >> "$mem"
}

# now_truncate_first <memory_file> <n_bytes>
# Drops ONLY the first n bytes (the span already compressed into
# today-*.md), preserving anything appended after the snapshot. Call only
# via now_locked, immediately after taking the same snapshot count under
# the same lock (see save-session.sh's NDC step).
#
# Writes the tail to a sibling temp file and `mv`s it over $mem (same
# directory, same filesystem — an atomic rename, not an O_TRUNC window).
# If `n` exceeds the file's current size, or `tail` itself fails, $mem is
# left completely untouched and a nonzero rc is returned — never the
# blind `: > $mem` of #142. The temp file is removed on every path.
now_truncate_first() {
    local mem="$1" n="$2" tmp size rc
    [ -z "$n" ] && return 0
    [ "$n" -eq 0 ] && return 0

    size=$(wc -c < "$mem") || return 1
    if [ "$n" -gt "$size" ]; then
        return 1
    fi

    tmp="${mem}.tail.$$"
    if tail -c +"$((n + 1))" "$mem" > "$tmp"; then
        mv -f "$tmp" "$mem"
        rc=$?
        [ "$rc" -ne 0 ] && rm -f "$tmp"
    else
        rc=$?
        rm -f "$tmp"
    fi
    return $rc
}
