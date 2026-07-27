#!/usr/bin/env bash
#
# lib-lock.sh — the one lock primitive in this plugin (#182).
#
# Why a directory and not a file:
#
#   `mkdir` either creates the directory or fails. One syscall, no check-then-
#   act, no window. The previous `set -o noclobber` + `echo $$ >` acquisition
#   had the right idea, but its *stale takeover* did not: `rm -f` (or a bare
#   overwrite) followed by a create is two operations, and no amount of
#   verification between or after them makes the pair atomic. Several processes
#   could each observe the same dead PID and each declare itself the new holder.
#
#   Measured on the old acquisition: 0/40 multi-winner rounds at N=2. At N=8 it
#   is never zero — 11/40 in the harness on #182, 40/40 in the one in
#   tests/test_lock_primitive.py, which holds the critical section for 0.1s so
#   overlaps are certain to be observed. The rate depends on the harness; the
#   point is only that it is zero at N=2 and not zero above it, which is why a
#   two-process test proves nothing here — see #182.
#
# Why takeover is `mv` and never `rm`:
#
#   Two processes renaming the same directory: exactly one succeeds, the loser
#   gets ENOENT. Single-winner by construction rather than by timing, which is
#   the whole bug. Do not "simplify" this back to `rm -rf` + retry.
#
# Not `flock`: absent on macOS (no /usr/bin/flock, and the GitHub macos runner
# image does not ship util-linux). Not `python3 -c` with `fcntl.flock` either —
# an advisory lock belongs to the open file description and is released the
# moment the Python child exits, so the shell would carry on believing it holds
# a lock it does not. Silent, and worse than no lock at all.
#
# Usage:
#   source "$(dirname "$0")/lib-lock.sh"
#   if lock_acquire "$LOCK_DIR" 0; then HAVE_LOCK=true; else exit 0; fi
#   trap 'lock_release "$LOCK_DIR"' EXIT
#
# Functions:
#   lock_acquire <lock_dir> [timeout_seconds]  -> 0 acquired, 1 timed out
#   lock_release <lock_dir>                    -> 0 released, 1 not ours

[ -n "${_REMEMBER_LIB_LOCK_SOURCED:-}" ] && return 0
_REMEMBER_LIB_LOCK_SOURCED=1

# Spin interval. Fractional sleep is not in POSIX; it works on macOS, GNU
# coreutils and Git-Bash, but probe rather than assume — an integer-only sleep
# would silently turn every contended acquire into a one-second stall.
if sleep 0.001 2>/dev/null; then
    _LOCK_SLEEP=0.05
else
    _LOCK_SLEEP=1
fi

# Sets $_LOCK_SELF to this process's own id. A FUNCTION THAT ASSIGNS, not one
# that echoes — and it must never be called as `$(...)`.
#
# bash 3.2 (the /bin/bash macOS still ships, and the platform this whole file
# exists for) has no BASHPID. `$$` is no substitute: every subshell of one shell
# reports the same value, so `lock_release`'s ownership check could not tell
# siblings apart and a process that never acquired could release another's lock.
#
# A child's PPID is the portable way to ask "who am I, actually" — but only when
# the child is spawned by THIS shell. Inside a command substitution the body runs
# in a forked subshell, so `$(_lock_self)` would report that ephemeral shell and
# hand out a different id on every call. Measured on 3.2: two winners, each
# holding a lock whose pid file named a third id neither of them recognised.
# Assigning into the caller's shell is what keeps the answer stable.
_lock_self_set() {
    if [ -n "${BASHPID:-}" ]; then
        _LOCK_SELF="$BASHPID"
        return 0
    fi
    local _probe
    _probe=$(mktemp "${TMPDIR:-/tmp}/remember-lockself-XXXXXX" 2>/dev/null) || {
        _LOCK_SELF="$$"
        return 0
    }
    sh -c 'echo $PPID' > "$_probe" 2>/dev/null
    _LOCK_SELF=$(cat "$_probe" 2>/dev/null) || true
    rm -f "$_probe" 2>/dev/null || true
    case "$_LOCK_SELF" in
        ''|*[!0-9]*) _LOCK_SELF="$$" ;;
    esac
    return 0
}

# Take a stale lock away from a dead holder. Returns 0 only for the single
# process that won the rename — everyone else gets 1 and keeps waiting.
_lock_try_steal() {
    local _dir="$1" _pid _seen _abandoned _owner _claim
    # Per-process, NOT a shared name: with one `pid.stealing` for everyone, a
    # second process could "restore" a claim that was still in flight and then
    # claim it itself, and both would proceed. Two winners at N=8, reproducibly.
    _lock_self_set
    _claim="${_dir}/pid.stealing.${_LOCK_SELF}"

    # A stealer killed between taking the pid file and writing its own leaves
    # the lock with no pid at all — and every later acquirer would then read an
    # empty pid, refuse to judge it stale (correctly: that is also what a holder
    # mid-acquisition looks like), and wait forever. A claim file whose owner is
    # dead is what tells those two cases apart. A claim whose owner is alive is
    # a takeover in progress and must be left alone.
    if [ ! -e "${_dir}/pid" ]; then
        for _abandoned in "${_dir}"/pid.stealing.*; do
            # No nullglob: with no match the loop body runs once with the
            # pattern itself, which -e rejects.
            [ -e "$_abandoned" ] || continue
            _owner="${_abandoned##*.}"
            case "$_owner" in
                *[!0-9]*) continue ;;
            esac
            kill -0 "$_owner" 2>/dev/null && continue
            # First dead claim becomes the pid again; any further ones are
            # litter from earlier abandoned takeovers — drop them rather than
            # leave them accumulating in the lock directory forever.
            if [ ! -e "${_dir}/pid" ]; then
                mv "$_abandoned" "${_dir}/pid" 2>/dev/null || true
            else
                rm -f "$_abandoned" 2>/dev/null || true
            fi
        done
    fi

    _pid=$(cat "${_dir}/pid" 2>/dev/null) || true

    # No pid file yet: the holder created the directory microseconds ago and has
    # not written it. That is a live lock mid-acquisition, not a stale one.
    [ -z "$_pid" ] && return 1
    case "$_pid" in
        *[!0-9]*) return 1 ;;
    esac
    kill -0 "$_pid" 2>/dev/null && return 1

    # Claim the right to take over by RENAMING the pid file. Rename is atomic
    # and the source disappears, so of the N processes that judged this lock
    # stale exactly one succeeds; the rest get ENOENT and go back to waiting.
    # Single-winner by construction, not by timing.
    #
    # The lock DIRECTORY is never removed. An earlier version of this moved the
    # whole directory aside and recreated it, which left the path free for a
    # microsecond — long enough for a third process's `mkdir` to succeed while
    # the previous holder still believed it held the lock. Measured at N=8:
    # two winners within the first three rounds. Taking the lock over in place
    # closes that window because the path is never empty.
    mv "${_dir}/pid" "$_claim" 2>/dev/null || return 1

    _seen=$(cat "$_claim" 2>/dev/null) || true
    if [ "$_seen" != "$_pid" ]; then
        mv "$_claim" "${_dir}/pid" 2>/dev/null || true
        return 1
    fi

    echo "$_LOCK_SELF" > "${_dir}/pid" 2>/dev/null || true
    rm -f "$_claim" 2>/dev/null || true
    return 0
}

# How long a lock directory with no pid and no claim must sit untouched before
# it is treated as an orphan rather than as a holder mid-acquisition. A holder
# writes its pid microseconds after `mkdir`, so anything this old is debris.
_LOCK_ADOPT_AFTER="${_LOCK_ADOPT_AFTER:-30}"

# Seconds since <dir> was last modified. `stat` is BSD on macOS and GNU on
# Linux with incompatible flags; try both, and report 0 (i.e. "fresh") if
# neither works, so an unreadable mtime can never trigger an adoption.
#
# The two probes are captured SEPARATELY, and that is the whole point. Written
# as `$(A || B)` the substitution captures the stdout of both: on GNU, `-f` is
# *filesystem* status, so `%m` is read as a filename that does not exist — the
# command exits 1, the fallback duly runs, but the real path has already
# printed its filesystem block to stdout. The block and the correct mtime are
# concatenated, the digits-only guard below rejects the result, and the
# function returns 0 for a directory of any age. Every caller then reads
# "fresh" forever and no orphan is ever adopted on Linux (#198).
_lock_dir_age() {
    local _mtime _now
    _mtime=$(stat -c %Y "$1" 2>/dev/null) || _mtime=""
    case "$_mtime" in
        ''|*[!0-9]*) _mtime=$(stat -f %m "$1" 2>/dev/null) || _mtime="" ;;
    esac
    case "$_mtime" in
        ''|*[!0-9]*) echo 0; return 0 ;;
    esac
    _now=$(date +%s)
    echo $(( _now - _mtime ))
}

# Adopt a lock directory that has no pid at all. A holder killed between
# `mkdir` succeeding and writing its pid leaves exactly this: no pid to judge
# stale, no claim file to explain it. Without adoption that lock can never be
# acquired again by anyone — a permanent, silent outage of every save, fixable
# only by deleting the directory by hand. The window is a couple of shell
# instructions wide, but "rare" and "unrecoverable" is a bad pair.
_lock_try_adopt() {
    local _dir="$1" _claim _owns_marker=0 _adopted=0
    [ -e "${_dir}/pid" ] && return 1
    for _claim in "${_dir}"/pid.stealing.*; do
        [ -e "$_claim" ] && return 1
    done
    [ "$(_lock_dir_age "$_dir")" -lt "$_LOCK_ADOPT_AFTER" ] && return 1

    # Single winner by construction, same as acquisition: `mkdir` or nothing.
    if ! mkdir "${_dir}/adopt" 2>/dev/null; then
        # An adopter killed between `mkdir adopt` and writing the pid strands
        # the marker: no pid, no claim, adopt/ present. `_lock_try_steal`
        # returns early with nothing to judge and every later `_lock_try_adopt`
        # fails here — the same permanent, silent outage this function exists
        # to answer, one level up (#198). A live adopter holds the marker for
        # two shell instructions, so one that has sat untouched past the
        # threshold is debris by exactly the argument used for the lock itself.
        #
        # `mv` rather than `rmdir` does the clearing, so that two adopters both
        # judging the same marker dead cannot both proceed on it: a rename
        # fails for everyone but the first.
        [ "$(_lock_dir_age "${_dir}/adopt")" -lt "$_LOCK_ADOPT_AFTER" ] && return 1
        mv "${_dir}/adopt" "${_dir}/adopt.dead.$$" 2>/dev/null || return 1
        rm -rf "${_dir}/adopt.dead.$$" 2>/dev/null || true
    else
        _owns_marker=1
    fi

    # The pid write, not the marker, is the single-winner test. A first cut at
    # this cleared a stranded marker and then recreated it, which meant an
    # adopter that lost the clearing race — having already read the marker as
    # debris — went on to displace the *fresh* marker the winner was holding,
    # and both adopted: 4 double-wins in 40 rounds. Not recreating the marker
    # is what closed that one, so do not reintroduce a `mkdir` here.
    #
    # `noclobber` makes the redirect an O_EXCL create. It is not what fixed
    # that race, and the concurrent test cannot reach it — with a marker
    # present every contender funnels into the `mv` and only one gets this
    # far. It covers the interleaving below instead.
    #
    # Refusing to overwrite is right for the adopter specifically: it entered
    # only because there was no pid, so a pid appearing underneath it means
    # somebody else legitimately took the lock and this adopter has lost. That
    # covers the `_lock_try_steal` window too — the rename of `pid` to a claim
    # is atomic, so one of the two always exists, and the only interleaving
    # that passes both guards above is one where the steal has already
    # finished and written its pid. Before this, the adopter would have
    # clobbered that pid with a plain redirect.
    #
    # It does NOT make adoption safe against a *live* holder stalled between
    # its `mkdir` and its own (plain, unguarded) pid write in `lock_acquire`.
    # Nothing here can: the two are indistinguishable from outside. That is
    # what _LOCK_ADOPT_AFTER is for, and the assumption it rests on is stated
    # above it — a holder writes its pid microseconds after `mkdir`, so
    # anything this old is debris.
    _lock_self_set

    ( set -o noclobber; echo "$_LOCK_SELF" > "${_dir}/pid" ) 2>/dev/null && _adopted=1

    # Only the process that created the marker removes it. An unguarded rmdir
    # here deletes whatever is at that path, which after a lost pid race is the
    # *winner's* live marker rather than this process's — reproducible, and
    # harmless today only because the pid write above has already settled the
    # lock and the guard at the top of this function turns away everyone who
    # comes after. Relying on that would make the marker's stated meaning
    # ("a live adopter holds this") false whenever the race is lost, and the
    # clearing logic above reads that meaning back out.
    [ "$_owns_marker" = 1 ] && rmdir "${_dir}/adopt" 2>/dev/null
    [ "$_adopted" = 1 ] || return 1
    return 0
}

# Acquire <lock_dir>, waiting up to [timeout_seconds] (default 0 = try once,
# but still take over a stale lock). Returns 0 on success, 1 on timeout.
lock_acquire() {
    local _dir="$1" _timeout="${2:-0}" _deadline _legacy
    _deadline=$(( $(date +%s) + _timeout ))

    mkdir -p "$(dirname "$_dir")" 2>/dev/null || true

    while :; do
        if mkdir "$_dir" 2>/dev/null; then
            _lock_self_set
            echo "$_LOCK_SELF" > "${_dir}/pid" 2>/dev/null || true
            return 0
        fi

        if [ -f "$_dir" ]; then
            # Pre-#182 install: the lock is a regular FILE holding a PID, and
            # `mkdir` can never succeed against one — without this, every save
            # would skip forever after an upgrade. But the old holder may still
            # be running across that upgrade, and deleting its lock would let a
            # second save start alongside it. Honour the PID: remove the file
            # only once nobody is behind it.
            _legacy=$(cat "$_dir" 2>/dev/null) || true
            case "$_legacy" in
                ''|*[!0-9]*) rm -f "$_dir" 2>/dev/null || true; continue ;;
            esac
            if ! kill -0 "$_legacy" 2>/dev/null; then
                rm -f "$_dir" 2>/dev/null || true
                continue
            fi
        elif { [ -e "$_dir" ] || [ -L "$_dir" ]; } && [ ! -d "$_dir" ]; then
            # Something at the path that is neither a lock directory nor a
            # legacy lock file — a dangling symlink, a FIFO, debris. `mkdir`
            # can never succeed against it and there is no holder to respect,
            # so clear it rather than spin here until the timeout, forever.
            rm -f "$_dir" 2>/dev/null || true
            continue
        # A won steal IS the lock: the takeover claims the existing directory in
        # place rather than recreating it, so there is nothing left to mkdir.
        # Works at timeout 0, so a stale lock is recovered on the first attempt.
        elif _lock_try_steal "$_dir"; then
            return 0
        elif _lock_try_adopt "$_dir"; then
            return 0
        fi

        [ "$(date +%s)" -ge "$_deadline" ] && return 1
        sleep "$_LOCK_SLEEP"
    done
}

# Release <lock_dir>. Ownership is structural — only the creator can have made
# the directory — so the pid check is defence against a future caller releasing
# a lock it never took, not part of the mutual-exclusion argument.
lock_release() {
    local _dir="$1" _pid
    _pid=$(cat "${_dir}/pid" 2>/dev/null) || true

    _lock_self_set
    if [ -n "$_pid" ] && [ "$_pid" != "$_LOCK_SELF" ]; then
        return 1
    fi
    rm -rf "$_dir" 2>/dev/null || true
    return 0
}
