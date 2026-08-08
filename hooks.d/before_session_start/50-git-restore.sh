#!/bin/bash
# ============================================================================
# 50-git-restore.sh — Fast-forward the memory store from its backup remote
# ============================================================================
#
# DESCRIPTION
#   The mirror of after_save/50-git-backup.sh. That hook pushes; nothing ever
#   read back, so a store used from two machines drifts and the second machine
#   reads stale memory, commits on top of it, and from then on can never push
#   at all (#253).
#
#   Runs on the before_session_start dispatch — before session-start-hook.sh
#   injects recent.md / archive.md into context, which is the only ordering
#   where a restore can affect the session it runs in.
#
#   FAST-FORWARD ONLY. Never merges, never rebases, never resets, never
#   checks out, never stashes. A diverged store is REFUSED and reported,
#   because recent.md and archive.md are rewritten wholesale by consolidation
#   rather than appended, so a conflict there is real and a wrong automatic
#   resolution corrupts memory silently.
#
#   OFF BY DEFAULT (git_restore.enabled). Nothing changes for existing
#   installs until someone asks for it.
#
# NO NETWORK ON THE CRITICAL PATH
#   This hook runs in front of the user's first prompt, and #227 was a plugin
#   that put ~8.7s there. Measured on a warm connection with nothing to
#   download, `git fetch` against GitHub costs ~1.7s; against an unreachable
#   remote it costs whatever the timeout is. That is not a price to pay before
#   every prompt, so the two halves are split by whether they touch the wire:
#
#     * the FAST-FORWARD is synchronous and purely local — it reads the
#       remote-tracking refs an EARLIER session already fetched. Measured
#       ~30ms per git invocation and ~40ms for the merge itself.
#     * the FETCH is detached and its result lands NEXT session. A restore
#       that arrives one session late is still a restore; a session start
#       that hangs on a credential prompt is a user leaving.
#
#   The detach costs one fork (measured 0.24ms here). It is not folded into
#   the backup hook's existing background subshell, which would cost zero
#   forks, for two reasons: #253's reporter was promised in writing that
#   `fetch`, `pull`, `merge` and `rebase` will never appear in that file, and
#   there is a test enforcing it; and a restore that only runs after a save
#   never runs at all in a read-only session, which is exactly the session
#   that most needs fresh memory.
#
# RUNTIME ENV (provided by session-start-hook.sh via dispatch)
#   PROJECT_DIR, PIPELINE_DIR, REMEMBER_DIR, REMEMBER_PROJECT
#
# ============================================================================

set -u  # not -e — we never want to fail loudly here

# ── Cheap guards FIRST, before sourcing anything ─────────────────────────────
# Sourcing log.sh loads and parses the merged config. Most installs are legacy
# mode (memory inside the project) and can never activate this hook, so they
# must not pay for it: `${VAR%/*}` is a shell expansion, not a `dirname` fork,
# and the two tests below cost zero subprocesses.
[ -n "${REMEMBER_DIR:-}" ] && [ -n "${PROJECT_DIR:-}" ] && [ -n "${PIPELINE_DIR:-}" ] || exit 0

REPO_ROOT="${REMEMBER_DIR%/*}"
SLUG="${REMEMBER_DIR##*/}"

# Legacy mode (REMEMBER_DIR is inside PROJECT_DIR) → never run.
[ "$REPO_ROOT" = "$PROJECT_DIR" ] && exit 0

# Prevent outer git env vars from overriding git -C behaviour. Same reasoning
# as the backup hook: a leaked GIT_DIR makes every `git -C … rev-parse`
# resolve against the leaked repo instead of the directory we asked about.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

# REPO_ROOT must be the toplevel of a git repo, not just inside one.
#
# Compared RESOLVED rather than textually (#260) — the backup half documents the
# same change at length. `--show-toplevel` answers with a resolved path while
# REPO_ROOT is spelled however REMEMBER_DIR was, so a single symlink anywhere in
# the path made both halves of this feature exit 0 above every log call: nothing
# pushed, nothing fetched, nothing restored, and hook-errors.log empty
# throughout.
_gr_realpath() {
    ( cd "$1" 2>/dev/null && pwd -P ) || printf '%s' "$1"
}
TOPLEVEL=$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null) || exit 0
if [ "$(_gr_realpath "$TOPLEVEL")" != "$(_gr_realpath "$REPO_ROOT")" ]; then
    # log.sh is sourced HERE and not above, on purpose. The cheap-guards-first
    # ordering exists so that a legacy install — the majority, which can never
    # activate this hook — does not pay to parse the config on every session
    # start. This is the rare branch and it is a REFUSAL, which is worth the
    # cost; the ordinary "not a git repo at all" exit above still says nothing,
    # because that one really is "nothing to do".
    source "$PIPELINE_DIR/scripts/log.sh" 2>/dev/null && \
        log "git-restore" "declined: $REPO_ROOT is not the toplevel of its git repository (that is $TOPLEVEL) — a memory store inside a larger repo is never fast-forwarded by this hook, deliberately. Nothing was restored."
    exit 0
fi

# ── Now we can afford logging + config ───────────────────────────────────────
source "$PIPELINE_DIR/scripts/log.sh"

# ── The gate. Default off (#253). ────────────────────────────────────────────
[ "$(config '.git_restore.enabled' 'false')" = "true" ] || exit 0

# ── Never restore into the project's own repository (#138) ───────────────────
# Same trap the backup hook documents at length: since #127 a session run from
# a linked git worktree keeps PROJECT_DIR on the worktree while REMEMBER_DIR is
# redirected into the MAIN checkout, so REPO_ROOT is the project repo and both
# guards above pass. On the write side that meant committing memory onto the
# project's branch; here it would mean fast-forwarding the user's SOURCE TREE
# from the project's origin at session start. Compare git common dirs, which
# every worktree of a repo shares.
_gr_common_dir() {
    local _d="$1" _out
    [ -d "$_d" ] || return 1
    _out=$(git -C "$_d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || _out=""
    if [ -z "$_out" ]; then
        _out=$(git -C "$_d" rev-parse --git-common-dir 2>/dev/null) || return 1
        [ -n "$_out" ] || return 1
        case "$_out" in
            /*|[A-Za-z]:/*|[A-Za-z]:\\*) ;;
            *) _out="$_d/$_out" ;;
        esac
    fi
    _gr_realpath "$_out"
}
PROJECT_COMMON_DIR=$(_gr_common_dir "$PROJECT_DIR") || PROJECT_COMMON_DIR=""
BACKUP_COMMON_DIR=$(_gr_common_dir "$REPO_ROOT") || BACKUP_COMMON_DIR=""
if [ -n "$PROJECT_COMMON_DIR" ] && [ "$PROJECT_COMMON_DIR" = "$BACKUP_COMMON_DIR" ]; then
    log "git-restore" "REPO_ROOT is the project repo (worktree/legacy), skip"
    exit 0
fi

# ── Config ───────────────────────────────────────────────────────────────────
# Remote/branch default to the backup half's, because a store that pushes to
# one place and reads from another is a misconfiguration nobody asked for.
GIT_RESTORE_REMOTE=$(config '.git_restore.remote' '')
[ -n "$GIT_RESTORE_REMOTE" ] || GIT_RESTORE_REMOTE=$(config '.git_backup.remote' '')
REMOTE_NAME="${GIT_RESTORE_REMOTE:-origin}"

GIT_RESTORE_BRANCH=$(config '.git_restore.branch' '')
[ -n "$GIT_RESTORE_BRANCH" ] || GIT_RESTORE_BRANCH=$(config '.git_backup.branch' '')

FETCH_TIMEOUT=$(config '.git_restore.fetch_timeout_seconds' '20')
case "$FETCH_TIMEOUT" in ''|*[!0-9]*|0) FETCH_TIMEOUT=20 ;; esac

# How many CONSECUTIVE session starts finding a diverged store before the human
# is interrupted rather than merely logged. Same escalation argument part 1
# settled for the push side: a divergence never clears itself, so a threshold
# can only postpone a true report, never swallow one — and systemMessage is the
# most intrusive surface in this codebase, so one that fires on every hiccup is
# one nobody reads. 0 disables the interruption; the log line stays either way.
DIVERGED_NOTICE_AFTER=$(config '.git_restore.diverged_notice_after' '3')
case "$DIVERGED_NOTICE_AFTER" in ''|*[!0-9]*) DIVERGED_NOTICE_AFTER=3 ;; esac

# ── State ────────────────────────────────────────────────────────────────────
# Beside the backup half's own state files — which are no longer at the store
# root (#261). `git merge --ff-only` refuses when an untracked file would be
# overwritten, so these two names sitting in the worktree were a collision
# waiting for the day a remote carried one, and the thing that would break is
# the fast-forward below. The git common dir is untracked by construction,
# never merged, never reported by `git status`, shared by every worktree — and
# needs no .gitignore and no user action, which is what makes it work for the
# installs that already exist.
if [ -n "$BACKUP_COMMON_DIR" ] && mkdir -p "$BACKUP_COMMON_DIR/remember" 2>/dev/null; then
    GR_STATE_DIR="$BACKUP_COMMON_DIR/remember"
else
    GR_STATE_DIR="$REPO_ROOT"
fi

# Carry forward what an older version left at the root, rather than starting
# over and silently losing a fetch's recorded outcome.
#
# COPY only — this half never removes anything from the store, and it does not
# ask git which files are tracked. `TestItCannotDestroyLocalWork` allows this
# file exactly five git subcommands, and that allowlist is the reason a
# fast-forward here can be trusted not to destroy local work; widening it to
# add `ls-files` for a tidy-up would spend the file's whole safety argument on
# housekeeping. Removing the old root copies is the backup half's job, which
# already reasons about tracked files and is under no such constraint.
if [ "$GR_STATE_DIR" != "$REPO_ROOT" ]; then
    for _gr_old in .git-restore-fetch .git-restore-diverged; do
        [ -f "$REPO_ROOT/$_gr_old" ] || continue
        [ -e "$GR_STATE_DIR/${_gr_old#.}" ] && continue
        cp "$REPO_ROOT/$_gr_old" "$GR_STATE_DIR/${_gr_old#.}" 2>/dev/null || true
    done
fi

FETCH_STATE_FILE="$GR_STATE_DIR/git-restore-fetch"
DIVERGED_STATE_FILE="$GR_STATE_DIR/git-restore-diverged"
# Deliberately the SAME lock the backup hook takes. The two halves now touch
# one repo from two lifecycle events — a session start and a save — and the
# fast-forward moves HEAD and rewrites the working tree while a backup may be
# part-way through `git add`/`git commit` on the same index. Sharing the lock
# is what makes that impossible; a second, private lock would have been no
# lock at all.
LOCK_FILE="$GR_STATE_DIR/git-backup.lock"

# ── Which branch, and does the fetched ref exist? ────────────────────────────
# `--abbrev-ref HEAD` on a repo with no commits prints "HEAD" and exits 0 in
# some git versions, so the branch name is not evidence of a born branch.
# Ask for the commit directly. This is the fixture hazard from part 1 in its
# production form: cloning a bare repo without `-b <branch>` lands on an unborn
# branch, and every check below would otherwise answer vacuously.
LOCAL_HEAD=$(git -C "$REPO_ROOT" rev-parse --verify --quiet HEAD 2>/dev/null) || LOCAL_HEAD=""

if [ -z "$GIT_RESTORE_BRANCH" ]; then
    GIT_RESTORE_BRANCH=$(git -C "$REPO_ROOT" symbolic-ref --short --quiet HEAD 2>/dev/null) || GIT_RESTORE_BRANCH=""
fi

REMOTE_REF="refs/remotes/$REMOTE_NAME/$GIT_RESTORE_BRANCH"

# ── The detached fetch, for NEXT session ─────────────────────────────────────
# Started at the END of this file, after the local decision has been made and
# reported off the refs a previous session left behind.
_spawn_fetch() {
    # Do not stack fetches. A previous one still inside its timeout window owns
    # the job; a previous one PAST it is reported as never-completed below and
    # is replaced.
    if [ -f "$FETCH_STATE_FILE" ]; then
        local _s='' _f='' _now _age
        while IFS='=' read -r _k _v; do
            case "$_k" in started) _s="$_v" ;; finished) _f="$_v" ;; esac
        done < "$FETCH_STATE_FILE"
        # 10# after the case, never instead of it (#327): "08" is all digits,
        # clears the guard, and is then read as octal -- so the age comparison
        # is abandoned and a fetch still inside its window gets a second one
        # stacked on top of it.
        case "$_s" in ''|*[!0-9]*) _s=0 ;; esac
        if [ -z "$_f" ] && [ "$_s" -gt 0 ]; then
            _now=$(date +%s)
            _age=$(( _now - 10#$_s ))
            if [ "$_age" -lt 0 ]; then
                # Range, not syntax -- #326's FIFTH site, which that issue's
                # four-site inventory does not name. A `started` AHEAD of now is
                # all digits, so it clears the guard above, and it makes _age
                # negative -- which reads as "well inside the window" and takes
                # the `return 0` below. The only writers of FETCH_STATE_FILE are
                # inside the subshell that return skips, so nothing ever
                # rewrites the record: no fetch is spawned again, ever, and
                # _fetch_health goes on answering "in-flight" about a process
                # that does not exist. Same geometry as the other four -- the
                # self-heal sits below the early exit.
                #
                # Fall through and spawn. The spawn rewrites the record, which
                # is the whole repair, and one line says why.
                report_error "git-restore" "WARNING: $FETCH_STATE_FILE says a fetch started $(( 0 - _age ))s in the FUTURE — the clock moved back, or the record is corrupt in a way a digits-only check cannot see. No fetch is running; starting a real one and rewriting the record."
            elif [ "$_age" -lt "$FETCH_TIMEOUT" ]; then
                return 0
            fi
        fi
    fi

    (
        # Drop the backup lock BEFORE doing anything else. fd 9 is where
        # _take_lock holds the flock, a forked child inherits every open
        # descriptor, and an inherited fd holds the SAME lock — so without this
        # the detached fetch keeps the whole store locked for its entire life.
        #
        # macOS ships no flock(1), so the fallback path runs there and nothing
        # is inherited; on Linux this was silent and total. Every session start
        # for the next `fetch_timeout_seconds` logged "store busy" and skipped,
        # which meant the divergence counter could never pass 1, the escalation
        # could never fire, and the repaired-store cleanup could never run —
        # and any after_save backup landing in that window was blocked too.
        # That is exactly the failure this file argues the fetch must not
        # cause: holding a repo-wide lock across network I/O.
        exec 9>&- 2>/dev/null || true

        _started=$(date +%s)
        printf 'started=%s\n' "$_started" > "$FETCH_STATE_FILE" 2>/dev/null || exit 0

        # Every way a fetch can ask a human a question, closed. GIT_TERMINAL_PROMPT
        # is what part 1 already uses on the push side; the askpass pair covers the
        # GUI helpers, which ignore it. A background process that pops a credential
        # dialog nobody is looking at is a fetch that never returns.
        export GIT_TERMINAL_PROMPT=0
        export GIT_ASKPASS=
        export SSH_ASKPASS=
        export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -oBatchMode=yes -oConnectTimeout=$FETCH_TIMEOUT}"

        # --no-tags --prune-tags: this is a memory store, not a release repo, and
        # the fetch should move exactly one remote-tracking ref.
        git -C "$REPO_ROOT" -c core.askPass= fetch --quiet --no-tags \
            "$REMOTE_NAME" ${GIT_RESTORE_BRANCH:+"$GIT_RESTORE_BRANCH"} >/dev/null 2>&1 &
        _fetch_pid=$!

        # A portable watchdog rather than timeout(1), which macOS does not ship.
        # Off the critical path, so the sleep-per-second spawn cost is free —
        # and one code path is one code path that gets tested.
        _waited=0
        while [ "$_waited" -lt "$FETCH_TIMEOUT" ]; do
            kill -0 "$_fetch_pid" 2>/dev/null || break
            sleep 1
            _waited=$((_waited + 1))
        done
        if kill -0 "$_fetch_pid" 2>/dev/null; then
            kill -9 "$_fetch_pid" 2>/dev/null || true
            _rc=124
        else
            wait "$_fetch_pid"
            _rc=$?
        fi

        # Written whether it worked or not. A fetch nobody is watching is the
        # house defect in a new costume: without this the next session cannot
        # tell "fetched, nothing new" from "the fetch never came back", and
        # "up to date" would be a lie told on no evidence.
        printf 'started=%s\nfinished=%s\nrc=%s\n' "$_started" "$(date +%s)" "$_rc" \
            > "$FETCH_STATE_FILE" 2>/dev/null || true
    ) </dev/null >/dev/null 2>&1 &
    disown $! 2>/dev/null || true
}

# ── What the last fetch has to say ───────────────────────────────────────────
# Three answers, never two (docs/validators.md, "Declining instead of
# guessing"): it worked / it failed / it never came back. Silence is not a
# fourth way of saying "up to date".
_fetch_health() {
    [ -f "$FETCH_STATE_FILE" ] || { echo "never-run"; return; }
    local _s='' _f='' _rc='' _now _age
    while IFS='=' read -r _k _v; do
        case "$_k" in started) _s="$_v" ;; finished) _f="$_v" ;; rc) _rc="$_v" ;; esac
    done < "$FETCH_STATE_FILE"
    # 10# after the case, never instead of it (#327). Without it "08" is octal,
    # the arithmetic fails, bash abandons this whole `if` body, and control
    # falls through to the `rc` test below with `_rc` empty -- so a fetch that
    # never came back is reported as one that FAILED with an unknown status.
    # Wrong state out of the three, and the remedy offered is for a failure
    # that did not happen.
    case "$_s" in ''|*[!0-9]*) _s=0 ;; esac
    if [ -z "$_f" ]; then
        _now=$(date +%s)
        _age=$(( _now - 10#$_s ))
        # `-ge 0` is the range half (#326, fifth site). A `started` ahead of now
        # makes _age negative, and negative is `-lt` any timeout -- so the one
        # answer that means "wait, something is already running" was being given
        # about nothing at all, forever. Downstream that is not merely a wrong
        # label: the caller then prints "already up to date" off refs no fetch
        # ever refreshed, which is precisely the "silence is not a fourth way of
        # saying up to date" this function's own header forbids.
        #
        # ABANDONED, not in-flight. We cannot substantiate a running fetch, and
        # this function's contract is three honest states rather than a guess.
        # Left pure -- the diagnostic belongs in _spawn_fetch, because this
        # function's stdout IS the verdict and is read through $( ).
        if [ "$_age" -ge 0 ] && [ "$_age" -lt "$FETCH_TIMEOUT" ]; then echo "in-flight"; else echo "abandoned"; fi
        return
    fi
    if [ "$_rc" = "0" ]; then echo "ok"; else echo "failed:${_rc:-unknown}"; fi
}

FETCH_HEALTH=$(_fetch_health)

# ── Lock — shared with the backup half ───────────────────────────────────────
# Taken around the LOCAL decision and the fast-forward only. The fetch above is
# deliberately outside it: it writes remote-tracking refs and objects and never
# the index or the working tree, git does its own ref locking, and holding a
# repo-wide lock across network I/O would let a slow remote block every backup
# for the length of the timeout.
_take_lock() {
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE" || return 1
        flock -n 9 || return 1
        return 0
    fi
    if ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null; then
        trap 'rm -f "$LOCK_FILE"' EXIT
        return 0
    fi
    local _pid
    _pid=$(cat "$LOCK_FILE" 2>/dev/null)
    kill -0 "$_pid" 2>/dev/null && return 1
    rm -f "$LOCK_FILE"
    ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null || return 1
    trap 'rm -f "$LOCK_FILE"' EXIT
    return 0
}

if ! _take_lock; then
    log "git-restore" "store busy (backup in progress), skip — the fast-forward will be retried next session"
    exit 0
fi

# ── Report, then decide ──────────────────────────────────────────────────────
# The fetch's health is reported BEFORE any conclusion is drawn from the refs,
# because that is the difference #253 part 1 exists to protect on the write
# side and it is exactly as easy to lose here: a store whose fetch has been
# failing for a week looks identical to a store that is genuinely current.
case "$FETCH_HEALTH" in
    ok)        : ;;
    never-run) log "git-restore" "no fetch has completed yet for $REPO_ROOT — the comparison below is against whatever refs are already on disk, which may be stale. A fetch starts in the background now and its result lands next session." ;;
    in-flight) log "git-restore" "a background fetch is still running — the comparison below is against the previous fetch's refs" ;;
    abandoned) log "git-restore" "WARNING: the last background fetch never completed (started $(cat "$FETCH_STATE_FILE" 2>/dev/null | head -1)) — could NOT check the remote. This is not 'up to date': the refs below are as old as the last fetch that did finish." ;;
    failed:*)  log "git-restore" "WARNING: the last background fetch FAILED (rc=${FETCH_HEALTH#failed:}) — could NOT check the remote. This is not 'up to date': the refs below are as old as the last fetch that did finish. Run 'git -C \"$REPO_ROOT\" fetch $REMOTE_NAME' to see git's own error." ;;
esac

if [ -z "$LOCAL_HEAD" ]; then
    log "git-restore" "local branch is unborn (no commits in $REPO_ROOT) — nothing to fast-forward onto, refusing. Clone or check out the backup branch by hand."
    _spawn_fetch
    exit 0
fi

if [ -z "$GIT_RESTORE_BRANCH" ]; then
    log "git-restore" "HEAD is detached and git_restore.branch is unset — refusing to guess which branch to restore"
    exit 0
fi

REMOTE_HEAD=$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$REMOTE_REF" 2>/dev/null) || REMOTE_HEAD=""
if [ -z "$REMOTE_HEAD" ]; then
    log "git-restore" "no fetched ref $REMOTE_REF — nothing to restore FROM (this is not 'up to date'). Check git_restore.remote / git_restore.branch."
    _spawn_fetch
    exit 0
fi

# ── Cleanly behind? ──────────────────────────────────────────────────────────
# `--left-right --count A...B` is the honest test and gives both numbers at
# once: <commits only on HEAD>TAB<commits only on the remote>. Comparing tips
# for equality, or asking only `merge-base --is-ancestor`, cannot tell "behind"
# from "diverged" — and those are the two cases that must never be confused,
# because one is a fast-forward and the other is a refusal.
COUNTS=$(git -C "$REPO_ROOT" rev-list --left-right --count "HEAD...$REMOTE_REF" 2>/dev/null) || COUNTS=""
AHEAD="${COUNTS%%	*}"
BEHIND="${COUNTS##*	}"
case "$AHEAD" in ''|*[!0-9]*) AHEAD="" ;; esac
case "$BEHIND" in ''|*[!0-9]*) BEHIND="" ;; esac

if [ -z "$AHEAD" ] || [ -z "$BEHIND" ]; then
    log "git-restore" "WARNING: could not compare HEAD with $REMOTE_REF — could NOT check, no restore attempted"
    _spawn_fetch
    exit 0
fi

if [ "$AHEAD" -gt 0 ] && [ "$BEHIND" -gt 0 ]; then
    # ── DIVERGED: refuse, and say so ─────────────────────────────────────────
    # 10# after the case (#327). Without it a corrupt counter makes the
    # increment an octal error, this branch is abandoned, and the DIVERGED
    # report below never runs -- a store that refused to restore, saying so
    # nowhere.
    _count=$(cat "$DIVERGED_STATE_FILE" 2>/dev/null || echo 0)
    case "$_count" in ''|*[!0-9]*) _count=0 ;; esac
    _count=$((10#$_count + 1))
    echo "$_count" > "$DIVERGED_STATE_FILE" 2>/dev/null || true

    log "git-restore" "ERROR: the memory store has DIVERGED — $AHEAD local commit(s) the remote does not have, $BEHIND remote commit(s) this machine does not have (consecutive session starts in this state: $_count). NOT restored, and nothing here will merge or rebase for you: recent.md and archive.md are rewritten wholesale by consolidation, so a wrong automatic resolution would corrupt memory silently. Resolve it by hand: git -C \"$REPO_ROOT\" log --oneline --left-right \"HEAD...$REMOTE_REF\""

    if [ "$DIVERGED_NOTICE_AFTER" -gt 0 ] && [ "$_count" -eq "$DIVERGED_NOTICE_AFTER" ]; then
        mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
        printf '%s\n' "remember: your memory store has DIVERGED from its backup remote. $AHEAD commit(s) here are not on the remote and $BEHIND commit(s) there are not here, so the memory loaded this session is missing them — and the backup cannot push either. Nothing will be merged or rebased for you. Resolve it by hand: git -C \"$REPO_ROOT\" log --oneline --left-right HEAD...$REMOTE_NAME/$GIT_RESTORE_BRANCH" \
            > "$REMEMBER_DIR/tmp/git-restore-notice" 2>/dev/null || true
    fi
    _spawn_fetch
    exit 0
fi

rm -f "$DIVERGED_STATE_FILE" 2>/dev/null || true

if [ "$BEHIND" -eq 0 ]; then
    if [ "$AHEAD" -gt 0 ]; then
        log "git-restore" "nothing to restore — $AHEAD local commit(s) ahead of $REMOTE_NAME/$GIT_RESTORE_BRANCH, none behind (the backup half pushes those)"
    else
        log "git-restore" "already up to date with $REMOTE_NAME/$GIT_RESTORE_BRANCH"
    fi
    _spawn_fetch
    exit 0
fi

# ── Cleanly behind → fast-forward ────────────────────────────────────────────
# --ff-only is the whole safety property: git refuses rather than creating a
# merge commit, and refuses rather than overwriting a modified working tree.
# There is no reset, no checkout -f, no clean, no stash anywhere in this file,
# and a test asserts that stays true — a fast-forward cannot destroy local
# work, but only as long as nothing else in here can.
if git -C "$REPO_ROOT" merge --ff-only "$REMOTE_REF" >/dev/null 2>&1; then
    log "git-restore" "restored $BEHIND commit(s) from $REMOTE_NAME/$GIT_RESTORE_BRANCH (${LOCAL_HEAD:0:7}..${REMOTE_HEAD:0:7}) into $REPO_ROOT — memory below reflects them"
else
    log "git-restore" "ERROR: fast-forward of $BEHIND commit(s) from $REMOTE_NAME/$GIT_RESTORE_BRANCH was REFUSED by git — most likely uncommitted local changes in $REPO_ROOT that it would overwrite. Nothing was restored and nothing was forced. Run 'git -C \"$REPO_ROOT\" merge --ff-only $REMOTE_REF' to see git's own reason."
fi

_spawn_fetch
exit 0
