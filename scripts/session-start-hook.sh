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
# STDIN
#   The SessionStart hook payload, as JSON. Only `session_id` is read, and only
#   to answer "which transcript in this directory is ours" — without it neither
#   recovery nor the capture-gap check can tell the current session from the
#   previous one, and both used to assume the answer from mtime position (#270).
#
#   The read is bounded in TIME and only in time — `read -t 1`, and never from a
#   tty — for the reason post-tool-hook.sh records: a hook that blocks on stdin
#   is not a slow session start, it is one that never starts.
#
#   The hook CONSUMES stdin, so the payload is re-published to hooks.d/
#   listeners around the dispatches, on the same three-state contract
#   post-tool-hook.sh established (#266).
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
#
# Parameter expansion, not three `dirname` forks (#230) — the same pattern
# log.sh and user-prompt-hook.sh already use. SessionStart runs once per session
# rather than per tool call, so this is not the hot path post-tool-hook.sh is;
# it is a startup cost the user waits on, paid for nothing. A path with no slash
# in it leaves the filename behind, not a directory; `dirname` answered "." and
# this must too.
_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="."
REMEMBER_PATHS_SOFT_FAIL=1 source "$_HOOK_DIR/resolve-paths.sh" || exit 0
source "$_HOOK_DIR/detect-tools.sh"
source "$_HOOK_DIR/bootstrap-dirs.sh"
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

# ── Which session is THIS one? (#270) ─────────────────────────────────────
# Both jobs below need to know our own transcript so they can exclude it. They
# used to assume its POSITION instead — "ours is newest, so slot 2 is the
# previous session" — and at source=startup Claude Code creates that transcript
# AFTER this hook has run. For that window the newest file IS the previous
# session, so slot 2 is the one before it, and both jobs act on the wrong
# session: a capture-gap warning about a session old enough to predate the
# evidence store, and a recovery force-save aimed away from the tail it exists
# to rescue.
#
# #206 named the enabler and shipped the other half of the fix: this hook never
# read its stdin, so it had neither `source` nor `session_id` and could not
# exclude itself. Only `session_id` is needed. Excluding our own transcript by
# id is correct at EVERY source, so there is no source list to enumerate and
# nothing that was being reported stops being reported.
#
# Reading stdin is only safe if it cannot wait forever, so this takes both
# guards post-tool-hook.sh documents: a tty stdin (hand invocation from a
# shell) is never read at all, and the read is bounded by `read -t 1`. bash 3.2
# has no sub-second -t, hence 1. This runs once per session rather than once
# per tool call, so the worst case is a second of startup — but it is still
# bounded, because the unbounded version is a session that never begins.
HOOK_STDIN=""
if [ ! -t 0 ]; then
    _line=""
    while IFS= read -r -t 1 _line || [ -n "$_line" ]; do
        HOOK_STDIN="$HOOK_STDIN$_line"
        _line=""
    done
fi

# The same deliberately narrow extractor post-tool-hook.sh uses, and for the
# same reason: the key must be followed by nothing but whitespace and a colon
# before the value's opening quote, so a `session_id` appearing inside some
# other field is not mistaken for the field. It is a heuristic and is treated
# as one — the result is validated as a path component below before anything
# is done with it.
_stdin_session_id() {
    local raw="$1" rest prefix value
    case "$raw" in *'"session_id"'*) ;; *) return 1 ;; esac
    rest=${raw#*\"session_id\"}
    prefix=${rest%%\"*}
    case "$prefix" in *[!:[:space:]]*) return 1 ;; esac
    value=${rest#*\"}
    value=${value%%\"*}
    [ -n "$value" ] || return 1
    printf '%s' "$value"
}

CURRENT_SESSION_ID=$(_stdin_session_id "$HOOK_STDIN" 2>/dev/null) || CURRENT_SESSION_ID=""
# stdin is not more trustworthy than a basename. This is compared against
# names taken off the transcript directory, and `..` would match nothing
# useful while `/` would match across directories, so it faces the same guard
# the basename-derived ids face — at the point of entry, not the point of use.
case "$CURRENT_SESSION_ID" in
    ''|.|..|*[!A-Za-z0-9._-]*) CURRENT_SESSION_ID="" ;;
esac

# ── Publish the consumed payload to hooks.d/ ──────────────────────────────
# This hook now reads stdin, so a listener that wanted the payload would find
# EOF where one used to be. It travels by the route #266 settled on: a file for
# the authoritative copy, the environment for payloads small enough to carry,
# and never PART of a payload — a listener holding a silently shortened one
# cannot tell it from a genuinely short one. Three states, the third disclosed:
#
#   FILE unset                     no payload arrived, or no listener
#   STDIN non-empty                the whole payload
#   STDIN empty and FILE set       too large for the environment; the file
#                                  holds it, complete
#
# Nothing is written at all unless a listener is installed — the shipped
# distribution's before_session_start/ and after_session_start/ hold a
# .gitkeep and, for the git hooks, scripts that never read stdin.
REMEMBER_HOOK_STDIN_MAX=32768
_hook_stdin_file=""

_session_start_listener() {
    local f
    for f in "$REMEMBER_HOOKS_DIR/before_session_start"/* \
             "$REMEMBER_HOOKS_DIR/after_session_start"/*; do
        [ -x "$f" ] && return 0
    done
    return 1
}

if [ -n "$HOOK_STDIN" ] && _session_start_listener; then
    _hook_stdin_file="$REMEMBER_DIR/tmp/session-start-stdin.$$"
    if (umask 077; printf '%s' "$HOOK_STDIN" > "$_hook_stdin_file") 2>/dev/null; then
        export REMEMBER_HOOK_STDIN_FILE="$_hook_stdin_file"
    else
        # A write that failed part way through leaves a short file. Nothing may
        # be told about it, and it may not be left behind either.
        rm -f "$_hook_stdin_file" 2>/dev/null
        _hook_stdin_file=""
    fi
    if [ "${#HOOK_STDIN}" -le "$REMEMBER_HOOK_STDIN_MAX" ]; then
        export REMEMBER_HOOK_STDIN="$HOOK_STDIN"
    else
        export REMEMBER_HOOK_STDIN=""
    fi
fi

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

# ── Which session was the PREVIOUS one? (#270) ────────────────────────────
# Resolved once, here, for the recovery block and the capture-gap check both.
# They ask the same question, and they used to ask it in two places with two
# copies of the same expression — which is exactly how two answers drift apart,
# and how a detector came to report on a different session from the one being
# rescued in the same invocation.
#
# "The newest transcript that is not ours" is correct at every source. At
# startup there is nothing to exclude and the newest genuinely IS the previous
# session. At resume/compact/fork ours exists and sorts newest, so excluding it
# lands on the same file the positional skip did. At `/clear` the id is reused
# and the transcript shared, so excluding by id excludes it there too.
PROJECT_PATH_SLUG="$(session_dir_slug "$PROJECT")"
SESSIONS_DIR="$(claude_projects_dir)/${PROJECT_PATH_SLUG}"

# ── The slug, written down once, for callers that are not bash (#294) ─────
# The slug is a pure function of PROJECT_DIR and PROJECT_DIR does not change
# mid-session, so a caller in another language had no reason to recompute it —
# and no way to ask for it except by sourcing lib-slug.sh in a subshell, once
# per tool call. The reporter of #294 drives this plugin from PowerShell and
# answered that by maintaining a port of session_dir_slug, which is how the
# long-path divergence was found: a second implementation of the one function
# whose disagreements are silent.
#
# So it is written here, where PROJECT_PATH_SLUG already exists two lines
# above. This costs one `mv`; the per-tool-call path is not touched at all,
# which is deliberate and is asserted by
# tests/test_session_slug_record_294.py::test_the_per_tool_call_path_is_not_touched.
#
# In tmp/, with the locks, the cooldown markers and the delivery record: it
# names one machine's session and one machine's absolute paths, and #285 is
# what happens when that kind of state is committed like memory. The git
# backup already excludes the whole directory.
#
# THREE STATES, NOT TWO. An empty slug is not an absence — it resolves to
# ~/.claude/projects/ ITSELF, a directory that exists and holds every
# project's transcripts, so a reader that cannot tell "nothing was written"
# from "the slug is empty" reads someone else's session and never knows. The
# record therefore always carries a status, and never carries a `slug=` key it
# cannot fill: absent means this never ran, status=unavailable means it ran
# and could not answer, status=ok means the value is usable.
#
# NO TIMESTAMP, deliberately. The slug is a pure function of the path, so a
# record written by a long-dead session is still correct; only a record
# written by a DIFFERENT project is wrong, and worktrees make that reachable —
# they share a REMEMBER_DIR with the main checkout (#56) while keeping their
# own PROJECT_DIR, so the last session to start owns this file. `project_dir`
# is the one field that decides whether the record applies to a reader. A
# timestamp beside it would only offer a staleness test that answers the wrong
# question.
_remember_write_slug_record() {
    local _dir="$REMEMBER_DIR/tmp" _tmp
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0
    _tmp="$_dir/session-slug.$$"

    # A newline is a legal byte in a POSIX filename and this record is
    # line-based, so a project path containing one would put `slug=…` inside
    # the value of `project_dir=` — a file that parses cleanly and says
    # something false. Refusing is the only honest answer, and saying which
    # state we are in is the point of the status field.
    local _reason=""
    if [ -z "$PROJECT_PATH_SLUG" ]; then
        _reason="empty-slug"
    else
        case "${PROJECT}${SESSIONS_DIR}${REMEMBER_DIR}" in
            *$'\n'*) _reason="unrepresentable-path" ;;
        esac
    fi

    # Written whole to a private temp name and moved into place, because two
    # worktrees of one repo start sessions against the same tmp/ and a reader
    # must never see half a record. CURRENT_SESSION_ID is already constrained
    # to [A-Za-z0-9._-] where it is read, so it cannot break a line here.
    if [ -n "$_reason" ]; then
        printf 'format=1\nstatus=unavailable\nreason=%s\n' "$_reason" \
            > "$_tmp" 2>/dev/null || { rm -f "$_tmp" 2>/dev/null; return 0; }
    else
        {
            printf 'format=1\n'
            printf 'status=ok\n'
            printf 'project_dir=%s\n' "$PROJECT"
            printf 'slug=%s\n' "$PROJECT_PATH_SLUG"
            printf 'sessions_dir=%s\n' "$SESSIONS_DIR"
            printf 'memory_dir=%s\n' "$REMEMBER_DIR"
            if [ -n "$CURRENT_SESSION_ID" ]; then
                printf 'session_id=%s\n' "$CURRENT_SESSION_ID"
            fi
        } > "$_tmp" 2>/dev/null || { rm -f "$_tmp" 2>/dev/null; return 0; }
    fi

    mv -f "$_tmp" "$_dir/session-slug" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    return 0
}
_remember_write_slug_record

# ── And somewhere a caller can NAME, in the layout we ship (#297) ─────────
# The record above answers "what is the slug", and in the layout
# config.user.example.json ships — "data_dir": "~/.remember/{slug}", under a
# _purpose that says to copy it — it answers from inside a directory the slug
# names. A caller holding project_dir and the template could not open it
# without already knowing what it says. That was written down as one documented
# hole when #296 shipped; it is the recommended configuration, and those are
# the installs most likely to have a non-bash caller in the first place.
#
# NOTHING IS DERIVED FROM project_dir. The alternative shape — a second copy at
# <store_root>/tmp/session-slug-<key from project_dir> — needs a key both sides
# compute the same way, which is a second algorithm over the project path, and
# deleting exactly that is what #294 and #296 were for. A language-neutral
# encoding does not escape it either: base64 of an N-byte path is 4*ceil(N/3),
# so the 260-character paths #294 was about come out at 348 bytes and the
# 300-character vector in tests/slug_vectors.py at 400, past the 255-byte
# filename limit everywhere — and truncating to fit means appending a hash, a
# slug algorithm under a different name. An index matched on project_dir
# verbatim asks the caller to compute nothing at all, and keeps the property
# the reporter's own directory scan has: it cannot answer wrongly, only fail
# to answer.
#
# WRITTEN ONLY WHEN THE SLUG NAMES THE STORE. REMEMBER_STORE_ROOT is empty in
# the legacy layout and in a single-directory external store, where it would
# equal REMEMBER_DIR and duplicate a record that is already reachable — two
# files saying one thing, with a second chance to disagree. The common layout
# pays nothing for the external one, which is also why this is not in
# bootstrap-dirs.sh.
#
# ONE FILE, MANY WRITERS — the difference from the record, which each project
# owns outright. Several projects, and several worktrees of one project, start
# sessions against one store root, and a read-modify-write across them loses
# rows silently. So the rewrite is taken under the plugin's one lock primitive
# and published with mv; a session that cannot take the lock writes no row and
# says nothing, because the per-project record remains authoritative and this
# is a way to find it, not a second source of truth.
#
# NO TIMESTAMPS, for the reason _remember_write_slug_record gives above. A row
# for a directory since deleted can never be MATCHED by a caller holding a live
# project_dir, and if that path is ever recreated the row is still correct — so
# rows are never expired by age, and never pruned by testing whether the
# directory still exists, which would drop correct rows for anything on an
# unmounted share. The only bound is the row cap, and the ordering it drops by
# is position, maintained by this rewrite. That is not a staleness test and no
# reader may use it as one.
#
# AND NO ROW AT ALL for a project_dir this file cannot hold. A newline is legal
# on POSIX and a tab is legal too, and both are structure here. The record can
# take status=unavailable for that case; a row cannot, because it would have to
# be keyed by the very value it is refusing. A caller with such a path finds no
# row and falls back — the same outcome as this never having run, which is the
# honest one.
SLUG_INDEX_LOCK_TIMEOUT=2
SLUG_INDEX_MAX_ROWS=1000
_remember_write_slug_index() {
    [ -n "${REMEMBER_STORE_ROOT:-}" ] || return 0
    [ "$REMEMBER_STORE_ROOT" != "$REMEMBER_DIR" ] || return 0
    [ -n "$PROJECT_PATH_SLUG" ] || return 0

    case "${PROJECT}${REMEMBER_DIR}" in
        *$'\n'*|*$'\t'*) return 0 ;;
    esac

    local _dir="$REMEMBER_STORE_ROOT/tmp"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0

    local _index="$_dir/sessions" _lock="$_dir/sessions.lock" _tmp

    # Sourced here rather than at the top of the file: this is the only caller,
    # and lib-lock.sh probes for fractional sleep at source time — a fork the
    # legacy layout has no reason to pay at every session start.
    source "$_HOOK_DIR/lib-lock.sh" 2>/dev/null || return 0
    command -v lock_acquire >/dev/null 2>&1 || return 0
    lock_acquire "$_lock" "$SLUG_INDEX_LOCK_TIMEOUT" || return 0

    _tmp="$_index.$$"
    {
        printf 'format=1\n'
        if [ -f "$_index" ]; then
            # Our own row dropped — it is re-appended below, so a project that
            # starts twice moves rather than doubles — and the oldest dropped at
            # the cap. A first line that is not ours means a format this version
            # cannot read: exit, print nothing, start the file over, rather than
            # carry rows forward under rules we do not know.
            awk -v self="$PROJECT" -v max="$((SLUG_INDEX_MAX_ROWS - 1))" '
                NR == 1 { if ($0 != "format=1") exit 0; next }
                $0 == "" { next }
                {
                    n = index($0, "\tproject_dir=")
                    if (n == 0) next
                    if (substr($0, n + 13) == self) next
                    rows[++c] = $0
                }
                END {
                    start = (c > max) ? c - max + 1 : 1
                    for (i = start; i <= c; i++) print rows[i]
                }
            ' "$_index" 2>/dev/null
        fi
        # project_dir LAST, and the only field whose value may contain a tab —
        # so a reader splits on the first three and keeps the remainder whole.
        # slug is ASCII by construction, and memory_dir was refused above if it
        # carried one.
        printf 'status=ok\tslug=%s\tmemory_dir=%s\tproject_dir=%s\n' \
            "$PROJECT_PATH_SLUG" "$REMEMBER_DIR" "$PROJECT"
    } > "$_tmp" 2>/dev/null || {
        rm -f "$_tmp" 2>/dev/null
        lock_release "$_lock" 2>/dev/null
        return 0
    }

    mv -f "$_tmp" "$_index" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    lock_release "$_lock" 2>/dev/null
    return 0
}
_remember_write_slug_index

# ── Is this store known by a second spelling? (#298) ──────────────────────
# Git's index is case-sensitive where NTFS is not, so a store can be spelled
# one way on disk and another in the repository that backs it up. That costs
# nothing while it stays on a case-insensitive filesystem — measured on the
# reporter's own Windows box, where both spellings resolve to the same
# directory object — and it splits the store in two on a case-sensitive
# restore, which is the machine least likely to be looking.
#
# Session start, not the per-tool-call path: `session_dir_slug` runs on every
# tool call and #299 pinned that path byte-identical. The whole check costs one
# `git ls-tree` and only for a store that has its own repository; the disk half
# costs no fork at all, and a store in the legacy layout pays nothing because
# the library returns not-applicable before either probe runs.
#
# Disclosure only, and everything downstream keys off ONE fact: whether the
# finding has changed since last session. The record at tmp/case-divergence
# always holds the current answer in all four states, and is rewritten only
# when that answer is different — which is also what makes the steady state
# fork-free. The human-facing notice and the "could not check" log line fire
# on that same change. The condition never clears itself and is harmless
# today, so repeating it every session start would spend the one channel a
# human actually reads (#200) on wallpaper — the argument `_push_and_report`
# makes for its threshold, with the threshold replaced by "say it again only
# when it says something different". `/remember:doctor` re-runs the check live
# and reports every time, which is where someone who suspects a problem looks.
_remember_write_case_divergence() {
    source "$_HOOK_DIR/lib-case-divergence.sh" 2>/dev/null || return 0
    command -v remember_case_divergence >/dev/null 2>&1 || return 0
    remember_case_divergence

    local _dir="$REMEMBER_DIR/tmp" _tmp _old="" _body="" NL=$'\n'

    # The record built as a string first, so it can be compared with what is
    # already on disk. Every field is appended unconditionally or inside an
    # `if` — never `[ -n … ] && …`, which as a group's last command makes a
    # correct write look like a failed one, and cost this file a record it had
    # already produced until the trace said so.
    _body="format=1${NL}status=$REMEMBER_CASE_STATUS"
    if [ "$REMEMBER_CASE_STATUS" != "not-applicable" ]; then
        _body="$_body${NL}resolved=$REMEMBER_CASE_RESOLVED"
        _body="$_body${NL}store_root=$REMEMBER_CASE_ROOT"
        _body="$_body${NL}disk_state=$REMEMBER_CASE_DISK_STATE"
        if [ -n "$REMEMBER_CASE_DISK_REASON" ]; then
            _body="$_body${NL}disk_reason=$REMEMBER_CASE_DISK_REASON"
        fi
        if [ -n "$REMEMBER_CASE_DISK_NAMES" ]; then
            _body="$_body${NL}disk_names=$REMEMBER_CASE_DISK_NAMES"
        fi
        _body="$_body${NL}git_state=$REMEMBER_CASE_GIT_STATE"
        if [ -n "$REMEMBER_CASE_GIT_REASON" ]; then
            _body="$_body${NL}git_reason=$REMEMBER_CASE_GIT_REASON"
        fi
        if [ -n "$REMEMBER_CASE_GIT_NAMES" ]; then
            _body="$_body${NL}git_names=$REMEMBER_CASE_GIT_NAMES"
        fi
    fi

    # Read what is already there BEFORE deciding anything: it answers both
    # "has the finding changed" (which is what the human-facing notice fires
    # on) and "is there anything to write at all". Read with the shell — a
    # `grep | tr` here was two forks on a path whose whole budget is one
    # `git ls-tree`, and in the legacy layout, where this check is
    # not-applicable and its record never changes, it was two forks for a file
    # that already said the right thing.
    if [ -f "$_dir/case-divergence" ]; then
        local _pline
        while IFS= read -r _pline; do
            _old="${_old:+$_old$NL}$_pline"
        done < "$_dir/case-divergence"
    fi

    if [ "$_old" != "$_body" ]; then
        [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0
        _tmp="$_dir/case-divergence.$$"
        printf '%s\n' "$_body" > "$_tmp" 2>/dev/null \
            || { rm -f "$_tmp" 2>/dev/null; return 0; }
        mv -f "$_tmp" "$_dir/case-divergence" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    fi

    case "$REMEMBER_CASE_STATUS" in
        diverged)
            log "case-divergence" "$REMEMBER_CASE_MESSAGE"
            # An unchanged record is an unchanged finding, and the human has
            # already been told. Saying it again every session start would
            # spend the one channel they actually read on a condition that is
            # harmless today and never clears itself.
            [ "$_old" = "$_body" ] && return 0
            printf '%s\n' "$REMEMBER_CASE_MESSAGE" \
                > "$_dir/case-divergence-notice" 2>/dev/null || true
            ;;
        unavailable)
            # Logged on change only. The commonest reason by far is
            # `not-a-repository` — an external store nobody has pointed a git
            # backup at — and that is a standing condition, not an event: one
            # identical line per session for the life of the install is the
            # wallpaper #252's five weeks of identical daily lines proved
            # nobody reads. It is still never rendered as agreement anywhere
            # that reports it; `/remember:doctor` says it every time.
            [ "$_old" = "$_body" ] && return 0
            log "case-divergence" "could not check whether this store is known by a second spelling (disk=$REMEMBER_CASE_DISK_STATE${REMEMBER_CASE_DISK_REASON:+/$REMEMBER_CASE_DISK_REASON} git=$REMEMBER_CASE_GIT_STATE${REMEMBER_CASE_GIT_REASON:+/$REMEMBER_CASE_GIT_REASON}) — this is not a report that they agree"
            ;;
    esac
    return 0
}
_remember_write_case_divergence

# Args: $1 — sessions dir. Prints the newest transcript that is not this
# session's, or nothing.
previous_transcript() {
    ls -t "$1"/*.jsonl 2>/dev/null | while IFS= read -r f; do
        base=${f##*/}
        base=${base%.jsonl}
        [ "$base" = "$CURRENT_SESSION_ID" ] && continue
        printf '%s\n' "$f"
        break
    done
}

if [ -n "$CURRENT_SESSION_ID" ]; then
    PREV_JSONL=$(previous_transcript "$SESSIONS_DIR")
else
    # No id, so "not ours" has no meaning and there is no right answer to
    # substitute — the positional guess is correct at resume and wrong at
    # startup, and nothing here can tell which. Recovery keeps it unchanged
    # rather than trading one guess for another: its failure mode is a save
    # aimed at the wrong session, which the next startup can still correct.
    # The capture-gap check gets no such fallback, because its failure mode is
    # an accusation — see below.
    PREV_JSONL=$(ls -t "$SESSIONS_DIR"/*.jsonl 2>/dev/null | tail -n +2 | head -1)
fi
PREV_ID=""
if [ -n "$PREV_JSONL" ]; then
    PREV_ID=${PREV_JSONL##*/}
    PREV_ID=${PREV_ID%.jsonl}
fi

# Asked ONCE, and before recovery forks (#270). Recovery force-saves in the
# background and the capture-gap check below re-read this same file through
# session_was_saved — so a session being rescued could read as unsaved in the
# very invocation that was rescuing it, or as saved, depending on which process
# won. The question is "was this captured", not "has that fork finished yet",
# and the answer to it does not change while this hook runs.
PREV_WAS_SAVED="no"
if [ -n "$PREV_ID" ] && session_was_saved "$PREV_ID"; then
    PREV_WAS_SAVED="yes"
fi

# ── Recovery: save the most recent missed session ──────────────────────────
if [ "$(config '.features.recovery' true)" = "true" ]; then
if [ -d "$SESSIONS_DIR" ] && [ -f "$LAST_SAVE_FILE" ] && [ -n "$PREV_ID" ]; then
    if [ "$PREV_WAS_SAVED" = "no" ]; then
        "$PLUGIN_ROOT/scripts/save-session.sh" "$PREV_ID" --force </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true
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
    #    by its own live saves, which touches nothing above. Read as it stood
    #    when this hook started, BEFORE the recovery fork above (#270) — asking
    #    again here would be asking a file that a background save is rewriting,
    #    and answering "has the rescue finished" instead of "was it captured".
    [ "$1" = "$PREV_ID" ] && [ "$PREV_WAS_SAVED" = "yes" ] && return 0
    [ "$1" != "$PREV_ID" ] && session_was_saved "$1" && return 0
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

# PREV_ID and PREV_JSONL were resolved once, above, for this check and for
# recovery both. Guard against the honest zero-tool session too: a conversation
# with no tool calls produces no PostToolUse either, and warning about that
# would be crying wolf.
CAPTURE_SKIPPED="$REMEMBER_DIR/tmp/capture-gap-skipped"

if [ -z "$CURRENT_SESSION_ID" ]; then
    # Without a session id this check cannot tell our own transcript from the
    # previous session's, and its output is an accusation. A missed gap is
    # still caught by /remember:doctor and, for the content itself, by the
    # recovery block above; a false one accuses a healthy install and spends
    # the credibility this warning needs the one time it is true. It has no
    # second chance to be believed — the same asymmetry that made three
    # independent sources count as evidence of capture rather than one.
    #
    # Silence is a positive claim too, so it is disclosed rather than assumed.
    # "No warning" must not mean both "the previous session was captured" and
    # "the question was never asked": an absence the checker produced, read as
    # an absence in the world, is the defect class this repo keeps filing on
    # (#144, #263, #266). doctor reports the marker.
    printf '%s\n' "the SessionStart payload carried no usable session_id" \
        > "$CAPTURE_SKIPPED" 2>/dev/null || true
    log "hook" "session-start: capture-gap check skipped — no session_id on stdin"
else
    rm -f "$CAPTURE_SKIPPED" 2>/dev/null || true

    # A gap is a fact about one past session, not a live condition, so it is
    # said once. Restarts are frequent and each re-examines the same previous
    # session; repeating the same true positive on every one is how it gets
    # tuned out. (This dedupe was structurally unable to help while the id was
    # wrong: a wrong id changes on every startup, so every restart minted a
    # fresh unreported id and warned again.)
    REPORTED_ID=$(cat "$CAPTURE_REPORTED" 2>/dev/null) || true

    if [ -n "$PREV_ID" ] && [ "$REPORTED_ID" != "$PREV_ID" ] \
       && ! capture_was_seen "$PREV_ID" \
       && grep -q '"tool_use"' "$PREV_JSONL" 2>/dev/null; then
        echo "remember: your previous session was not captured. If you just installed or enabled the plugin, that is expected — capture starts now. Otherwise its hooks were not registered for that session; run /remember:doctor." \
            > "$REMEMBER_DIR/tmp/capture-gap-notice" 2>/dev/null || true
        printf '%s' "$PREV_ID" > "$CAPTURE_REPORTED" 2>/dev/null || true
    fi
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
#
# The record is PER-CLONE and lives in tmp/, with the locks and the cooldown
# markers — not beside the memory, where the git backup committed it like any
# other file (#285). The fingerprint is a cksum of the handoff CONTENT, so it
# matches on every machine that has that handoff, and a second machine's FIRST
# session was therefore told the note had already been delivered N times since
# another machine's clock. The record that exists to stop a stale handoff
# reading as fresh instead made a fresh one read as stale: the same lie
# inverted, and in the direction that suppresses action rather than duplicating
# it. Over-delivery is the safe direction and is already what #221 chose.
#
# There was no shared value it could have held instead. Two of the three fields
# are per-clone by construction — a wall clock and a session count — and only
# the fingerprint is globally meaningful, which is exactly the field that
# carried the harm. Sharing it also conflicted on every concurrent session
# start, and no merge driver repairs that: union emits both key sets and yields
# a malformed record, so this is the one file in the store where "keep both
# sides" is wrong.
REMEMBER_HANDOFF_STATE="$REMEMBER_DIR/tmp/remember.delivered"

# Carry an existing record to its new home rather than resetting it — this
# machine's delivery history is still true about this machine.
#
# The MOVE is also what retires the tracked copy. An ignore rule does nothing to
# a file git already tracks, and a `git rm --cached` whose path still exists in
# the working tree is undone by the very next path-limited commit, which takes
# its content from the working tree. With the old path gone, the backup's
# ordinary add/commit stages the deletion like any other, and the remote learns
# it once.
#
# A record arriving from a pull is DISCARDED, never adopted: it describes some
# other machine's sessions, and it is the reason this issue exists.
_REMEMBER_HANDOFF_STATE_LEGACY="$REMEMBER_DIR/remember.delivered"
if [ -f "$_REMEMBER_HANDOFF_STATE_LEGACY" ]; then
    if [ -f "$REMEMBER_HANDOFF_STATE" ]; then
        rm -f "$_REMEMBER_HANDOFF_STATE_LEGACY" 2>/dev/null
    else
        mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null
        mv "$_REMEMBER_HANDOFF_STATE_LEGACY" "$REMEMBER_HANDOFF_STATE" 2>/dev/null \
            || rm -f "$_REMEMBER_HANDOFF_STATE_LEGACY" 2>/dev/null
    fi
fi

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

# The payload file does not outlive the dispatches it was published for.
[ -n "$_hook_stdin_file" ] && rm -f "$_hook_stdin_file" 2>/dev/null

# Explicit, because the line above is the last command and it is false whenever
# no payload file was written — the common case. Falling off the end would exit
# 1 from a hook documented to always exit 0.
exit 0
