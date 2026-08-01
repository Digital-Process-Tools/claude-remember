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

# Prevent outer git env vars from overriding git -C behaviour. This must happen
# *before* the activation guards below, not just inside the worker subshell: a
# leaked GIT_DIR (bare-repo dotfiles setups that export it, or invocation from
# inside another git hook) makes every `git -C … rev-parse` resolve against the
# leaked repo instead of the directory we asked about — collapsing both sides of
# the #138 common-dir comparison onto the same wrong value. The guards are the
# code that most needs the sanitization, so they get it first.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

# Legacy mode (REMEMBER_DIR is inside PROJECT_DIR) → never run.
[ "$REPO_ROOT" = "$PROJECT_DIR" ] && exit 0

# REPO_ROOT must be the toplevel of a git repo, not just inside one.
#
# Compared RESOLVED rather than textually (#260). `--show-toplevel` answers with
# a resolved path while REPO_ROOT is spelled however REMEMBER_DIR was, so any
# symlink in any component made the two differ — and this `exit 0` sits above
# every log call in the file, so the backup did nothing, forever, while the
# store looked exactly like a healthy one. macOS makes that ordinary rather than
# exotic: /var is a symlink to /private/var, so every store under TMPDIR is
# already in this state.
#
# The guard itself is kept, because it is what stops the hooks operating on a
# user's project repo by accident: a store that is a SUBDIRECTORY of some larger
# repo must still be refused. Resolving both sides narrows the refusal to that
# case instead of catching honest stores with it.
_gb_realpath() {
    ( cd "$1" 2>/dev/null && pwd -P ) || printf '%s' "$1"
}
TOPLEVEL=$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null) || exit 0
if [ "$(_gb_realpath "$TOPLEVEL")" != "$(_gb_realpath "$REPO_ROOT")" ]; then
    # Three states, not two: this is not "ran, nothing to do". The store sits
    # inside a repository the hook declines to manage, and until now that was
    # indistinguishable from a store it was managing correctly.
    log "git-backup" "declined: $REPO_ROOT is not the toplevel of its git repository (that is $TOPLEVEL) — a memory store inside a larger repo is never committed or pushed by this hook, deliberately. Nothing here is backed up. Give the store its own repository if you want it backed up."
    exit 0
fi

# ── Never back up into the project's own repository (#138) ───────────────────
# The equality check above only catches the plain legacy layout. Since #127,
# a session run from a linked git worktree keeps PROJECT_DIR on the worktree
# while REMEMBER_DIR is redirected into the *main checkout* — so REPO_ROOT is
# the project repo, both guards above pass, and the hook would delete the
# protective .remember/.gitignore, commit the whole memory tree onto whatever
# branch the main checkout has out, and push it to the project's origin.
#
# Compare git *common dirs* instead of paths: every worktree of a repo shares
# one common dir, so this covers the plain, worktree, and subdirectory cases at
# once, while a repo dedicated to memory backup keeps a different common dir and
# still activates. Old git without --path-format falls back to resolving the
# relative form; if even that fails we leave the previous behaviour untouched.
_gb_common_dir() {
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
    _gb_realpath "$_out"
}

PROJECT_COMMON_DIR=$(_gb_common_dir "$PROJECT_DIR") || PROJECT_COMMON_DIR=""
BACKUP_COMMON_DIR=$(_gb_common_dir "$REPO_ROOT") || BACKUP_COMMON_DIR=""
if [ -n "$PROJECT_COMMON_DIR" ] && [ "$PROJECT_COMMON_DIR" = "$BACKUP_COMMON_DIR" ]; then
    debug_enabled 0 && \
        log "git-backup" "REPO_ROOT is the project repo (worktree/legacy), skip"
    exit 0
fi

# ── Where hook state lives (#261) ────────────────────────────────────────────
# Not at the store root. `git merge --ff-only` refuses when an untracked file
# would be overwritten, so every state file sitting beside the user's memory is
# a name the restore half collides with the day a remote carries it — the
# mechanism the feature depends on being the mechanism that breaks it. It also
# meant every `git status` in the store showed noise the plugin created, which
# trains a user to ignore that output.
#
# The git common dir is the right home, and it is the only option that needs no
# .gitignore, no user action, and no edit by the plugin to a tracked file inside
# the user's own backup repository: git never tracks it, never merges it, never
# reports it. It is shared by every worktree of the repo, which is what keeps
# the lock shared with the restore half. These values are also genuinely
# per-clone — a timestamp, a counter, a lock, a recorded URL are properties of
# this machine's relationship with the store, not of the memory in it.
#
# If it cannot be determined we stay exactly where we were rather than invent a
# location: three states, and the third is "could not relocate", not a guess.
if [ -n "$BACKUP_COMMON_DIR" ] && mkdir -p "$BACKUP_COMMON_DIR/remember" 2>/dev/null; then
    GB_STATE_DIR="$BACKUP_COMMON_DIR/remember"
else
    GB_STATE_DIR="$REPO_ROOT"
fi

# Existing stores already have these files at the root, and they are carried
# forward rather than abandoned. `.git-backup-remote` is the reason this is not
# optional: it records the URL trusted on the first push and aborts if it later
# changes, so starting fresh at a new location would re-record whatever URL is
# configured now — silently forgiving exactly the change the check exists to
# catch. A file git TRACKS is copied and left in place; staging a deletion
# inside the user's backup repository is not this hook's business. The lock is
# not carried at all — a lock is not state, and a leftover one is only litter.
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

# ── Cooldown ─────────────────────────────────────────────────────────────────
COOLDOWN_MARKER="$GB_STATE_DIR/last-git-backup-ts"
BACKUP_COOLDOWN=$(config ".cooldowns.git_backup_seconds" 900)
if [ -f "$COOLDOWN_MARKER" ]; then
    LAST_MOD=$(cat "$COOLDOWN_MARKER" 2>/dev/null || echo 0)
    # Unvalidated file content inside $(( )) under `set -u` does NOT merely skip
    # the cooldown (#258 reports the direction the other way round). Bash
    # evaluates the content as an ARITHMETIC EXPRESSION, so a marker holding a
    # letter is an unbound variable reference and the hook DIES on the next
    # line — above the lock, above the add, above the commit. And because the
    # marker used to be rewritten only after a successful commit, it stayed
    # corrupt: one bad byte from a crash mid-write stopped the backup
    # permanently, with dispatch's nameless `hook failed` as the only trace.
    # Falling back to 0 self-heals, because the run it allows is the run that
    # rewrites the marker.
    case "$LAST_MOD" in
        ''|*[!0-9]*) LAST_MOD=0 ;;
    esac
    ELAPSED=$(( $(date +%s) - LAST_MOD ))
    if [ "$ELAPSED" -lt "$BACKUP_COOLDOWN" ]; then
        debug_enabled 0 && log "git-backup" "cooldown ${ELAPSED}s < ${BACKUP_COOLDOWN}s, skip"
        exit 0
    fi
fi

# ── Lock ─────────────────────────────────────────────────────────────────────
# Prefer flock(1): it acquires atomically on an open fd, eliminating the
# TOCTOU window between rm and noclobber re-acquire.  On macOS without
# util-linux flock, we fall back to the noclobber pattern; the race is benign
# (worst case: two concurrent commits to disjoint slug subtrees, which git
# serializes via its own index lock).
LOCK_FILE="$GB_STATE_DIR/git-backup.lock"
GB_LOCK_STYLE=noclobber
if command -v flock >/dev/null 2>&1; then
    GB_LOCK_STYLE=flock
    # flock path: open/create the lock file on fd 9 and acquire exclusively,
    # non-blocking.  If another instance holds it, exit silently.
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        debug_enabled 0 && log "git-backup" "flock held by another instance, skip"
        exit 0
    fi
    # Lock is held on fd 9 for the lifetime of this process.
else
    # Fallback: noclobber pattern.  A TOCTOU window exists between the stale-lock
    # rm and the re-acquire, but the race is benign — see comment above.
    if ! ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null; then
        LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
        if kill -0 "$LOCK_PID" 2>/dev/null; then
            debug_enabled 0 && log "git-backup" "locked by PID $LOCK_PID, skip"
            exit 0
        fi
        log "git-backup" "stale lock (PID $LOCK_PID dead), taking over"
        # Delete stale lock, then re-acquire atomically via noclobber.
        rm -f "$LOCK_FILE"
        ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null || exit 0
    fi
fi

# ── Config snapshot — read BEFORE backgrounding ──────────────────────────────
# Every git_backup.* value is read here, in the parent, and inherited by the
# subshell as a plain variable. Read inside it instead and they race the parent's
# exit: lib-memory-dir.sh installs an EXIT trap that deletes $REMEMBER_CONFIG,
# and once the merged config is gone config() quietly returns each caller's
# default (issue #135). A slow backup would then push to the wrong place — or to
# nowhere, with remote and branch emptied — and nothing would say so, because a
# missing config file is not itself reported.
#
# git_backup.remote / git_backup.branch let users with multiple remotes or a
# non-standard tracking config pin exactly where memory is pushed. Both empty
# (the default) → bare `git push`, relying on the branch's upstream tracking.
GIT_BACKUP_REMOTE=$(config ".git_backup.remote" "")
GIT_BACKUP_BRANCH=$(config ".git_backup.branch" "")
# Which remote a bare `git push` would ACTUALLY use (#257). This was hardcoded
# to `origin` whenever git_backup.remote is unset, while `_push` in that same
# case runs a bare `git push` — which follows the branch's upstream. Two
# consequences, and the second is worse than the filed one:
#
#   * a store whose upstream is any other remote was told "no remote configured,
#     skipping push" on every save while holding a perfectly good push target,
#     so it backed up nowhere, forever, at one info-level line per save;
#   * the URL the security check below records and validates was not the URL the
#     push goes to, so the poisoned-remote guard could be sidestepped entirely by
#     a `branch.<name>.remote` the check never looks at.
#
# Ask git the same question `git push` asks. `@{push}` is the exact answer and
# needs a git that understands it; `branch.<name>.remote` is the fallback, and
# `origin` remains the last resort, which is where every store that was working
# before still lands.
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
    # Only a NAMED remote is usable below, and the candidate is not guaranteed
    # to be one: `branch.<name>.remote` may legally hold a URL, in which case
    # `git remote get-url <that url>` is not a question with an answer and the
    # store would be declared remoteless — which is the very failure this block
    # exists to remove, reintroduced one layer along. So the candidate has to
    # prove it names a remote before it is believed; anything else falls back to
    # `origin`, which is where every store that worked before still lands.
    if [ -n "$GB_CAND" ] && git -C "$REPO_ROOT" remote get-url "$GB_CAND" >/dev/null 2>&1; then
        REMOTE_NAME="$GB_CAND"
    fi
fi
[ -n "$REMOTE_NAME" ] || REMOTE_NAME=origin

# We pass --no-gpg-sign by default so background commits never hang on a
# passphrase prompt. Users with non-interactive signing (e.g. a hardware key)
# can set git_backup.gpg_sign=true to drop the flag and honour their own
# commit.gpgSign config. Empty flag (unquoted) = no extra arg (#62).
GIT_BACKUP_GPG_SIGN=$(config ".git_backup.gpg_sign" "false")
GPG_SIGN_FLAG="--no-gpg-sign"
if [ "$GIT_BACKUP_GPG_SIGN" = "true" ]; then
    GPG_SIGN_FLAG=""
fi

# Read here too, though its default is the safe one: falling back to false only
# aborts a push, where a stale true would let a changed remote through.
ALLOW_REMOTE_CHANGE=$(config ".git_backup.allow_remote_change" "false")

# How many CONSECUTIVE permanently-rejected pushes before the human is
# interrupted rather than merely logged (#253). 0 disables the interruption and
# leaves the log line, which stays honest either way. Falling back to the
# default on a non-numeric value is the safe direction: the alternative is
# arithmetic on garbage deciding whether a stopped backup gets reported.
REJECT_NOTICE_AFTER=$(config ".git_backup.reject_notice_after" "3")
case "$REJECT_NOTICE_AFTER" in
    ''|*[!0-9]*) REJECT_NOTICE_AFTER=3 ;;
esac

# How many CONSECUTIVE failed commits before the human is interrupted (#257).
# The same argument as the rejection counter above, and it applies harder: a
# rejected push at least leaves the commit on this machine, whereas a failed
# commit records the memory in no git history at all. The causes are durable by
# nature — no user.email, a full disk, an index lock left by a crashed git, a
# pre-commit hook installed on the backup repo — so none of them self-heals and
# the threshold can only postpone a true report, never swallow one.
COMMIT_NOTICE_AFTER=$(config ".git_backup.commit_notice_after" "3")
case "$COMMIT_NOTICE_AFTER" in
    ''|*[!0-9]*) COMMIT_NOTICE_AFTER=3 ;;
esac

# How many consecutive saves with NO remote at all before saying so once (#257).
# Deliberately higher than the two above and deliberately ONE-SHOT, because this
# is the one case in this file where the condition may be entirely intentional:
# someone who wants a local-only history is not experiencing a bug. Nagging them
# every save would cost `systemMessage` its meaning for the cases that are bugs,
# which is the more serious loss. So the threshold is evidence that this is a
# steady state rather than a store mid-setup, and it is said once for the
# lifetime of the store. 0 disables it entirely.
NO_REMOTE_NOTICE_AFTER=$(config ".git_backup.no_remote_notice_after" "10")
case "$NO_REMOTE_NOTICE_AFTER" in
    ''|*[!0-9]*) NO_REMOTE_NOTICE_AFTER=10 ;;
esac

# ── Background subshell — never blocks save-session.sh ───────────────────────
(
    # Only the noclobber path unlinks (#258). flock's ownership is fd-based, so
    # removing the path while holding it opens a window where a second instance
    # opens the file, locks the now-unlinked inode, and a third creates a fresh
    # inode at the same path and locks that — two concurrent backups on one
    # store. The restore half's `_take_lock` already installs this trap only on
    # its noclobber branch; this call site simply had not adopted it.
    [ "$GB_LOCK_STYLE" = "flock" ] || trap 'rm -f "$LOCK_FILE"' EXIT

    # Prevent outer git env vars from overriding git -C behaviour.
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

    # ── Push, and tell the three states apart (#253) ─────────────────────────
    # A network blip and a non-fast-forward rejection are different in kind. The
    # first will succeed later. The second cannot succeed on ANY later attempt
    # without a human, and the backup has in fact stopped. Reporting both as
    # "will retry next backup" promises something the tool cannot deliver — the
    # reporter ran twelve days on that promise, 15 deferrals against 8 pushes,
    # with memory never leaving the machine and nothing anywhere saying so.
    #
    # So: three states, never two. Pushed / deferred, will retry / rejected,
    # stopped, here is what to do.
    REJECT_STATE_FILE="$GB_STATE_DIR/git-backup-rejected"

    # stdout is git's --porcelain per-ref status. stderr is dropped on purpose:
    # it carries hints, transport noise and any pre-push hook's chatter, and
    # claude-supertool#641 is precisely what happens when a verdict about the
    # remote is read off text the remote did not write.
    _push() {
        if [ -n "$GIT_BACKUP_REMOTE" ]; then
            GIT_TERMINAL_PROMPT=0 git -C "$REPO_ROOT" push --porcelain "$GIT_BACKUP_REMOTE" ${GIT_BACKUP_BRANCH:+"$GIT_BACKUP_BRANCH"} 2>/dev/null
        else
            GIT_TERMINAL_PROMPT=0 git -C "$REPO_ROOT" push --porcelain 2>/dev/null
        fi
    }

    # The refs git ITSELF flagged as rejected, one per line, read off stdin.
    #
    # Empty output is an absence of information and never a "no": an unreachable
    # remote, a dead credential helper or a killed transport produce no per-ref
    # lines at all, and every one of those stays a quiet deferral exactly as it
    # was. Loudness here requires positive evidence from git.
    #
    # The grammar is git's: `<flag>TAB<from>:<to>TAB<summary>`, with `!` for a
    # rejected ref, `*` for a new branch and `+` for a forced update — the three
    # cannot be confused, unlike the free text they replace. Only lines after
    # the `To <url>` header are considered, because a pre-push hook's own output
    # precedes it, and only exact three-tab-field lines count.
    _rejected_refs() {
        awk -F'\t' '
            !hdr { if ($0 ~ /^To /) hdr = 1; next }
            hdr && NF == 3 && $1 == "!" { print $2 " " $3 }
        '
    }

    # One decision for all three push sites. The funnel #253 reports had three
    # mouths; fixing one would have left the reported case reachable through the
    # other two, and the next site added would drift again.
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
        case "$_count" in
            ''|*[!0-9]*) _count=0 ;;
        esac
        _count=$((_count + 1))
        echo "$_count" > "$REJECT_STATE_FILE" 2>/dev/null || true

        log "git-backup" "ERROR: push REJECTED by the remote — the backup has STOPPED for $SLUG and will not resume on its own (consecutive rejections: $_count). git rejected: ${_rejected%;}. The commit exists on this machine only. Nothing here will fetch, merge or rebase for you: run 'git -C \"$REPO_ROOT\" push' to see git's own advice and resolve it by hand — recent.md and archive.md are rewritten wholesale by consolidation, so a wrong automatic resolution would corrupt memory silently."

        # Escalation, not alarm. systemMessage is the only hook output the HUMAN
        # sees, and it is also the most intrusive surface in this codebase — one
        # that fires on every hiccup is one nobody reads, which would cost more
        # than it buys. A transient failure can never reach this branch at all,
        # and a rejection never clears itself, so the threshold only postpones a
        # true report by a few backups; it can never swallow one.
        if [ "$REJECT_NOTICE_AFTER" -gt 0 ] && [ "$_count" -eq "$REJECT_NOTICE_AFTER" ]; then
            mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            printf '%s\n' "remember: git backup has STOPPED. The remote rejected the last $_count pushes from $REPO_ROOT and will not accept them on a retry — memory is still being committed locally, but it is not reaching your backup remote. Run: git -C \"$REPO_ROOT\" push — then resolve the divergence yourself. Nothing will be merged or rebased for you." \
                > "$REMEMBER_DIR/tmp/git-backup-notice" 2>/dev/null || true
        fi
        return 0
    }

    # Remove the bootstrap-written per-slug .gitignore (contains "*") that was placed
    # to prevent commits when memory lived inside a project repo. In external git-backup
    # mode it blocks all staging; the root-level .gitignore covers logs/tmp exclusions.
    SLUG_GITIGNORE="$REPO_ROOT/$SLUG/.gitignore"
    if [ -f "$SLUG_GITIGNORE" ] && [ "$(cat "$SLUG_GITIGNORE")" = "*" ]; then
        rm -f "$SLUG_GITIGNORE"
        log "git-backup" "removed per-slug .gitignore (legacy bootstrap artifact)"
    fi

    # ── An exclusion the plugin owns (#285) ──────────────────────────────────
    # The comment above says the root-level .gitignore covers logs/tmp. The
    # plugin never creates that file, and in external mode it deletes the only
    # one it does write. So in every store where the user did not hand-author a
    # root .gitignore, logs/ and tmp/ are committed and pushed like memory —
    # and tmp/ is where the handoff delivery record now lives, which is
    # per-clone state that must never reach another machine.
    #
    # $GIT_DIR/info/exclude, not a .gitignore, for the three reasons #261 chose
    # the git common dir: no user action, no edit by this hook to a tracked file
    # inside the user's own backup repository, and nothing that can itself be
    # committed and land on someone else's machine. It is per-clone, which is
    # exactly the scope of what it excludes. Anchored with a leading slash so a
    # slug is matched at the repo root and nowhere else, and appended only when
    # absent so this does not grow a line per save.
    if [ -n "$BACKUP_COMMON_DIR" ] && mkdir -p "$BACKUP_COMMON_DIR/info" 2>/dev/null; then
        GB_EXCLUDE_FILE="$BACKUP_COMMON_DIR/info/exclude"
        for _gb_rule in "/$SLUG/logs/" "/$SLUG/tmp/"; do
            grep -qxF "$_gb_rule" "$GB_EXCLUDE_FILE" 2>/dev/null && continue
            printf '%s\n' "$_gb_rule" >> "$GB_EXCLUDE_FILE" 2>/dev/null || true
        done
    fi

    # Auto-untrack logs/tmp if they were accidentally staged before the
    # exclusion was in place. This can only finish the job for a path that no
    # longer exists in the working tree — see the delivery record's own note in
    # session-start-hook.sh for why a tracked file has to move, not be ignored.
    git -C "$REPO_ROOT" rm --cached -- "$SLUG/logs/" "$SLUG/tmp/" 2>/dev/null || true

    # ── A pathspec that cannot match is not an empty store (#263) ────────────
    # `git add -- "$SLUG/"` staging nothing has two completely different causes
    # and the guard below cannot tell them apart: the store has nothing new to
    # save, or the pathspec matches no path git tracks. The reporter ran twelve
    # days on the second while being told the first, every few minutes.
    #
    # Their slug differed from the tracked one by the case of the drive letter
    # alone. NTFS is case-insensitive, so REMEMBER_DIR resolved, memory saved,
    # and every layer above git was satisfied. Git's pathspecs are case-sensitive
    # on every platform, so `add` matched nothing and `diff --cached --quiet`
    # answered "clean" — the correct answer to the question it was asked, and the
    # wrong answer to the one that mattered.
    #
    # So: three states, not two. Committed / clean / this slug cannot address its
    # own history. The third is asked FIRST, and asked of git rather than of the
    # filesystem, because a case-insensitive filesystem is precisely what hides
    # it — and asked whether or not anything changed, because the condition is
    # structural and a store that is clean right now will still lose the next
    # thing it writes.
    #
    # `:(icase)` needs a git that understands pathspec magic; an older one errors
    # and prints nothing, which lands back on the previous behaviour rather than
    # on a false accusation.
    #
    # Nothing is renamed automatically. A case-only rename of a tracked memory
    # tree is exactly the class of repair `_push_and_report` already refuses to
    # perform for a diverged remote: recent.md and archive.md are rewritten
    # wholesale by consolidation, so a wrong guess here corrupts memory silently.
    # A brand-new slug has no tracked path either and is the normal first-backup
    # case, so a report requires positive evidence — a tracked sibling that
    # differs from this slug by case alone.
    if [ -z "$(git -C "$REPO_ROOT" ls-files -- "$SLUG/" 2>/dev/null | head -n 1)" ]; then
        TRACKED_VARIANT=$(git -C "$REPO_ROOT" ls-files -- ":(icase)$SLUG/" 2>/dev/null \
            | awk -F/ 'NF > 1 { print $1; exit }')
        if [ -n "$TRACKED_VARIANT" ] && [ "$TRACKED_VARIANT" != "$SLUG" ]; then
            FIX_CMD="git -C '$REPO_ROOT' mv -- '$TRACKED_VARIANT' '$SLUG.tmp' && git -C '$REPO_ROOT' mv -- '$SLUG.tmp' '$SLUG'"
            log "git-backup" "ERROR: this project's memory is tracked as '$TRACKED_VARIANT/' but this session computed '$SLUG/' — git pathspecs are case-sensitive, so nothing under '$SLUG/' can ever be staged and the backup has STOPPED for this project. It will not resume on its own. Nothing here will rename it for you: run $FIX_CMD (two steps, because a case-only rename is a no-op on a case-insensitive filesystem), then commit."
            mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            printf '%s\n' "remember: git backup has STOPPED for this project. Its memory is tracked in $REPO_ROOT as '$TRACKED_VARIANT/' but is being written to '$SLUG/', and git can match neither to the other — every save since is committed nowhere. Rename the tracked directory: $FIX_CMD (two steps — a case-only rename is a no-op on a case-insensitive filesystem), then commit. Nothing will be renamed for you." \
                > "$REMEMBER_DIR/tmp/git-backup-notice" 2>/dev/null || true
            exit 0
        fi
    fi

    # Stage only this slug's subtree. -- required: slug names may start with '-'.
    git -C "$REPO_ROOT" add -- "$SLUG/" 2>/dev/null

    # Anything actually staged?
    if git -C "$REPO_ROOT" diff --cached --quiet -- "$SLUG/" 2>/dev/null; then
        log "git-backup" "nothing to commit for $SLUG, skip"
        exit 0
    fi

    # ── Committed / failed, and WHY it failed (#257) ─────────────────────────
    # This branch used to be `log "ERROR: commit failed"; exit 0`, with git's own
    # stderr thrown away by `2>&1`. That is strictly worse than the rejected
    # push #253 reports: there the commit existed locally and only the remote
    # copy was missing, whereas here the memory is recorded NOWHERE — and the
    # caller was told success. The one line it did print named no cause, so a
    # user reading it learned only the half they already knew.
    #
    # This cannot make the commit succeed. There is no repair available: a
    # missing user.email or a full disk is not something a backup hook may fix
    # on someone's behalf, and the file's standing rule is that nothing is
    # renamed, merged or rebased for you. So the fix is disclosure, and the
    # failure being chosen is loud-and-still-not-backing-up over
    # quiet-and-still-not-backing-up. The hook still exits 0 and still never
    # blocks the agent.
    TS=$(_remember_date '+%H:%M')
    COMMIT_FAIL_STATE_FILE="$GB_STATE_DIR/git-backup-commit-failed"

    # Stamped on BOTH outcomes (#258), and deliberately not any earlier than
    # this. The filed defect is that a store whose commits fail never engages
    # the throttle at all, so it runs the full add/rm/commit on every single
    # save while committing nothing — the opposite of what a cooldown is for.
    #
    # Stamping at the top of this subshell instead would also cover the
    # nothing-to-commit path, and that is worse than the bug: a save with no
    # new memory would then push the next check out by a whole cooldown window,
    # so real memory written a minute later would sit unbacked-up for the rest
    # of it. That trades a wasteful no-op for a slower backup, and slower is the
    # direction that loses data. `test_nothing_to_commit_no_op` pins it.
    _gb_stamp_cooldown() { date +%s > "$COOLDOWN_MARKER" 2>/dev/null || true; }

    if COMMIT_ERR=$(git -C "$REPO_ROOT" commit $GPG_SIGN_FLAG \
            -m "auto: $SLUG $TS" \
            -- "$SLUG/" 2>&1 >/dev/null); then
        log "git-backup" "committed $SLUG"
        _gb_stamp_cooldown
        rm -f "$COMMIT_FAIL_STATE_FILE" 2>/dev/null || true
    else
        _gb_stamp_cooldown
        COMMIT_ERR=$(printf '%s' "$COMMIT_ERR" | tr '\n' ' ')
        _cfail=$(cat "$COMMIT_FAIL_STATE_FILE" 2>/dev/null || echo 0)
        case "$_cfail" in
            ''|*[!0-9]*) _cfail=0 ;;
        esac
        _cfail=$((_cfail + 1))
        echo "$_cfail" > "$COMMIT_FAIL_STATE_FILE" 2>/dev/null || true

        log "git-backup" "ERROR: commit FAILED for $SLUG — this memory is recorded in NO git history at all, not locally and not on any remote, and the backup has STOPPED for this project (consecutive failures: $_cfail). git said: ${COMMIT_ERR:-<no output>}. Run 'git -C \"$REPO_ROOT\" commit -- \"$SLUG/\"' to see it yourself."

        if [ "$COMMIT_NOTICE_AFTER" -gt 0 ] && [ "$_cfail" -eq "$COMMIT_NOTICE_AFTER" ]; then
            mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            printf '%s\n' "remember: git backup has STOPPED. The last $_cfail commits into $REPO_ROOT failed, so this project's memory is on disk but in no git history — not locally, and not on your backup remote. git said: ${COMMIT_ERR:-<no output>}. Run: git -C \"$REPO_ROOT\" commit -- \"$SLUG/\"" \
                > "$REMEMBER_DIR/tmp/git-backup-notice" 2>/dev/null || true
        fi
        exit 0
    fi

    # ── Remote URL validation ─────────────────────────────────────────────────
    # On first push, record the remote URL. On subsequent pushes, abort if the
    # URL changed — a changed URL could mean a poisoned config.json pointing to
    # an attacker-controlled remote. Set git_backup.allow_remote_change=true to
    # override (e.g. when intentionally re-pointing to a new private repo).
    REMOTE_STATE_FILE="$GB_STATE_DIR/git-backup-remote"
    NO_REMOTE_STATE_FILE="$GB_STATE_DIR/git-backup-no-remote"
    NO_REMOTE_NOTIFIED_FILE="$GB_STATE_DIR/git-backup-no-remote-notified"
    CURRENT_REMOTE=$(git -C "$REPO_ROOT" remote get-url "$REMOTE_NAME" 2>/dev/null || true)

    if [ -z "$CURRENT_REMOTE" ]; then
        # ── Backs up nowhere, forever (#257 case 2) ──────────────────────────
        # Correct behaviour on any single invocation and wrong as a steady
        # state: one info-level line per save is how a half-finished setup
        # becomes a permanent absence of off-machine backup, and nothing
        # distinguished "deliberately local-only" from "the setup was never
        # finished". It still cannot be distinguished from inside this hook —
        # so it is asked ONCE, of the only person who knows.
        _nr=$(cat "$NO_REMOTE_STATE_FILE" 2>/dev/null || echo 0)
        case "$_nr" in
            ''|*[!0-9]*) _nr=0 ;;
        esac
        _nr=$((_nr + 1))
        echo "$_nr" > "$NO_REMOTE_STATE_FILE" 2>/dev/null || true

        log "git-backup" "no remote configured for '$REMOTE_NAME' in $REPO_ROOT — the commit exists on this machine ONLY and nothing is backed up off it (consecutive saves in this state: $_nr). Add one: git -C \"$REPO_ROOT\" remote add origin <url>"

        if [ "$NO_REMOTE_NOTICE_AFTER" -gt 0 ] && \
           [ "$_nr" -ge "$NO_REMOTE_NOTICE_AFTER" ] && \
           [ ! -f "$NO_REMOTE_NOTIFIED_FILE" ]; then
            : > "$NO_REMOTE_NOTIFIED_FILE" 2>/dev/null || true
            mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            printf '%s\n' "remember: your memory store at $REPO_ROOT has no git remote, so $_nr saves so far have been committed locally and backed up nowhere. If that is deliberate, nothing further is needed — this will not be said again. If the setup was never finished: git -C \"$REPO_ROOT\" remote add origin <url> && git -C \"$REPO_ROOT\" push -u origin HEAD" \
                > "$REMEMBER_DIR/tmp/git-backup-notice" 2>/dev/null || true
        fi
    elif [ ! -f "$REMOTE_STATE_FILE" ]; then
        rm -f "$NO_REMOTE_STATE_FILE" 2>/dev/null || true
        # First push — record the URL and proceed.
        echo "$CURRENT_REMOTE" > "$REMOTE_STATE_FILE"
        log "git-backup" "git backup configured to push to: $CURRENT_REMOTE (remote '$REMOTE_NAME', branch '${GIT_BACKUP_BRANCH:-<upstream tracking>}')"
        _push_and_report
    else
        RECORDED_REMOTE=$(cat "$REMOTE_STATE_FILE" 2>/dev/null || true)
        if [ "$CURRENT_REMOTE" != "$RECORDED_REMOTE" ]; then
            if [ "$ALLOW_REMOTE_CHANGE" = "true" ]; then
                # Explicit override — update state file and push.
                echo "$CURRENT_REMOTE" > "$REMOTE_STATE_FILE"
                log "git-backup" "remote URL changed (allow_remote_change=true): $CURRENT_REMOTE"
                _push_and_report
            else
                log "git-backup" "ERROR: remote URL changed from '$RECORDED_REMOTE' to '$CURRENT_REMOTE' — push aborted (set git_backup.allow_remote_change=true to override)"
            fi
        else
            # Remote matches recorded URL — safe to push.
            _push_and_report
        fi
    fi
) </dev/null >/dev/null 2>&1 &
disown $! 2>/dev/null || true

exit 0
