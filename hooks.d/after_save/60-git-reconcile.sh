#!/bin/bash

set -u  # not -e -- we never want to fail loudly here

source "$PIPELINE_DIR/scripts/log.sh"
source "$PIPELINE_DIR/scripts/lib-lock.sh"

RECONCILE_ENABLED=$(config ".git_reconcile.enabled" "false")
[ "$RECONCILE_ENABLED" = "true" ] || exit 0

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

if [ -n "$BACKUP_COMMON_DIR" ] && mkdir -p "$BACKUP_COMMON_DIR/remember" 2>/dev/null; then
    RC_STATE_DIR="$BACKUP_COMMON_DIR/remember"
else
    RC_STATE_DIR="$REPO_ROOT"
fi

GIT_RECONCILE_REMOTE=$(config ".git_reconcile.remote" "")
GIT_RECONCILE_BRANCH=$(config ".git_reconcile.branch" "")
[ -n "$GIT_RECONCILE_REMOTE" ] || GIT_RECONCILE_REMOTE=$(config ".git_backup.remote" "")
[ -n "$GIT_RECONCILE_BRANCH" ] || GIT_RECONCILE_BRANCH=$(config ".git_backup.branch" "")

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

RC_LOCK_DIR="$RC_STATE_DIR/git-reconcile.lock"
if ! lock_acquire "$RC_LOCK_DIR" 0; then
    debug_enabled 0 && log "git-reconcile" "another reconcile instance holds the lock for $REPO_ROOT, skip"
    exit 0
fi

CONSOLIDATION_LOCK_DIR="$REMEMBER_DIR/tmp/consolidation.lock"

NDC_GEN_FILE="$REMEMBER_DIR/tmp/ndc-generation"
_grc_bump_ndc_gen() {
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

(
    _grc_cleanup() {
        lock_release "$CONSOLIDATION_LOCK_DIR" >/dev/null 2>&1 || true
        lock_release "$RC_LOCK_DIR" >/dev/null 2>&1 || true
    }
    trap _grc_cleanup EXIT

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
        if git -C "$REPO_ROOT" merge --ff-only "$REMOTE_REF" >/dev/null 2>&1; then
            log "git-reconcile" "fast-forwarded $BEHIND commit(s) from $REMOTE_NAME/$BRANCH_NAME"
            rm -f "$CONFLICT_STATE_FILE" 2>/dev/null || true
            _grc_bump_ndc_gen
        else
            log "git-reconcile" "WARNING: fast-forward of $BEHIND commit(s) from $REMOTE_NAME/$BRANCH_NAME was refused by git -- uncommitted local changes most likely. No-op."
        fi
        exit 0
    fi

    _grc_conflict_files() {
        git -C "$REPO_ROOT" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ';'
    }

    _grc_rebase_in_progress() {
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

    _grc_foreign_changes() {
        git -C "$REPO_ROOT" status --porcelain 2>/dev/null | while IFS= read -r _grc_line; do
            _grc_code="${_grc_line:0:2}"
            _grc_path="${_grc_line:3}"
            if [[ "$_grc_code" == *R* ]] && [[ "$_grc_path" == *" -> "* ]]; then
                _grc_path="${_grc_path##* -> }"
            fi
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

    if _grc_rebase_in_progress; then
        log "git-reconcile" "declined: a rebase (or merge/cherry-pick) is already in progress in $REPO_ROOT and it is NOT this run's own -- .git/rebase-merge or rebase-apply already existed before this reconcile attempt started. Most likely someone is resolving a real conflict by hand right now. Nothing was touched -- finish or abort it yourself: git -C ${_dq}$REPO_ROOT${_dq} rebase --continue (or --abort)."
        exit 0
    fi

    git -C "$REPO_ROOT" rebase "$REMOTE_REF" >/dev/null 2>/dev/null
    REBASE_RC=$?
    if [ "$REBASE_RC" -eq 0 ]; then
        _grc_bump_ndc_gen
        if git -C "$REPO_ROOT" push --porcelain -- "$REMOTE_NAME" "$BRANCH_NAME" >/dev/null 2>/dev/null; then
            log "git-reconcile" "rebased $AHEAD local commit(s) onto $REMOTE_NAME/$BRANCH_NAME ($BEHIND commit(s)) and pushed"
            rm -f "$CONFLICT_STATE_FILE" 2>/dev/null || true
            exit 0
        fi
        git -C "$REPO_ROOT" -c core.askPass= fetch --quiet --no-tags -- "$REMOTE_NAME" "$BRANCH_NAME" >/dev/null 2>&1
        REMOTE_HEAD2=$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$REMOTE_REF" 2>/dev/null) || REMOTE_HEAD2=""
        if [ -n "$REMOTE_HEAD2" ]; then
            if _grc_rebase_in_progress; then
                log "git-reconcile" "declined: a rebase (or merge/cherry-pick) is already in progress in $REPO_ROOT and it is NOT this run's own -- .git/rebase-merge or rebase-apply already existed before this retry could start. Nothing was touched -- finish or abort it yourself: git -C ${_dq}$REPO_ROOT${_dq} rebase --continue (or --abort)."
                exit 0
            fi
            git -C "$REPO_ROOT" rebase "$REMOTE_REF" >/dev/null 2>/dev/null
            REBASE_RC2=$?
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

    if _grc_rebase_in_progress; then
        _grc_report_conflict "$(_grc_conflict_files)" first-attempt
    else
        log "git-reconcile" "WARNING: git rebase on $REMOTE_REF failed to start in $REPO_ROOT -- no-op, nothing was changed. Run it by hand to see git's own reason."
    fi
) </dev/null >/dev/null 2>&1 &
disown $! 2>/dev/null || true

exit 0
