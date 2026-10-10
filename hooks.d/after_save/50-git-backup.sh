#!/bin/bash

set -u  # not -e -- we never want to fail loudly here

source "$PIPELINE_DIR/scripts/log.sh"

REPO_ROOT=$(dirname "$REMEMBER_DIR")
SLUG=$(basename "$REMEMBER_DIR")

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

[ "$REPO_ROOT" = "$PROJECT_DIR" ] && exit 0

_gb_realpath() {
    ( cd "$1" 2>/dev/null && pwd -P ) || printf '%s' "$1"
}
TOPLEVEL=$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null) || exit 0
if [ "$(_gb_realpath "$TOPLEVEL")" != "$(_gb_realpath "$REPO_ROOT")" ]; then
    log "git-backup" "declined: $REPO_ROOT is not the toplevel of its git repository (that is $TOPLEVEL) -- a memory store inside a larger repo is never committed or pushed by this hook, deliberately. Nothing here is backed up. Give the store its own repository if you want it backed up."
    exit 0
fi

_gb_common_dir() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
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
    _gb_realpath "$_out"
}

PROJECT_COMMON_DIR=$(_gb_common_dir "$PROJECT_DIR") || PROJECT_COMMON_DIR=""
BACKUP_COMMON_DIR=$(_gb_common_dir "$REPO_ROOT") || BACKUP_COMMON_DIR=""
if [ -n "$PROJECT_COMMON_DIR" ] && [ "$PROJECT_COMMON_DIR" = "$BACKUP_COMMON_DIR" ]; then
    debug_enabled 0 && \
        log "git-backup" "REPO_ROOT is the project repo (worktree/legacy), skip"
    exit 0
fi

if [ -n "$BACKUP_COMMON_DIR" ] && mkdir -p "$BACKUP_COMMON_DIR/remember" 2>/dev/null; then
    GB_STATE_DIR="$BACKUP_COMMON_DIR/remember"
else
    GB_STATE_DIR="$REPO_ROOT"
fi

if [ "$GB_STATE_DIR" != "$REPO_ROOT" ]; then
    for _gb_old in .last-git-backup-ts .git-backup-remote .git-backup-rejected \
                   .git-restore-fetch .git-restore-diverged; do
        [ -f "$REPO_ROOT/$_gb_old" ] || continue
        [ -e "$GB_STATE_DIR/${_gb_old#.}" ] && continue
        cp "$REPO_ROOT/$_gb_old" "$GB_STATE_DIR/${_gb_old#.}" 2>/dev/null || continue
        if [ -z "$(git -C "$REPO_ROOT" ls-files -- "$_gb_old" 2>/dev/null)" ]; then
            rm -f "$REPO_ROOT/$_gb_old" 2>/dev/null || true
        fi
    done
    if [ -f "$REPO_ROOT/.git-backup.lock" ] && \
       [ -z "$(git -C "$REPO_ROOT" ls-files -- .git-backup.lock 2>/dev/null)" ]; then
        rm -f "$REPO_ROOT/.git-backup.lock" 2>/dev/null || true
    fi
fi

COOLDOWN_MARKER="$GB_STATE_DIR/last-git-backup-ts"
BACKUP_COOLDOWN=$(config ".cooldowns.git_backup_seconds" 900)
if [ -f "$COOLDOWN_MARKER" ]; then
    LAST_MOD=$(cat "$COOLDOWN_MARKER" 2>/dev/null || echo 0)
    if [ -z "$LAST_MOD" ] || [ "${LAST_MOD#*[!0-9]}" != "$LAST_MOD" ]; then
        LAST_MOD=0
    fi
    ELAPSED=$(( $(date +%s) - 10#$LAST_MOD ))
    if [ "$ELAPSED" -lt 0 ]; then
        report_error "git-backup" "WARNING: $COOLDOWN_MARKER is $(( 0 - ELAPSED ))s ahead of now -- the clock moved back, or the marker is corrupt in a way a digits-only check cannot see. Resetting it and backing up; the cooldown resumes from now."
        date +%s > "$COOLDOWN_MARKER" 2>/dev/null || true
    elif [ "$ELAPSED" -lt "$BACKUP_COOLDOWN" ]; then
        debug_enabled 0 && log "git-backup" "cooldown ${ELAPSED}s < ${BACKUP_COOLDOWN}s, skip"
        exit 0
    fi
fi

LOCK_FILE="$GB_STATE_DIR/git-backup.lock"
GB_LOCK_STYLE=noclobber
if command -v flock >/dev/null 2>&1; then
    GB_LOCK_STYLE=flock
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        debug_enabled 0 && log "git-backup" "flock held by another instance, skip"
        exit 0
    fi
else
    if ! ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null; then
        LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
        if kill -0 "$LOCK_PID" 2>/dev/null; then
            debug_enabled 0 && log "git-backup" "locked by PID $LOCK_PID, skip"
            exit 0
        fi
        log "git-backup" "stale lock (PID $LOCK_PID dead), taking over"
        rm -f "$LOCK_FILE"
        ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null || exit 0
    fi
fi

GIT_BACKUP_REMOTE=$(config ".git_backup.remote" "")
GIT_BACKUP_BRANCH=$(config ".git_backup.branch" "")

printf -v _dq '\042'
if [ "${GIT_BACKUP_REMOTE#-}" != "$GIT_BACKUP_REMOTE" ] \
    || [ "${GIT_BACKUP_REMOTE#*:}" != "$GIT_BACKUP_REMOTE" ] \
    || [ "${GIT_BACKUP_REMOTE#*/}" != "$GIT_BACKUP_REMOTE" ]; then
        report_error "git-backup" "WARNING: configured git_backup.remote '$GIT_BACKUP_REMOTE' is not a plain remote name (leading '-', or contains ':' or '/') -- refusing to use it, falling back to the branch's push target. A config.json restored from a shared store can carry an attacker-controlled value here; treat this as untrusted."
        GIT_BACKUP_REMOTE=""
fi
if [ "${GIT_BACKUP_BRANCH#-}" != "$GIT_BACKUP_BRANCH" ] || [[ "$GIT_BACKUP_BRANCH" == *:* ]]; then
    report_error "git-backup" "WARNING: configured git_backup.branch '$GIT_BACKUP_BRANCH' starts with '-' or contains ':' -- refusing to use it as a git push operand (a colon makes it a src:dst refspec, not a branch name)."
    GIT_BACKUP_BRANCH=""
fi
REMOTE_NAME="$GIT_BACKUP_REMOTE"
if [ -z "$REMOTE_NAME" ]; then
    GB_CAND=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{push}' 2>/dev/null) || GB_CAND=""
    GB_CAND="${GB_CAND%%/*}"
    if [ -z "$GB_CAND" ]; then
        GB_BRANCH=$(git -C "$REPO_ROOT" symbolic-ref --short --quiet HEAD 2>/dev/null) || GB_BRANCH=""
        if [ -n "$GB_BRANCH" ]; then
            GB_CAND=$(git -C "$REPO_ROOT" config --get "branch.$GB_BRANCH.remote" 2>/dev/null) || GB_CAND=""
        fi
    fi
    if [ -n "$GB_CAND" ] && git -C "$REPO_ROOT" remote get-url "$GB_CAND" >/dev/null 2>&1; then
        REMOTE_NAME="$GB_CAND"
    fi
fi
[ -n "$REMOTE_NAME" ] || REMOTE_NAME=origin

GIT_BACKUP_GPG_SIGN=$(config ".git_backup.gpg_sign" "false")
GPG_SIGN_FLAG="--no-gpg-sign"
if [ "$GIT_BACKUP_GPG_SIGN" = "true" ]; then
    GPG_SIGN_FLAG=""
fi

ALLOW_REMOTE_CHANGE=$(config ".git_backup.allow_remote_change" "false")

REJECT_NOTICE_AFTER=$(config ".git_backup.reject_notice_after" "3")
if [ -z "$REJECT_NOTICE_AFTER" ] || [ "${REJECT_NOTICE_AFTER#*[!0-9]}" != "$REJECT_NOTICE_AFTER" ]; then
    REJECT_NOTICE_AFTER=3
fi

COMMIT_NOTICE_AFTER=$(config ".git_backup.commit_notice_after" "3")
if [ -z "$COMMIT_NOTICE_AFTER" ] || [ "${COMMIT_NOTICE_AFTER#*[!0-9]}" != "$COMMIT_NOTICE_AFTER" ]; then
    COMMIT_NOTICE_AFTER=3
fi

NO_REMOTE_NOTICE_AFTER=$(config ".git_backup.no_remote_notice_after" "10")
if [ -z "$NO_REMOTE_NOTICE_AFTER" ] || [ "${NO_REMOTE_NOTICE_AFTER#*[!0-9]}" != "$NO_REMOTE_NOTICE_AFTER" ]; then
    NO_REMOTE_NOTICE_AFTER=10
fi

RECONCILE_ENABLED=$(config '.git_reconcile.enabled' 'false')

(
    [ "$GB_LOCK_STYLE" = "flock" ] || trap 'rm -f "$LOCK_FILE"' EXIT

    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

    _gb_commit_untrack() {
        if [ -n "$GPG_SIGN_FLAG" ]; then
            git -C "$REPO_ROOT" commit --no-gpg-sign \
                -m "auto: stop tracking $SLUG/logs, $SLUG/tmp and $SLUG/config.json"
        else
            git -C "$REPO_ROOT" commit \
                -m "auto: stop tracking $SLUG/logs, $SLUG/tmp and $SLUG/config.json"
        fi
    }
    _gb_commit_slug() {
        if [ -n "$GPG_SIGN_FLAG" ]; then
            git -C "$REPO_ROOT" commit --no-gpg-sign -m "auto: $SLUG $1" -- "$SLUG/"
        else
            git -C "$REPO_ROOT" commit -m "auto: $SLUG $1" -- "$SLUG/"
        fi
    }

    REJECT_STATE_FILE="$GB_STATE_DIR/git-backup-rejected"

    _push() {
        if [ -n "$GIT_BACKUP_REMOTE" ] && [ -n "$GIT_BACKUP_BRANCH" ]; then
            GIT_TERMINAL_PROMPT=0 git -C "$REPO_ROOT" push --porcelain -- "$GIT_BACKUP_REMOTE" "$GIT_BACKUP_BRANCH" 2>/dev/null
        elif [ -n "$GIT_BACKUP_REMOTE" ]; then
            GIT_TERMINAL_PROMPT=0 git -C "$REPO_ROOT" push --porcelain -- "$GIT_BACKUP_REMOTE" 2>/dev/null
        else
            GIT_TERMINAL_PROMPT=0 git -C "$REPO_ROOT" push --porcelain 2>/dev/null
        fi
    }

    _rejected_refs() {
        awk -F'\t' '
            !hdr { if ($0 ~ /^To /) hdr = 1; next }
            hdr && NF == 3 && $1 == "!" { print $2 " " $3 }
        '
    }

    _push_and_report() {
        local _out _rc _rejected _count
        _out=$(_push)
        _rc=$?

        if [ "$_rc" -eq 0 ]; then
            log "git-backup" "pushed $SLUG"
            rm -f "$REJECT_STATE_FILE" 2>/dev/null || true
            return 0
        fi

        _rejected=$(printf '%s\n' "$_out" | _rejected_refs | tr '\n' ';')
        if [ -z "$_rejected" ]; then
            log "git-backup" "push deferred (will retry next backup)"
            return 0
        fi

        _count=$(cat "$REJECT_STATE_FILE" 2>/dev/null || echo 0)
        if [ -z "$_count" ] || [ "${_count#*[!0-9]}" != "$_count" ]; then
            _count=0
        fi
        _count=$((10#$_count + 1))
        echo "$_count" > "$REJECT_STATE_FILE" 2>/dev/null || true

        if [ "$RECONCILE_ENABLED" = "true" ]; then
            log "git-backup" "ERROR: push REJECTED by the remote -- the backup has STOPPED for $SLUG (consecutive rejections: $_count). git_reconcile.enabled is on, but 60-git-reconcile.sh is hard-disabled pending #969, so nothing will fetch, rebase or push this automatically right now. git rejected: ${_rejected%;}. The commit exists on this machine only. Run 'git -C ${_dq}$REPO_ROOT${_dq} push' to see git's own advice -- recent.md and archive.md are rewritten wholesale by consolidation, so a wrong automatic resolution would corrupt memory silently."
        else
            log "git-backup" "ERROR: push REJECTED by the remote -- the backup has STOPPED for $SLUG and will not resume on its own (consecutive rejections: $_count). git rejected: ${_rejected%;}. The commit exists on this machine only. Nothing here will fetch, merge or rebase for you: run 'git -C ${_dq}$REPO_ROOT${_dq} push' to see git's own advice and resolve it by hand -- recent.md and archive.md are rewritten wholesale by consolidation, so a wrong automatic resolution would corrupt memory silently."
        fi

        if [ "$REJECT_NOTICE_AFTER" -gt 0 ] && [ "$_count" -eq "$REJECT_NOTICE_AFTER" ]; then
            mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            if [ "$RECONCILE_ENABLED" = "true" ]; then
                printf '%s\n' "remember: git backup has STOPPED for now. The remote rejected the last $_count pushes from $REPO_ROOT -- memory is still being committed locally, but it is not reaching your backup remote yet. git_reconcile.enabled is on, but 60-git-reconcile.sh is hard-disabled pending #969, so nothing will fetch, rebase or push this automatically right now. Run: git -C ${_dq}$REPO_ROOT${_dq} push -- to see git's own advice." \
                    > "$REMEMBER_DIR/tmp/git-backup-notice" 2>/dev/null || true
            else
                printf '%s\n' "remember: git backup has STOPPED. The remote rejected the last $_count pushes from $REPO_ROOT and will not accept them on a retry -- memory is still being committed locally, but it is not reaching your backup remote. Run: git -C ${_dq}$REPO_ROOT${_dq} push -- then resolve the divergence yourself. Nothing will be merged or rebased for you." \
                    > "$REMEMBER_DIR/tmp/git-backup-notice" 2>/dev/null || true
            fi
        fi
        return 0
    }

    SLUG_GITIGNORE="$REPO_ROOT/$SLUG/.gitignore"
    if [ -f "$SLUG_GITIGNORE" ] && [ "$(cat "$SLUG_GITIGNORE")" = "*" ]; then
        rm -f "$SLUG_GITIGNORE"
        log "git-backup" "removed per-slug .gitignore (legacy bootstrap artifact)"
    fi

    if [ -n "$BACKUP_COMMON_DIR" ] && mkdir -p "$BACKUP_COMMON_DIR/info" 2>/dev/null; then
        GB_EXCLUDE_FILE="$BACKUP_COMMON_DIR/info/exclude"
        for _gb_rule in "/$SLUG/logs/" "/$SLUG/tmp/" "/$SLUG/config.json" "/tmp/"; do
            grep -qxF "$_gb_rule" "$GB_EXCLUDE_FILE" 2>/dev/null && continue
            printf '%s\n' "$_gb_rule" >> "$GB_EXCLUDE_FILE" 2>/dev/null || true
        done
    else
        report_error "git-backup" "WARNING: could not create $BACKUP_COMMON_DIR/info -- the logs/tmp/config.json exclusion was NOT written, so this backup's git add may stage config.json"
    fi

    if [ -n "$(git -C "$REPO_ROOT" ls-files -- "$SLUG/logs/" "$SLUG/tmp/" "$SLUG/config.json" 2>/dev/null | head -n 1)" ]; then
        if ! git -C "$REPO_ROOT" diff --cached --quiet 2>/dev/null; then
            log "git-backup" "$SLUG/logs, $SLUG/tmp or $SLUG/config.json are tracked by a version older than the exclusion, but this store has staged changes in its index -- untracking them would commit those too, so it is left for the next backup. If that config.json ever carried a haiku.oauth_token, it is already in this store's git history regardless of when the untrack commit lands -- treat it as compromised and rotate it (claude setup-token)."
        elif git -C "$REPO_ROOT" rm -r -q --cached --ignore-unmatch -- "$SLUG/logs/" "$SLUG/tmp/" "$SLUG/config.json" 2>/dev/null \
            && _gb_commit_untrack >/dev/null 2>&1; then
            log "git-backup" "untracked $SLUG/logs, $SLUG/tmp and $SLUG/config.json -- a version older than the exclusion had committed them. They stop being pushed from now on; commits that already carry them are left untouched, because removing those means rewriting history and force-pushing, which breaks every other clone of this store. If that config.json ever carried a haiku.oauth_token, treat it as compromised and rotate it (claude setup-token) -- claude.ai itself may still accept the old token even though this plugin no longer reads it (#860), and anyone with access to this store's git history can still see it."
        else
            git -C "$REPO_ROOT" reset -q 2>/dev/null || true
            log "git-backup" "could not untrack $SLUG/logs, $SLUG/tmp or $SLUG/config.json; the index was restored and the next backup retries. If that config.json ever carried a haiku.oauth_token, it is already in this store's git history regardless of whether this untrack attempt ever succeeds -- treat it as compromised and rotate it (claude setup-token) -- claude.ai itself may still accept the old token even though this plugin no longer reads it (#860), and anyone with access to this store's git history can still see it."
        fi
    fi

    if [ -z "$(git -C "$REPO_ROOT" ls-files -- "$SLUG/" 2>/dev/null | head -n 1)" ]; then
        TRACKED_VARIANT=$(git -C "$REPO_ROOT" ls-files -- ":(icase)$SLUG/" 2>/dev/null \
            | awk -F/ 'NF > 1 { print $1; exit }')
        if [ -n "$TRACKED_VARIANT" ] && [ "$TRACKED_VARIANT" != "$SLUG" ]; then
            FIX_CMD="git -C '$REPO_ROOT' mv -- '$TRACKED_VARIANT' '$SLUG.tmp' && git -C '$REPO_ROOT' mv -- '$SLUG.tmp' '$SLUG'"
            log "git-backup" "ERROR: this project's memory is tracked as '$TRACKED_VARIANT/' but this session computed '$SLUG/' -- git pathspecs are case-sensitive, so nothing under '$SLUG/' can ever be staged and the backup has STOPPED for this project. It will not resume on its own. Nothing here will rename it for you: run $FIX_CMD (two steps, because a case-only rename is a no-op on a case-insensitive filesystem), then commit."
            mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            printf '%s\n' "remember: git backup has STOPPED for this project. Its memory is tracked in $REPO_ROOT as '$TRACKED_VARIANT/' but is being written to '$SLUG/', and git can match neither to the other -- every save since is committed nowhere. Rename the tracked directory: $FIX_CMD (two steps -- a case-only rename is a no-op on a case-insensitive filesystem), then commit. Nothing will be renamed for you." \
                > "$REMEMBER_DIR/tmp/git-backup-notice" 2>/dev/null || true
            exit 0
        fi
    fi

    git -C "$REPO_ROOT" add -- "$SLUG/" 2>/dev/null

    if git -C "$REPO_ROOT" diff --cached --quiet -- "$SLUG/" 2>/dev/null; then
        log "git-backup" "nothing to commit for $SLUG, skip"
        exit 0
    fi

    TS=$(_remember_date '+%H:%M')
    COMMIT_FAIL_STATE_FILE="$GB_STATE_DIR/git-backup-commit-failed"

    _gb_stamp_cooldown() { date +%s > "$COOLDOWN_MARKER" 2>/dev/null || true; }

    if COMMIT_ERR=$(_gb_commit_slug "$TS" 2>&1 >/dev/null); then
        log "git-backup" "committed $SLUG"
        _gb_stamp_cooldown
        rm -f "$COMMIT_FAIL_STATE_FILE" 2>/dev/null || true
    else
        _gb_stamp_cooldown
        COMMIT_ERR=$(printf '%s' "$COMMIT_ERR" | tr '\n' ' ')
        _cfail=$(cat "$COMMIT_FAIL_STATE_FILE" 2>/dev/null || echo 0)
        if [ -z "$_cfail" ] || [ "${_cfail#*[!0-9]}" != "$_cfail" ]; then
            _cfail=0
        fi
        _cfail=$((10#$_cfail + 1))
        echo "$_cfail" > "$COMMIT_FAIL_STATE_FILE" 2>/dev/null || true

        log "git-backup" "ERROR: commit FAILED for $SLUG -- this memory is recorded in NO git history at all, not locally and not on any remote, and the backup has STOPPED for this project (consecutive failures: $_cfail). git said: ${COMMIT_ERR:-<no output>}. Run 'git -C ${_dq}$REPO_ROOT${_dq} commit -- ${_dq}$SLUG/${_dq}' to see it yourself."

        if [ "$COMMIT_NOTICE_AFTER" -gt 0 ] && [ "$_cfail" -eq "$COMMIT_NOTICE_AFTER" ]; then
            mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            printf '%s\n' "remember: git backup has STOPPED. The last $_cfail commits into $REPO_ROOT failed, so this project's memory is on disk but in no git history -- not locally, and not on your backup remote. git said: ${COMMIT_ERR:-<no output>}. Run: git -C ${_dq}$REPO_ROOT${_dq} commit -- ${_dq}$SLUG/${_dq}" \
                > "$REMEMBER_DIR/tmp/git-backup-notice" 2>/dev/null || true
        fi
        exit 0
    fi

    REMOTE_STATE_FILE="$GB_STATE_DIR/git-backup-remote"
    NO_REMOTE_STATE_FILE="$GB_STATE_DIR/git-backup-no-remote"
    NO_REMOTE_NOTIFIED_FILE="$GB_STATE_DIR/git-backup-no-remote-notified"
    CURRENT_REMOTE=$(git -C "$REPO_ROOT" remote get-url "$REMOTE_NAME" 2>/dev/null || true)

    if [ -z "$CURRENT_REMOTE" ]; then
        _nr=$(cat "$NO_REMOTE_STATE_FILE" 2>/dev/null || echo 0)
        if [ -z "$_nr" ] || [ "${_nr#*[!0-9]}" != "$_nr" ]; then
            _nr=0
        fi
        _nr=$((10#$_nr + 1))
        echo "$_nr" > "$NO_REMOTE_STATE_FILE" 2>/dev/null || true

        log "git-backup" "no remote configured for '$REMOTE_NAME' in $REPO_ROOT -- the commit exists on this machine ONLY and nothing is backed up off it (consecutive saves in this state: $_nr). Add one: git -C ${_dq}$REPO_ROOT${_dq} remote add origin <url>"

        if [ "$NO_REMOTE_NOTICE_AFTER" -gt 0 ] && \
           [ "$_nr" -ge "$NO_REMOTE_NOTICE_AFTER" ] && \
           [ ! -f "$NO_REMOTE_NOTIFIED_FILE" ]; then
            : > "$NO_REMOTE_NOTIFIED_FILE" 2>/dev/null || true
            mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            printf '%s\n' "remember: your memory store at $REPO_ROOT has no git remote, so $_nr saves so far have been committed locally and backed up nowhere. If that is deliberate, nothing further is needed -- this will not be said again. If the setup was never finished: git -C ${_dq}$REPO_ROOT${_dq} remote add origin <url> && git -C ${_dq}$REPO_ROOT${_dq} push -u origin HEAD" \
                > "$REMEMBER_DIR/tmp/git-backup-notice" 2>/dev/null || true
        fi
    elif [ ! -f "$REMOTE_STATE_FILE" ]; then
        rm -f "$NO_REMOTE_STATE_FILE" 2>/dev/null || true
        echo "$CURRENT_REMOTE" > "$REMOTE_STATE_FILE"
        log "git-backup" "git backup configured to push to: $CURRENT_REMOTE (remote '$REMOTE_NAME', branch '${GIT_BACKUP_BRANCH:-<upstream tracking>}')"
        _push_and_report
    else
        RECORDED_REMOTE=$(cat "$REMOTE_STATE_FILE" 2>/dev/null || true)
        if [ "$CURRENT_REMOTE" != "$RECORDED_REMOTE" ]; then
            if [ "$ALLOW_REMOTE_CHANGE" = "true" ]; then
                echo "$CURRENT_REMOTE" > "$REMOTE_STATE_FILE"
                log "git-backup" "remote URL changed (allow_remote_change=true): $CURRENT_REMOTE"
                _push_and_report
            else
                log "git-backup" "ERROR: remote URL changed from '$RECORDED_REMOTE' to '$CURRENT_REMOTE' -- push aborted (set git_backup.allow_remote_change=true to override)"
            fi
        else
            _push_and_report
        fi
    fi
) </dev/null >/dev/null 2>&1 &
disown $! 2>/dev/null || true

exit 0
