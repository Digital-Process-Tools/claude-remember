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
#   is never zero — 11/40 in the harness on #182, 40/40 in the one that was in
#   tests/test_lock_primitive.py at the time, which held the critical section
#   for 0.1s so overlaps were certain to be observed. The rate depends on the
#   harness; the point is only that it is zero at N=2 and not zero above it,
#   which is why a two-process test proves nothing here — see #182.
#
#   That harness counted winners. It no longer exists: counting acquisitions is
#   not a measurement of mutual exclusion, because a contender scheduled after
#   the holder released takes the free lock legitimately and is counted as a
#   concurrent one (#293). The current tests record entry to and exit from the
#   critical section in one ordered log and fail on overlap instead.
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
        # `mv` rather than `rmdir` does the clearing. Do NOT read that as the
        # reason adoption is single-winner: `rmdir` is equally single-winner
        # here — exactly one of N concurrent callers removes the directory and
        # the rest get ENOENT — and mutating this line to it leaves every test
        # in tests/test_lock_primitive.py green, correctly (#293). The gate is
        # the `noclobber` pid write below, as the comment on it says: mutated
        # even to `rm -rf`, which really does succeed for every caller, 8-way
        # contention over 60 rounds still gives 0 doubled adoptions and 60/60
        # rounds won. What `mv` buys is only that it clears a marker directory
        # that is not empty, where `rmdir` would fail and wedge the lock it
        # exists to unwedge.
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

# ---------------------------------------------------------------------------
# Hold-duration recorder (#226) — OFF unless REMEMBER_LOCK_TIMING=1
# ---------------------------------------------------------------------------
#
# #226 asks whether the NDC commit's 30s save.lock timeout is doing what its
# reasoning claims, and answers its own question: instrument lock_acquire /
# lock_release across a normal working day, then set the default from the tail
# of the distribution rather than from intuition. #234 did exactly that for
# staging.lock — ~30ms per NDC append, ~200ms for a 3-file retire loop, polled
# at 1ms — and set 10s from it. save.lock is still unmeasured, and it is the
# one held across a summarize Haiku call.
#
# This records. It decides nothing: no timeout default changes on the strength
# of a number produced in a sandbox, because a synthetic figure that reads as
# measured is the defect #226 is about, one layer up.
#
# WHY OPT-IN AND NOT ALWAYS-ON
#   save-session.sh runs on a PostToolUse hook. #227 measured that path at a
#   p50 of 8.7s on Windows/ARM64, #230 split out the prefix paid per tool call,
#   and #204 is an external report of the plugin blocking a user's prompts. An
#   always-on recorder costs one `wc` per record, plus two `date` spawns per
#   lock use below bash 5 — on every machine, forever, to answer a question
#   asked once. A measurement that slows the path it measures is self-defeating
#   and would also bias its own numbers. Off, this is one string comparison:
#   no file, no spawn, pinned by tests/test_lock_timing.py.
#
# RESOLUTION IS DISCLOSED, NOT ASSUMED
#   There is no portable spawn-free millisecond clock. Three tiers, best first,
#   probed once: bash >= 5's EPOCHREALTIME (microseconds, no spawn), GNU
#   `date +%s%N` (milliseconds, one spawn), and plain `date +%s` (whole
#   seconds, one spawn) — which is what macOS's /bin/bash 3.2 with BSD date
#   gets. A python3 fallback was rejected: at ~60ms per spawn it costs more
#   than a staging.lock hold, so it would report millisecond precision while
#   inventing most of the milliseconds. The tier lands in every row, because a
#   distribution read at a finer resolution than it was taken at is the same
#   false confidence this issue was filed about. One second is coarse for
#   staging.lock and adequate for save.lock, whose holds are the seconds-long
#   ones #226 is asking about.
#
#   The clock is read after acquisition and before release, so the *release*
#   reading's own cost falls inside the interval. At the `s` tier that is
#   invisible; at `ms` it is one `date`, around 1-5ms, and holds this close to
#   the noise floor are not what a 30s timeout is set from.
#
# WHERE IT GOES, AND WHAT BOUNDS IT
#   $REMEMBER_DIR/logs/lock-timing.tsv, or REMEMBER_LOCK_TIMING_FILE. Bounded
#   at REMEMBER_LOCK_TIMING_MAX lines (default 5000, ~350KB; the header is one
#   of them) — this repo has just spent a day on unbounded things. At the cap
#   recording STOPS and appends a `# CAPPED` line rather than rolling: a rolled
#   file silently drops the oldest records, and with them the shape of the tail
#   a timeout would be set from. A `.capped` sibling marker makes the stop a
#   builtin test rather than a re-scan on every later record.
#
# WHEN IT CANNOT RECORD
#   It says so once per process — through `log` when the caller sourced log.sh,
#   otherwise on stderr — and carries on. Locking must not fail because its
#   instrument did. But it must not fail *silently*: a measurement that did not
#   happen and one that showed nothing look identical in an empty file.
#
_LOCK_TIMING="${REMEMBER_LOCK_TIMING:-0}"
_LOCK_TIMING_MAX="${REMEMBER_LOCK_TIMING_MAX:-5000}"
_LOCK_TIMING_PRECISION=""
_LOCK_TIMING_FILE=""
_LOCK_TIMING_NOW=0
_LOCK_TIMING_DISCLOSED=0

# True only for a `date` that really honours %N: BSD date prints a literal `N`
# and some print `%N`, both rejected by the digits-only test; a `date` that
# drops the format entirely yields 10 digits, rejected by the length test.
_lock_timing_has_ns_date() {
    local _n
    _n=$(date +%s%N 2>/dev/null) || return 1
    case "$_n" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "${#_n}" -ge 16 ] || return 1
    return 0
}

if [ "$_LOCK_TIMING" = 1 ]; then
    # The version test as well as the variable: EPOCHREALTIME is a shell
    # builtin variable on bash >= 5 and an ordinary one everywhere else, so an
    # exported stale value in a bash 3.2 environment would otherwise pin every
    # row to one frozen timestamp and report every hold as 0ms — a plausible
    # number, wrong, and indistinguishable from a very fast machine.
    if [ "${BASH_VERSINFO[0]:-0}" -ge 5 ] && [ -n "${EPOCHREALTIME:-}" ]; then
        _LOCK_TIMING_PRECISION="us"
    elif _lock_timing_has_ns_date; then
        _LOCK_TIMING_PRECISION="ms"
    else
        _LOCK_TIMING_PRECISION="s"
    fi
fi

# The three conversions are functions of their ARGUMENT, and the reading is
# taken by the caller. That split is not tidiness — it is the only way these
# branches can be tested at all.
#
# No machine reaches more than one tier: bash 5 always picks `us`, macOS's
# /bin/bash 3.2 with BSD date always picks `s`, and `ms` is picked by neither of
# the runners this repo has. So a test must be able to hand a conversion a
# reading of its choosing. The obvious way — assign EPOCHREALTIME and call the
# clock — is a trap: on bash >= 5 EPOCHREALTIME is a dynamically generated
# variable whose value comes from the clock on every reference, so the
# assignment does not survive and the conversion is handed `now` instead. That
# is silent, it is green on macOS (where the variable is an ordinary one) and
# red on Linux, and it tests nothing on the platform that actually has the tier.
#
# Taking the reading as an argument makes every tier drivable everywhere, and
# it is the same code the live caller runs — not a parallel copy.
#
# Malformed input yields 0 rather than a shell arithmetic error: a clock that
# cannot be read must not take locking down with it. 0 propagates into an
# absurd duration rather than a plausible one, so it is visible in the report
# instead of passing for data.

# EPOCHREALTIME (bash >= 5), "seconds.microseconds", to epoch milliseconds.
_lock_timing_us_to_ms() {
    local _r="$1" _s _f
    # The separator is locale-dependent — de_DE gives `1753980000,123456`.
    case "$_r" in
        *[.,]*) _s="${_r%%[.,]*}"; _f="${_r#*[.,]}" ;;
        *)      _s="$_r"; _f="000000" ;;
    esac
    case "$_s" in
        ''|*[!0-9]*) _LOCK_TIMING_NOW=0; return 0 ;;
    esac
    case "$_f" in
        *[!0-9]*) _f="000000" ;;
    esac
    # Truncation, never rounding: a hold must not come back longer than it was.
    _f="${_f}000"
    _LOCK_TIMING_NOW=$(( _s * 1000 + 10#${_f:0:3} ))
    return 0
}

# `date +%s%N` (GNU) to epoch milliseconds.
_lock_timing_ns_to_ms() {
    case "$1" in
        ''|*[!0-9]*) _LOCK_TIMING_NOW=0; return 0 ;;
    esac
    _LOCK_TIMING_NOW=$(( $1 / 1000000 ))
    return 0
}

# `date +%s` to epoch milliseconds.
_lock_timing_s_to_ms() {
    case "$1" in
        ''|*[!0-9]*) _LOCK_TIMING_NOW=0; return 0 ;;
    esac
    _LOCK_TIMING_NOW=$(( $1 * 1000 ))
    return 0
}

# Assigns $_LOCK_TIMING_NOW, epoch milliseconds. An assigning function, like
# _lock_self_set and for the same reason: in $( ) the reading would happen in a
# forked subshell, which is a spawn on the path whose spawns are the point.
_lock_timing_now() {
    local _n
    case "$_LOCK_TIMING_PRECISION" in
        us)
            _lock_timing_us_to_ms "$EPOCHREALTIME"
            ;;
        ms)
            _n=$(date +%s%N 2>/dev/null) || _n=""
            _lock_timing_ns_to_ms "$_n"
            ;;
        *)
            _n=$(date +%s 2>/dev/null) || _n=""
            _lock_timing_s_to_ms "$_n"
            ;;
    esac
    return 0
}

# Resolves $_LOCK_TIMING_FILE with parameter expansion only — no subshell.
_lock_timing_target() {
    if [ -n "${REMEMBER_LOCK_TIMING_FILE:-}" ]; then
        _LOCK_TIMING_FILE="$REMEMBER_LOCK_TIMING_FILE"
    elif [ -n "${REMEMBER_DIR:-}" ]; then
        _LOCK_TIMING_FILE="${REMEMBER_DIR}/logs/lock-timing.tsv"
    else
        _LOCK_TIMING_FILE=""
    fi
}

_lock_timing_disclose() {
    # `if`, not `[ ... ] && return`: an AND-list that evaluates false is the
    # statement's exit status, and every caller here runs under `set -e`.
    if [ "$_LOCK_TIMING_DISCLOSED" = 1 ]; then
        return 0
    fi
    _LOCK_TIMING_DISCLOSED=1
    # `declare -F`, not `command -v`: the question is whether the CALLER SOURCED
    # log.sh, and `command -v log` does not ask it. macOS ships /usr/bin/log —
    # the system logging binary — so on the platform lib-lock.sh exists for that
    # test is true on every machine, sourced or not. The branch then ran
    # `/usr/bin/log lock-timing "..."`, which exits 64 on an unknown subcommand,
    # and under `set -e` that aborts the caller MID-SAVE with save.lock held. In
    # this repo every real sourcer takes log.sh first, so a function shadowed the
    # binary and it never fired; it was one new caller away from firing, and the
    # one path it can take down is the recorder's own way of saying it recorded
    # nothing. An instrument whose failure notice is the failure is worse than a
    # silent one. `declare -F` is a bash builtin, true only for a shell function,
    # and works on bash 3.2.
    if declare -F log >/dev/null 2>&1; then
        # log() ends in `|| echo ... >&2` so it cannot fail, but this function is
        # the last thing that may take a save down, and `set -e` does not care
        # what the callee is supposed to do.
        log "lock-timing" "$1" || printf 'remember lock-timing: %s\n' "$1" >&2
    else
        printf 'remember lock-timing: %s\n' "$1" >&2
    fi
    return 0
}

# bash 3.2 has no associative arrays, so the per-lock start time lives in a
# variable named after the sanitized path. Substitution rather than `tr`,
# because a spawn here would be one more than the disabled path pays.
_lock_timing_key() {
    _LOCK_TIMING_KEY="${1//[!A-Za-z0-9]/_}"
}

# _lock_timing_record <lock_dir> <event> <outcome> <wait_ms> <held_ms|->
_lock_timing_record() {
    local _name="${1##*/}" _dir _n
    _lock_timing_target
    if [ -z "$_LOCK_TIMING_FILE" ]; then
        _lock_timing_disclose "REMEMBER_LOCK_TIMING=1 but neither REMEMBER_LOCK_TIMING_FILE nor REMEMBER_DIR is set — nothing is being recorded"
        return 0
    fi
    if [ -e "${_LOCK_TIMING_FILE}.capped" ]; then
        return 0
    fi

    _dir="${_LOCK_TIMING_FILE%/*}"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || true

    if [ -f "$_LOCK_TIMING_FILE" ]; then
        _n=$(wc -l < "$_LOCK_TIMING_FILE" 2>/dev/null | tr -d ' ')
        case "$_n" in
            ''|*[!0-9]*) _n=0 ;;
        esac
        if [ "$_n" -ge "$_LOCK_TIMING_MAX" ]; then
            { printf '# CAPPED\t%s lines, REMEMBER_LOCK_TIMING_MAX=%s reached — recording STOPPED here. Nothing was rolled or overwritten, so every record above is real; the distribution below this point is simply missing. Raise the cap or move this file to keep measuring.\n' \
                "$_n" "$_LOCK_TIMING_MAX" >> "$_LOCK_TIMING_FILE"; } 2>/dev/null \
                || _lock_timing_disclose "could not append the cap notice to $_LOCK_TIMING_FILE"
            { : > "${_LOCK_TIMING_FILE}.capped"; } 2>/dev/null || true
            _lock_timing_disclose "$_LOCK_TIMING_FILE reached REMEMBER_LOCK_TIMING_MAX=$_LOCK_TIMING_MAX lines — recording stopped, nothing rolled"
            return 0
        fi
    else
        # Two processes can both pass this test and both append; a duplicate
        # comment line is harmless and a truncating `>` would not be.
        { printf '# ts_ms\tlock\tevent\toutcome\twait_ms\theld_ms\tprecision\tpid\n' >> "$_LOCK_TIMING_FILE"; } 2>/dev/null \
            || _lock_timing_disclose "could not create $_LOCK_TIMING_FILE"
    fi

    # Braced so the redirect failure is reported by the shell INSIDE the
    # suppression — `printf ... >> f 2>/dev/null` opens f before running printf,
    # so the error escapes that redirect's scope (the #204 lesson).
    # ${BASHPID:-$$}, not $$: the NDC commit runs in a backgrounded SUBSHELL of
    # save-session.sh, and $$ is the same value there as in its parent — so the
    # two sides of the very contention #226 is about would be indistinguishable
    # in the pid column. Zero spawns, unlike _lock_self_set's bash 3.2 path,
    # where it degrades to $$ and the column says so by being equal.
    { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$_LOCK_TIMING_NOW" "$_name" "$2" "$3" "$4" "$5" \
        "$_LOCK_TIMING_PRECISION" "${BASHPID:-$$}" >> "$_LOCK_TIMING_FILE"; } 2>/dev/null \
        || _lock_timing_disclose "could not append to $_LOCK_TIMING_FILE"
    return 0
}

# Acquire <lock_dir>, waiting up to [timeout_seconds] (default 0 = try once,
# but still take over a stale lock). Returns 0 on success, 1 on timeout.
#
# A wrapper around the unchanged primitive rather than four hooks at its four
# return sites: the timing must not become something a future edit to the
# acquisition logic has to remember to carry.
lock_acquire() {
    local _t0 _waited
    if [ "$_LOCK_TIMING" != 1 ]; then
        _lock_acquire_impl "$@"
        return $?
    fi
    _lock_timing_now
    _t0="$_LOCK_TIMING_NOW"
    if _lock_acquire_impl "$@"; then
        _lock_timing_now
        _waited=$(( _LOCK_TIMING_NOW - _t0 ))
        _lock_timing_key "$1"
        eval "_LOCK_TIMING_T0_${_LOCK_TIMING_KEY}=\$_LOCK_TIMING_NOW"
        eval "_LOCK_TIMING_W_${_LOCK_TIMING_KEY}=\$_waited"
        return 0
    fi
    _lock_timing_now
    # The row #226 exists to ask about. A timeout leaves only a log line today,
    # so the one event that would prove the default too small is the one absent
    # from any distribution built from holds alone.
    _lock_timing_record "$1" acquire timeout "$(( _LOCK_TIMING_NOW - _t0 ))" "-"
    return 1
}

_lock_acquire_impl() {
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
    local _t0 _wait
    if [ "$_LOCK_TIMING" != 1 ]; then
        _lock_release_impl "$@"
        return $?
    fi
    _lock_release_impl "$@" || return 1
    _lock_timing_now
    _lock_timing_key "$1"
    eval "_t0=\${_LOCK_TIMING_T0_${_LOCK_TIMING_KEY}:-}"
    eval "_wait=\${_LOCK_TIMING_W_${_LOCK_TIMING_KEY}:-}"
    if [ -z "$_t0" ]; then
        # Released by a process that never acquired here. Structurally this
        # should not happen, and the honest answer is to say the duration is
        # unknown rather than to compute one from a start that does not exist.
        _lock_timing_record "$1" release unpaired "-" "-"
        return 0
    fi
    eval "unset _LOCK_TIMING_T0_${_LOCK_TIMING_KEY} _LOCK_TIMING_W_${_LOCK_TIMING_KEY}"
    _lock_timing_record "$1" release ok "$_wait" "$(( _LOCK_TIMING_NOW - _t0 ))"
    return 0
}

_lock_release_impl() {
    local _dir="$1" _pid
    _pid=$(cat "${_dir}/pid" 2>/dev/null) || true

    _lock_self_set
    if [ -n "$_pid" ] && [ "$_pid" != "$_LOCK_SELF" ]; then
        return 1
    fi
    rm -rf "$_dir" 2>/dev/null || true
    return 0
}
