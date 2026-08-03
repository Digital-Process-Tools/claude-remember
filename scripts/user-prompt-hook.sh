#!/bin/bash
# ============================================================================
# user-prompt-hook.sh — UserPromptSubmit hook for the Remember plugin
# ============================================================================
#
# DESCRIPTION
#   Runs on every user prompt submission. Injects the current timestamp
#   so the agent knows what time it is during the conversation.
#
# USAGE
#   Called automatically by Claude Code's UserPromptSubmit hook system.
#   Not intended for manual invocation.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT   Plugin install directory (set by Claude Code)
#   CLAUDE_PROJECT_DIR   Project root (default: .)
#
# DEPENDENCIES
#   lib-clock.sh    (the timestamp, without a process where bash can do it)
#   lib-env-cache.sh (replays an already-resolved REMEMBER_DIR / REMEMBER_TZ)
#   jq (for config.json reading via log.sh — first run in a project only)
#   log.sh (for timezone, dispatch via hooks.d/ — first run in a project only)
#
# EXIT CODES
#   0   Always (hook must not block the agent)
#
# OUTPUT
#   Prints "[HH:MM TZ — username]" to stdout.
#
# COST (#227)
#   This runs on every prompt the user submits and the user waits for it. It
#   used to source resolve-paths.sh → bootstrap-dirs.sh → log.sh to print one
#   line: 19 processes on macOS, 27 on the Windows-ARM64-under-QEMU box
#   @MargeryOlethea measured, where per-spawn costs of 150-800ms turned it into
#   a p50 of 8718ms — blocking, on every prompt, with 6 outright timeouts in 256
#   runs. It now takes the chain only when it has no already-resolved answer to
#   replay, which is once per project per config change.
#
#   That matters more here than the numbers suggest: on UserPromptSubmit a
#   non-zero exit does not merely error, it BLOCKS THE PROMPT AND ERASES WHAT
#   THE USER TYPED. Every process this does not start is an opportunity to fail
#   that it does not take.
#
# ============================================================================

_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="."

# --- Nested summarizer: there is no project here (#204) ---
# Normally this guard lives in resolve-paths.sh, which the fast path below does
# not reach. It has to hold for every hook this plugin registers — the whole
# point of #204 was that any one of them alone scaffolds a memory directory
# under the summarizer's temp dir and injects into its context.
[ -n "${REMEMBER_NESTED_SUMMARIZER:-}" ] && exit 0

source "$_HOOK_DIR/lib-clock.sh"
source "$_HOOK_DIR/lib-env-cache.sh"

# --- Resolve paths ---
# Two facts are needed below: REMEMBER_DIR (for the capture-gap notice) and
# REMEMBER_TZ (for the timestamp). If a previous hook in this project already
# derived them from the same config files, replay that instead of re-deriving
# it; lib-env-cache.sh validates and declines, it never computes.
#
# hooks.d/after_user_prompt/ is checked here because a listener there is the one
# thing on this path that needs log.sh's dispatch(). The distribution ships no
# such directory, so `dispatch` was already a no-op for everyone — but a user
# who creates one gets the documented behaviour, at the old price.
_REMEMBER_FAST=0
if _remember_env_cache_load && [ ! -d "$PIPELINE_DIR/hooks.d/after_user_prompt" ]; then
    _REMEMBER_FAST=1
    # Both normally come from the chain: umask from resolve-paths.sh (#68),
    # SYS_TMPDIR from bootstrap-dirs.sh. Same values, no processes.
    umask 077
    SYS_TMPDIR="${TMPDIR:-/tmp}"
    dispatch() { :; }
fi

if [ "$_REMEMBER_FAST" = "0" ]; then
    # Opt into resolve-paths.sh's soft-failure mode — see the comment in
    # session-start-hook.sh. This hook must never block the agent, so a
    # resolution failure is a silent no-op, not a crash.
    REMEMBER_PATHS_SOFT_FAIL=1 source "$_HOOK_DIR/resolve-paths.sh" || exit 0
    source "$_HOOK_DIR/bootstrap-dirs.sh"
    source "$_HOOK_DIR/log.sh" 2>/dev/null
    _remember_env_cache_publish
fi

# --- Notices for the human (#200, #253) ───────────────────────────────────
# Other hooks leave a file here when they find something the HUMAN has to know
# and the model cannot act on. They are delivered from this hook rather than
# from the one that found them because `systemMessage` is the only hook output
# the HUMAN sees, and a notice only the model sees is how #200 stayed invisible
# for a day in the first place — and how #253 stayed invisible for twelve.
#
#   capture-gap-notice  session-start-hook.sh: the PREVIOUS session ran
#                       SessionStart but never PostToolUse — the signature of a
#                       plugin enabled mid-session, whose hooks Claude Code
#                       never wired in.
#   git-backup-notice   after_save/50-git-backup.sh: the backup remote has
#                       rejected the last N pushes, so memory is committed
#                       locally and going nowhere, and no retry will fix it.
#   git-restore-notice  before_session_start/50-git-restore.sh: the store has
#                       DIVERGED from its backup remote, so the memory loaded
#                       this session is missing what the other machine wrote,
#                       and nothing will merge or rebase it for you.
#   case-divergence-notice
#                       session-start-hook.sh: this store is known by a second
#                       spelling that differs only in case (#298). Harmless on
#                       the case-insensitive filesystem it is sitting on, and it
#                       splits the store in two on a case-sensitive restore.
#                       Written only when the finding CHANGES: the condition
#                       never clears itself, so a notice every session would
#                       spend this channel on wallpaper.
#
# Consumed on read: these are one-line nudges, not persistent banners. Adding
# one is deliberately cheap and deliberately rare — this channel interrupts a
# human mid-thought, and one that fires often is one that gets tuned out.
NOTICE_MSG=""
for _notice_name in capture-gap-notice git-backup-notice git-restore-notice case-divergence-notice; do
    NOTICE_FILE="$REMEMBER_DIR/tmp/$_notice_name"
    [ -f "$NOTICE_FILE" ] || continue
    _notice_body=$(cat "$NOTICE_FILE" 2>/dev/null)
    rm -f "$NOTICE_FILE" 2>/dev/null || true
    [ -n "$_notice_body" ] || continue
    if [ -n "$NOTICE_MSG" ]; then
        NOTICE_MSG="$NOTICE_MSG
$_notice_body"
    else
        NOTICE_MSG="$_notice_body"
    fi
done

# --- Timestamp + context injection ---
# Collected rather than echoed: with a notice to deliver the whole reply has to
# become one JSON object, and stdout cannot be half plain text and half JSON.
# The no-notice path below still prints exactly what it always did.
CTX=$(
CTX_PCT=""
CTX_PCT_FILE="${SYS_TMPDIR:-/tmp}/claude-ctx-pct"
if [ -f "$CTX_PCT_FILE" ]; then
  # `read`, not `cat`: this file exists on every prompt for anyone running the
  # status line, so the process was not the rare path it looks like (#227).
  # Status untested on purpose: `read` returns 1 on a file with no trailing
  # newline, having set the variable anyway — and a status line writing "45"
  # without one is the likely case, not the exotic one. CTX_PCT is already ""
  # if nothing was read.
  read -r CTX_PCT < "$CTX_PCT_FILE" 2>/dev/null
fi
# whoami was a process for a value the shell already has. They differ only where
# the effective user is not the login user (`su` without `-`), which is not a
# shape a Claude Code hook runs in — and whoami is still the fallback when the
# environment carries neither name.
_REMEMBER_WHO="${USER:-${USERNAME:-}}"
[ -n "$_REMEMBER_WHO" ] || _REMEMBER_WHO=$(whoami 2>/dev/null)
_REMEMBER_NOW=$(_remember_date '+%H:%M %Z')
if [ -n "$CTX_PCT" ]; then
  TIMESTAMP="[$_REMEMBER_NOW — $_REMEMBER_WHO — ${CTX_PCT}%]"
  echo "$TIMESTAMP"
  if [ "$CTX_PCT" -ge 95 ] 2>/dev/null; then
    echo "WARNING: Context at ${CTX_PCT}%. Run /remember to save session state before context death."
  fi
else
  echo "[$_REMEMBER_NOW — $_REMEMBER_WHO]"
fi

# ── Dispatch: after_user_prompt ─────────────────────────────────────────
dispatch "after_user_prompt"
)

# detect-tools.sh is deliberately NOT sourced here — it hard-exits when python
# is missing, and this hook must never block a prompt. jq is resolved directly.
JQ_BIN="${JQ:-jq}"
if [ -z "$NOTICE_MSG" ]; then
    printf '%s\n' "$CTX"
else
    # jq's status must not become this hook's status. Left as the last command
    # of the script, a jq usage error (exit 2) is read by Claude Code as
    # "block this prompt" — for UserPromptSubmit, exit 2 BLOCKS AND ERASES
    # what the user typed. A cosmetic notice must never be able to do that, so
    # the JSON is built first and only printed if it was actually produced.
    _JSON=""
    if command -v "$JQ_BIN" >/dev/null 2>&1; then
        _JSON=$(printf '%s\n' "$CTX" | "$JQ_BIN" -Rs --arg msg "$NOTICE_MSG" \
            '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:.},systemMessage:$msg}' 2>/dev/null) \
            || _JSON=""
    fi
    if [ -n "$_JSON" ]; then
        printf '%s\n' "$_JSON"
    else
        # No jq, or jq failed: a plain-text reply cannot carry systemMessage,
        # so the notice goes into context instead. The model sees it and can
        # relay it — worse than the terminal line, better than swallowing the
        # one thing worth saying, and far better than eating the prompt.
        printf '%s\n' "$CTX"
        printf 'remember: %s\n' "$NOTICE_MSG"
    fi
fi

# Never inherit a failure status from anything above: this hook is documented
# to always exit 0, and on UserPromptSubmit a non-zero exit is not cosmetic.
exit 0
