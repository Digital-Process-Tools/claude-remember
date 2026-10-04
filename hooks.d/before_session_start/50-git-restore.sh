#!/bin/bash

set -u  # not -e -- we never want to fail loudly here

[ -n "${REMEMBER_DIR:-}" ] && [ -n "${PROJECT_DIR:-}" ] && [ -n "${PIPELINE_DIR:-}" ] || exit 0

case "${OSTYPE:-}" in
    msys|cygwin) _gr_normalized_dir="${REMEMBER_DIR//\\//}"
                 _gr_normalized_project="${PROJECT_DIR//\\//}" ;;
    *)           _gr_normalized_dir="$REMEMBER_DIR"
                 _gr_normalized_project="$PROJECT_DIR" ;;
esac
REPO_ROOT="${_gr_normalized_dir%/*}"
SLUG="${_gr_normalized_dir##*/}"
unset _gr_normalized_dir

[ "$REPO_ROOT" = "$_gr_normalized_project" ] && exit 0
unset _gr_normalized_project

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

_gr_realpath() {
    ( cd "$1" 2>/dev/null && pwd -P ) || printf '%s' "$1"
}
TOPLEVEL=$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null) || exit 0
if [ "$(_gr_realpath "$TOPLEVEL")" != "$(_gr_realpath "$REPO_ROOT")" ]; then
    source "$PIPELINE_DIR/scripts/log.sh" 2>/dev/null && \
        log "git-restore" "declined: $REPO_ROOT is not the toplevel of its git repository (that is $TOPLEVEL) -- a memory store inside a larger repo is never fast-forwarded by this hook, deliberately. Nothing was restored."
    exit 0
fi

_gr_cheap_restore_enabled() {
    [ -n "${REMEMBER_CONFIG:-}" ] && [ -f "$REMEMBER_CONFIG" ] || return 1
    if command -v jq >/dev/null 2>&1; then
        jq -r '.git_restore.enabled // false' "$REMEMBER_CONFIG" 2>/dev/null
    elif command -v "${PYTHON:-python3}" >/dev/null 2>&1; then
        "${PYTHON:-python3}" -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    sys.exit(1)
v = d.get("git_restore", {}).get("enabled", False) if isinstance(d, dict) else False
print("true" if v is True or v == "true" else "false")
' "$REMEMBER_CONFIG" 2>/dev/null
    else
        return 1
    fi
}
_GR_CHEAP_RC=0
_GR_CHEAP_ENABLED=$(_gr_cheap_restore_enabled) || _GR_CHEAP_RC=$?
unset -f _gr_cheap_restore_enabled
if [ "$_GR_CHEAP_RC" -eq 0 ] && [ "$_GR_CHEAP_ENABLED" != "true" ]; then
    exit 0
fi

source "$PIPELINE_DIR/scripts/log.sh"

[ "$(config '.git_restore.enabled' 'false')" = "true" ] || exit 0

_gr_common_dir() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local _d="$1" _out
    [ -d "$_d" ] || return 1
    _out=$(git -C "$_d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || _out=""
    if [ -z "$_out" ]; then
        _out=$(git -C "$_d" rev-parse --git-common-dir 2>/dev/null) || return 1
        [ -n "$_out" ] || return 1
        case "$_out" in
            /*|[A-Za-z]:[/\\]*) ;;
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

GIT_RESTORE_REMOTE=$(config '.git_restore.remote' '')
[ -n "$GIT_RESTORE_REMOTE" ] || GIT_RESTORE_REMOTE=$(config '.git_backup.remote' '')
REMOTE_NAME="${GIT_RESTORE_REMOTE:-origin}"

GIT_RESTORE_BRANCH=$(config '.git_restore.branch' '')
[ -n "$GIT_RESTORE_BRANCH" ] || GIT_RESTORE_BRANCH=$(config '.git_backup.branch' '')

printf -v _dq '\042'
if [ "${REMOTE_NAME#-}" != "$REMOTE_NAME" ] \
    || [ "${REMOTE_NAME#*:}" != "$REMOTE_NAME" ] \
    || [ "${REMOTE_NAME#*/}" != "$REMOTE_NAME" ]; then
        report_error "git-restore" "WARNING: configured remote '$REMOTE_NAME' is not a plain remote name (leading '-', or contains ':' or '/') -- refusing to use it, falling back to 'origin'. A config.json restored from the backup remote can carry an attacker-controlled value here; treat this as untrusted."
        REMOTE_NAME="origin"
fi
case "$GIT_RESTORE_BRANCH" in
    -*|*:*)
        report_error "git-restore" "WARNING: configured branch '$GIT_RESTORE_BRANCH' starts with '-' or contains ':' -- refusing to use it as a git fetch operand (a colon makes it a src:dst refspec, not a branch name)."
        GIT_RESTORE_BRANCH=""
        ;;
esac

FETCH_TIMEOUT=$(config '.git_restore.fetch_timeout_seconds' '20')
case "$FETCH_TIMEOUT" in ''|*[!0-9]*|0) FETCH_TIMEOUT=20 ;; esac

DIVERGED_NOTICE_AFTER=$(config '.git_restore.diverged_notice_after' '3')
if [ -z "$DIVERGED_NOTICE_AFTER" ] || [ "${DIVERGED_NOTICE_AFTER#*[!0-9]}" != "$DIVERGED_NOTICE_AFTER" ]; then DIVERGED_NOTICE_AFTER=3; fi

if [ -n "$BACKUP_COMMON_DIR" ] && mkdir -p "$BACKUP_COMMON_DIR/remember" 2>/dev/null; then
    GR_STATE_DIR="$BACKUP_COMMON_DIR/remember"
else
    GR_STATE_DIR="$REPO_ROOT"
fi

if [ "$GR_STATE_DIR" != "$REPO_ROOT" ]; then
    for _gr_old in .git-restore-fetch .git-restore-diverged; do
        [ -f "$REPO_ROOT/$_gr_old" ] || continue
        [ -e "$GR_STATE_DIR/${_gr_old#.}" ] && continue
        cp "$REPO_ROOT/$_gr_old" "$GR_STATE_DIR/${_gr_old#.}" 2>/dev/null || true
    done
fi

FETCH_STATE_FILE="$GR_STATE_DIR/git-restore-fetch"
DIVERGED_STATE_FILE="$GR_STATE_DIR/git-restore-diverged"
LOCK_FILE="$GR_STATE_DIR/git-backup.lock"

LOCAL_HEAD=$(git -C "$REPO_ROOT" rev-parse --verify --quiet HEAD 2>/dev/null) || LOCAL_HEAD=""

if [ -z "$GIT_RESTORE_BRANCH" ]; then
    GIT_RESTORE_BRANCH=$(git -C "$REPO_ROOT" symbolic-ref --short --quiet HEAD 2>/dev/null) || GIT_RESTORE_BRANCH=""
fi

REMOTE_REF="refs/remotes/$REMOTE_NAME/$GIT_RESTORE_BRANCH"

_spawn_fetch() {
    if [ -f "$FETCH_STATE_FILE" ]; then
        local _s='' _f='' _now _age
        while IFS='=' read -r _k _v; do
            case "$_k" in started) _s="$_v" ;; finished) _f="$_v" ;; esac
        done < "$FETCH_STATE_FILE"
        if [ -z "$_s" ] || [ "${_s#*[!0-9]}" != "$_s" ]; then _s=0; fi
        if [ -z "$_f" ] && [ "$_s" -gt 0 ]; then
            _now=$(date +%s)
            _age=$(( _now - 10#$_s ))
            if [ "$_age" -lt 0 ]; then
                report_error "git-restore" "WARNING: $FETCH_STATE_FILE says a fetch started $(( 0 - _age ))s in the FUTURE -- the clock moved back, or the record is corrupt in a way a digits-only check cannot see. No fetch is running; starting a real one and rewriting the record."
            elif [ "$_age" -lt "$FETCH_TIMEOUT" ]; then
                return 0
            fi
        fi
    fi

    (
        exec 9>&- 2>/dev/null || true

        _started=$(date +%s)
        printf 'started=%s\n' "$_started" > "$FETCH_STATE_FILE" 2>/dev/null || exit 0

        export GIT_TERMINAL_PROMPT=0
        export GIT_ASKPASS=
        export SSH_ASKPASS=
        if [ -z "${GIT_SSH_COMMAND:-}" ]; then
            printf -v GIT_SSH_COMMAND 'ssh -oBatchMode=yes -oConnectTimeout=%s' "$FETCH_TIMEOUT"
        fi
        export GIT_SSH_COMMAND

        if [ -n "$GIT_RESTORE_BRANCH" ]; then
            git -C "$REPO_ROOT" -c core.askPass= fetch --quiet --no-tags \
                -- "$REMOTE_NAME" "$GIT_RESTORE_BRANCH" >/dev/null 2>&1 &
        else
            git -C "$REPO_ROOT" -c core.askPass= fetch --quiet --no-tags \
                -- "$REMOTE_NAME" >/dev/null 2>&1 &
        fi
        _fetch_pid=$!

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

        printf 'started=%s\nfinished=%s\nrc=%s\n' "$_started" "$(date +%s)" "$_rc" \
            > "$FETCH_STATE_FILE" 2>/dev/null || true
    ) </dev/null >/dev/null 2>&1 &
    disown $! 2>/dev/null || true
}

_fetch_health() {
    [ -f "$FETCH_STATE_FILE" ] || { echo "never-run"; return; }
    local _s='' _f='' _rc='' _now _age
    while IFS='=' read -r _k _v; do
        case "$_k" in started) _s="$_v" ;; finished) _f="$_v" ;; rc) _rc="$_v" ;; esac
    done < "$FETCH_STATE_FILE"
    if [ -z "$_s" ] || [ "${_s#*[!0-9]}" != "$_s" ]; then _s=0; fi
    if [ -z "$_f" ]; then
        _now=$(date +%s)
        _age=$(( _now - 10#$_s ))
        if [ "$_age" -ge 0 ] && [ "$_age" -lt "$FETCH_TIMEOUT" ]; then echo "in-flight"; else echo "abandoned"; fi
        return
    fi
    if [ "$_rc" = "0" ]; then echo "ok"; else echo "failed:${_rc:-unknown}"; fi
}

FETCH_HEALTH=$(_fetch_health)

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
    log "git-restore" "store busy (backup in progress), skip -- the fast-forward will be retried next session"
    exit 0
fi

case "$FETCH_HEALTH" in
    ok)        : ;;
    never-run) log "git-restore" "no fetch has completed yet for $REPO_ROOT -- the comparison below is against whatever refs are already on disk, which may be stale. A fetch starts in the background now and its result lands next session." ;;
    in-flight) log "git-restore" "a background fetch is still running -- the comparison below is against the previous fetch's refs" ;;
    abandoned) log "git-restore" "WARNING: the last background fetch never completed (started $(cat "$FETCH_STATE_FILE" 2>/dev/null | head -1)) -- could NOT check the remote. This is not 'up to date': the refs below are as old as the last fetch that did finish." ;;
    failed:*)  log "git-restore" "WARNING: the last background fetch FAILED (rc=${FETCH_HEALTH#failed:}) -- could NOT check the remote. This is not 'up to date': the refs below are as old as the last fetch that did finish. Run 'git -C ${_dq}$REPO_ROOT${_dq} fetch $REMOTE_NAME' to see git's own error." ;;
esac

if [ -z "$LOCAL_HEAD" ]; then
    log "git-restore" "local branch is unborn (no commits in $REPO_ROOT) -- nothing to fast-forward onto, refusing. Clone or check out the backup branch by hand."
    _spawn_fetch
    exit 0
fi

if [ -z "$GIT_RESTORE_BRANCH" ]; then
    log "git-restore" "HEAD is detached and git_restore.branch is unset -- refusing to guess which branch to restore"
    exit 0
fi

REMOTE_HEAD=$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$REMOTE_REF" 2>/dev/null) || REMOTE_HEAD=""
if [ -z "$REMOTE_HEAD" ]; then
    log "git-restore" "no fetched ref $REMOTE_REF -- nothing to restore FROM (this is not 'up to date'). Check git_restore.remote / git_restore.branch."
    _spawn_fetch
    exit 0
fi

COUNTS=$(git -C "$REPO_ROOT" rev-list --left-right --count "HEAD...$REMOTE_REF" 2>/dev/null) || COUNTS=""
AHEAD="${COUNTS%%	*}"
BEHIND="${COUNTS##*	}"
if [ -z "$AHEAD" ] || [ "${AHEAD#*[!0-9]}" != "$AHEAD" ]; then AHEAD=""; fi
if [ -z "$BEHIND" ] || [ "${BEHIND#*[!0-9]}" != "$BEHIND" ]; then BEHIND=""; fi

if [ -z "$AHEAD" ] || [ -z "$BEHIND" ]; then
    log "git-restore" "WARNING: could not compare HEAD with $REMOTE_REF -- could NOT check, no restore attempted"
    _spawn_fetch
    exit 0
fi

if [ "$AHEAD" -gt 0 ] && [ "$BEHIND" -gt 0 ]; then
    _count=$(cat "$DIVERGED_STATE_FILE" 2>/dev/null || echo 0)
    if [ -z "$_count" ] || [ "${_count#*[!0-9]}" != "$_count" ]; then _count=0; fi
    _count=$((10#$_count + 1))
    echo "$_count" > "$DIVERGED_STATE_FILE" 2>/dev/null || true

    log "git-restore" "ERROR: the memory store has DIVERGED -- $AHEAD local commit(s) the remote does not have, $BEHIND remote commit(s) this machine does not have (consecutive session starts in this state: $_count). NOT restored, and nothing here will merge or rebase for you: recent.md and archive.md are rewritten wholesale by consolidation, so a wrong automatic resolution would corrupt memory silently. Resolve it by hand: git -C ${_dq}$REPO_ROOT${_dq} log --oneline --left-right ${_dq}HEAD...$REMOTE_REF${_dq}"

    if [ "$DIVERGED_NOTICE_AFTER" -gt 0 ] && [ "$_count" -eq "$DIVERGED_NOTICE_AFTER" ]; then
        mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
        printf '%s\n' "remember: your memory store has DIVERGED from its backup remote. $AHEAD commit(s) here are not on the remote and $BEHIND commit(s) there are not here, so the memory loaded this session is missing them -- and the backup cannot push either. Nothing will be merged or rebased for you. Resolve it by hand: git -C ${_dq}$REPO_ROOT${_dq} log --oneline --left-right HEAD...$REMOTE_NAME/$GIT_RESTORE_BRANCH" \
            > "$REMEMBER_DIR/tmp/git-restore-notice" 2>/dev/null || true
    fi
    _spawn_fetch
    exit 0
fi

rm -f "$DIVERGED_STATE_FILE" 2>/dev/null || true

if [ "$BEHIND" -eq 0 ]; then
    if [ "$AHEAD" -gt 0 ]; then
        log "git-restore" "nothing to restore -- $AHEAD local commit(s) ahead of $REMOTE_NAME/$GIT_RESTORE_BRANCH, none behind (the backup half pushes those)"
    else
        log "git-restore" "already up to date with $REMOTE_NAME/$GIT_RESTORE_BRANCH"
    fi
    _spawn_fetch
    exit 0
fi

if git -C "$REPO_ROOT" merge --ff-only "$REMOTE_REF" >/dev/null 2>&1; then
    log "git-restore" "restored $BEHIND commit(s) from $REMOTE_NAME/$GIT_RESTORE_BRANCH (${LOCAL_HEAD:0:7}..${REMOTE_HEAD:0:7}) into $REPO_ROOT -- memory below reflects them"

    _gr_config_rel="$SLUG/config.json"
    if [ -n "$LOCAL_HEAD" ] \
        && git -C "$REPO_ROOT" cat-file -e "$LOCAL_HEAD:$_gr_config_rel" 2>/dev/null \
        && ! git -C "$REPO_ROOT" cat-file -e "HEAD:$_gr_config_rel" 2>/dev/null \
        && [ ! -e "$REPO_ROOT/$_gr_config_rel" ]; then
        mkdir -p "$(dirname "$REPO_ROOT/$_gr_config_rel")" 2>/dev/null || true
        if git -C "$REPO_ROOT" show "$LOCAL_HEAD:$_gr_config_rel" > "$REPO_ROOT/$_gr_config_rel.restore-tmp" 2>/dev/null \
            && mv "$REPO_ROOT/$_gr_config_rel.restore-tmp" "$REPO_ROOT/$_gr_config_rel"; then
            log "git-restore" "restored $_gr_config_rel after this fast-forward adopted a commit that stopped tracking it (#741) -- another machine ran 'git rm --cached' on it during the #719 upgrade; the file is preserved on disk exactly as it was, but stays OUT of the index, matching the new HEAD"
        else
            rm -f "$REPO_ROOT/$_gr_config_rel.restore-tmp" 2>/dev/null || true
            log "git-restore" "ERROR: this fast-forward adopted a commit that stopped tracking $_gr_config_rel (#741) and restoring its pre-merge content to $REPO_ROOT/$_gr_config_rel FAILED -- check it by hand; if it carried a live haiku.oauth_token, it may now be gone"
        fi
    fi
else
    log "git-restore" "ERROR: fast-forward of $BEHIND commit(s) from $REMOTE_NAME/$GIT_RESTORE_BRANCH was REFUSED by git -- most likely uncommitted local changes in $REPO_ROOT that it would overwrite. Nothing was restored and nothing was forced. Run 'git -C ${_dq}$REPO_ROOT${_dq} merge --ff-only $REMOTE_REF' to see git's own reason."
fi

_spawn_fetch
exit 0
