#!/usr/bin/env bash
#
# lib-lock.sh — the one lock primitive in this plugin (#182).
#
# Why a directory and not a file:
#
#   `mkdir` either creates the directory or fails. One syscall, no check-then-
#   act, no window -- true on every platform this file has been tested on,
#   with one exception: Ubuntu 26.04 (ubuntu-latest) lost mutual exclusion
#   outright, observed at rates up to 10/10 overlapping rounds (#1108). The
#   mechanism is a hypothesis, not a measurement: a `mkdir` that reports
#   success for a directory that already exists would produce exactly this
#   symptom, and nothing ruling that hypothesis in or out was observed
#   directly. `_lock_acquire_impl` below does not take `mkdir`'s return
#   code as ownership on its own any more either way -- see the noclobber
#   guard at its primary `mkdir` call, the same pattern `_lock_try_adopt`
#   already used for exactly this reason. The previous
#   `set -o noclobber` + `echo $$ >` acquisition had the right idea, but its
#   *stale takeover* did not: `rm -f` (or a bare overwrite) followed by a
#   create is two operations, and no amount of verification between or after
#   them makes the pair atomic. Several processes could each observe the same
#   dead PID and each declare itself the new holder.
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
#   lock_release <lock_dir>                    -> 0 released, 1 not ours (or ownership could not be confirmed in time)
#
# CROSS-HOST CONTRACT (Claude Code and Codex sharing one .remember/ store on
# one machine, #491): this primitive is safe there, deliberately, because it
# never looks at which CLI a process belongs to. `mkdir` and `kill -0` are
# OS-level, process-table operations -- they see a PID, never a host. Every
# lock directory used against this file is named by a fixed literal
# (save.lock, consolidation.lock, staging.lock), never by session id, so a
# Codex session and a Claude Code session contend for the exact same lock the
# exact same way two Claude Code sessions would. Proven under real
# concurrent writes from two simulated hosts in
# tests/test_cross_host_lock_contract_491.py, not merely asserted.
#
# One limitation survives, and it is NOT new here: `kill -0`-based staleness
# detection (`_lock_try_steal` below) cannot tell a dead holder's PID from an
# unrelated LIVE process that has since been assigned the same PID by the OS
# ("PID recycling") -- the lock is then judged live and is not recovered
# until that unrelated process also exits. This is exactly as likely between
# two Claude Code processes racing on their own as it is between a Claude
# Code process and a Codex process; a second host does not make PID reuse
# more probable, because `kill -0` cannot see which CLI started a PID in
# either case. See the same test file for a reproduction that documents
# this as an accepted, general limitation rather than a fix.
#
# Two bounds close separate gaps in that same `kill -0` check, both landing
# in `_lock_try_steal` (#1101, #1103):
#
#   - A holder can be ALIVE and still never make progress -- blocked on I/O,
#     a wedged pipe, anything short of exiting. `kill -0` reports that
#     holder as healthy forever. `_LOCK_MAX_HOLD_SECONDS` bounds how long
#     any live holder may keep the lock before a later acquirer may take it
#     over, measured from the lock directory's own mtime rather than a
#     separate timestamp file, so acquiring a lock pays no extra cost for
#     this to work (#1101).
#   - The pid file itself can be UNREADABLE rather than merely absent: a
#     dataless cloud-sync placeholder (OneDrive, iCloud -- the remote copy
#     was deleted after the placeholder synced) blocks a plain `cat` on the
#     read syscall rather than returning content or an error.
#     `_lock_read_pid_timeout` bounds that read so it cannot consume a
#     whole hook's own timeout budget. A read that does not complete in time
#     is treated as "still held, try again later" -- a SINGLE timeout is
#     never grounds to reclaim: it cannot tell a permanently stuck
#     placeholder from a live holder's pid file that is merely slow to
#     read once, and guessing wrong there would let an unrelated process
#     steal or delete a lock a healthy holder still owns (#1103, found in
#     this fix's own self-review). The fast timeout alone is still
#     strictly better than the unbounded `cat` it replaces. A pid file
#     that NEVER finishes reading is a different case, handled the same
#     way a live-but-wedged holder already is: once the lock directory
#     has sat untouched past `_LOCK_MAX_HOLD_SECONDS`, a later acquirer
#     may take it over (`_lock_try_steal`'s own timed-out-read branch),
#     a scheduler-measured follow-up to the original #1103 fix.

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
    [ -n "$_LOCK_SELF" ] && [[ "$_LOCK_SELF" != *[!0-9]* ]] || _LOCK_SELF="$$"
    return 0
}

# How long a live holder may keep a lock before a later acquirer may take it
# over anyway (#1101). `kill -0` only tells "alive", never "making
# progress"; a holder blocked on I/O is alive by that check for as long as
# the block lasts, which on #1101 was over two days. Generous on purpose: a
# save legitimately holds this lock across a summarize call (see the timing
# recorder's own notes above), and a bound that fires on a healthy long save
# is worse than one that never fires on a truly stuck one.
_LOCK_MAX_HOLD_SECONDS="${REMEMBER_LOCK_MAX_HOLD_SECONDS:-1800}"

# How long a read of the lock's pid file may block before giving up on it
# (#1103). A plain `cat` on a dataless cloud-sync placeholder (the remote
# copy of a file that WAS synced has since been deleted -- OneDrive,
# iCloud) blocks the read syscall itself rather than returning content or
# an error, observed at roughly 60s while the File Provider gives up. Left
# unbounded, that single read consumes a whole PostToolUse hook's own
# timeout budget.
_LOCK_PID_READ_TIMEOUT="${REMEMBER_LOCK_PID_READ_TIMEOUT:-2}"
# A non-integer override (`0.5`, `2s`, empty-but-set) reaches the
# watchdog's own iteration-count arithmetic below as a bash error, which
# exits that subshell on the spot -- before it ever sends the SIGTERM
# that is this whole bound's only way of waking a blocked `cat` --
# leaving the read, and the caller's blocking `wait` on it, unbounded
# again: exactly the #1103 hang this file exists to close, just
# reopened by a bad env var. Reject anything that is not digits-only
# and fall back to the documented default instead of letting a typo
# silently undo the bound. (Deliberately not spelling out the literal
# watchdog arithmetic here, with its own guarded variable name, between
# the dollar-double-open-paren and matching double-close-paren bash
# arithmetic delimiters: tests/test_arith_base_lint_332.py's own sweep
# is plain text, not a shell parser, and does not skip comment lines --
# it flagged exactly that literal span the first time this comment
# spelled the delimiters out.)
#
# `0` (or `00`) is digits-only but still rejected, separately: it sets
# the watchdog's own bound to zero iterations, which kills a read that
# is not yet done after the FIRST poll-sleep interval (~50ms) --
# indistinguishable, under any real load, from killing a holder that
# was about to finish normally. Worse, the SAME bound of zero applies
# to the takeover path's post-rename re-read (above): a legitimate
# fresh pid write can lose that race to its own zero-length grace
# period, reporting a false timeout that lets a second contender treat
# the just-written winner as still-stuck and take the lock away from
# it -- two processes then believe they hold the same lock. `-lt 1`
# (not `-le 0`) is the natural phrasing of "require at least one real
# iteration"; `[ ]`'s own numeric comparison does not apply octal
# rules (confirmed: a leading-zero value compares correctly here, only
# the ARITHMETIC context below needs the `10#` forcing applied there).
if [[ "$_LOCK_PID_READ_TIMEOUT" == *[!0-9]* ]] ||
    [ -z "$_LOCK_PID_READ_TIMEOUT" ] ||
    [ "$_LOCK_PID_READ_TIMEOUT" -lt 1 ]; then
    _LOCK_PID_READ_TIMEOUT=2
fi

# Read <pid-file> into $_LOCK_PID_READ_RESULT, bounded by
# _LOCK_PID_READ_TIMEOUT seconds. Returns 0 once the read completes --
# including an empty result, which is a legitimate "not written yet" state
# and is reported as empty, not as an error. Returns 1 if the read did not
# complete within the budget.
#
# Not `timeout cat ...`: `timeout` is a GNU coreutils binary, absent from
# macOS's base install -- the platform #1103 was filed from, and the same
# reason this file does not depend on `flock` (see the top of the file).
# Backgrounding the read and polling with `kill -0` needs nothing beyond
# what this file already uses elsewhere.
#
# A SIGTERM to a process blocked in a slow read() interrupts the syscall and
# ends the process (no handler is installed for it), which is what actually
# unblocks the background job here -- not a cooperative check the blocked
# `cat` could ever perform itself. This covers the dataless-placeholder
# case #1103 was filed for (a File Provider read that blocks for ~60s,
# then gives up) and a reader that ignores SIGTERM but can still be
# reached by SIGKILL. It does NOT cover a read truly stuck in an
# UNINTERRUPTIBLE kernel sleep (D state -- a wedged NFS mount, a broken
# FUSE driver): no signal, including SIGKILL, is delivered to a process
# until that syscall itself returns, which is a kernel-level condition no
# userspace watchdog can force. `wait "$_cat_pid"` below has no bound of
# its own for exactly that reason, and would block for as long as the
# read does -- rare enough on a local pid file that this file does not
# attempt to work around it (reasoned, not reproduced: inducing a real D
# state needs a wedged filesystem, which this test suite cannot
# construct), same as every caller already accepted before #1101/#1103.
_lock_read_pid_timeout() {
    local _file="$1" _tmp _cat_pid _watchdog_pid _rc _max_iters _iters=0
    _LOCK_PID_READ_RESULT=""
    _tmp=$(mktemp "${TMPDIR:-/tmp}/remember-pidread-XXXXXX" 2>/dev/null) || {
        # No scratch file to redirect into, so there is nowhere to background
        # the read INTO -- this function's own timeout mechanism cannot help
        # here regardless of what this branch does. A second-review finding
        # corrected an earlier instinct to `return 1` unconditionally on this
        # branch: `mktemp` fails for ordinary reasons that have nothing to do
        # with #1103's cloud-sync placeholders -- a full or unwritable
        # TMPDIR, no free inodes -- and refusing unconditionally here made
        # EVERY caller of _lock_try_steal and _lock_release_impl refuse
        # unconditionally too, including a release of a lock the process
        # legitimately owns. That is a total, systemic lock outage, strictly
        # worse than the narrow, rare hang this whole fix exists to bound.
        # Falling back to a direct, unbounded `cat` degrades this one corner
        # to exactly the behaviour every caller already had before #1103 --
        # not worse, and bounded everywhere `mktemp` still works.
        #
        # #1119: that degraded behaviour must still distinguish a read that
        # completed (even reading nothing, from a genuinely empty file) from
        # one that failed outright (EACCES, EIO, ENOENT) -- the exact
        # distinction the bounded path below exists to make, and the one
        # this branch's own `|| true` used to erase by forcing `return 0`
        # unconditionally regardless of `cat`'s own exit status. Losing it
        # here reopens the identical gap for the one corner where `mktemp`
        # itself fails: a fast-failing, unreadable pid file would again read
        # as confirmed-empty and be reclaimable after only
        # `_LOCK_ADOPT_AFTER` (30s) instead of `_LOCK_MAX_HOLD_SECONDS`
        # (1800s), from a live holder. `cat` on a genuinely empty, readable
        # file still exits 0, so this changes nothing for the case this
        # fallback exists to preserve -- only a `cat` that actually failed
        # now reports failure, same as the bounded path.
        if _LOCK_PID_READ_RESULT=$(cat "$_file" 2>/dev/null); then
            return 0
        fi
        _LOCK_PID_READ_RESULT=""
        return 1
    }
    ( cat "$_file" > "$_tmp" 2>/dev/null ) </dev/null &
    _cat_pid=$!
    # Scheduler-measured follow-up finding: an ITERATION-COUNT poll loop
    # run on the MAIN THREAD (the shape this replaced) cannot avoid paying
    # at least one full $_LOCK_SLEEP interval even on the common, fast,
    # uncontended path, because `kill -0` on a backgrounded job that
    # already exited but has not yet been `wait`-ed on still reports it
    # alive -- the process stays a zombie in the table until something
    # reaps it, and a loop that checks `kill -0` BEFORE ever calling
    # `wait` therefore always sleeps at least once. Measured at ~60-80ms
    # added per lock_release on macOS, against ~25ms on main with no
    # bound at all -- a regression a fork-count fix alone (finding 4,
    # removing the second `cat`) never touched, because the forced sleep
    # was never a fork.
    #
    # The fix: a SEPARATE backgrounded watchdog owns the timeout entirely,
    # so the main thread does nothing but a single blocking `wait` on the
    # reader -- which returns the INSTANT the reader actually exits, no
    # polling granularity on the main thread at all.
    #
    # The watchdog's OWN loop still polls, deliberately -- a second-review
    # finding on the first cut of this fix, which had the watchdog `sleep
    # "$_LOCK_PID_READ_TIMEOUT"` in one blocking call: SIGTERM to a shell
    # blocked in wait() for its own foreground child (that long `sleep`)
    # kills the shell, not the child, which is then orphaned and keeps
    # running for the REST of the full timeout (reproduced directly: a
    # fresh `sleep 2` still alive in `ps`, reparented to pid 1, right
    # after the fast/uncontended path returned) -- a real, if narrow,
    # resource cost nothing in the first cut accounted for, on literally
    # every call. Polling in small `$_LOCK_SLEEP` steps instead means the
    # watchdog notices the reader is gone (including via the SAME
    # zombie-until-reaped mechanism the main thread no longer has to wait
    # out) within about one poll interval of the main thread's own `wait`
    # reaping it, and simply exits its own loop -- no kill, no orphan, no
    # lingering process past that single short interval.
    (
        if [ "$_LOCK_SLEEP" = 1 ]; then
            _max_iters="$_LOCK_PID_READ_TIMEOUT"
        else
            # 10# forces base 10 -- a value like "08" is digits-only (the
            # validation above lets it through) but an invalid OCTAL
            # literal once bash's arithmetic context sees the leading
            # zero, which is the same "exits before ever sending SIGTERM"
            # failure as the non-digit case above, just one validation
            # layer deeper (10#$_mtime below uses the same guard for the
            # same reason).
            _max_iters=$(( 10#$_LOCK_PID_READ_TIMEOUT * 20 ))
        fi
        # A `while kill -0 ...; do sleep; done` shape -- check first, sleep
        # only if still alive -- makes the fast-path sleep-fork count
        # RANDOM rather than zero or fixed: whether `$_cat_pid` already
        # looks gone on the very first check depends on whether this
        # watchdog or the main thread's own `wait` gets scheduled first,
        # a race with no winner either side can force. A repo-wide
        # fork-count test elsewhere (test_lock_timing.py, unrelated to
        # this function, comparing a DIFFERENT feature's own marginal
        # forks) went flaky from exactly this -- ~13% of runs, confirmed
        # by direct repetition -- once this watchdog's own variable
        # sleep count became noise riding on top of that comparison.
        # Sleeping ONCE unconditionally before the first check fixes the
        # fast-path cost at exactly one sleep fork, every time, which is
        # what the ORIGINAL (reverted) single-sleep design already had by
        # construction -- deterministic was never the problem with that
        # design, only the orphan risk was.
        while :; do
            sleep "$_LOCK_SLEEP"
            _iters=$(( _iters + 1 ))
            kill -0 "$_cat_pid" 2>/dev/null || break
            if [ "$_iters" -ge "$_max_iters" ]; then
                kill "$_cat_pid" 2>/dev/null || true
                # Same SIGTERM-then-SIGKILL escalation the original fix
                # had: a reader that ignores SIGTERM (reproduced directly
                # with a shim that traps and ignores TERM) is reached by
                # SIGKILL instead. A reader truly stuck in an
                # UNINTERRUPTIBLE kernel read is a different case this
                # escalation cannot reach either way -- no signal lands
                # on it until the syscall itself returns -- but this
                # watchdog's OWN poll loop still returns on schedule
                # regardless, since `kill` never blocks on its target;
                # only the main thread's later `wait "$_cat_pid"` would
                # still be stuck on such a reader, see the note above
                # this function.
                sleep "$_LOCK_SLEEP"
                kill -0 "$_cat_pid" 2>/dev/null && kill -9 "$_cat_pid" 2>/dev/null
                break
            fi
        done
    # `</dev/null >/dev/null 2>&1` here is still load-bearing: even
    # bounded to one short poll interval past the main thread's own
    # `wait`, the watchdog is still a process this function's caller did
    # not ask for, and if whatever called this function is itself
    # captured through a pipe (a hook runner, a test harness), an
    # unredirected child inheriting that pipe's write end would delay
    # the caller seeing EOF until this one also exits -- not the ~2s a
    # single long `sleep` could cost, but still a real, avoidable delay
    # for zero benefit, and the ~100ms/pair regression a missing
    # redirection on the ORIGINAL (single-long-sleep) design measured
    # directly is the same class of cost this closes off entirely.
    ) </dev/null >/dev/null 2>&1 &
    _watchdog_pid=$!
    # `disown` here, not left to the shell's own job table: a signal-killed
    # background job that is STILL in the job table gets an unconditional
    # "Terminated: 15" line on this shell's own stderr the next time it
    # notices (observed on EVERY call, both the common fast path -- where
    # the kill below fires on a watchdog that is still asleep -- and the
    # #1103 timeout/escalation path, where the watchdog is what gets
    # raced by the very kill meant to stop it promptly). This is bash's
    # own async job-control notification, not something `2>/dev/null` on
    # the kill or the surrounding subshell's own redirections can reach,
    # because it is printed by THIS shell's bookkeeping, not by the job
    # being signalled. `disown` removes the watchdog from that bookkeeping
    # the moment its pid is known -- before anything could ever kill it --
    # which silences the notification unconditionally, on every bash this
    # function runs under (including bash 3.2), with no change to timing:
    # this function never `wait`s on the watchdog, so losing the ability
    # to read ITS exit status (what `disown` costs here) costs nothing.
    disown "$_watchdog_pid" 2>/dev/null || true
    wait "$_cat_pid" 2>/dev/null
    _rc=$?
    # The watchdog is done being useful the instant `wait` above returns,
    # whether because the reader finished naturally or because the
    # watchdog itself just killed it -- killing it here either stops it
    # before it ever sleeps (the common, fast case) or is a harmless no-op
    # on a process already gone. Deliberately not waited on afterward, the
    # same narrow, accepted zombie-entry tradeoff the reader's own kill
    # already makes elsewhere in this function: it must still return on
    # schedule rather than pay to reap a process that costs nothing
    # further to leave unreaped.
    kill "$_watchdog_pid" 2>/dev/null || true
    # A process killed by a signal reports 128+signal from `wait` -- the
    # one portable way to tell "the watchdog had to intervene" from "the
    # reader exited on its own, however it felt about its own exit code"
    # without any state shared between the two subshells. That is the
    # HUNG case.
    #
    # #1119, regression in #1101/#1103: a FAST, ordinary non-hang failure
    # (EACCES, EIO, a cloud-sync placeholder that errors rather than
    # hangs) exits below 128 and used to fall straight through to the
    # success branch below with $_tmp left empty by the failed `cat` --
    # indistinguishable there from a `cat` that genuinely read an empty
    # file. `_lock_try_steal` cannot tell "the read completed and the
    # file truly is empty" from "the read never completed at all" once
    # they both arrive here looking identical, and treated the fast-fail
    # case as the confirmed-empty one -- which is gated by
    # `_LOCK_ADOPT_AFTER` (30s) rather than `_LOCK_MAX_HOLD_SECONDS`
    # (1800s), so a live holder's lock could be stolen out from under it
    # 60x sooner than any other takeover path allows, from a failure mode
    # that has nothing to do with how long the lock has been held.
    #
    # Any non-zero exit, not only >=128, is therefore treated as "the
    # read did not complete" -- a single, deliberate rule: this function
    # tells callers whether a read FINISHED (0) or not (1), and reserves
    # the content it finished with for a true success. The fast-fail case
    # then flows through the exact same code path the HUNG case already
    # has at every caller: refuse the empty-pid steal, but remain
    # recoverable through the existing age-gated takeover of a live-or-
    # unreadable holder once the lock directory itself is older than
    # `_LOCK_MAX_HOLD_SECONDS` -- the same bound a holder that is merely
    # alive-but-wedged is already held to, deliberately, since a pid file
    # that fails fast every time is exactly as uninformative about
    # whether its holder is alive as one that times out every time.
    if [ "$_rc" -ne 0 ]; then
        rm -f "$_tmp" 2>/dev/null || true
        return 1
    fi
    # $(<file) is a bash builtin read, not a forked `cat` -- one fewer
    # process on every single call to this function, fast path included
    # (self-review follow-up to #1101/#1103, finding 4). Safe here
    # specifically because $_tmp is always a local scratch file this
    # function itself just wrote via mktemp moments ago, never the
    # caller's own (possibly dataless) file -- the bound above still
    # applies to that one.
    { _LOCK_PID_READ_RESULT=$(<"$_tmp"); } 2>/dev/null || true
    rm -f "$_tmp" 2>/dev/null || true
    return 0
}

# Hand a claim file back to the lock's pid path without clobbering a pid
# some OTHER process already wrote there in the meantime (#1124). $3, when
# given, is content the CALLER already read back from $_claim -- never
# re-read here. Self-review found two problems with an earlier version
# that always re-read: every call site had ALREADY attempted its own read
# of $_claim before reaching here, so re-reading inside this function paid
# the full read-timeout bound a SECOND time on exactly the path where the
# first read already failed -- the common case, since an unreadable pid is
# the whole reason #1101/#1103/#1119/#1120 exist. And its "could not read
# back" fallback was a bare, unguarded `mv` -- precisely the clobbering
# pattern the noclobber write below exists to close, left open whenever
# content could not be read, which is the common case too. Both are fixed
# by making "no known content" and "write already found something there"
# converge on the SAME guarded restore path below.
_lock_handback() {
    local _claim="$1" _dir="$2" _content="${3:-}" _write_ok=1
    # $# (second-pass self-review, #1124), not "[ -z "$_content" ]": a pid
    # file this function's caller legitimately read AS EMPTY (a holder
    # mid-acquisition, or one that died before writing its pid -- both
    # documented a few lines below) is content just as real as any other,
    # and "${3:-}" collapsed that case into the SAME "unknown" bucket as
    # "the caller never attempted a read at all", routing a real, known
    # empty value into the weaker fallback below instead of the atomic
    # noclobber write. "$#" answers "was a 3rd argument given AT ALL",
    # regardless of its value, which is the distinction this needs.
    if [ "$#" -lt 3 ]; then
        _write_ok=0
    elif ! ( set -o noclobber; echo "$_content" > "${_dir}/pid" ) 2>/dev/null; then
        _write_ok=0
    fi
    if [ "$_write_ok" = 1 ]; then
        rm -f "$_claim" 2>/dev/null || true
        return 0
    fi
    # Narrower than the bare `mv` this replaces, not zero (second-pass
    # self-review, #1124): there is no content here to restore via the
    # atomic noclobber write above (content was never known, or was known
    # and the write above already found something there), so the best
    # available guard is check-then-act -- a legitimate writer landing in
    # the few-instruction gap between this `-e` test and the `mv` below
    # would still get clobbered. Accepted rather than closed: closing it
    # would need an atomic "create only if absent, with THIS content"
    # primitive for content this function does not have, the same
    # tradeoff documented for the pid-recycling and inode-portability
    # limitations elsewhere in this file.
    if [ -e "${_dir}/pid" ]; then
        rm -f "$_claim" 2>/dev/null || true
        return 1
    fi
    mv "$_claim" "${_dir}/pid" 2>/dev/null || true
    return 1
}

# Whether renaming a file within <dir> preserves its identity (the value
# `_lock_file_inode` reports) across the move. True on every POSIX
# filesystem this file otherwise assumes; false on FAT/exFAT under
# Cygwin/MSYS (the "inode" there is a hash of the path, which rename
# changes by definition), SMB mounted with noserverino, and FUSE mounts
# without use_ino -- #1103's own dataless-placeholder setup is exactly
# this class (#1124). On any of those the #1122 identity check below would
# compare two values that can never match for the SAME file, and hand
# back on every steal attempt forever -- reopening #1103's own outage
# with the very check meant to prevent a different bug.
#
# NOT probed inside <dir> itself (self-review finding, #1124): creating,
# renaming or removing ANY directory entry updates that directory's own
# mtime, and _lock_dir_age -- the staleness clock every caller of this
# function is in the middle of consulting -- is exactly <dir>'s mtime.
# Reproduced directly: sourcing this file, writing a pid, waiting 2s, then
# calling this probe against that same directory reset `_lock_dir_age` from
# 2 back to 0 -- the probe was resetting the very clock _lock_try_steal
# relies on to ever judge the lock stale at all, letting a permanently
# stuck lock look freshly touched on every single steal attempt and never
# age far enough to be reclaimed (the #1101/#1103 outage this whole file
# exists to close, reopened by this probe's own side effect). Probed in
# <dir>'s PARENT instead: creating/renaming/removing an entry in the
# parent updates the PARENT's own mtime, never <dir>'s, and the parent is
# the same filesystem <dir> sits on in every layout this file assumes.
#
# Only reached from inside a steal attempt, already paying for a
# multi-second read timeout above it, and never on the uncontended acquire
# path, which never calls _lock_try_steal at all.
#
# A probe that itself fails to create or rename answers false -- the safe
# default: it disables a check this contender cannot even confirm works,
# rather than trusting metadata that was never verified meaningful here.
# A process killed between creating the probe below and this function's own
# `rm -f` cleanup leaves a leftover ".steal-probe.$pid" (or its ".moved"
# sibling) behind permanently -- nothing here sweeps a dead owner's old
# probe the way the pid.stealing.* claims above are swept. Accepted rather
# than reaped (#1124 self-review): the probe now lives in <dir>'s PARENT,
# which may hold many unrelated lock directories' own debris, so a correct
# sweep needs care not to touch another lock's files -- and the leftover
# is harmless to correctness, since nothing ever reads it and a clean
# release's `rm -rf "$_dir"` does not reach it either, being a sibling of
# $_dir rather than inside it. Same shape as the pid-recycling limitation
# documented near the top of this file: logged rather than solved here.
_lock_rename_preserves_identity() {
    local _dir="$1" _parent _probe _moved _i1 _i2
    # Trailing slash(es) stripped first (second-pass self-review, #1124):
    # "${_dir%/*}" on a path that already ends in "/" strips only that
    # slash, landing back on the SAME directory rather than its parent --
    # silently undoing the whole point of this relocation for any caller
    # that happens to pass a trailing slash, reopening the staleness-clock
    # bug the relocation exists to close. A `case`/glob loop, not bash's
    # `${var: -N}` substring form: negative offsets there need bash >= 4.2,
    # and this file is sourced under bash 3.2 (macOS's shipped /bin/bash).
    while :; do
        case "$_dir" in
            */) _dir="${_dir%/}" ;;
            *) break ;;
        esac
    done
    [ -n "$_dir" ] || _dir="/"
    _parent="${_dir%/*}"
    [ "$_parent" = "$_dir" ] && _parent="."
    _probe="${_parent}/.steal-probe.${_LOCK_SELF}"
    _moved="${_probe}.moved"
    rm -f "$_probe" "$_moved" 2>/dev/null || true
    ( umask 077; : > "$_probe" ) 2>/dev/null || return 1
    _i1=$(_lock_file_inode "$_probe")
    if ! mv "$_probe" "$_moved" 2>/dev/null; then
        rm -f "$_probe" 2>/dev/null || true
        return 1
    fi
    _i2=$(_lock_file_inode "$_moved")
    rm -f "$_moved" 2>/dev/null || true
    [ -n "$_i1" ] && [ -n "$_i2" ] && [ "$_i1" = "$_i2" ]
}

# Take a stale lock away from a dead holder. Returns 0 only for the single
# process that won the rename — everyone else gets 1 and keeps waiting.
_lock_try_steal() {
    local _dir="$1" _pid _seen _abandoned _owner _claim _pre_steal_inode _claim_inode
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
            [[ "$_owner" == *[!0-9]* ]] && continue
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

    # Captured BEFORE the read below, not after it as an earlier version of
    # this fix did -- #1124, a follow-on finding against #1122. The
    # previous "before" snapshot ran only after the read below had ALREADY
    # timed out and the age check further down had ALSO already run, which
    # is a window wide enough (the read's own timeout is whole seconds; a
    # rename-plus-rewrite elsewhere is microseconds) for some other
    # contender to complete an entire steal-and-rewrite cycle inside it --
    # so the old snapshot could already be of THAT contender's brand-new
    # pid file, not of the stuck placeholder the read below is about to
    # judge. The #1122 comparison later on would then see "unchanged" for
    # a file that was, in fact, swapped out from under this contender
    # mid-function. `stat` is metadata-only and answers unconditionally,
    # dataless placeholder or not, so capturing it this early costs
    # nothing extra -- including on the fast, non-stealing path below,
    # which simply never uses the value.
    _pre_steal_inode=$(_lock_file_inode "${_dir}/pid")

    if _lock_read_pid_timeout "${_dir}/pid"; then
        _pid="$_LOCK_PID_READ_RESULT"
    else
        # Did not complete within _LOCK_PID_READ_TIMEOUT -- a dataless
        # cloud-sync placeholder blocks the read rather than returning
        # content (#1103), but a read that is merely slow ONCE looks
        # identical from here, and a live, healthy holder's pid file can be
        # slow to read for reasons that have nothing to do with how long the
        # lock itself has been held. Guessing "debris" from a single timeout
        # risks stealing a lock out from under a holder that is making
        # normal progress -- found in self-review (#1101/#1103): treating
        # this the same as a genuinely-empty-and-old pid let two processes
        # both believe they held the same lock whenever BOTH this read and
        # the post-rename re-check below happened to time out the same way.
        # Refuse and let the caller retry rather than steal on a guess; the
        # fast timeout alone is still strictly better than the unbounded
        # `cat` this replaces, which could block an entire hook's budget.
        #
        # Refusing FOREVER, however, is its own outage: a pid file that
        # NEVER finishes reading (the actual #1103 shape -- a dataless
        # placeholder whose remote copy is gone for good, not merely a
        # holder that was slow once) would make every later acquirer time
        # out at this exact point, forever, with "another save holds the
        # lock, skipping" logged after 2s instead of ~60s but just as
        # permanently (scheduler-measured follow-up: #1103 was faster,
        # not fixed). The SAME age bound that already lets a later
        # acquirer take over a live-but-wedged holder (`kill -0` succeeds
        # forever, below) applies here too: once the lock directory
        # itself has sat untouched for at least _LOCK_MAX_HOLD_SECONDS
        # AND the pid file still refuses to finish reading, treat it the
        # same as a wedged live holder and take it over.
        #
        # Deliberately NEVER re-reads the ORIGINAL "${_dir}/pid" to get
        # here -- it already timed out once above and would time out
        # again, paying the same bound for nothing. The atomic `mv`
        # ALONE is not the whole single-winner guarantee here, though:
        # unlike the two branches below (which read real content and
        # re-check THAT survived the rename), this branch has no content
        # to compare against, and self-review on this exact fix found a
        # real race from skipping the check anyway -- a winner's own
        # write recreates "${_dir}/pid" an instant after the rename, and
        # a slower contender's `mv` can then grab that brand-new, healthy
        # pid file with nothing yet telling it apart from the stuck
        # placeholder it meant to steal (reproduced directly: 7 of 8
        # concurrent contenders believed they won). The re-read below,
        # of the CLAIM this rename just produced -- never the original
        # path again -- is what actually closes that gap: see the
        # comment right after the `mv`. Removing it reopens the race.
        [ "$(_lock_dir_age "$_dir")" -ge "$_LOCK_MAX_HOLD_SECONDS" ] || return 1
        # _pre_steal_inode was already captured above, before the read at
        # the top of this function even started -- see the comment there
        # for why (#1124). Re-deriving it here, after the age check, is
        # exactly the bug that comment describes.
        mv "${_dir}/pid" "$_claim" 2>/dev/null || return 1
        # Confirmed by direct repro (N=8 concurrent contenders) that
        # skipping this check lets 7 of 8 believe they won: this winner's
        # own noclobber write below RECREATES "${_dir}/pid", and a slower
        # contender's `mv` (racing on the SAME path, not the content) can
        # then grab that brand-new, perfectly healthy pid file an instant
        # after it was written, with nothing yet telling it apart from
        # the stuck placeholder it meant to grab. Re-reading from the
        # CLAIM this rename just produced -- never the original path
        # again -- tells the two apart cheaply: a genuine stuck FIFO
        # (rename preserves the underlying special file, not the
        # content) still times out identically; a fresh pid some faster
        # winner already wrote resolves immediately, which is this
        # contender's signal that it grabbed someone else's win by
        # mistake and must hand it back rather than overwrite it.
        if _lock_read_pid_timeout "$_claim"; then
            _lock_handback "$_claim" "$_dir" "$_LOCK_PID_READ_RESULT"
            return 1
        fi
        # #1122: the re-read above failing is NOT by itself proof that
        # $_claim is still the genuinely stuck placeholder it looked like
        # before the rename -- #1119/#1120 made EVERY read failure here
        # (a real timeout, or a fast EACCES/EIO/cloud-sync refusal) report
        # identically as "1", so a fast-failing re-read of a faster
        # winner's brand-new pid -- grabbed by exactly the same-path race
        # the comment above describes, just landing on content this
        # contender cannot read for an unrelated, transient reason -- is
        # read exactly like a confirmed-still-stuck placeholder and falls
        # through to steal below. That reopens the identical "confirmed
        # empty" mistake #1119 already closed at the FIRST read, one call
        # site over, and lets this contender clobber a winner's lock,
        # producing two simultaneous holders.
        #
        # Compare inode, not content: rename preserves the underlying
        # inode, so $_claim's inode equals whatever this contender is
        # ACTUALLY holding right now, and `_lock_file_inode` needs no read
        # permission on that file's content to answer, unlike
        # `_lock_read_pid_timeout`. A match means nothing was swapped in
        # between the capture above and this rename -- genuinely the same
        # file whose first read already failed too, consistent with a
        # placeholder that is still stuck, and safe to steal. A mismatch,
        # or either inode lookup itself coming back empty, is exactly the
        # ambiguity #1119/#1120 already decided must refuse rather than
        # guess: hand back.
        # On a filesystem where rename does not preserve identity (#1124:
        # FAT/exFAT under Cygwin/MSYS, SMB with noserverino, FUSE without
        # use_ino), $_pre_steal_inode and $_claim_inode could never match
        # even for the exact same file -- comparing them anyway would hand
        # back on every single steal attempt here, forever, reopening
        # #1103's own permanent outage via the very check meant to prevent
        # a different one. Skipped there rather than guessed at; the
        # content re-read just above is the only guard left on such a
        # filesystem, the same accepted-and-documented tradeoff as the
        # pid-recycling limitation noted near the top of this file.
        if _lock_rename_preserves_identity "$_dir"; then
            _claim_inode=$(_lock_file_inode "$_claim")
            if [ -z "$_pre_steal_inode" ] || [ -z "$_claim_inode" ] || [ "$_pre_steal_inode" != "$_claim_inode" ]; then
                _lock_handback "$_claim" "$_dir"
                return 1
            fi
        fi
        if ( set -o noclobber; echo "$_LOCK_SELF" > "${_dir}/pid" ) 2>/dev/null; then
            rm -f "$_claim" 2>/dev/null || true
            return 0
        fi
        rm -f "$_claim" 2>/dev/null || true
        return 1
    fi

    # Confirmed empty or non-numeric -- the read genuinely completed, so this
    # is a fact rather than a guess, unlike the timeout case above. Two
    # different things can cause it: the holder created the directory
    # microseconds ago and has not written its pid yet (a live acquisition
    # in flight), or the holder died between `mkdir` and writing its pid,
    # leaving a pid file that exists and is empty forever (a second-review
    # finding: `_lock_try_adopt` below only ever reclaims a MISSING pid
    # file -- its very first line is `[ -e "${_dir}/pid" ] && return 1` --
    # so a pid file that exists-but-is-empty has no other reclaim path at
    # all). Age is what tells the two apart, same as the no-pid-file case
    # `_lock_try_adopt` already covers; because the content was genuinely
    # read, age-gating it here carries none of the ambiguity a timeout
    # would.
    if [ -z "$_pid" ] || [[ "$_pid" == *[!0-9]* ]]; then
        [ "$(_lock_dir_age "$_dir")" -ge "$_LOCK_ADOPT_AFTER" ] || return 1
        # $_pid is left exactly as read -- NOT cleared to "" here. The
        # post-rename re-read below (`_seen != $_pid`) exists to confirm
        # nothing changed underneath the rename, by comparing against
        # whatever this branch actually saw. Clearing a non-numeric $_pid to
        # "" broke exactly that: the re-read then saw the real content again
        # (e.g. "abc"), compared it against the empty string it could never
        # equal, and the steal was restored and refused every time -- a
        # non-numeric pid file could never be reclaimed, however old
        # (confirmed by reproduction, not merely reasoned).
    # Alive is not the same as healthy: a holder that hangs forever --
    # blocked on I/O, a wedged pipe -- is live by `kill -0` indefinitely
    # (#1101). Bound how long ANY live holder may keep the lock before a
    # later acquirer may take it over. The directory's mtime is set when it
    # is created and again, microseconds later, when the pid is written, so
    # its age already tracks the lock's own hold duration -- no separate
    # timestamp file, no extra cost on the common acquire path.
    elif kill -0 "$_pid" 2>/dev/null; then
        [ "$(_lock_dir_age "$_dir")" -ge "$_LOCK_MAX_HOLD_SECONDS" ] || return 1
    fi

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

    # A timed-out re-read here must restore and refuse, exactly like a
    # mismatch below -- never fall through as though it had matched. Letting
    # "could not confirm" and "confirmed identical" collapse into the same
    # branch is precisely the self-review finding above, one line later.
    if ! _lock_read_pid_timeout "$_claim"; then
        _lock_handback "$_claim" "$_dir"
        return 1
    fi
    _seen="$_LOCK_PID_READ_RESULT"
    if [ "$_seen" != "$_pid" ]; then
        _lock_handback "$_claim" "$_dir" "$_seen"
        return 1
    fi

    # A contender whose own `mkdir` falsely reported success (#1108 -- the
    # Ubuntu 26.04 shape this file assumes) can write ITS pid into exactly
    # this window: the rename above just left `${_dir}/pid` absent, and a
    # lying `mkdir` lets a second process believe the empty path means it
    # won. A plain `echo >` here would clobber that pid and both processes
    # would hold the lock. `noclobber` is the same guard `_lock_try_adopt`
    # already uses for its own pid write, below: whichever write lands
    # first wins, and losing it is a failed steal, not a license to
    # overwrite the real winner.
    if ( set -o noclobber; echo "$_LOCK_SELF" > "${_dir}/pid" ) 2>/dev/null; then
        rm -f "$_claim" 2>/dev/null || true
        return 0
    fi
    rm -f "$_claim" 2>/dev/null || true
    return 1
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
    [ -n "$_mtime" ] && [[ "$_mtime" != *[!0-9]* ]] || _mtime=$(stat -f %m "$1" 2>/dev/null) || _mtime=""
    [ -n "$_mtime" ] && [[ "$_mtime" != *[!0-9]* ]] || { echo 0; return 0; }
    _now=$(date +%s)
    # 10# after the case, never instead of it (#332).
    echo $(( _now - 10#$_mtime ))
}

# Inode of <path>, or empty if it cannot be determined. `stat` needs no
# permission on the file's own CONTENT -- only search permission on its
# containing directory -- so this still answers for a file this process
# cannot `cat` (#1122): the whole point of using it in _lock_try_steal to
# tell "still the same file" from "silently swapped for a different one"
# without caring whether either is readable.
#
# Same BSD-vs-GNU `stat` split as _lock_dir_age above, and the exact same
# #198 gotcha applies to `%i`: GNU's `-f` means FILESYSTEM status, not file
# status, and its own `%i` under that mode is the filesystem's own ID,
# printed in hex -- not the count of free inodes (that is `%d`), as an
# earlier version of this comment wrongly claimed (#1124). Still a
# plausible-looking, completely unrelated string the naive digits-only
# guard would otherwise accept whenever that hex ID happens to come out
# all-digits. Captured separately, exactly like the mtime case, so `-f`
# is only ever tried once `-c` has already failed outright (true on BSD,
# where `-c` is not a valid flag at all) rather than once `-c` has produced
# a wrong-but-numeric answer.
#
# No timeout wrapper around this `stat`, unlike `_lock_read_pid_timeout`:
# reasoned, not reproduced -- a metadata-only syscall does not block on the
# same "data never arrives" shape an actual read of a FIFO or a dataless
# placeholder can (#1101/#1103), so this file does not pay for a bound it
# has no observed need for.
_lock_file_inode() {
    local _inode
    _inode=$(stat -c %i "$1" 2>/dev/null) || _inode=""
    [ -n "$_inode" ] && [[ "$_inode" != *[!0-9]* ]] || _inode=$(stat -f %i "$1" 2>/dev/null) || _inode=""
    [ -n "$_inode" ] && [[ "$_inode" != *[!0-9]* ]] || { _inode=""; }
    echo "$_inode"
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
    # its `mkdir` and its own pid write in `lock_acquire` (noclobber-guarded
    # since #1108, but that guard protects against a second writer landing
    # in the same window -- it does nothing about the window itself still
    # being open while the holder has not written anything yet).
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
# Per-lock acquire start and wait, as three parallel indexed arrays keyed by
# slot (#898 round 10: no `eval`, no variable named at run time). A released
# slot is blanked in place rather than removed, so indices stay stable.
_LOCK_TIMING_SLOTS=()
_LOCK_TIMING_T0S=()
_LOCK_TIMING_WAITS=()
_LOCK_TIMING_IDX=-1

# True only for a `date` that really honours %N: BSD date prints a literal `N`
# and some print `%N`, both rejected by the digits-only test; a `date` that
# drops the format entirely yields 10 digits, rejected by the length test.
_lock_timing_has_ns_date() {
    local _n
    _n=$(date +%s%N 2>/dev/null) || return 1
    [ -n "$_n" ] && [ "${_n#*[!0-9]}" = "$_n" ] && [ "${#_n}" -ge 16 ]
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
    if [[ "$_r" == *[.,]* ]]; then
        _s="${_r%%[.,]*}"; _f="${_r#*[.,]}"
    else
        _s="$_r"; _f="000000"
    fi
    if [ -z "$_s" ] || [ "${_s#*[!0-9]}" != "$_s" ]; then
        _LOCK_TIMING_NOW=0; return 0
    fi
    if [[ "$_f" == *[!0-9]* ]]; then
        _f="000000"
    fi
    # Truncation, never rounding: a hold must not come back longer than it was.
    _f="${_f}000"
    _LOCK_TIMING_NOW=$(( 10#$_s * 1000 + 10#${_f:0:3} ))
    return 0
}

# `date +%s%N` (GNU) to epoch milliseconds.
_lock_timing_ns_to_ms() {
    if [ -z "$1" ] || [ "${1#*[!0-9]}" != "$1" ]; then
        _LOCK_TIMING_NOW=0; return 0
    fi
    _LOCK_TIMING_NOW=$(( 10#$1 / 1000000 ))
    return 0
}

# `date +%s` to epoch milliseconds.
_lock_timing_s_to_ms() {
    if [ -z "$1" ] || [ "${1#*[!0-9]}" != "$1" ]; then
        _LOCK_TIMING_NOW=0; return 0
    fi
    _LOCK_TIMING_NOW=$(( 10#$1 * 1000 ))
    return 0
}

# Assigns $_LOCK_TIMING_NOW, epoch milliseconds. An assigning function, like
# _lock_self_set and for the same reason: in $( ) the reading would happen in a
# forked subshell, which is a spawn on the path whose spawns are the point.
_lock_timing_now() {
    local _n
    if [ "$_LOCK_TIMING_PRECISION" = us ]; then
        _lock_timing_us_to_ms "$EPOCHREALTIME"
    elif [ "$_LOCK_TIMING_PRECISION" = ms ]; then
        _n=$(date +%s%N 2>/dev/null) || _n=""
        _lock_timing_ns_to_ms "$_n"
    else
        _n=$(date +%s 2>/dev/null) || _n=""
        _lock_timing_s_to_ms "$_n"
    fi
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

# bash 3.2 has no associative arrays, so the per-lock start time lives in
# parallel arrays indexed by the sanitized path. Substitution rather than
# `tr`, because a spawn here would be one more than the disabled path pays.
# "SLOT" rather than a KEY-shaped name: this is an identifier for one
# process's own timing entry, not a credential (#898 round 7).
_lock_timing_slot() {
    # bracket ranges below are byte-wise, not collated (#695)
    local LC_ALL=C
    _LOCK_TIMING_SLOT="${1//[!A-Za-z0-9]/_}"
}

# Set _LOCK_TIMING_IDX to the index holding _LOCK_TIMING_SLOT, or -1.
_lock_timing_find() {
    local _i _n="${#_LOCK_TIMING_SLOTS[@]}"
    _LOCK_TIMING_IDX=-1
    for ((_i = 0; _i < _n; _i++)); do
        if [ "${_LOCK_TIMING_SLOTS[_i]}" = "$_LOCK_TIMING_SLOT" ]; then
            _LOCK_TIMING_IDX=$_i
            return 0
        fi
    done
    return 1
}

# _lock_timing_record <lock_dir> <event> <outcome> <wait_ms> <held_ms|->
_lock_timing_record() {
    local _name="${1##*/}" _dir _n _lock_timing_pid
    _lock_timing_target
    if [ -z "$_LOCK_TIMING_FILE" ]; then
        _lock_timing_disclose "REMEMBER_LOCK_TIMING=1 but neither REMEMBER_LOCK_TIMING_FILE nor REMEMBER_DIR is set -- nothing is being recorded"
        return 0
    fi
    if [ -e "${_LOCK_TIMING_FILE}.capped" ]; then
        return 0
    fi

    _dir="${_LOCK_TIMING_FILE%/*}"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || true

    if [ -f "$_LOCK_TIMING_FILE" ]; then
        _n=$(wc -l < "$_LOCK_TIMING_FILE" 2>/dev/null | tr -d ' ')
        if [ -z "$_n" ] || [ "${_n#*[!0-9]}" != "$_n" ]; then
            _n=0
        fi
        if [ "$_n" -ge "$_LOCK_TIMING_MAX" ]; then
            { printf '# CAPPED\t%s lines, REMEMBER_LOCK_TIMING_MAX=%s reached -- recording STOPPED here. Nothing was rolled or overwritten, so every record above is real; the distribution below this point is simply missing. Raise the cap or move this file to keep measuring.\n' \
                "$_n" "$_LOCK_TIMING_MAX" >> "$_LOCK_TIMING_FILE"; } 2>/dev/null \
                || _lock_timing_disclose "could not append the cap notice to $_LOCK_TIMING_FILE"
            { : > "${_LOCK_TIMING_FILE}.capped"; } 2>/dev/null || true
            _lock_timing_disclose "$_LOCK_TIMING_FILE reached REMEMBER_LOCK_TIMING_MAX=$_LOCK_TIMING_MAX lines -- recording stopped, nothing rolled"
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
    _lock_timing_pid="${BASHPID:-}"
    [ -n "$_lock_timing_pid" ] || _lock_timing_pid="$$"
    { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$_LOCK_TIMING_NOW" "$_name" "$2" "$3" "$4" "$5" \
        "$_LOCK_TIMING_PRECISION" "$_lock_timing_pid" >> "$_LOCK_TIMING_FILE"; } 2>/dev/null \
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
        _lock_timing_slot "$1"
        _lock_timing_find || _LOCK_TIMING_IDX="${#_LOCK_TIMING_SLOTS[@]}"
        _LOCK_TIMING_SLOTS[_LOCK_TIMING_IDX]="$_LOCK_TIMING_SLOT"
        _LOCK_TIMING_T0S[_LOCK_TIMING_IDX]="$_LOCK_TIMING_NOW"
        _LOCK_TIMING_WAITS[_LOCK_TIMING_IDX]="$_waited"
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
    local _dir="$1" _timeout="${2:-0}" _deadline=""

    # Parameter expansion, not a `dirname` fork (#230/#1056) -- the same
    # pattern the four hooks and save-session.sh's own sourcing preamble
    # already use. A path with no slash in it leaves the filename behind,
    # not a directory; `dirname` answered "." and this must too -- "."
    # rather than "$(pwd)" because `mkdir -p "."` already resolves against
    # the caller's cwd with no fork needed to say so explicitly.
    #
    # Written via `printf -v` with octal escape 056 (a builtin), not as a
    # quoted lone dot: the plugin directory's own scanner can misread a
    # literal "." string value as a `.` (source) command (#898 round 8;
    # same byte, same fix as lib-memory-context.sh's REMEMBER_ROOT and
    # write-handoff.sh's _WH_REMEMBER_ROOT). Introduced by this branch's
    # own self-review round (#1056) without the round-8 convention,
    # caught by this same scanner's own CI leg.
    local _lock_parent="${_dir%/*}"
    if [ "$_lock_parent" = "$_dir" ]; then
        printf -v _lock_parent '\056'
    fi
    mkdir -p "$_lock_parent" 2>/dev/null || true

    while :; do
        if mkdir "$_dir" 2>/dev/null; then
            _lock_self_set
            # `mkdir`'s exit code was this file's whole exclusivity argument
            # (see the header) and on every platform tested before #1108
            # that held. Observed on Ubuntu 26.04 (#1108): mutual exclusion
            # lost outright, at rates up to 10/10 overlapping rounds. The
            # mechanism -- `mkdir` reporting success for a directory that
            # already existed -- is a hypothesis this fix does not depend
            # on confirming, not an independently measured fact. `noclobber`
            # is the same O_EXCL-style guard `_lock_try_adopt` already uses
            # below, for exactly this reason: it is bash's own open(2) flag,
            # done by the shell itself, not an external binary, so it does
            # not depend on `mkdir` -- or any other coreutils command --
            # being exclusive on the host.
            if ( set -o noclobber; echo "$_LOCK_SELF" > "${_dir}/pid" ) 2>/dev/null; then
                return 0
            fi
            if [ ! -e "${_dir}/pid" ]; then
                # The write failed for a reason that is NOT another
                # process winning the noclobber race -- permission denied,
                # a disk quota, a fork limit -- so nobody holds this
                # directory. Remove what we just created ourselves rather
                # than leaving a pid-less orphan that blocks every later
                # acquirer until the adoption threshold (#1108 review).
                rmdir "$_dir" 2>/dev/null || true
            fi
            # Either someone else's write won the race genuinely (pid now
            # present) or we just cleared our own failed attempt above --
            # either way, fall through to the same steal/adopt/wait path
            # any other contender takes instead of returning 0 for a
            # directory we do not actually hold alone.
        fi

        if { [ -e "$_dir" ] || [ -L "$_dir" ]; } && [ ! -d "$_dir" ]; then
            # Something at the path that is not a lock directory — a dangling
            # symlink, a FIFO, debris, or a pre-#182 lock FILE (v0.8.8; its PID
            # is no longer honoured since #898, no holder that old still runs).
            # `mkdir` can never succeed against it, so clear it rather than
            # spin here until the timeout, forever.
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

        # Computed lazily, on first actual need (#1056 self-review finding):
        # the common case is an uncontended lock that succeeds on the very
        # first `mkdir` above, where `_deadline` is never read at all --
        # paying a `date` fork for a value nothing uses is the identical
        # "fork whether or not it's needed" pattern #1056 removes elsewhere
        # on this same hot path. Deferring it shifts the effective deadline
        # later by the time one steal/adopt attempt takes, never earlier --
        # the direction a lock timeout can safely drift.
        [ -n "$_deadline" ] || _deadline=$(( $(date +%s) + _timeout ))
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
    _lock_timing_slot "$1"
    _t0=""
    _wait=""
    if _lock_timing_find; then
        _t0="${_LOCK_TIMING_T0S[_LOCK_TIMING_IDX]}"
        _wait="${_LOCK_TIMING_WAITS[_LOCK_TIMING_IDX]}"
    fi
    if [ -z "$_t0" ]; then
        # Released by a process that never acquired here. Structurally this
        # should not happen, and the honest answer is to say the duration is
        # unknown rather than to compute one from a start that does not exist.
        _lock_timing_record "$1" release unpaired "-" "-"
        return 0
    fi
    _LOCK_TIMING_T0S[_LOCK_TIMING_IDX]=""
    _LOCK_TIMING_WAITS[_LOCK_TIMING_IDX]=""
    _lock_timing_record "$1" release ok "$_wait" "$(( _LOCK_TIMING_NOW - _t0 ))"
    return 0
}

_lock_release_impl() {
    local _dir="$1" _pid _claim _seen
    if _lock_read_pid_timeout "${_dir}/pid"; then
        _pid="$_LOCK_PID_READ_RESULT"
    else
        # Same dataless-placeholder hazard as the read in _lock_try_steal
        # (#1103) -- bounded rather than left to block the release path too.
        # UNLIKE _lock_try_steal, this function's caller believes IT holds
        # the lock: an inconclusive read must never fall through to removing
        # a directory that might belong to someone else. A first cut here
        # treated a timeout the same as a genuinely empty pid (which skips
        # the ownership check below and removes unconditionally) -- found in
        # self-review to let a process whose own read was merely slow once
        # delete a live, unrelated holder's lock. Refuse rather than guess.
        return 1
    fi

    _lock_self_set
    if [ -n "$_pid" ] && [ "$_pid" != "$_LOCK_SELF" ]; then
        return 1
    fi

    # The read above and the `rm -rf` below are two separate operations, and
    # a steal can land in between: a holder judged stale (> _LOCK_MAX_HOLD_
    # SECONDS, #1101) while it is ALIVE and about to release legitimately
    # races a stealer that renames the pid file, re-reads it, and writes its
    # own pid -- all while this release is still only holding the pid VALUE
    # it read earlier, not the file itself. Nothing stopped the `rm -rf`
    # below from firing after that takeover completed, deleting the new
    # owner's lock out from under it; a third process then recreates the
    # directory and two holders exist. Closed the same way `_lock_try_steal`
    # already closes its own single-winner race: rename the pid file OUT
    # from under the directory first. Whichever of this release's rename or
    # a concurrent steal's rename happens first wins; the other gets ENOENT
    # (or a content mismatch on re-read) and backs off instead of
    # proceeding on stale information.
    # A known, accepted gap (review finding, left undone like the PID-
    # recycling limitation documented at the top of this file rather than
    # chased further): if THIS process is killed between the mv below and
    # this function's own cleanup, the resulting pid.releasing.$_LOCK_SELF
    # is stranded with a dead owner. _lock_try_steal reclaims an abandoned
    # pid.stealing.* claim exactly this way; nothing here does the same for
    # pid.releasing.* yet, so a crashed release can leave one stray file
    # per occurrence. It does not wedge the lock -- _lock_try_adopt still
    # reclaims the lock itself once the pid file is absent and the
    # directory is old enough -- it only litters, narrowly and rarely.
    _claim="${_dir}/pid.releasing.${_LOCK_SELF}"
    if ! mv "${_dir}/pid" "$_claim" 2>/dev/null; then
        return 1
    fi
    # Two different takeovers can land in this window, and each needs a
    # different check -- a second-review finding on the first cut of this
    # fix, which only had the first of the two below and broke the very
    # steal-vs-release test it was meant to pin, because the two checks
    # catch opposite timings, not the same one twice.
    #
    # A STEALER rewrites "${_dir}/pid" BEFORE this mv has a chance to run:
    # whichever of this release's mv or the stealer's own mv of
    # "${_dir}/pid" is issued first still wins the PATH, but if the
    # stealer's rename-then-rewrite completed first, the content this mv
    # just carried into $_claim is the STEALER's pid, not the one this
    # function read at the top -- compare against $_pid to catch that.
    { _seen=$(<"$_claim"); } 2>/dev/null || true
    if [ "$_seen" != "$_pid" ]; then
        # $_claim now holds whatever this mv actually captured -- the
        # stealer's own pid, since the stealer's write landed in
        # "${_dir}/pid" before this mv ran. "${_dir}/pid" is absent right
        # now (this mv just took the only thing that was there), so
        # restoring $_claim's real content is what puts the new owner's
        # pid back where it belongs -- never our own stale belief, which
        # is why this restores $_claim unmodified rather than writing
        # $_pid.
        mv "$_claim" "${_dir}/pid" 2>/dev/null || true
        return 1
    fi
    # An ADOPTER creates a BRAND NEW "${_dir}/pid" AFTER this mv, from
    # nothing: _lock_try_adopt fires whenever "${_dir}/pid" is ABSENT and
    # the directory is old enough, both true for the whole window this mv
    # just opened (a stale-but-alive holder's age already exceeds the
    # adopt threshold in any sane configuration). A content compare can
    # never see this -- nothing we hold changes when someone else creates
    # a different file -- so check existence instead: if it exists again,
    # someone else's pid is there now, and it is theirs to keep.
    if [ -e "${_dir}/pid" ]; then
        rm -f "$_claim" 2>/dev/null || true
        return 1
    fi
    rm -f "$_claim" 2>/dev/null || true
    rm -rf "$_dir" 2>/dev/null || true
    return 0
}
