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
#   jq (for config.json reading via log.sh)
#   log.sh (for timezone, dispatch via hooks.d/)
#
# EXIT CODES
#   0   Always (hook must not block the agent)
#
# OUTPUT
#   Prints "[HH:MM TZ — username]" to stdout.
#
# ============================================================================

# --- Resolve paths ---
# Opt into resolve-paths.sh's soft-failure mode — see the comment in
# session-start-hook.sh. This hook must never block the agent, so a resolution
# failure is a silent no-op, not a crash.
REMEMBER_PATHS_SOFT_FAIL=1 source "$(dirname "$0")/resolve-paths.sh" || exit 0
source "$(dirname "$0")/bootstrap-dirs.sh"
source "$(dirname "$0")/log.sh" 2>/dev/null

# --- Capture-gap notice (#200) ────────────────────────────────────────────
# session-start-hook.sh leaves this when the PREVIOUS session ran SessionStart
# but never PostToolUse — the signature of a plugin enabled mid-session, whose
# hooks Claude Code never wired in. It is delivered here rather than there
# because `systemMessage` is the only hook output the HUMAN sees, and a notice
# only the model sees is how this stayed invisible for a day in the first place.
#
# Consumed on read: this is a one-line nudge, not a persistent banner.
NOTICE_FILE="$REMEMBER_DIR/tmp/capture-gap-notice"
NOTICE_MSG=""
if [ -f "$NOTICE_FILE" ]; then
    NOTICE_MSG=$(cat "$NOTICE_FILE" 2>/dev/null)
    rm -f "$NOTICE_FILE" 2>/dev/null || true
fi

# --- Timestamp + context injection ---
# Collected rather than echoed: with a notice to deliver the whole reply has to
# become one JSON object, and stdout cannot be half plain text and half JSON.
# The no-notice path below still prints exactly what it always did.
CTX=$(
CTX_PCT=""
CTX_PCT_FILE="${SYS_TMPDIR:-/tmp}/claude-ctx-pct"
if [ -f "$CTX_PCT_FILE" ]; then
  CTX_PCT=$(cat "$CTX_PCT_FILE" 2>/dev/null)
fi
if [ -n "$CTX_PCT" ]; then
  TIMESTAMP="[$(_remember_date '+%H:%M %Z') — $(whoami) — ${CTX_PCT}%]"
  echo "$TIMESTAMP"
  if [ "$CTX_PCT" -ge 95 ] 2>/dev/null; then
    echo "WARNING: Context at ${CTX_PCT}%. Run /remember to save session state before context death."
  fi
else
  echo "[$(_remember_date '+%H:%M %Z') — $(whoami)]"
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
