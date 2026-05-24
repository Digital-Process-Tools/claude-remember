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

# ── Lock (atomic via noclobber) ───────────────────────────────────────────────
LOCK_FILE="$REPO_ROOT/.git-backup.lock"
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

# ── Background subshell — never blocks save-session.sh ───────────────────────
(
    trap 'rm -f "$LOCK_FILE"' EXIT

    # Prevent outer git env vars from overriding git -C behaviour.
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

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
    if git -C "$REPO_ROOT" commit --no-gpg-sign \
            -m "auto: $SLUG $TS" \
            -- "$SLUG/" >/dev/null 2>&1; then
        log "git-backup" "committed $SLUG"
        date +%s > "$COOLDOWN_MARKER"
    else
        log "git-backup" "ERROR: commit failed for $SLUG"
        exit 0
    fi

    # Push, but tolerate any error (no remote, no network, auth, etc.).
    # GIT_TERMINAL_PROMPT=0 ensures missing credentials fail fast, not hang.
    if GIT_TERMINAL_PROMPT=0 git -C "$REPO_ROOT" push >/dev/null 2>&1; then
        log "git-backup" "pushed $SLUG"
    else
        log "git-backup" "push deferred (will retry next backup)"
    fi
) </dev/null >/dev/null 2>&1 &
disown $! 2>/dev/null || true

exit 0
