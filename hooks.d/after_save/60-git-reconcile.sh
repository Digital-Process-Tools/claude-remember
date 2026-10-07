#!/bin/bash
# ============================================================================
# 60-git-reconcile.sh — optional two-way reconcile for a memory store written
# from two machines (#903)
# ============================================================================
#
# DESCRIPTION
#   Off by default (config key: git_reconcile.enabled). 50-git-backup.sh keeps
#   its commit-and-push-only promise from #253, and 50-git-restore.sh stays
#   fast-forward only -- a diverged store is still refused and reported by
#   that hook. This file is the ONLY place a rebase happens in this plugin,
#   and only when a human has opted in to it.
#
#   Runs after 50-git-backup.sh on the after_save dispatch (50- then 60-, same
#   dispatch pass). When enabled:
#     - fetches the configured remote/branch
#     - behind only: fast-forwards (the backup half already pushes "ahead
#       only"; this covers the case a store with git_restore.enabled=false
#       would otherwise never catch up on until the next manual pull)
#     - ahead AND behind (diverged): rebases local commits onto the remote
#       tip and pushes, with one retry if the remote moved again between the
#       fetch and the push
#     - a conflict during the rebase: drops the rebase state and resets
#       ONLY this run's own slug subtree back to its pre-rebase content
#       (git rebase --quit + a scoped checkout, #943 -- NOT git rebase
#       --abort, which resets the whole tracked tree), then reports the
#       conflicting file names through a git-reconcile-notice the same way
#       50-git-restore.sh's diverged notice works. A different slug
#       sharing this REPO_ROOT that landed a tracked write of its own
#       while the rebase was in flight is never touched, at any point,
#       because nothing outside this run's own slug subtree is ever
#       named by the reset.
#
#   Never resets, never force-pushes, never creates a merge commit -- there is
#   no `reset --hard`, `push --force` or `merge` (non-ff-only) anywhere in
#   this file, and a test asserts that stays true.
#
#   No-op when:
#     - git_reconcile.enabled is not "true"
#     - REMEMBER_DIR is in legacy mode, REPO_ROOT is not the toplevel of its
#       own repo, or REPO_ROOT is the project's own repo (same guards as
#       50-git-backup.sh, #138/#260)
#     - HEAD is detached and no branch is configured
#     - another instance of this hook holds its own lock
#     - this slug's consolidation lock is held -- recent.md and archive.md are
#       being rewritten wholesale right now, and reconcile must never touch
#       the tree while that happens (shares consolidation's own lock dir,
#       scripts/run-consolidation.sh's LOCK_DIR)
#     - nothing is behind at all (50-git-backup.sh already pushes "ahead only")
#
#   NDC compression (save-session.sh's background now.md->today-*.md summarizer)
#   is the OTHER writer of now.md, and its own staleness guards (live size >=
#   snapshot, a generation marker) were blind to a rebase/merge landing here
#   while they were mid-check -- they only ever detected another NDC round
#   (#932). This file cannot share save-session.sh's own save.lock the way it
#   shares consolidation's: save-session.sh holds save.lock across its ENTIRE
#   run, spanning the exact window this hook's background subshell runs in on
#   every single save, so acquiring it here would decline almost every save
#   rather than only the rare NDC-mid-commit one. Instead, every successful
#   fast-forward/rebase below bumps NDC's own generation marker
#   ("$REMEMBER_DIR/tmp/ndc-generation") -- the same counter NDC already
#   compares before/after its own commit -- so NDC's EXISTING guard also
#   catches this hook as a writer, at no steady-state cost.
#
# RUNTIME ENV (provided by save-session.sh via dispatch)
#   PROJECT_DIR, PIPELINE_DIR, REMEMBER_DIR, REMEMBER_PROJECT
#
# ============================================================================

set -u  # not -e -- we never want to fail loudly here

# ── Source logging (log(), config(), report_error()) and the lock primitive
# consolidation itself uses, so this file can share it rather than invent a
# second one (scripts/lib-lock.sh, #182).
source "$PIPELINE_DIR/scripts/log.sh"
source "$PIPELINE_DIR/scripts/lib-lock.sh"

# ── Off by default ───────────────────────────────────────────────────────────
RECONCILE_ENABLED=$(config ".git_reconcile.enabled" "false")
[ "$RECONCILE_ENABLED" = "true" ] || exit 0

# ── Activation guard — identical shape to 50-git-backup.sh ──────────────────
REPO_ROOT=$(dirname "$REMEMBER_DIR")
SLUG=$(basename "$REMEMBER_DIR")

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

[ "$REPO_ROOT" = "$PROJECT_DIR" ] && exit 0

_grc_realpath() {
    ( cd "$1" 2>/dev/null && pwd -P ) || printf '%s' "$1"
}

TOPLEVEL=$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null) || exit 0
if [ "$(_grc_realpath "$TOPLEVEL")" != "$(_grc_realpath "$REPO_ROOT")" ]; then
    log "git-reconcile" "declined: $REPO_ROOT is not the toplevel of its git repository (that is $TOPLEVEL) -- nothing here is reconciled."
    exit 0
fi

_grc_common_dir() {
    local LC_ALL=C
    local _d="$1" _out
    [ -d "$_d" ] || return 1
    _out=$(git -C "$_d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || _out=""
    if [ -z "$_out" ]; then
        _out=$(git -C "$_d" rev-parse --git-common-dir 2>/dev/null) || return 1
        [ -n "$_out" ] || return 1
        if [ "${_out#/}" = "$_out" ] && [ "${_out#[A-Za-z]:[/\\]}" = "$_out" ]; then
            _out="$_d/$_out"
        fi
    fi
    _grc_realpath "$_out"
}

PROJECT_COMMON_DIR=$(_grc_common_dir "$PROJECT_DIR") || PROJECT_COMMON_DIR=""
BACKUP_COMMON_DIR=$(_grc_common_dir "$REPO_ROOT") || BACKUP_COMMON_DIR=""
if [ -n "$PROJECT_COMMON_DIR" ] && [ "$PROJECT_COMMON_DIR" = "$BACKUP_COMMON_DIR" ]; then
    debug_enabled 0 && log "git-reconcile" "REPO_ROOT is the project repo (worktree/legacy), skip"
    exit 0
fi

# ── Where this hook's own lock/state lives — shared location with backup/restore
if [ -n "$BACKUP_COMMON_DIR" ] && mkdir -p "$BACKUP_COMMON_DIR/remember" 2>/dev/null; then
    RC_STATE_DIR="$BACKUP_COMMON_DIR/remember"
else
    RC_STATE_DIR="$REPO_ROOT"
fi

# ── Config snapshot — read BEFORE backgrounding, same reasoning as #135 on
# the backup half: config() quietly returns a default once REMEMBER_CONFIG's
# EXIT trap has deleted it, which races a slow reconcile started in the parent.
GIT_RECONCILE_REMOTE=$(config ".git_reconcile.remote" "")
GIT_RECONCILE_BRANCH=$(config ".git_reconcile.branch" "")
[ -n "$GIT_RECONCILE_REMOTE" ] || GIT_RECONCILE_REMOTE=$(config ".git_backup.remote" "")
[ -n "$GIT_RECONCILE_BRANCH" ] || GIT_RECONCILE_BRANCH=$(config ".git_backup.branch" "")

# #723 (sibling site, same finding class as backup/restore): config.json is
# git-tracked and can be a poisoned copy on a shared store, so a
# git_reconcile.remote/.branch (or the git_backup.* fallback) read from it is
# untrusted for anything that reaches git's argv. Same two checks as the other
# two hooks: a leading '-' is parsed as an option, and ':' or '/' make the
# value a transport URL/refspec rather than a plain name this repo trusts.
printf -v _dq '\042'
if [ "${GIT_RECONCILE_REMOTE#-}" != "$GIT_RECONCILE_REMOTE" ] \
    || [ "${GIT_RECONCILE_REMOTE#*:}" != "$GIT_RECONCILE_REMOTE" ] \
    || [ "${GIT_RECONCILE_REMOTE#*/}" != "$GIT_RECONCILE_REMOTE" ]; then
    report_error "git-reconcile" "WARNING: configured git_reconcile.remote (or git_backup.remote) '$GIT_RECONCILE_REMOTE' is not a plain remote name (leading '-', or contains ':' or '/') -- refusing to use it, falling back to the branch's push target. Treat this as untrusted on a shared store."
    GIT_RECONCILE_REMOTE=""
fi
if [ "${GIT_RECONCILE_BRANCH#-}" != "$GIT_RECONCILE_BRANCH" ] || [[ "$GIT_RECONCILE_BRANCH" == *:* ]]; then
    report_error "git-reconcile" "WARNING: configured git_reconcile.branch (or git_backup.branch) '$GIT_RECONCILE_BRANCH' starts with '-' or contains ':' -- refusing to use it as a git operand (a colon makes it a src:dst refspec, not a branch name)."
    GIT_RECONCILE_BRANCH=""
fi

# Which remote a bare push/fetch would actually use -- same derivation as
# 50-git-backup.sh's REMOTE_NAME (#257): @{push}, then branch.<name>.remote,
# then origin as the last resort.
REMOTE_NAME="$GIT_RECONCILE_REMOTE"
if [ -z "$REMOTE_NAME" ]; then
    RC_CAND=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{push}' 2>/dev/null) || RC_CAND=""
    RC_CAND="${RC_CAND%%/*}"
    if [ -z "$RC_CAND" ]; then
        RC_HEAD_BRANCH=$(git -C "$REPO_ROOT" symbolic-ref --short --quiet HEAD 2>/dev/null) || RC_HEAD_BRANCH=""
        if [ -n "$RC_HEAD_BRANCH" ]; then
            RC_CAND=$(git -C "$REPO_ROOT" config --get "branch.$RC_HEAD_BRANCH.remote" 2>/dev/null) || RC_CAND=""
        fi
    fi
    if [ -n "$RC_CAND" ] && git -C "$REPO_ROOT" remote get-url "$RC_CAND" >/dev/null 2>&1; then
        REMOTE_NAME="$RC_CAND"
    fi
fi
[ -n "$REMOTE_NAME" ] || REMOTE_NAME=origin

BRANCH_NAME="$GIT_RECONCILE_BRANCH"
if [ -z "$BRANCH_NAME" ]; then
    BRANCH_NAME=$(git -C "$REPO_ROOT" symbolic-ref --short --quiet HEAD 2>/dev/null) || BRANCH_NAME=""
fi
if [ -z "$BRANCH_NAME" ]; then
    log "git-reconcile" "HEAD is detached and git_reconcile.branch/.git_backup.branch is unset -- refusing to guess which branch to reconcile"
    exit 0
fi

# ── This hook's own lock — prevents two reconcile instances (e.g. two slugs
# saving close together) from racing a fetch/rebase/push on the same
# REPO_ROOT. A rebase/push sequence is not the idempotent, disjoint-subtree
# case 50-git-backup.sh's own flock/noclobber comment relies on to call that
# race "benign" -- a second instance can abort/rewrite history the first is
# mid-operation on -- so this reuses lib-lock.sh's mkdir+stale-PID-takeover
# primitive (already sourced above for the consolidation lock) rather than
# repeat 50-git-backup.sh's flock/noclobber pattern.
#
# ACQUIRED HERE, in the foreground -- the directory must exist before this
# process exits so a concurrent instance (or a test) checking right away sees
# it immediately -- but the PID RECORDED inside it is corrected below, right
# after the real worker is forked. Recording the FOREGROUND's own pid here
# and leaving it would be wrong: this process forks the background subshell
# and exits within milliseconds, so a second invocation's staleness check
# would find that pid already dead and wrongly declare the lock stale while
# the real work is still running in the (differently-pid'd) subshell. The
# worker's pid is written into the SAME lock dir once `$!` is known, closing
# that window to the few shell instructions between the two -- the same
# microsecond-scale window lib-lock.sh's own `_lock_try_adopt` already
# accepts elsewhere for exactly this "holder writes its pid a moment after
# creating the directory" shape.
RC_LOCK_DIR="$RC_STATE_DIR/git-reconcile.lock"
if ! lock_acquire "$RC_LOCK_DIR" 0; then
    debug_enabled 0 && log "git-reconcile" "another reconcile instance holds the lock for $REPO_ROOT, skip"
    exit 0
fi

# Lock dir consolidation itself acquires -- scripts/run-consolidation.sh's own
# LOCK_DIR, "${REMEMBER_DIR}/tmp/consolidation.lock". Checked (and HELD for the
# duration below) rather than merely read, so consolidation cannot start
# mid-reconcile either: recent.md and archive.md are rewritten wholesale by
# consolidation, and a rebase touching the working tree while that is in
# flight is exactly the corruption the maintainer's review asked this file to
# rule out.
CONSOLIDATION_LOCK_DIR="$REMEMBER_DIR/tmp/consolidation.lock"

# NDC's own staleness marker -- save-session.sh's NDC_GEN_FILE,
# "$REMEMBER_DIR/tmp/ndc-generation" (#932). NOT a lock: save-session.sh
# holds its own save.lock across its ENTIRE run (foreground append through
# housekeeping, scripts/save-session.sh:83-1597), spanning the exact window
# this hook's background subshell is forked and runs in on every single
# save -- acquiring save.lock here the same way CONSOLIDATION_LOCK_DIR is
# acquired above would therefore decline on nearly every save, not just the
# rare moment NDC is actually mid-commit, and would defeat this hook's one
# job. NDC's commit step already KNOWS how to detect "something else
# rewrote now.md since my snapshot": it compares this same generation
# counter before/after re-acquiring save.lock around its own tail+mv
# (scripts/save-session.sh:1385-1406) -- it just never saw this hook as a
# possible writer, only another NDC round. Bumping the SAME counter after a
# successful fast-forward/rebase below makes NDC's EXISTING guard also catch
# this hook, at zero steady-state cost: if no NDC round is concurrently
# reading it, nobody notices the bump; if one is, it correctly skips its
# commit the same way it already does for an overlapping NDC round (a
# visible duplicate in today-*.md, never silent loss).
NDC_GEN_FILE="$REMEMBER_DIR/tmp/ndc-generation"
_grc_bump_ndc_gen() {
    # Best-effort, not locked: a failed read or write here just means a
    # concurrent NDC round that snapshotted the current value keeps
    # believing it, the exact pre-#932 state -- strictly no worse than
    # before this hook bumped anything at all. `10#` guards the same
    # leading-zero-as-octal trap save-session.sh's own bump already guards
    # against (see its line ~1512 comment).
    #
    # Same non-regular-file guard save-session.sh's own marker_write_ok()
    # applies before it ever opens NDC_GEN_FILE (scripts/save-session.sh's
    # marker_write_ok, ~line 211): a FIFO at this path has no reader here,
    # so a bare `>` or `cat` on it blocks in open(2) before any redirection
    # error exists, hanging this backgrounded subshell forever -- the exact
    # hang class #625/#634/#642/#653 fixed at every OTHER accessor of this
    # same file. This hook does not source save-session.sh (only log.sh and
    # lib-lock.sh), so the check is reimplemented locally rather than called.
    if [ -e "$NDC_GEN_FILE" ] && [ ! -f "$NDC_GEN_FILE" ]; then
        log "git-reconcile" "WARNING: could not bump $NDC_GEN_FILE -- it exists but is not a regular file, and opening it is refused (a FIFO here would block this process forever). Remove or replace it."
        return 1
    fi
    local _grc_gen
    _grc_gen=$(cat "$NDC_GEN_FILE" 2>/dev/null)
    if [ -z "$_grc_gen" ] || [[ "$_grc_gen" == *[!0-9]* ]]; then
        _grc_gen=0
    fi
    { echo $(( 10#$_grc_gen + 1 )) > "$NDC_GEN_FILE"; } 2>/dev/null || true
}

# ── Background subshell — never blocks save-session.sh ───────────────────────
(
    _grc_cleanup() {
        lock_release "$CONSOLIDATION_LOCK_DIR" >/dev/null 2>&1 || true
        lock_release "$RC_LOCK_DIR" >/dev/null 2>&1 || true
    }
    trap _grc_cleanup EXIT

    # Second-pass review (#903): the lock dir already exists (the foreground
    # created it via lock_acquire before forking this subshell, so a
    # concurrent checker sees it immediately), but its recorded holder is
    # still the FOREGROUND's own pid until corrected. Correcting it from OUT
    # THERE, after `$!` is known, raced this subshell's own cleanup trap: a
    # fast decline (e.g. the consolidation-lock check just below, a couple of
    # shell builtins away) could run `lock_release` before that outside write
    # landed, find the pid still the foreground's, fail the self-id match,
    # and leave the lock held by a dead pid until the next instance's stale-
    # takeover logic recovers it. Writing our OWN id, from IN HERE, as the
    # very first thing, needs no such ordering: by the time any exit path
    # (including this one) reaches the trap, the recorded holder is already
    # correct, unconditionally.
    _lock_self_set
    echo "$_LOCK_SELF" > "$RC_LOCK_DIR/pid" 2>/dev/null || true

    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

    if ! lock_acquire "$CONSOLIDATION_LOCK_DIR" 0; then
        log "git-reconcile" "declined: consolidation holds the lock for $SLUG -- recent.md/archive.md are being rewritten right now. Will try again next save."
        exit 0
    fi

    export GIT_TERMINAL_PROMPT=0
    export GIT_ASKPASS=
    export SSH_ASKPASS=

    REMOTE_REF="refs/remotes/$REMOTE_NAME/$BRANCH_NAME"
    CONFLICT_STATE_FILE="$RC_STATE_DIR/git-reconcile-conflict"

    git -C "$REPO_ROOT" -c core.askPass= fetch --quiet --no-tags -- "$REMOTE_NAME" "$BRANCH_NAME" >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        log "git-reconcile" "fetch from $REMOTE_NAME/$BRANCH_NAME failed or was unreachable -- no-op, will retry next save"
        exit 0
    fi

    REMOTE_HEAD=$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$REMOTE_REF" 2>/dev/null) || REMOTE_HEAD=""
    if [ -z "$REMOTE_HEAD" ]; then
        log "git-reconcile" "no fetched ref $REMOTE_REF -- nothing to reconcile against. Check git_reconcile.remote / git_reconcile.branch."
        exit 0
    fi

    COUNTS=$(git -C "$REPO_ROOT" rev-list --left-right --count "HEAD...$REMOTE_REF" 2>/dev/null) || COUNTS=""
    AHEAD="${COUNTS%%	*}"
    BEHIND="${COUNTS##*	}"
    if [ -z "$AHEAD" ] || [ "${AHEAD#*[!0-9]}" != "$AHEAD" ]; then AHEAD=""; fi
    if [ -z "$BEHIND" ] || [ "${BEHIND#*[!0-9]}" != "$BEHIND" ]; then BEHIND=""; fi
    if [ -z "$AHEAD" ] || [ -z "$BEHIND" ]; then
        log "git-reconcile" "WARNING: could not compare HEAD with $REMOTE_REF -- no-op"
        exit 0
    fi

    if [ "$BEHIND" -eq 0 ]; then
        log "git-reconcile" "nothing to reconcile -- $AHEAD commit(s) ahead, 0 behind (50-git-backup.sh already pushes those)"
        rm -f "$CONFLICT_STATE_FILE" 2>/dev/null || true
        exit 0
    fi

    if [ "$AHEAD" -eq 0 ]; then
        # Cleanly behind -- fast-forward. --ff-only refuses rather than
        # creating a merge commit or overwriting a modified working tree;
        # same safety property as 50-git-restore.sh's own fast-forward.
        if git -C "$REPO_ROOT" merge --ff-only "$REMOTE_REF" >/dev/null 2>&1; then
            log "git-reconcile" "fast-forwarded $BEHIND commit(s) from $REMOTE_NAME/$BRANCH_NAME"
            rm -f "$CONFLICT_STATE_FILE" 2>/dev/null || true
            _grc_bump_ndc_gen
        else
            log "git-reconcile" "WARNING: fast-forward of $BEHIND commit(s) from $REMOTE_NAME/$BRANCH_NAME was refused by git -- uncommitted local changes most likely. No-op."
        fi
        exit 0
    fi

    # ── Diverged: rebase onto the remote tip, then push. One retry if the
    # remote moves again between the fetch above and this push.
    _grc_conflict_files() {
        git -C "$REPO_ROOT" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ';'
    }

    _grc_rebase_in_progress() {
        # --path-format=absolute is required here, not optional (confirmed by
        # a real reproduction, not reasoned): --git-path alone returns a path
        # relative to the CALLING process's cwd, which -C does not change --
        # so from a different cwd the check silently looked in the wrong
        # place, reported "no conflict", and left a stray rebase-merge
        # directory behind on disk. Same mitigation _grc_common_dir already
        # applies to --git-common-dir, extended to this call site.
        local _rd1 _rd2
        _rd1=$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-path rebase-merge 2>/dev/null) || _rd1=""
        _rd2=$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-path rebase-apply 2>/dev/null) || _rd2=""
        if [ -n "$_rd1" ] && [ -d "$_rd1" ]; then
            return 0
        fi
        if [ -n "$_rd2" ] && [ -d "$_rd2" ]; then
            return 0
        fi
        return 1
    }

    # #939/#942/#943: `git rebase --abort` resets the WHOLE tracked tree
    # back to ORIG_HEAD, not just the files this rebase touched
    # (git-rebase(1)). RC_LOCK_DIR and CONSOLIDATION_LOCK_DIR above only
    # exclude another reconcile instance and this SAME slug's own
    # consolidation round -- nothing stops a DIFFERENT slug sharing this
    # REPO_ROOT (save-session.sh's own foreground append to its now.md,
    # 50-git-backup.sh's add/commit, that slug's own consolidation/NDC)
    # from landing a real, tracked write while this slug's rebase is in
    # flight. An earlier version of this fix (#939, then #942) snapshotted
    # that foreign state BEFORE calling --abort and copied it out of git's
    # reach -- but the snapshot and the abort are two separate steps, and a
    # write landing in the gap between them is not in the snapshot and is
    # still discarded (#943, the TOCTOU this file's own #939 commit message
    # already named and deferred). _grc_foreign_changes stays, but ONLY to
    # describe what survived for the log line below -- nothing here any
    # longer decides what to preserve based on it.
    _grc_foreign_changes() {
        # Every porcelain line outside "$SLUG/" is a write this run did not
        # make: this run's OWN churn (the conflict itself, any cleanly
        # applied commit before it) lives under "$SLUG/" only, since a save
        # commits to its own slug's subtree alone.
        #
        # Rename-splitting only applies when the status CODE itself says
        # "R" -- sniffing the path text for a literal " -> " substring
        # instead (an earlier version of this check) misreads a plain,
        # non-renamed path whose own name happens to contain that
        # substring as a rename, truncating it to whatever follows the
        # last " -> " and silently misclassifying it (review finding,
        # reproduced: an untracked "other-slug/weird -> test-slug" was
        # read as path "test-slug" -- an exact match for $SLUG, so a
        # foreign file was wrongly waved through as this run's own).
        git -C "$REPO_ROOT" status --porcelain 2>/dev/null | while IFS= read -r _grc_line; do
            _grc_code="${_grc_line:0:2}"
            _grc_path="${_grc_line:3}"
            if [[ "$_grc_code" == *R* ]] && [[ "$_grc_path" == *" -> "* ]]; then
                _grc_path="${_grc_path##* -> }"
            fi
            # A path with odd characters is double-quoted by porcelain.
            # Strip a leading/trailing double quote through a variable
            # holding the literal character, rather than an escaped quote
            # inside the pattern.
            _grc_dq='"'
            _grc_path="${_grc_path#"$_grc_dq"}"
            _grc_path="${_grc_path%"$_grc_dq"}"
            if [[ "$_grc_path" != "$SLUG"/* ]] && [[ "$_grc_path" != "$SLUG" ]]; then
                printf '%s\n' "$_grc_path"
            fi
        done
    }

    _grc_report_conflict() {
        local _files="$1" _attempt="$2" _msg_extra _foreign _quit_ok _checkout_ok

        # #954: by the time this function is entered, `git rebase` has
        # already failed with a real conflict -- which means it already
        # rewrote $SLUG/now.md to conflict-marker content (replaying commits
        # up to the conflicting one) before stopping. The tree is already
        # rewritten here, the exact same "bump as soon as the tree is
        # rewritten, regardless of downstream success" shape the
        # fast-forward and successful-rebase call sites above are bumped
        # for -- this path was the one now.md-rewriting path that never
        # called it, leaving an NDC round that snapshots now.md inside this
        # window unable to detect the overlap (#932 reasoned consequence).
        # Bumped unconditionally, before the quit/checkout outcome is even
        # known, so a FAILED quit or checkout (still handled below, still
        # reported honestly) does not suppress it either.
        _grc_bump_ndc_gen

        # #943: instead of snapshotting a foreign write and racing it
        # against `git rebase --abort` (the #939/#942 approach, and the
        # TOCTOU gap that fix could not close), never let the abort touch
        # anything outside $SLUG/ in the first place -- there is then
        # nothing to race, because nothing outside $SLUG/ is ever a
        # candidate for being discarded.
        #
        #   1. `git rebase --quit` drops the rebase state (.git/rebase-merge
        #      or rebase-apply) WITHOUT resetting HEAD, the index, or the
        #      working tree (git-rebase(1)) -- unlike --abort, which is
        #      effectively a `reset --hard` to ORIG_HEAD across the WHOLE
        #      tree.
        #   2. `git symbolic-ref HEAD refs/heads/$BRANCH_NAME` points HEAD
        #      back at the branch. This is a plain ref rewrite, not a
        #      checkout, so it touches nothing on disk -- safe here because
        #      a rebase (whether it eventually succeeds or not) never moves
        #      the branch ref itself until its very last step; a rebase
        #      that is still in progress (conflicted, or quit out of here)
        #      left that ref exactly where it was before this run started.
        #   3. `git checkout HEAD -- "$SLUG"` restores ONLY the $SLUG/
        #      subtree (index + working tree) to that pre-rebase content.
        #      Every commit this run could have replayed is confined to
        #      $SLUG/ (a save commits to its own slug's subtree alone, see
        #      _grc_foreign_changes above), so this fully undoes whatever
        #      the rebase did, scoped to exactly the part of the tree this
        #      run owns. No other path is ever named, read, or written by
        #      any of these three commands.
        #
        # Verified against a real repository, not merely reasoned: a write
        # injected into another slug's tracked file in the exact window
        # between the rebase failing and this sequence running survives
        # untouched, while $SLUG/'s own content and HEAD land exactly where
        # a successful `rebase --abort` would have put them.
        if git -C "$REPO_ROOT" rebase --quit >/dev/null 2>&1; then
            _quit_ok=1
        else
            _quit_ok=0
        fi

        _checkout_ok=0
        if [ "$_quit_ok" -eq 1 ] \
            && git -C "$REPO_ROOT" symbolic-ref HEAD "refs/heads/$BRANCH_NAME" >/dev/null 2>&1 \
            && git -C "$REPO_ROOT" checkout HEAD -- "$SLUG" >/dev/null 2>&1; then
            _checkout_ok=1
            # Review finding: `checkout <tree-ish> -- <pathspec>` only ever
            # restores a path that EXISTS in <tree-ish> -- it never removes
            # a path present in the index/working tree but absent from it.
            # Reproduced: a conflict where the upstream side being rebased
            # onto has its OWN earlier commit adding a new file under
            # $SLUG/ (no local commit involved at all) leaves that file
            # checked into the index as "A" (added) once the rebase sets
            # up that new base to replay onto -- `git rebase --quit` does
            # not touch it, and the checkout above does not remove it,
            # since HEAD's own pre-rebase tree never had it either. `git
            # rebase --abort` would have pruned it (it is a full `reset
            # --hard` across the whole tree); this scoped sequence must
            # prune it too, but ONLY within $SLUG/ -- never touching a
            # foreign path is still the entire point of #943.
            while IFS= read -r _grc_added; do
                [ -n "$_grc_added" ] || continue
                git -C "$REPO_ROOT" rm -f -q -- "$_grc_added" >/dev/null 2>&1 || _checkout_ok=0
            done < <(git -C "$REPO_ROOT" diff --name-only --diff-filter=A HEAD -- "$SLUG" 2>/dev/null)
        fi

        if [ "$_quit_ok" -eq 0 ] && _grc_rebase_in_progress; then
            _msg_extra="git rebase --quit itself FAILED -- the rebase is still in progress and conflict markers are still in the tree"
        elif [ "$_checkout_ok" -eq 0 ]; then
            _msg_extra="the rebase state was cleared, but restoring $SLUG/'s own pre-rebase content FAILED -- check $SLUG/ by hand, it may still carry conflict markers"
        else
            _foreign=$(_grc_foreign_changes)
            if [ -n "$_foreign" ]; then
                _msg_extra="aborted; a concurrent write that landed outside $SLUG/ while the rebase was in flight (${_foreign//$'\n'/; }) was never touched -- only $SLUG/ was reset"
            else
                _msg_extra="aborted, the tree is unchanged"
            fi
        fi

        log "git-reconcile" "ERROR: reconcile CONFLICT ($_attempt) rebasing onto $REMOTE_NAME/$BRANCH_NAME -- $_msg_extra. Conflicting file(s): ${_files%;}. Resolve by hand: git -C ${_dq}$REPO_ROOT${_dq} rebase ${_dq}$REMOTE_REF${_dq}"
        mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
        printf '%s\n' "remember: git reconcile hit a CONFLICT rebasing onto $REMOTE_NAME/$BRANCH_NAME -- $_msg_extra. Conflicting file(s): ${_files%;}. Resolve by hand: git -C ${_dq}$REPO_ROOT${_dq} rebase ${_dq}$REMOTE_REF${_dq}" \
            > "$REMEMBER_DIR/tmp/git-reconcile-notice" 2>/dev/null || true
        echo 1 > "$CONFLICT_STATE_FILE" 2>/dev/null || true
    }

    # #946: `_grc_rebase_in_progress` is a pure directory-existence check --
    # it cannot tell THIS run's own rebase (about to be started below) from
    # a rebase/merge/cherry-pick already sitting there for some OTHER
    # reason, most importantly a human mid-way through resolving a real
    # conflict by hand -- this hook's own notice above literally tells them
    # to do exactly that ("Resolve by hand: git -C ... rebase ..."). Without
    # this check, firing again while that resolution is in progress would
    # have this hook's OWN `git rebase` call below fail with "already in
    # progress", `_grc_rebase_in_progress` below (the existing post-check
    # right before the "Rebase itself failed" branch) would then read that
    # pre-existing state as THIS run's own freshly-conflicted rebase, and
    # `_grc_report_conflict` would run `rebase --quit` plus the
    # scoped checkout/rm OVER the human's in-progress, uncommitted work --
    # discarding it, with zero copies left anywhere (reproduced for real:
    # a half-typed, never-staged resolution destroyed, and a commit made
    # mid-resolution left reachable only via the reflog). So this check
    # MUST run before the rebase call, not after -- checking after can only
    # ever see "a rebase-merge/rebase-apply directory exists", never WHOSE.
    if _grc_rebase_in_progress; then
        log "git-reconcile" "declined: a rebase (or merge/cherry-pick) is already in progress in $REPO_ROOT and it is NOT this run's own -- .git/rebase-merge or rebase-apply already existed before this reconcile attempt started. Most likely someone is resolving a real conflict by hand right now. Nothing was touched -- finish or abort it yourself: git -C ${_dq}$REPO_ROOT${_dq} rebase --continue (or --abort)."
        exit 0
    fi

    git -C "$REPO_ROOT" rebase "$REMOTE_REF" >/dev/null 2>/dev/null
    REBASE_RC=$?
    if [ "$REBASE_RC" -eq 0 ]; then
        # The working tree (now.md included) is already rewritten the moment
        # the rebase itself lands -- regardless of whether the push below
        # succeeds, is rejected and retried, or ends up deferred. Bump here,
        # not after the push, so a deferred/retried push still counts.
        _grc_bump_ndc_gen
        if git -C "$REPO_ROOT" push --porcelain -- "$REMOTE_NAME" "$BRANCH_NAME" >/dev/null 2>/dev/null; then
            log "git-reconcile" "rebased $AHEAD local commit(s) onto $REMOTE_NAME/$BRANCH_NAME ($BEHIND commit(s)) and pushed"
            rm -f "$CONFLICT_STATE_FILE" 2>/dev/null || true
            exit 0
        fi
        # Rebase landed cleanly, but the push was rejected: the remote moved
        # again between the fetch above and this push. One retry.
        git -C "$REPO_ROOT" -c core.askPass= fetch --quiet --no-tags -- "$REMOTE_NAME" "$BRANCH_NAME" >/dev/null 2>&1
        REMOTE_HEAD2=$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$REMOTE_REF" 2>/dev/null) || REMOTE_HEAD2=""
        if [ -n "$REMOTE_HEAD2" ]; then
            # #946: same guard as the first attempt above, applied here too --
            # a foreign rebase/merge could just as easily appear in the window
            # between the first rebase landing/push being rejected and this
            # retry's own rebase call.
            if _grc_rebase_in_progress; then
                log "git-reconcile" "declined: a rebase (or merge/cherry-pick) is already in progress in $REPO_ROOT and it is NOT this run's own -- .git/rebase-merge or rebase-apply already existed before this retry could start. Nothing was touched -- finish or abort it yourself: git -C ${_dq}$REPO_ROOT${_dq} rebase --continue (or --abort)."
                exit 0
            fi
            git -C "$REPO_ROOT" rebase "$REMOTE_REF" >/dev/null 2>/dev/null
            REBASE_RC2=$?
            # Same reasoning as the first attempt's bump above: the tree is
            # rewritten as soon as this retry's rebase itself lands, whether
            # or not the push just below also succeeds.
            [ "$REBASE_RC2" -eq 0 ] && _grc_bump_ndc_gen
            if [ "$REBASE_RC2" -eq 0 ] && git -C "$REPO_ROOT" push --porcelain -- "$REMOTE_NAME" "$BRANCH_NAME" >/dev/null 2>/dev/null; then
                log "git-reconcile" "rebased and pushed onto $REMOTE_NAME/$BRANCH_NAME on retry (remote moved between fetch and push)"
                rm -f "$CONFLICT_STATE_FILE" 2>/dev/null || true
                exit 0
            fi
            if [ "$REBASE_RC2" -ne 0 ] && _grc_rebase_in_progress; then
                _grc_report_conflict "$(_grc_conflict_files)" retry
                exit 0
            fi
        fi
        log "git-reconcile" "push deferred after retry (will try again next save) -- $REMOTE_NAME/$BRANCH_NAME moved again or the transport was unreachable"
        exit 0
    fi

    # Rebase itself failed. A conflict leaves .git/rebase-merge or
    # rebase-apply in place; anything else (e.g. the rebase command failing to
    # start at all) is a no-op rather than a tree left half-rebased.
    if _grc_rebase_in_progress; then
        _grc_report_conflict "$(_grc_conflict_files)" first-attempt
    else
        log "git-reconcile" "WARNING: git rebase on $REMOTE_REF failed to start in $REPO_ROOT -- no-op, nothing was changed. Run it by hand to see git's own reason."
    fi
) </dev/null >/dev/null 2>&1 &
disown $! 2>/dev/null || true

exit 0
