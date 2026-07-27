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
# handles the status itself. Most likely to fail here for a NESTED `claude -p`
# session (e.g. the remember pipeline's own Haiku call): it has no
# CLAUDE_PROJECT_DIR and isn't a real project, so just no-op.
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

# ── Dispatch: before_session_start ────────────────────────────────────────
dispatch "before_session_start"

# ── Cleanup + health check ─────────────────────────────────────────────────
rm -f "$REMEMBER_DIR/tmp/save-session.pid"

# ── Recovery: save the most recent missed session ──────────────────────────
if [ "$(config '.features.recovery' true)" = "true" ]; then
PROJECT_PATH_SLUG="$(session_dir_slug "$PROJECT")"
SESSIONS_DIR="$(claude_projects_dir)/${PROJECT_PATH_SLUG}"
LAST_SAVE_FILE="$REMEMBER_DIR/tmp/last-save.json"

if [ -d "$SESSIONS_DIR" ] && [ -f "$LAST_SAVE_FILE" ]; then
    LAST_JSONL=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null | tail -n +2 | head -1)
    if [ -n "$LAST_JSONL" ]; then
        LAST_ID=$(basename "$LAST_JSONL" .jsonl)
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
        SAVED_QUERY='def isline: type == "number" and ((isnan or isinfinite) | not) and . == floor; if (((.sessions // {})[$id]) | isline) or (.session == $id and (.line | isline)) then "saved" else "unsaved" end'
        SAVED_STATE=$($JQ -r --arg id "$LAST_ID" "$SAVED_QUERY" "$LAST_SAVE_FILE" 2>/dev/null)
        if [ "$SAVED_STATE" != "saved" ]; then
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
# Judged by IDENTITY: post-tool-hook.sh writes the session id it saw into
# capture-alive, and if that is not the previous session's id then PostToolUse
# never ran for it. Comparing mtimes instead failed — bash 3.2's `-nt` works to
# the second, so a healthy session whose first tool call landed inside the same
# second as a stamp was reported as broken.
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

# The second-newest transcript is the previous session — the same convention
# the recovery block above uses. Guard against the honest zero-tool session
# too: a conversation with no tool calls produces no PostToolUse either, and
# warning about that would be crying wolf.
PREV_SLUG="$(session_dir_slug "$PROJECT")"
PREV_JSONL=$(ls -t "$(claude_projects_dir)/${PREV_SLUG}"/*.jsonl 2>/dev/null | tail -n +2 | head -1)
PREV_ID=""
[ -n "$PREV_JSONL" ] && PREV_ID=$(basename "$PREV_JSONL" .jsonl)
SEEN_ID=$(cat "$CAPTURE_ALIVE" 2>/dev/null) || true
if [ -n "$PREV_ID" ] && [ "$SEEN_ID" != "$PREV_ID" ] \
   && grep -q '"tool_use"' "$PREV_JSONL" 2>/dev/null; then
    echo "remember: your previous session was not captured. If you just installed or enabled the plugin, that is expected — capture starts now. Otherwise its hooks were not registered for that session; run /remember:doctor." \
        > "$REMEMBER_DIR/tmp/capture-gap-notice" 2>/dev/null || true
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
# identity/memory — so it always lands in context. Read once, then consume.
if [ -f "$REMEMBER_HANDOFF" ] && [ -s "$REMEMBER_HANDOFF" ]; then
    echo "=== LAST HANDOFF ==="
    cat "$REMEMBER_HANDOFF"
    echo ""
    : > "$REMEMBER_HANDOFF"
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
