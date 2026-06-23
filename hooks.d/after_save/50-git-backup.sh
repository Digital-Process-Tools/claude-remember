#!/bin/bash
# ============================================================================
# 50-git-backup.sh — Commit & push the current slug's memory to git
# ============================================================================
#
# DESCRIPTION
#   Runs on the after_save dispatch. If $REMEMBER_DIR's parent is itself a
#   git toplevel (and not the project directory), commits the current slug's
#   subtree and pushes to the configured remote.
#
#   No-op when:
#     - REMEMBER_DIR is in legacy mode (parent = PROJECT_DIR)
#     - Parent is not a git toplevel
#     - Another instance holds the global lock
#     - Backup cooldown hasn't elapsed
#     - There is nothing to commit for this slug
#
# RUNTIME ENV (provided by save-session.sh via dispatch)
#   PROJECT_DIR, PIPELINE_DIR, REMEMBER_DIR, REMEMBER_PROJECT
#
# ============================================================================

set -u  # not -e — we never want to fail loudly here

# ── Source logging (gives us log(), config(), _remember_date(), REMEMBER_TZ) ─
source "$PIPELINE_DIR/scripts/log.sh"

# ── Activation guard ─────────────────────────────────────────────────────────
REPO_ROOT=$(dirname "$REMEMBER_DIR")
SLUG=$(basename "$REMEMBER_DIR")

# Legacy mode (REMEMBER_DIR is inside PROJECT_DIR) → never run.
[ "$REPO_ROOT" = "$PROJECT_DIR" ] && exit 0

# REPO_ROOT must be the toplevel of a git repo, not just inside one.
TOPLEVEL=$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null) || exit 0
[ "$TOPLEVEL" = "$REPO_ROOT" ] || exit 0

# ── Cooldown ─────────────────────────────────────────────────────────────────
COOLDOWN_MARKER="$REPO_ROOT/.last-git-backup-ts"
BACKUP_COOLDOWN=$(config ".cooldowns.git_backup_seconds" 900)
if [ -f "$COOLDOWN_MARKER" ]; then
    LAST_MOD=$(cat "$COOLDOWN_MARKER" 2>/dev/null || echo 0)
    ELAPSED=$(( $(date +%s) - LAST_MOD ))
    if [ "$ELAPSED" -lt "$BACKUP_COOLDOWN" ]; then
        [ "${REMEMBER_DEBUG:-0}" = "1" ] && log "git-backup" "cooldown ${ELAPSED}s < ${BACKUP_COOLDOWN}s, skip"
        exit 0
    fi
fi

# ── Lock ─────────────────────────────────────────────────────────────────────
# Prefer flock(1): it acquires atomically on an open fd, eliminating the
# TOCTOU window between rm and noclobber re-acquire.  On macOS without
# util-linux flock, we fall back to the noclobber pattern; the race is benign
# (worst case: two concurrent commits to disjoint slug subtrees, which git
# serializes via its own index lock).
LOCK_FILE="$REPO_ROOT/.git-backup.lock"
if command -v flock >/dev/null 2>&1; then
    # flock path: open/create the lock file on fd 9 and acquire exclusively,
    # non-blocking.  If another instance holds it, exit silently.
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        [ "${REMEMBER_DEBUG:-0}" = "1" ] && log "git-backup" "flock held by another instance, skip"
        exit 0
    fi
    # Lock is held on fd 9 for the lifetime of this process.
else
    # Fallback: noclobber pattern.  A TOCTOU window exists between the stale-lock
    # rm and the re-acquire, but the race is benign — see comment above.
    if ! ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null; then
        LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
        if kill -0 "$LOCK_PID" 2>/dev/null; then
            [ "${REMEMBER_DEBUG:-0}" = "1" ] && log "git-backup" "locked by PID $LOCK_PID, skip"
            exit 0
        fi
        log "git-backup" "stale lock (PID $LOCK_PID dead), taking over"
        # Delete stale lock, then re-acquire atomically via noclobber.
        rm -f "$LOCK_FILE"
        ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null || exit 0
    fi
fi

# ── Background subshell — never blocks save-session.sh ───────────────────────
(
    trap 'rm -f "$LOCK_FILE"' EXIT

    # Prevent outer git env vars from overriding git -C behaviour.
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

    # ── Configurable push target (#63) ────────────────────────────────────────
    # git_backup.remote / git_backup.branch let users with multiple remotes or a
    # non-standard tracking config pin exactly where memory is pushed. Both empty
    # (the default) → bare `git push`, relying on the branch's upstream tracking.
    GIT_BACKUP_REMOTE=$(config ".git_backup.remote" "")
    GIT_BACKUP_BRANCH=$(config ".git_backup.branch" "")
    REMOTE_NAME="${GIT_BACKUP_REMOTE:-origin}"

    # ── Configurable commit signing (#62) ─────────────────────────────────────
    # We pass --no-gpg-sign by default so background commits never hang on a
    # passphrase prompt. Users with non-interactive signing (e.g. a hardware key)
    # can set git_backup.gpg_sign=true to drop the flag and honour their own
    # commit.gpgSign config. Empty flag (unquoted) = no extra arg.
    GIT_BACKUP_GPG_SIGN=$(config ".git_backup.gpg_sign" "false")
    GPG_SIGN_FLAG="--no-gpg-sign"
    [ "$GIT_BACKUP_GPG_SIGN" = "true" ] && GPG_SIGN_FLAG=""

    _push() {
        if [ -n "$GIT_BACKUP_REMOTE" ]; then
            GIT_TERMINAL_PROMPT=0 git -C "$REPO_ROOT" push "$GIT_BACKUP_REMOTE" ${GIT_BACKUP_BRANCH:+"$GIT_BACKUP_BRANCH"} >/dev/null 2>&1
        else
            GIT_TERMINAL_PROMPT=0 git -C "$REPO_ROOT" push >/dev/null 2>&1
        fi
    }

    # Remove the bootstrap-written per-slug .gitignore (contains "*") that was placed
    # to prevent commits when memory lived inside a project repo. In external git-backup
    # mode it blocks all staging; the root-level .gitignore covers logs/tmp exclusions.
    SLUG_GITIGNORE="$REPO_ROOT/$SLUG/.gitignore"
    if [ -f "$SLUG_GITIGNORE" ] && [ "$(cat "$SLUG_GITIGNORE")" = "*" ]; then
        rm -f "$SLUG_GITIGNORE"
        log "git-backup" "removed per-slug .gitignore (legacy bootstrap artifact)"
    fi

    # Auto-untrack logs/tmp if they were accidentally staged before .gitignore was in place.
    git -C "$REPO_ROOT" rm --cached -- "$SLUG/logs/" "$SLUG/tmp/" 2>/dev/null || true

    # Stage only this slug's subtree. -- required: slug names may start with '-'.
    git -C "$REPO_ROOT" add -- "$SLUG/" 2>/dev/null

    # Anything actually staged?
    if git -C "$REPO_ROOT" diff --cached --quiet -- "$SLUG/" 2>/dev/null; then
        log "git-backup" "nothing to commit for $SLUG, skip"
        exit 0
    fi

    TS=$(_remember_date '+%H:%M')
    if git -C "$REPO_ROOT" commit $GPG_SIGN_FLAG \
            -m "auto: $SLUG $TS" \
            -- "$SLUG/" >/dev/null 2>&1; then
        log "git-backup" "committed $SLUG"
        date +%s > "$COOLDOWN_MARKER"
    else
        log "git-backup" "ERROR: commit failed for $SLUG"
        exit 0
    fi

    # ── Remote URL validation ─────────────────────────────────────────────────
    # On first push, record the remote URL. On subsequent pushes, abort if the
    # URL changed — a changed URL could mean a poisoned config.json pointing to
    # an attacker-controlled remote. Set git_backup.allow_remote_change=true to
    # override (e.g. when intentionally re-pointing to a new private repo).
    REMOTE_STATE_FILE="$REPO_ROOT/.git-backup-remote"
    CURRENT_REMOTE=$(git -C "$REPO_ROOT" remote get-url "$REMOTE_NAME" 2>/dev/null || true)
    ALLOW_REMOTE_CHANGE=$(config ".git_backup.allow_remote_change" "false")

    if [ -z "$CURRENT_REMOTE" ]; then
        log "git-backup" "no remote configured, skipping push"
    elif [ ! -f "$REMOTE_STATE_FILE" ]; then
        # First push — record the URL and proceed.
        echo "$CURRENT_REMOTE" > "$REMOTE_STATE_FILE"
        log "git-backup" "git backup configured to push to: $CURRENT_REMOTE (remote '$REMOTE_NAME', branch '${GIT_BACKUP_BRANCH:-<upstream tracking>}')"
        if _push; then
            log "git-backup" "pushed $SLUG"
        else
            log "git-backup" "push deferred (will retry next backup)"
        fi
    else
        RECORDED_REMOTE=$(cat "$REMOTE_STATE_FILE" 2>/dev/null || true)
        if [ "$CURRENT_REMOTE" != "$RECORDED_REMOTE" ]; then
            if [ "$ALLOW_REMOTE_CHANGE" = "true" ]; then
                # Explicit override — update state file and push.
                echo "$CURRENT_REMOTE" > "$REMOTE_STATE_FILE"
                log "git-backup" "remote URL changed (allow_remote_change=true): $CURRENT_REMOTE"
                if _push; then
                    log "git-backup" "pushed $SLUG"
                else
                    log "git-backup" "push deferred (will retry next backup)"
                fi
            else
                log "git-backup" "ERROR: remote URL changed from '$RECORDED_REMOTE' to '$CURRENT_REMOTE' — push aborted (set git_backup.allow_remote_change=true to override)"
            fi
        else
            # Remote matches recorded URL — safe to push.
            if _push; then
                log "git-backup" "pushed $SLUG"
            else
                log "git-backup" "push deferred (will retry next backup)"
            fi
        fi
    fi
) </dev/null >/dev/null 2>&1 &
disown $! 2>/dev/null || true

exit 0
