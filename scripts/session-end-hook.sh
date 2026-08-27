#!/bin/bash
# ============================================================================
# session-end-hook.sh — SessionEnd hook for the Remember plugin
# ============================================================================
#
# DESCRIPTION
#   Fires when a Claude Code session ends. Flushes whatever has not yet been
#   saved to now.md, ignoring both the cooldown (cooldowns.save_seconds) and
#   the min-human-message gate that PostToolUse's routine saves respect
#   (#345). Those gates exist to throttle a LIVE session; this hook runs once,
#   at the point where there is no next tool call and no next cooldown window
#   to catch up on the missed span. A session that ends in conversation rather
#   than tool calls — a design discussion, a review, a decision — is exactly
#   what PostToolUse's delta/cooldown gates can leave unsaved, because nothing
#   after the last save cleared them.
#
#   Does NOT write a handoff note. `/remember` (skills/remember/SKILL.md)
#   composes remember.md from the model's own first-person recollection of
#   the session — "What's done, what's not", "what to pick up", written in
#   "I". There is no model turn running at SessionEnd for this hook to
#   narrate from; it is a bash script with the raw transcript, not the agent
#   that lived the session. A generated placeholder ("session ended, see
#   now.md") would silently overwrite a real handoff a user wrote earlier in
#   the same session with something that carries no forward-looking content
#   at all — worse than leaving the existing file alone, and adjacent to
#   #341's stale-banner problem rather than a fix for it. now.md (this hook's
#   actual output) is what a next session's recovery block and consolidation
#   pipeline read; a real, older remember.md is left untouched.
#
# STDIN
#   The SessionEnd payload. `session_id` and `reason` are read with the same
#   bounded, non-blocking approach post-tool-hook.sh uses for `session_id`:
#   never from a tty, and time-bounded (`read -t 1`) so a pipe held open with
#   nothing in it costs at most a second rather than hanging session teardown.
#
#   `reason` is documented (Claude Code hooks reference, checked 2026-08) as
#   one of clear/resume/logout/prompt_input_exit/other, but is treated here as
#   an opaque, unvalidated token — logged for diagnostics, never branched on.
#   No reason this hook has actually observed in the wild disqualifies a
#   flush: the whole point is that this is the last chance, not a routine
#   tick, so every reason gets the same unconditional attempt.
#
#   Whether SessionEnd fires at all on a crash, a killed terminal, or a
#   process hitting the usage cap is NOT established by that same reference —
#   it documents the graceful paths (clear, logout, prompt_input_exit,
#   resume) and is silent on the abrupt ones. This hook cannot make it fire
#   where Claude Code itself would not invoke it; `features.recovery` softens
#   exactly that gap from the next session's start and is deliberately left
#   in place rather than treated as superseded by this hook (#345).
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT   Plugin install directory (set by Claude Code)
#   CLAUDE_PROJECT_DIR   Project root (default: .)
#
# EXIT CODES
#   0   Always, and immediately — the flush itself runs in a backgrounded
#       subshell (see the flush section below) so this hook's own exit is not
#       waiting on save-session.sh at all. A failed flush is reported loudly
#       once that subshell finishes (report_error(), which reaches both the
#       daily log and hook-errors.log — surfaced by /remember:doctor) rather
#       than swallowed silently: the other hooks in this plugin can afford
#       silence on failure because there is always a next tool call or a
#       next session to retry from. This one is the last chance a session
#       gets, which is also why it is the one hook here NOT allowed to lose
#       its own failure to the same 60s kill it is trying to survive.
#
# ============================================================================

# --- Where this script lives ---
# Same parameter-expansion resolution as post-tool-hook.sh / user-prompt-hook.sh
# (#230) rather than three `dirname` forks. A path with no slash in it leaves
# the filename behind, not a directory; `dirname` answered "." and this must
# too.
_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="."

# --- Nested summarizer: there is no project here (#204) ---
# Same guard every hook in this plugin carries: this plugin can re-enter its
# own hooks from a nested/headless session, and scaffolding a memory
# directory under the summarizer's own temp dir is a bug regardless of which
# hook does it.
[ -n "${REMEMBER_NESTED_SUMMARIZER:-}" ] && exit 0

source "$_HOOK_DIR/lib-clock.sh"

# --- Read stdin: session_id and reason ---
# Cleared, not merely left alone (#266) — see post-tool-hook.sh's identical
# comment. This plugin can re-enter its own hooks from a nested session, and
# both names are exported at points elsewhere in this plugin's hooks, so a
# stale value here would be the plausible-and-wrong answer rather than the
# honest absent one.
unset REMEMBER_HOOK_STDIN REMEMBER_HOOK_STDIN_FILE

HOOK_STDIN=""
if [ ! -t 0 ]; then
    _line=""
    while IFS= read -r -t 1 _line || [ -n "$_line" ]; do
        HOOK_STDIN="$HOOK_STDIN$_line"
        _line=""
    done
fi

# The same deliberately narrow extractor post-tool-hook.sh and
# session-start-hook.sh use: the key must be followed by nothing but
# whitespace and a colon before the value's opening quote, so a field of the
# same name appearing inside some other part of the payload is not mistaken
# for it. Duplicated rather than sourced from session-start-hook.sh, for the
# same reason that file gives for keeping its own copy: a hook that has to
# survive a broken install is better served by a few duplicated lines than a
# shared library it might fail to source.
_stdin_json_string() {
    local key="$1" raw="$2" rest prefix value
    case "$raw" in *"\"$key\""*) ;; *) return 1 ;; esac
    rest=${raw#*\"$key\"}
    prefix=${rest%%\"*}
    case "$prefix" in *[!:[:space:]]*) return 1 ;; esac
    value=${rest#*\"}
    value=${value%%\"*}
    [ -n "$value" ] || return 1
    printf '%s' "$value"
}

STDIN_SESSION_ID=$(_stdin_json_string session_id "$HOOK_STDIN" 2>/dev/null) || STDIN_SESSION_ID=""
# stdin is not more trustworthy than a basename — same validation
# post-tool-hook.sh applies before this id becomes a path component or an
# argument to another script.
case "$STDIN_SESSION_ID" in
    ''|.|..|*[!A-Za-z0-9._-]*) STDIN_SESSION_ID="" ;;
esac

SESSION_END_REASON=$(_stdin_json_string reason "$HOOK_STDIN" 2>/dev/null) || SESSION_END_REASON=""
# Not narrowed to a known enum on purpose (see STDIN comment above) — only
# sanitised so an unexpected payload shape cannot put an arbitrary byte
# sequence into a log line.
case "$SESSION_END_REASON" in
    ''|*[!A-Za-z0-9_]*) SESSION_END_REASON="unknown" ;;
esac

# --- Resolve paths, tools, directories, logging ---
# Opt into resolve-paths.sh's soft-failure mode, exactly as post-tool-hook.sh
# does: this hook must never block session teardown, so a resolution failure
# (e.g. a nested/headless session with no CLAUDE_PROJECT_DIR) is a silent
# no-op, not a crash.
REMEMBER_PATHS_SOFT_FAIL=1 source "$_HOOK_DIR/resolve-paths.sh" || exit 0
source "$_HOOK_DIR/detect-tools.sh"
source "$_HOOK_DIR/bootstrap-dirs.sh"
source "$PIPELINE_DIR/scripts/log.sh" 2>/dev/null
# log.sh returns early on a store it cannot create a logs/ dir in — before it
# defines log(), report_error() or dispatch() — so the source succeeding
# above is not the same question as those existing (#361, #372). Same guard
# post-tool-hook.sh and user-prompt-hook.sh already carry, for the same
# reason: `declare -F`, NOT `type` or `command -v` — on macOS /usr/bin/log is
# Apple's unified-logging CLI, so `type log` is true whether or not a shell
# function was ever defined, the guard would pass, and the stub below would
# never be installed: `log "hook" "..."` two lines down would instead exec
# that binary, dump its own usage text to stderr, and exit 64 from a hook
# documented "EXIT CODES: 0 Always". Measured on bash 3.2.57 (macOS).
#
# Unlike the no-op stubs the hot paths install, these two still have to
# report SOMETHING: this is the one file whose own docstring (EXIT CODES,
# above) promises a failed flush is "reported loudly ... rather than
# swallowed silently", and the `report_error` call further down (guarding
# the `[ ! -d "$REMEMBER_DIR" ]` branch) is reachable only when this source
# has already failed for that exact reason. log.sh's own log() documents
# "Falls back to stderr if log file is unwritable" — this reproduces exactly
# that fallback, because it is the same fallback for the same reason:
# $REMEMBER_DIR/logs is what could not be created. hook-errors.log and the
# notices channel user-prompt-hook.sh reads (#200, #253) are both files under
# that same directory, so neither is reachable here; stderr is the only
# channel left, and bootstrap-dirs.sh only redirects it into hook-errors.log
# once that directory exists (bootstrap-dirs.sh:230-231) — on this path it is
# still going wherever Claude Code sends an unredirected hook's stderr,
# which is not nowhere.
declare -F log >/dev/null 2>&1 || log() {
    printf '%s [%s] %s\n' "$(_remember_date +%H:%M:%S)" "$1" "$2" >&2
}
declare -F report_error >/dev/null 2>&1 || report_error() { log "$1" "$2"; }
log "hook" "session-end: reason=$SESSION_END_REASON session=${STDIN_SESSION_ID:-unresolved}"

# bootstrap-dirs.sh's mkdir is best-effort, and by the time this line runs it
# has already tried once for THIS invocation — so unlike an ordinary "nothing
# to flush" exit, reaching here means that attempt just failed (read-only
# root, missing parent). Reported, not silently folded into the same no-op
# every other early exit in this hook takes: without this line, a store that
# can never be created and a session with nothing new to save are the same
# line in hook-errors.log, which is no line at all.
if [ ! -d "$REMEMBER_DIR" ]; then
    report_error "session-end" "WARNING: $REMEMBER_DIR does not exist and could not be created — nothing was flushed at session end."
    exit 0
fi

SAVE_SCRIPT="$PIPELINE_DIR/scripts/save-session.sh"
if [ ! -f "$SAVE_SCRIPT" ]; then
    report_error "session-end" "WARNING: $SAVE_SCRIPT is missing — nothing was flushed at session end. Reinstall the plugin."
    exit 0
fi

# --- Flush, unconditionally, in the BACKGROUND ---
# save-session.sh --force bypasses its own cooldown timer AND its
# min-human-message gate (see its own USAGE block) — exactly the two gates
# this issue exists to route around. It does NOT bypass the zero-exchange
# gate: a session with nothing new since the last save advances the saved
# position without a Haiku call, so this hook costs nothing extra when there
# is genuinely nothing to flush.
#
# Backgrounded, the same way post-tool-hook.sh forks its own call — NOT run
# and waited on in the foreground, which an earlier version of this hook did.
# Claude Code kills a hook process after `hooks.dispatch_timeout_seconds`'
# sibling budget for the events it waits on: this repo's own README documents
# "Claude Code kills a hook at 60s of its own accord" for exactly this
# reason. save-session.sh's own Haiku call already asks for up to 120s and
# NDC compression up to 180s (scripts/save-session.sh) — both past 60s on
# the sessions this hook exists to rescue, which are the long, content-heavy
# ones. A foreground
# wait risks losing the ENTIRE flush to Claude Code's own kill with no trace
# at all; a backgrounded one gets to keep running after this hook returns,
# the same way `hooks.d/after_save/50-git-backup.sh`'s own git push does
# ("a listener blocked in a foreground child leaks that child when the
# script is killed" — the shape this rewrite avoids). The trade is explicit:
# this hook can no longer report a flush failure to the SAME invocation of
# `/remember:doctor` that ran a second later, only to hook-errors.log once
# the background flush itself finishes — which is what the subshell below
# does.
#
# tmp/save-session.pid is the SAME marker post-tool-hook.sh's own
# background fork writes (scripts/post-tool-hook.sh), not a second one: both
# are "a save-session.sh is in flight" and nothing downstream needs to tell
# them apart.
mkdir -p "$REMEMBER_DIR/logs/autonomous" 2>/dev/null
_END_LOG="$REMEMBER_DIR/logs/autonomous/session-end-$(_remember_date +%H%M%S).log"
(
    if [ -n "$STDIN_SESSION_ID" ]; then
        bash "$SAVE_SCRIPT" "$STDIN_SESSION_ID" --force
    else
        bash "$SAVE_SCRIPT" --force
    fi
    _flush_status=$?
    if [ "$_flush_status" -ne 0 ]; then
        report_error "session-end" "WARNING: save-session.sh --force exited $_flush_status at session end — this session's unsaved tail may be lost. See $_END_LOG for what save-session.sh itself logged."
    fi
) < /dev/null > "$_END_LOG" 2>&1 &
echo $! > "$REMEMBER_DIR/tmp/save-session.pid" 2>/dev/null
disown 2>/dev/null || true

exit 0
