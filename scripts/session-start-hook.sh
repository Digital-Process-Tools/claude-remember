#!/bin/bash
# ============================================================================
# session-start-hook.sh — SessionStart hook for the Remember plugin
# ============================================================================
#
# DESCRIPTION
#   Runs at the beginning of every Claude Code session. Performs three jobs:
#   1. Injects memory files (identity, core memories, today, now, recent,
#      archive) into the session context via stdout.
#   2. Recovers the most recent missed session by launching save-session.sh
#      with --force in the background.
#   3. Triggers background maintenance: consolidation of past-day staging
#      files and team memory digest refresh.
#   4. Dispatches before_session_start / after_session_start via hooks.d/.
#
# USAGE
#   Called automatically by Claude Code's SessionStart hook system.
#   Not intended for manual invocation.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT   Plugin install directory (set by Claude Code)
#   CLAUDE_PROJECT_DIR   Project root (default: .)
#
# DEPENDENCIES
#   jq (for config.json reading)
#   save-session.sh (for session recovery)
#   run-consolidation.sh (for staging compression)
#   log.sh (for dispatch via hooks.d/)
#
# EXIT CODES
#   0   Always (hook must not block session startup)
#
# OUTPUT
#   Prints memory content to stdout for injection into session context.
#   Sections: === HANDOFF ===, === MEMORY ===, === MEMORY CONSOLIDATION ===
#   hooks.d/ listeners may add their own (e.g., === TEAM ===).
#
# ============================================================================

# resolve-paths.sh exits its caller on failure by default (a caller that keeps
# going with unresolved paths writes memory to the wrong place). This hook is
# documented to never block session startup, so it opts into soft failure and
# handles the status itself, no-oping on any unresolvable root.
#
# This used to claim the nested `claude -p` summarizer would fail here because
# it "has no CLAUDE_PROJECT_DIR". It has one: Claude Code sets it afresh in the
# child from that session's cwd, so resolution SUCCEEDED and the hook ran with
# the temp dir as its project (#204). resolve-paths.sh now stops on the
# REMEMBER_NESTED_SUMMARIZER marker instead, and returns 1 into the `|| exit 0`
# below.
REMEMBER_PATHS_SOFT_FAIL=1 source "$(dirname "$0")/resolve-paths.sh" || exit 0
source "$(dirname "$0")/detect-tools.sh"
source "$(dirname "$0")/bootstrap-dirs.sh"
PLUGIN_ROOT="$PIPELINE_DIR"
PROJECT="$PROJECT_DIR"
source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
# log.sh is sourced with stderr suppressed; a silent failure (e.g. read-only
# mount where log.sh `return 1`s) would leave _remember_date / log / dispatch
# undefined, crashing later with a cryptic `command not found`. Surface a clear
# diagnostic up front. Exit 127 (command-missing) to match the degraded-env
# contract that tolerates rc in (0, 127), not a bare 1.
if ! command -v _remember_date >/dev/null 2>&1; then
    echo "session-start-hook: ERROR — failed to source $PLUGIN_ROOT/scripts/log.sh" >&2
    exit 127
fi
TODAY=$(_remember_date '+%Y-%m-%d')
log "hook" "session-start: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR REMEMBER_DIR=$REMEMBER_DIR"

# Publish what the chain above just resolved, so user-prompt-hook.sh does not
# repeat it on every prompt (#227). Republishing unconditionally here is what
# bounds the staleness of anything the cache cannot detect — a project that
# became a linked git worktree, say — to a single session.
source "$PLUGIN_ROOT/scripts/lib-env-cache.sh"
_remember_env_cache_publish

# ── Dispatch: before_session_start ────────────────────────────────────────
dispatch "before_session_start"

# ── Cleanup + health check ─────────────────────────────────────────────────
rm -f "$REMEMBER_DIR/tmp/save-session.pid"

# ── "Was session X saved?" ────────────────────────────────────────────────
# Hoisted out of the recovery block below because the capture-gap check
# further down asks the same question of the same file, and the two answers
# must not be allowed to drift: a detector that tells you a session "was not
# captured" while the save record says it was captured 72/72 is reporting on
# its own bookkeeping, not on anything you lost (#206).
#
# Ask whether this session was EVER saved, not whether it owns the one
# slot: positions are keyed by session now, and the old equality test
# force-saved an already-saved session whenever another had saved since
# (issue #140). Legacy single-slot files still answer correctly.
# Type-check the value, not just the key: the python readers require an
# int, so `has($id)` alone would call a corrupt {"id": null} entry saved
# while they resume it from 0 — re-summarizing the whole span, which is
# what #140 exists to prevent.
# isinfinite guard: JSON has no infinity literal, but 1e400 overflows to
# one, and floor(infinite) == infinite — so it would read as a line
# number here while python's is_integer() rejects it. jq would then call
# the session saved and the recovery force-save below would never fire,
# losing that session's tail entirely.
LAST_SAVE_FILE="$REMEMBER_DIR/tmp/last-save.json"
SAVED_QUERY='def isline: type == "number" and ((isnan or isinfinite) | not) and . == floor; if (((.sessions // {})[$id]) | isline) or (.session == $id and (.line | isline)) then "saved" else "unsaved" end'

# Args: $1 — session id. Exit 0 if last-save.json records it as saved.
session_was_saved() {
    [ -n "$1" ] && [ -f "$LAST_SAVE_FILE" ] || return 1
    [ "$($JQ -r --arg id "$1" "$SAVED_QUERY" "$LAST_SAVE_FILE" 2>/dev/null)" = "saved" ]
}

# ── Recovery: save the most recent missed session ──────────────────────────
if [ "$(config '.features.recovery' true)" = "true" ]; then
PROJECT_PATH_SLUG="$(session_dir_slug "$PROJECT")"
SESSIONS_DIR="$(claude_projects_dir)/${PROJECT_PATH_SLUG}"

if [ -d "$SESSIONS_DIR" ] && [ -f "$LAST_SAVE_FILE" ]; then
    LAST_JSONL=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null | tail -n +2 | head -1)
    if [ -n "$LAST_JSONL" ]; then
        LAST_ID=$(basename "$LAST_JSONL" .jsonl)
        if ! session_was_saved "$LAST_ID"; then
            "$PLUGIN_ROOT/scripts/save-session.sh" "$LAST_ID" --force </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true
        fi
    fi
fi
fi

# ── Capture-gap detection (#200) ──────────────────────────────────────────
# Claude Code reads hook registrations at session start, so a plugin enabled
# MID-session has none of its hooks wired for that session: PostToolUse never
# fires and capture silently does nothing, for hours, with nothing in the logs
# to say so. The reporter lost a day to it and found only a lone session-start
# line.
#
# It cannot be caught while it is happening. Nothing inside a hook can see
# which hooks are registered — no env var, no file, and `/hooks` is a UI a
# script cannot invoke — and SessionStart's `source` field is only
# startup/resume/clear/compact/fork, so a plugin-enable is indistinguishable
# from a fresh start. Afterwards, though, the signature is exact: a session
# where SessionStart ran and PostToolUse never did.
#
# Judged by IDENTITY: post-tool-hook.sh writes the session id it saw, and if
# there is no record for the previous session's id then PostToolUse never ran
# for it. Comparing mtimes instead failed — bash 3.2's `-nt` works to the
# second, so a healthy session whose first tool call landed inside the same
# second as a stamp was reported as broken.
#
# By MEMBERSHIP, not equality (#206). The first cut stored one id, last-write-
# wins, which answers "which session most recently made a tool call" — a
# different question, and the two come apart the instant anything writes after
# X did. Two real installs found the seam:
#
#   * `/clear` does not mint a new session id. SessionStart fires while the
#     transcript, the id and the .jsonl all stay the same, and by then the
#     CURRENT session has made tool calls, so the slot holds the current id
#     while PREV_ID resolves to a genuinely older session. The mismatch was
#     structurally guaranteed, regardless of capture health — a warning on
#     every /clear, forever. Same for `compact` and `fork`.
#   * A session captured by the NEXT session's recovery block rather than its
#     own live saves. save-session.sh's recovery path never touches
#     capture-alive, so the slot names something else entirely, while
#     last-save.json records the session as fully saved. With the default
#     delta/cooldown thresholds this is the ordinary case for a short session,
#     not an edge case. (Reported independently by ca-sringert on #206.)
#
# Filtering on SessionStart's `source` — the fix the issue proposed — would
# silence the first and not the second, and would leave the store still unable
# to answer the question it is asked. So the store changed shape instead: a
# per-session marker directory, membership-tested. Three sources are accepted
# as evidence of capture, because a false positive here costs more than a
# false negative: this warning has no second chance to be believed, whereas a
# missed gap is still caught by /remember:doctor and, for the content itself,
# by the recovery block directly above.
#
# Deliberately NOT gated on "have we run before". A first cut required a prior
# session-start stamp, to keep a fresh install from being greeted with a
# warning — which sounds right and defeats the entire purpose: during a
# mid-session enable NO hook runs, so no stamp is written, so the one incident
# this exists to report was the exact case it stayed silent for. It could only
# ever have caught a recurrence.
#
# So the question is just "was the previous session captured", and the answer
# is reported whether or not this plugin has run before. A fresh install does
# see it once per project, which is honest: memory really does start here, and
# the wording says so.
CAPTURE_ALIVE="$REMEMBER_DIR/tmp/capture-alive"
CAPTURE_SEEN_DIR="$REMEMBER_DIR/tmp/capture-alive.d"
CAPTURE_REPORTED="$REMEMBER_DIR/tmp/capture-gap-reported"
CAPTURE_SEEN_KEEP=200

SEEN_ID=$(cat "$CAPTURE_ALIVE" 2>/dev/null) || true

# Args: $1 — session id. Exit 0 if anything can vouch for it having been
# captured. Any one source suffices; they fail independently.
capture_was_seen() {
    [ -n "$1" ] || return 1
    # 1. Per-session marker from post-tool-hook.sh — "PostToolUse ran for this
    #    session", written pre-throttle, so it means WIRED, not saved.
    #    Same id check the writer applies: this is a basename off the
    #    transcript dir, and `..` would make `-e` true for every id.
    case "$1" in
        .|..|*[!A-Za-z0-9._-]*) : ;;
        *) [ -e "$CAPTURE_SEEN_DIR/$1" ] && return 0 ;;
    esac
    # 2. The legacy single slot. Kept as evidence rather than dropped so the
    #    first run after an upgrade — old file present, new store empty — is
    #    not itself a false positive. It can still speak for exactly one
    #    session, which is all it ever could.
    [ "$SEEN_ID" = "$1" ] && return 0
    # 3. The save record. Covers the session captured by recovery rather than
    #    by its own live saves, which touches nothing above.
    session_was_saved "$1" && return 0
    return 1
}

# Bounded: one marker per session accumulates in a tmp dir nothing else
# prunes. Newest are kept — those are the only ones the check ever reads.
if [ -d "$CAPTURE_SEEN_DIR" ]; then
    ls -t "$CAPTURE_SEEN_DIR" 2>/dev/null | tail -n "+$((CAPTURE_SEEN_KEEP + 1))" \
    | while IFS= read -r stale; do
        [ -n "$stale" ] && rm -f "$CAPTURE_SEEN_DIR/$stale" 2>/dev/null || true
    done
fi

# The second-newest transcript is the previous session — the same convention
# the recovery block above uses. Guard against the honest zero-tool session
# too: a conversation with no tool calls produces no PostToolUse either, and
# warning about that would be crying wolf.
PREV_SLUG="$(session_dir_slug "$PROJECT")"
PREV_JSONL=$(ls -t "$(claude_projects_dir)/${PREV_SLUG}"/*.jsonl 2>/dev/null | tail -n +2 | head -1)
PREV_ID=""
[ -n "$PREV_JSONL" ] && PREV_ID=$(basename "$PREV_JSONL" .jsonl)

# A gap is a fact about one past session, not a live condition, so it is said
# once. Restarts are frequent and each re-examines the same previous session;
# repeating the same true positive on every one is how it gets tuned out.
REPORTED_ID=$(cat "$CAPTURE_REPORTED" 2>/dev/null) || true

if [ -n "$PREV_ID" ] && [ "$REPORTED_ID" != "$PREV_ID" ] \
   && ! capture_was_seen "$PREV_ID" \
   && grep -q '"tool_use"' "$PREV_JSONL" 2>/dev/null; then
    echo "remember: your previous session was not captured. If you just installed or enabled the plugin, that is expected — capture starts now. Otherwise its hooks were not registered for that session; run /remember:doctor." \
        > "$REMEMBER_DIR/tmp/capture-gap-notice" 2>/dev/null || true
    printf '%s' "$PREV_ID" > "$CAPTURE_REPORTED" 2>/dev/null || true
fi

# ── Identity: per-project → user-global → plugin-bundled ──────────────────
# User-global tier: <REMEMBER_ROOT>/identity.md (external mode only).
# In legacy mode REMEMBER_ROOT == PROJECT_DIR, so we skip it there.
REMEMBER_ROOT=$(dirname "$REMEMBER_DIR")
if [ -f "$REMEMBER_DIR/identity.md" ]; then
    IDENTITY_FILE="$REMEMBER_DIR/identity.md"
elif [ -f "$REMEMBER_ROOT/identity.md" ] && [ "$REMEMBER_ROOT" != "$PROJECT_DIR" ]; then
    IDENTITY_FILE="$REMEMBER_ROOT/identity.md"
else
    IDENTITY_FILE="$PLUGIN_ROOT/identity.md"
fi

CORE_MEMORIES="$REMEMBER_DIR/core-memories.md"
REMEMBER_RECENT="$REMEMBER_DIR/recent.md"
REMEMBER_ARCHIVE="$REMEMBER_DIR/archive.md"
REMEMBER_HANDOFF="$REMEMBER_DIR/remember.md"
REMEMBER_NOW="$REMEMBER_DIR/now.md"
REMEMBER_TODAY_FILE="$REMEMBER_DIR/today-${TODAY}.md"

# ── Handoff path hint (consumed by the /remember skill) ───────────────────
# Emitted only in external mode. In legacy mode REMEMBER_HANDOFF resolves to
# {project}/.remember/remember.md — the exact path the skill defaults to when
# no === HANDOFF === block is present, so the hint would be pure noise.
if [ "$REMEMBER_ROOT" != "$PROJECT_DIR" ]; then
    echo "=== HANDOFF ==="
    echo "Write next handoff to: $REMEMBER_HANDOFF"
    echo ""
fi

# ── Last handoff (injected FIRST so it survives context-preview truncation) ─
# The session-start output can be large; the harness may deliver only a leading
# preview to the agent. Emit the previous session's handoff up top — before
# identity/memory — so it always lands in context.
#
# Delivery is recorded, never destructive (#221). This block used to truncate
# the slot the moment it read it, which is only correct if every session that
# starts will eventually write a handoff back. Plenty do not: a scheduled task
# whose prompt is read-only, a `claude -p` one-shot, a session abandoned before
# /remember. Each of those consumed the note meant for the next human session
# and left a 0-byte file, with nothing anywhere saying so.
#
# Detecting those sessions is not on the table — there is no signal for "this
# one will write a handoff back", and a guess that is wrong in the unsafe
# direction destroys data silently, which is the bug. So nothing is discarded
# until a replacement lands: /remember overwrites this same path, and the new
# content is what retires the old.
#
# The cost of keeping it is that the same note can be delivered more than once,
# and a stale handoff that reads as fresh is the same silent lie in new clothes.
# So a delivery record (fingerprint + first delivery + count) sits beside the
# slot, and any re-delivery of already-delivered content says so out loud.
REMEMBER_HANDOFF_STATE="$REMEMBER_DIR/remember.delivered"

# Content fingerprint for the handoff slot. cksum is POSIX and present
# everywhere this plugin runs, including Git Bash; the size fallback exists so
# a missing cksum degrades to "re-delivery is under-detected", never to a crash
# in a hook documented to never block session startup.
# Args: $1 — file to fingerprint. Prints the fingerprint.
_remember_handoff_fingerprint() {
    if command -v cksum >/dev/null 2>&1; then
        cksum < "$1" | tr ' ' '-'
    else
        wc -c < "$1" | tr -d ' '
    fi
}

if [ -f "$REMEMBER_HANDOFF" ] && [ -s "$REMEMBER_HANDOFF" ]; then
    HANDOFF_FP=$(_remember_handoff_fingerprint "$REMEMBER_HANDOFF")
    PREV_FP=""
    FIRST_DELIVERED=""
    DELIVERIES=0
    if [ -f "$REMEMBER_HANDOFF_STATE" ]; then
        while IFS='=' read -r _hkey _hval; do
            case "$_hkey" in
                fingerprint) PREV_FP="$_hval" ;;
                first_delivered) FIRST_DELIVERED="$_hval" ;;
                deliveries) DELIVERIES="$_hval" ;;
            esac
        done < "$REMEMBER_HANDOFF_STATE"
    fi
    # A hand-edited or half-written record must not turn into an arithmetic
    # error inside the hook.
    case "$DELIVERIES" in ''|*[!0-9]*) DELIVERIES=0 ;; esac

    echo "=== LAST HANDOFF ==="
    if [ -n "$PREV_FP" ] && [ "$HANDOFF_FP" = "$PREV_FP" ]; then
        DELIVERIES=$((DELIVERIES + 1))
        echo "[already delivered ${DELIVERIES} times since ${FIRST_DELIVERED:-an earlier session} — no new handoff has been written since, so this is pending replacement, not news. You may already have acted on it. Running /remember replaces it.]"
    else
        DELIVERIES=1
        FIRST_DELIVERED=$(_remember_date '+%Y-%m-%d %H:%M')
    fi
    cat "$REMEMBER_HANDOFF"
    echo ""
    printf 'fingerprint=%s\nfirst_delivered=%s\ndeliveries=%s\n' \
        "$HANDOFF_FP" "$FIRST_DELIVERED" "$DELIVERIES" \
        > "$REMEMBER_HANDOFF_STATE" 2>/dev/null
elif [ -f "$REMEMBER_HANDOFF_STATE" ]; then
    # Slot emptied by hand (or by an older version of this hook): the record
    # describes content that no longer exists, and keeping it would mislabel a
    # future handoff that happens to fingerprint the same.
    rm -f "$REMEMBER_HANDOFF_STATE"
fi

# ── History hint ───────────────────────────────────────────────────────────
cat "$PLUGIN_ROOT/prompts/session-history-hint.txt" 2>/dev/null
echo ""

# ── Inject memory into context ────────────────────────────────────────────
HAS_MEMORY=""
for MFILE in "$IDENTITY_FILE" "$CORE_MEMORIES" "$REMEMBER_TODAY_FILE" "$REMEMBER_NOW" "$REMEMBER_RECENT" "$REMEMBER_ARCHIVE"; do
    if [ -f "$MFILE" ]; then
        HAS_MEMORY="true"
    fi
done
# Rotated slices are memory too. A store can hold nothing but them — rotate an
# oversized archive and the fresh archive.md stays empty until the next
# consolidation — and gating on the list above meant the whole section was
# skipped, so the one state issue #124 is written to fix printed nothing at all.
ROTATED_ARCHIVES=$(ls "$REMEMBER_DIR"/archive-*.md 2>/dev/null | sort)
if [ -n "$ROTATED_ARCHIVES" ]; then
    HAS_MEMORY="true"
fi

if [ -n "$HAS_MEMORY" ]; then
    echo "=== MEMORY ==="
    for MFILE in "$IDENTITY_FILE" "$CORE_MEMORIES" "$REMEMBER_TODAY_FILE" "$REMEMBER_NOW" "$REMEMBER_RECENT" "$REMEMBER_ARCHIVE"; do
        if [ -f "$MFILE" ] && [ -s "$MFILE" ]; then
            BASENAME=$(basename "$MFILE")
            echo "--- $BASENAME ---"
            cat "$MFILE"
            echo ""
        fi
    done
    # ── Rotated archives: named, not injected (#124) ──────────────────────
    # An oversized archive.md is rotated to archive-YYYY-MM-DD.md and a fresh
    # one started (#123). The bytes are kept, but nothing in the read path
    # ever named them, so that slice of memory sat in cold storage no recall
    # reached — "no memory lost" was true mechanically and false in practice.
    #
    # Named rather than cat'd on purpose: these files were rotated BECAUSE
    # they were too large to fit a prompt, so injecting them would rebuild
    # the problem rotation exists to solve. The agent greps them when a
    # question reaches past what is in context.
    if [ -n "$ROTATED_ARCHIVES" ]; then
        # Newest ROTATED_LIST_MAX by date, because rotations accumulate for the
        # life of a store and this prints on every single session start. The
        # glob is given for the rest so nothing becomes unreachable again —
        # which was the whole point of naming them.
        ROTATED_LIST_MAX=10
        ROTATED_COUNT=$(echo "$ROTATED_ARCHIVES" | wc -l | tr -d ' ')
        # Order by the date and rotation number IN THE NAME, parsed — not by
        # raw name, and not by mtime.
        #
        # Raw name is wrong because a second rotation the same day is
        # archive-DATE-2.md, and '-' (0x2D) sorts before '.' (0x2E), so the
        # later sibling sorts ahead of the base file it followed.
        #
        # mtime looked like the fix and is worse: git checkout writes files in
        # byte-lexicographic order, so cloning the git-backed store (which
        # hooks.d/after_save/50-git-backup.sh exists to make possible) hands
        # archive-DATE-2.md an EARLIER mtime than archive-DATE.md. That
        # reintroduces the same inversion on every restore, for every file,
        # instead of only inside a same-day cluster.
        #
        # The name carries the truth: the date, then the rotation number, with
        # the un-suffixed file being that day's first. Zero-padding the number
        # makes the composed key sort correctly as plain text.
        ROTATED_NEWEST=$(echo "$ROTATED_ARCHIVES" | while read -r _archive; do
            [ -n "$_archive" ] || continue
            _core=${_archive##*/}
            _core=${_core#archive-}
            _core=${_core%.md}
            # Leading '(' on each pattern: bash 3.2 (still what macOS ships)
            # miscounts the parens of a case inside $( ) without it.
            case "$_core" in
                (*-*-*-*) _date=${_core%-*}; _seq=${_core##*-} ;;
                (*)       _date=$_core;      _seq=1 ;;
            esac
            case "$_seq" in (''|*[!0-9]*) _seq=1 ;; esac
            printf '%s-%010d\t%s\n' "$_date" "$_seq" "$_archive"
        done | sort | tail -n "$ROTATED_LIST_MAX" | cut -f2-)
        echo "--- rotated archives (not shown; grep on request) ---"
        echo "$ROTATED_NEWEST" | while read -r _archive; do
            [ -f "$_archive" ] || continue
            printf '%s (%s bytes)\n' "$_archive" "$(wc -c < "$_archive" | tr -d ' ')"
        done
        if [ "$ROTATED_COUNT" -gt "$ROTATED_LIST_MAX" ]; then
            printf '... and %s older: %s/archive-*.md\n' \
                "$((ROTATED_COUNT - ROTATED_LIST_MAX))" "$REMEMBER_DIR"
        fi
        echo ""
    fi
    echo ""
fi

# ── Consolidation trigger ─────────────────────────────────────────────────
# If past-day staging files exist, compress them in the background.
STAGING_COUNT=$(ls "$REMEMBER_DIR/today-"*.md 2>/dev/null | grep -v "today-${TODAY}.md" | grep -v "\.done\.md" | wc -l | tr -d ' ')
if [ "$STAGING_COUNT" -gt 0 ]; then
    echo "=== MEMORY CONSOLIDATION ==="
    echo "$STAGING_COUNT day(s) of memory to compress. Running consolidation in background..."
    nohup "$PLUGIN_ROOT/scripts/run-consolidation.sh" </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true
    echo ""
fi

# ── Dispatch: after_session_start ────────────────────────────────────────
# Plugins register here via hooks.d/after_session_start/
# e.g., team-memory hook injects === TEAM === section
dispatch "after_session_start"
