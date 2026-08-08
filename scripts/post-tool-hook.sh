#!/bin/bash
# ============================================================================
# post-tool-hook.sh — PostToolUse hook for the Remember plugin
# ============================================================================
#
# DESCRIPTION
#   Fires after every Claude Code tool call. Counts new JSONL lines since the
#   last save, and when the delta exceeds a threshold (default 50 lines),
#   launches save-session.sh in the background. Also outputs a team memory
#   nudge as additionalContext to remind the agent to log team-worthy knowledge.
#
#   Which session it is recording for comes from the `session_id` on stdin,
#   NOT from the newest transcript in the session dir (#212) — with two
#   sessions live in one project those are different answers, and the wrong one
#   writes a durable marker vouching for a session that did no work. The
#   transcript that is measured is resolved from that same id, because the
#   line count is compared against a position keyed by it. See the block above
#   CURRENT_LINES.
#
# USAGE
#   Called automatically by Claude Code's PostToolUse hook system.
#   Not intended for manual invocation.
#
# STDIN
#   The PostToolUse JSON payload. It embeds the full tool result, so it is
#   UNBOUNDED IN SIZE — hundreds of KB after a large Read or a verbose Grep.
#   Consumed here, and published to hooks.d/after_post_tool scripts at the
#   bottom of this file. It is deliberately not exported for the body of this
#   hook to carry; the block above the publish says why (#266).
#
#   The read is bounded in TIME and only in time — `read -t 1`, and never from
#   a tty, because this fires on every tool call and a blocking read is a hung
#   agent rather than a slow one. Nothing here caps how MUCH is read. The size
#   question is answered at the publish, not at the read; do not take the word
#   "bounded" in this section as an answer to it.
#
#   Anything unusable falls back to the pre-#212 mtime behaviour rather than
#   failing.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT   Plugin install directory (set by Claude Code)
#   CLAUDE_PROJECT_DIR   Project root (default: .)
#
# DEPENDENCIES
#   python3 (for JSON parsing of last-save.json)
#   jq (for reading config.json threshold)
#   save-session.sh (launched in background when threshold met)
#
# EXIT CODES
#   0   Always (hook must not block the agent)
#
# ============================================================================

# --- Resolve paths ---
# Opt into resolve-paths.sh's soft-failure mode — see the comment in
# session-start-hook.sh. This hook must never block the agent, so a resolution
# failure (e.g. a nested/headless session with no CLAUDE_PROJECT_DIR) is a
# silent no-op, not a crash.
#
# Resolved by parameter expansion rather than three `dirname` forks (#230) —
# the same pattern log.sh and user-prompt-hook.sh already use. A path with no
# slash in it (invoked as `bash post-tool-hook.sh` from the scripts dir) leaves
# the filename behind, not a directory; `dirname` answered "." and this must too.
_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="."
REMEMBER_PATHS_SOFT_FAIL=1 source "$_HOOK_DIR/resolve-paths.sh" || exit 0
source "$_HOOK_DIR/detect-tools.sh"
source "$_HOOK_DIR/bootstrap-dirs.sh"
PLUGIN_ROOT="$PIPELINE_DIR"
PROJECT="$PROJECT_DIR"
source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
log "hook" "post-tool: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR PYTHON=$PYTHON REMEMBER_DIR=$REMEMBER_DIR"
# "PostToolUse ran at all" (#200) — written here, before every early exit
# below, because the question the doctor asks is whether this hook is WIRED,
# and the answers to that and to "did it find a transcript" are different.
# Putting it after the transcript check made it unwritable in exactly the
# slug-mismatch case (#144) it most needed to distinguish, so the doctor told
# those users the hook had never fired and to restart Claude Code — advice
# that does nothing for a wrong slug.
: > "$REMEMBER_DIR/tmp/post-tool-ran" 2>/dev/null || true

# --- Which session is this invocation FOR? (#212) ---
# PostToolUse supplies the answer on stdin. Read it here, once, before anything
# else wants it.
#
# Into a plain shell variable, NOT an exported one (#266). Everything below
# this line forks, and an exported string the size of a tool result makes every
# one of those forks fail. This hook does consume stdin, so a
# hooks.d/after_post_tool script that wanted the payload would find EOF — it
# still gets it, by a route that is not the environment, at the bottom of this
# file.
#
# Reading stdin is only safe if it cannot wait forever — this runs on EVERY
# tool call, so a blocking read is a hung agent, not a slow one. Two guards:
# a tty stdin (hand invocation from a shell) is never read at all, and the read
# itself is bounded by `-t 1` so a pipe held open with nothing in it costs a
# second and then gives up. bash 3.2 has no sub-second -t, hence 1.
# Cleared, not merely left alone. This plugin can re-enter its own hooks from a
# nested session (#204), and both names are exported at the bottom of this file
# — so without this an inner invocation could read an OUTER invocation's
# payload and take it for its own. Absent is the correct answer when this
# invocation has no payload; a stale one is the plausible-and-wrong answer.
unset REMEMBER_HOOK_STDIN REMEMBER_HOOK_STDIN_FILE

HOOK_STDIN=""
if [ ! -t 0 ]; then
    _line=""
    while IFS= read -r -t 1 _line || [ -n "$_line" ]; do
        HOOK_STDIN="$HOOK_STDIN$_line"
        _line=""
    done
fi

# Extract "session_id" without forking a JSON parser on every tool call.
# Deliberately narrow: the key must be followed by nothing but whitespace and a
# colon before the value's opening quote, so a `session_id` appearing inside
# tool_input (a Grep pattern, a file being read) is not mistaken for the field.
# It is a heuristic and it is treated as one — the caller validates the result
# as a path component AND requires it to name a transcript that exists here
# before anything is done with it.
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

STDIN_SESSION_ID=$(_stdin_session_id "$HOOK_STDIN" 2>/dev/null) || STDIN_SESSION_ID=""
# stdin is not more trustworthy than a basename. The id becomes both a path
# component under capture-alive.d/ and a transcript filename, so it faces the
# same guard the basename-derived id has always faced, at the point of entry.
case "$STDIN_SESSION_ID" in
    ''|.|..|*[!A-Za-z0-9._-]*) STDIN_SESSION_ID="" ;;
esac

SAVE_SCRIPT="$PLUGIN_ROOT/scripts/save-session.sh"
LAST_SAVE_FILE="$REMEMBER_DIR/tmp/last-save.json"
PID_FILE="$REMEMBER_DIR/tmp/save-session.pid"
SESSION_DIR="$(claude_projects_dir)/$(session_dir_slug "$PROJECT")"

[ -f "$SAVE_SCRIPT" ] || exit 0

# --- Count JSONL lines in the current session ---
# A slug that does not match the directory Claude Code actually created leaves
# nothing to read here, and the whole pipeline no-ops for the life of the
# session. That used to be a bare `exit 0`, so the only symptom was memory
# quietly never appearing (issue #144).
#
# Say so instead — but on a timer, not once ever. This runs on every tool call,
# so a line per call would bury the logs; a plain once-only sentinel is worse
# though, because the cause here is environmental (a mis-slugged path stays
# mis-slugged), so the warning would fire once and every later session would be
# silent again — the very failure being fixed. An hourly repeat keeps a
# persistent cause visible without flooding.
#
# `ls -t … | head -1` is two processes and it STAYS two processes (#230). The
# obvious replacement — glob the directory and keep the newest with `[ "$f" -nt
# "$newest" ]` — is spawn-free and WRONG here: under bash 3.2 (stock macOS)
# `-nt` compares mtimes at ONE-SECOND granularity, the same trap recorded below
# the capture-alive marker. Two transcripts written in the same second tie, and
# a tie makes the loop keep whichever the glob listed first — alphabetical order,
# not newest. `ls -t` reads the full mtime and does not tie.
#
# The value picked here decides which session this invocation is recorded FOR
# whenever stdin does not answer (#212). Trading a correct answer for two
# processes on the WRITE path is not a trade; misattributing a tool call is a
# bug, and this repo has twice turned that bug into an outage (#204, #129).
NOTICE_TTL=3600
LATEST_JSONL=$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)
if [ -z "$LATEST_JSONL" ]; then
    NOTICE_MARKER="$REMEMBER_DIR/tmp/no-transcript-notice"
    NOTICE_LAST=0
    if [ -f "$NOTICE_MARKER" ]; then
        # `read`, not `cat` (#230). Status untested on purpose: `read` returns 1
        # on a file with no trailing newline having set the variable anyway, and
        # a timestamp written with `printf` is exactly that file. The case guard
        # below is what decides whether the value is usable.
        read -r NOTICE_LAST < "$NOTICE_MARKER" 2>/dev/null
        case "$NOTICE_LAST" in ''|*[!0-9]*) NOTICE_LAST=0 ;; esac
    fi
    # 10# after the case, never instead of it (#322): "08"/"09" are all digits,
    # so they clear the guard and are then read as octal. Bash abandons the rest
    # of this if body at that point -- including the `exit 0` below -- so the
    # notice is never sent and the line under `fi` deletes the marker instead.
    # 10# on an empty string is itself an error on bash 5, which is why the case
    # stays in front.
    if [ $(( $(_remember_date +%s) - 10#$NOTICE_LAST )) -ge "$NOTICE_TTL" ]; then
        mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null
        _remember_date +%s > "$NOTICE_MARKER" 2>/dev/null
        if [ -d "$SESSION_DIR" ]; then
            log "hook" "no .jsonl transcript in $SESSION_DIR — nothing to save yet"
        else
            log "hook" "WARNING: no session dir for this project: $SESSION_DIR (slug of $PROJECT). Memory cannot save until it matches a directory under $(claude_projects_dir)/"
        fi
    fi
    exit 0
fi
# Gated on the marker existing (#230): this line runs on every tool call, and
# the file it clears is written only in the slug-mismatch case above — which is
# to say, essentially never. `rm` on a path that is not there is a process spent
# to do nothing.
[ -f "$REMEMBER_DIR/tmp/no-transcript-notice" ] && \
    rm -f "$REMEMBER_DIR/tmp/no-transcript-notice" 2>/dev/null

# --- One transcript, one session, one answer (#212) ---
# Everything below is about ONE session: the marker vouches for it, the saved
# position is keyed by it, and save-session.sh is handed it by id. The line
# count is compared against that position, so it has to count THAT session's
# transcript — "the newest file in the directory" was only ever a stand-in for
# it, and with two sessions live in one project the stand-in names the wrong
# one. Resolve the transcript from the session id and both uses agree by
# construction.
#
# The mtime pick above is kept as the fallback, unchanged, and is still what
# decides whether there is anything here to save at all. If stdin did not
# answer — an older CLI, a different invocation path, a test harness — or if it
# named a session with no transcript in this project, the hook records exactly
# what it recorded before this change. Misattributing a tool call is a bug;
# capturing nothing is an outage, and this repo has twice traded the first for
# the second (#204, #129).
TRANSCRIPT="$LATEST_JSONL"
if [ -n "$STDIN_SESSION_ID" ] && [ -f "$SESSION_DIR/$STDIN_SESSION_ID.jsonl" ]; then
    TRANSCRIPT="$SESSION_DIR/$STDIN_SESSION_ID.jsonl"
else
    [ -n "$STDIN_SESSION_ID" ] && log "hook" "post-tool: stdin session $STDIN_SESSION_ID has no transcript in $SESSION_DIR — falling back to newest"
fi

# `wc -l` on BSD pads its output with leading spaces, which is why `tr -d ' '`
# was here. Arithmetic expansion strips whitespace on its own, so the pipe's
# second process goes (#230) — and a `wc` that printed nothing usable now yields
# 0 rather than the empty string, which every comparison below already treated
# as 0 anyway.
CURRENT_LINES=$(wc -l < "$TRANSCRIPT" 2>/dev/null)
CURRENT_LINES=$(( CURRENT_LINES + 0 ))
# Parameter expansion, not `basename` (#230): strip the directory, then the
# extension. Same answer, no process.
SESSION_ID="${TRANSCRIPT##*/}"
SESSION_ID="${SESSION_ID%.jsonl}"

# Which session PostToolUse serviced, for the capture-gap check in
# session-start-hook.sh (#200). Distinct from the post-tool-ran marker above:
# that one answers "is this hook wired", this one answers "for which session",
# and it can only be written once a transcript has actually been found.
#
# Before any throttle or early return BELOW this line: the question is "did it
# run for this session", not "did it save".
#
# The SESSION ID, not a timestamp. `-nt` compares mtimes at one-second
# granularity under bash 3.2, so a sign of life landing in the same second as
# the session-start stamp read as "not newer" and reported a healthy session as
# broken. Identity has no clock to lose to.
# Written via rename, the same lesson last-save.json learned in #140: a plain
# redirect truncates first, so a process killed between truncate and write
# leaves an EMPTY marker — which matches no session id, and the next
# SessionStart reports a healthy session as a gap.
#
# ONE MARKER PER SESSION, not one slot shared by all of them (#206). A single
# slot answers "which session most recently made a tool call" — a different
# question from "was session X captured", and the moment anything writes after
# X the answer for X is gone. Not hypothetical: after a `/clear` the CURRENT
# session's own tool calls overwrite it while SessionStart still has the
# previous session to judge, so every healthy session read as broken, once per
# /clear, forever.
#
# The single file is still written. doctor.sh reads it, and it is the only
# evidence an install upgrading from the old format has for the session that
# straddles the upgrade.
# `[ -d ]` before the `mkdir` (#230). Every tool call after the first in a
# session finds this directory already there, and asking `mkdir -p` to confirm
# that costs a process each time. The mkdir still runs — and still gates the
# write on its success — the first time, and on any run where it is missing.
if [ -d "$REMEMBER_DIR/tmp/capture-alive.d" ] \
    || mkdir -p "$REMEMBER_DIR/tmp/capture-alive.d" 2>/dev/null; then
    # The id becomes a path component, so it is checked rather than trusted:
    # it comes from a filename in the transcript dir, and `..` or a slash
    # would escape the store. Real session ids are UUIDs.
    case "$SESSION_ID" in
        ''|.|..|*[!A-Za-z0-9._-]*) : ;;
        *) : > "$REMEMBER_DIR/tmp/capture-alive.d/$SESSION_ID" 2>/dev/null || true ;;
    esac
fi
if printf '%s' "$SESSION_ID" > "$REMEMBER_DIR/tmp/capture-alive.$$" 2>/dev/null; then
    mv -f "$REMEMBER_DIR/tmp/capture-alive.$$" "$REMEMBER_DIR/tmp/capture-alive" 2>/dev/null \
        || rm -f "$REMEMBER_DIR/tmp/capture-alive.$$" 2>/dev/null || true
fi

# --- Get last saved position (from last-save.json) ---
# Positions are keyed by session (issue #140), so ask for THIS session rather
# than whether it happens to own the one slot — two live sessions used to
# overwrite each other and re-summarize whole spans as duplicates.
#
# Delegated to pipeline.shell rather than parsed here. This hook used to carry
# its own json reader, and it drifted: every other reader learned that a bool
# is not a position and an integral float is, while this one kept a bare
# isinstance check and reported 0 for positions the rest of the pipeline
# resumed from, defeating the delta throttle for the life of a session.
LAST_LINE=0
if [ -f "$LAST_SAVE_FILE" ]; then
    LAST_LINE=$(cd "$PIPELINE_DIR" && $PYTHON -m pipeline.shell read-position "$LAST_SAVE_FILE" "$SESSION_ID" 2>/dev/null)
    case "$LAST_LINE" in ''|*[!0-9]*) LAST_LINE=0 ;; esac
fi

DELTA=$((CURRENT_LINES - LAST_LINE))
SAVE_TRIGGERED=""

# --- Don't fork a save that save-session.sh will only discard on cooldown ---
# The delta throttle above keys on save *position* (last-save.json), which is
# only written after a successful save. Until the first save lands LAST_LINE is
# 0, so DELTA is the whole transcript and always clears the threshold — and in
# an agentic session (many tool calls, few human turns) the min-human gate keeps
# that first save from ever landing, so the hook would fork a save-session.sh on
# every tool call for the whole session (issue #125). save-session.sh already
# rate-limits on the last-save-ts cooldown marker; consult it here so the fork
# is skipped entirely instead of spawned only to be discarded milliseconds later.
IN_COOLDOWN=false
COOLDOWN_MARKER="$REMEMBER_DIR/tmp/last-save-ts"
if [ -f "$COOLDOWN_MARKER" ]; then
    # `read`, not `cat` (#230) — see the note on NOTICE_LAST above. This one is
    # on the hot path proper: the cooldown marker exists for the whole of any
    # session that has saved once, so this ran on every tool call.
    LAST_TS=0
    read -r LAST_TS < "$COOLDOWN_MARKER" 2>/dev/null
    case "$LAST_TS" in ''|*[!0-9]*) LAST_TS=0 ;; esac
    SAVE_COOLDOWN=$(config ".cooldowns.save_seconds" 120)
    case "$SAVE_COOLDOWN" in ''|*[!0-9]*) SAVE_COOLDOWN=120 ;; esac
    # 10# for the same reason as the notice marker above (#322). Only LAST_TS
    # needs it: SAVE_COOLDOWN is the right-hand operand of `[ ... -lt ... ]`,
    # and `test` parses base 10 without evaluating -- measured, `[ 9 -lt 010 ]`
    # is true. Marking it too would advertise a gap that is not there.
    [ $(( $(_remember_date +%s) - 10#$LAST_TS )) -lt "$SAVE_COOLDOWN" ] && IN_COOLDOWN=true
fi

# --- Fire save if delta exceeds threshold and no save already running ---
DELTA_THRESHOLD=$(config ".thresholds.delta_lines_trigger" 50)
if [ "$DELTA" -gt "$DELTA_THRESHOLD" ] && [ "$IN_COOLDOWN" = false ]; then
    ALREADY_RUNNING=false
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
        if kill -0 "$OLD_PID" 2>/dev/null; then
            ALREADY_RUNNING=true
        fi
    fi

    if [ "$ALREADY_RUNNING" = false ]; then
        mkdir -p "$REMEMBER_DIR/logs/autonomous"
        nohup "$SAVE_SCRIPT" "$SESSION_ID" > "$REMEMBER_DIR/logs/autonomous/save-$(_remember_date +%H%M%S).log" 2>&1 &
        echo $! > "$PID_FILE"
        SAVE_TRIGGERED="true"
    fi
fi

# --- Dispatch: after_post_tool ---
export REMEMBER_SAVE_TRIGGERED="$SAVE_TRIGGERED"

# The stdin payload is published to listeners HERE, and only around the
# dispatch that consumes it (#266).
#
# It used to be `export REMEMBER_HOOK_STDIN="$HOOK_STDIN"` at the top of the
# file. The payload embeds the whole tool result, so after a large Read it is
# hundreds of KB, and an exported variable sits in `envp` for every `execve`
# the rest of the hook performs. Every one of them then fails E2BIG — `sed`
# from lib-slug.sh, `head` from here, `date` from lib-clock.sh, `rm` on exit,
# four different libraries — silently, because this hook must not block the
# agent, and on every large tool call for the rest of the session.
#
# The environment was never a working route for this payload. Linux caps a
# SINGLE string at MAX_ARG_STRLEN (32 pages = 131072 bytes) in `envp` exactly
# as in `argv`, independently of the total ARG_MAX the size limit is usually
# discussed in terms of. So on the platform this was reported from, the exec
# that would hand a 200 KB payload to a listener is the exec that fails: the
# capability was declared and could not be performed. macOS has no per-string
# cap and a 1 MB total, which is why it took an outside report to find — here
# it "works" right up to the point the whole environment blows the total.
#
# So the payload travels in a file, and REMEMBER_HOOK_STDIN keeps carrying it
# only while it is small enough to be carried. What it must never do is carry
# PART of it. A listener holding a silently shortened payload cannot tell it
# from a genuinely short one, and that is this repo's own defect class (#144,
# #263): a surface that appears to answer the question and does not. Three
# states, and the third is disclosed rather than disguised:
#
#   FILE unset                     no payload arrived on stdin, or no listener
#   STDIN non-empty                the whole payload, exactly as before
#   STDIN empty and FILE set       too large for the environment; the file
#                                  holds it, complete
#
# REMEMBER_HOOK_STDIN_FILE is the authoritative route: where it is set it is
# always the complete payload, so a listener that only ever reads the file is
# always right and never needs to know a cap exists. (If that write fails —
# a full or read-only tmp — an oversized payload is unavailable rather than
# short. Absent beats plausible-and-wrong.)
#
# None of it happens unless a listener is installed. The distribution ships
# hooks.d/after_post_tool/ holding nothing but a .gitkeep, and this runs on
# every tool call, so writing a payload to disk for a consumer that does not
# exist is a per-tool-call write bought for nobody. Tested with a glob rather
# than a fork, the same way dispatch defers `id` (#230).
REMEMBER_HOOK_STDIN_MAX=32768
_hook_stdin_file=""

_after_post_tool_listener() {
    local f
    for f in "$REMEMBER_HOOKS_DIR/after_post_tool"/*; do
        [ -x "$f" ] && return 0
    done
    return 1
}

if [ -n "$HOOK_STDIN" ] && _after_post_tool_listener; then
    _hook_stdin_file="$REMEMBER_DIR/tmp/hook-stdin.$$"
    # The payload carries tool output, so it is owner-readable only, and it
    # does not outlive the dispatch below.
    if (umask 077; printf '%s' "$HOOK_STDIN" > "$_hook_stdin_file") 2>/dev/null; then
        export REMEMBER_HOOK_STDIN_FILE="$_hook_stdin_file"
    else
        # A write that failed part way through (a full tmp) leaves a short file
        # holding tool output. Nothing may be told about it — a half payload is
        # the truncation this fix exists to refuse — and it may not be left
        # behind either.
        rm -f "$_hook_stdin_file" 2>/dev/null
        _hook_stdin_file=""
    fi
    if [ "${#HOOK_STDIN}" -le "$REMEMBER_HOOK_STDIN_MAX" ]; then
        export REMEMBER_HOOK_STDIN="$HOOK_STDIN"
    else
        export REMEMBER_HOOK_STDIN=""
    fi
fi

dispatch "after_post_tool"

[ -n "$_hook_stdin_file" ] && rm -f "$_hook_stdin_file" 2>/dev/null

# Explicit, because the line above is the last command and it is false whenever
# no payload file was written — which is the common case. Falling off the end
# would exit 1 and make a hook that must never fail report failure on every
# ordinary tool call.
exit 0
