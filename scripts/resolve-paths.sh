#!/bin/bash
# ============================================================================
# resolve-paths.sh — Single source of truth for pipeline path resolution
# ============================================================================
#
# DESCRIPTION
#   Resolves PROJECT_DIR (the user's project root) and PIPELINE_DIR (the
#   plugin's install location) from environment variables set by Claude Code.
#   All pipeline scripts source this file instead of computing paths inline.
#
#   Supports three install layouts:
#     1. Local:       $PROJECT/.claude/remember/scripts/resolve-paths.sh
#     2. Marketplace: ~/.claude/plugins/cache/*/remember/*/scripts/resolve-paths.sh
#     3. Symlinked:   Any of the above with symlinks in the chain
#
# USAGE
#   source "$(dirname "$0")/resolve-paths.sh" || exit <caller-appropriate-code>
#   # Now PROJECT_DIR and PIPELINE_DIR are set and validated
#
#   This file is ALWAYS sourced, never executed directly. On failure it is
#   LOUD BY DEFAULT: it prints FATAL and `exit 1`s the caller, because a caller
#   that continues with unresolved paths writes memory to the wrong place —
#   worse than a crash.
#
#   A caller that must never terminate its host process opts out by setting
#   REMEMBER_PATHS_SOFT_FAIL=1 before sourcing; failure then `return 1`s and the
#   caller decides. Only the three Claude Code hooks do this — they are
#   documented "EXIT CODES: 0 Always" (a bare `exit` inside a sourced file kills
#   the whole hook process, which crashes the nested Haiku session that runs
#   with no resolvable project root). They pair it with `|| exit 0`.
#
#   The default is loud on purpose: a future caller that forgets to check the
#   status still fails safely instead of silently continuing with empty paths.
#
# ENVIRONMENT (inputs)
#   CLAUDE_PROJECT_DIR    Project root (set by Claude Code hooks)
#   CLAUDE_PLUGIN_ROOT    Plugin install directory (set by Claude Code hooks)
#
# ENVIRONMENT (outputs)
#   PROJECT_DIR           Resolved project root (validated to exist)
#   PIPELINE_DIR          Resolved plugin root (validated to exist)
#
# ENVIRONMENT (opt-in)
#   REMEMBER_PATHS_SOFT_FAIL=1   Signal failure with `return 1` instead of
#                                exiting the caller. Set by the hook scripts.
#   REMEMBER_NESTED_SUMMARIZER   Set by pipeline/haiku.py on the nested
#                                `claude -p` it spawns. There is no project
#                                here — resolve nothing and stop.
#
# RETURN CODES
#   1   Path resolution failed, and the caller opted into soft failure.
#       Without the opt-in, resolution failure exits the caller with 1.
#
# ============================================================================

# --- Nested summarizer: there is no project here (#204) ---
# The Haiku call in pipeline/haiku.py runs `claude -p` with cwd=gettempdir().
# Claude Code loads plugins in that child and derives its CLAUDE_PROJECT_DIR
# from that cwd, so every hook this plugin registers fires inside the
# summarizer with the temp dir as its "project" — scaffolding a memory
# directory under the temp dir's slug and injecting session-start output into
# the summarizer's own context.
#
# The guard belongs here rather than in any one hook: SessionStart,
# UserPromptSubmit and PostToolUse are all registered, all source this file,
# and each one alone is enough to create the directory. This is the only place
# that covers all three, and the only place a fourth hook would inherit it.
if [ -n "${REMEMBER_NESTED_SUMMARIZER:-}" ]; then
    if [ "${REMEMBER_PATHS_SOFT_FAIL:-0}" = "1" ]; then
        return 1
    fi
    exit 0
fi

# --- Restrict file creation permissions ---
# Prevent log files, memory files, and temp files from being world/group readable.
# On multi-user machines (shared dev box, CI runner, jumphost) the default umask
# (022) creates files as -rw-r--r--, leaking project paths, branch names, token
# counts, and memory contents to any local user.  Setting 077 here covers every
# downstream file created after this source: logs, .remember/ dirs, TMPDIR temps.
umask 077

# --- Resolve PIPELINE_DIR (where the plugin code lives) ---
#
# Priority:
#   1. CLAUDE_PLUGIN_ROOT (set by Claude Code for marketplace installs)
#   2. Walk up from this script's real location to find the plugin root
#      (works for local installs where scripts/ is inside the plugin dir)
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PLUGIN_ROOT_CANDIDATE="$(cd "$_SCRIPT_DIR/.." && pwd)"

# _resolve_paths_fail <message> [log_dir]
# Report a resolution failure, then apply the caller's failure policy: `return 1`
# when REMEMBER_PATHS_SOFT_FAIL=1, otherwise exit the caller (the default).
_resolve_paths_fail() {
    echo "$1" >&2
    if [ -n "${2:-}" ] && [ -d "$2" ]; then
        echo "$(date '+%H:%M:%S') [resolve] $1" >> "$2/memory-$(date '+%Y-%m-%d').log" 2>/dev/null
    fi
    [ "${REMEMBER_PATHS_SOFT_FAIL:-0}" = "1" ] && return 1
    exit 1
}

if [ -n "$CLAUDE_PLUGIN_ROOT" ]; then
    PIPELINE_DIR="$CLAUDE_PLUGIN_ROOT"
elif [ -f "$_PLUGIN_ROOT_CANDIDATE/pipeline/haiku.py" ]; then
    # Local install: scripts/ is one level below the plugin root
    PIPELINE_DIR="$_PLUGIN_ROOT_CANDIDATE"
else
    _msg="FATAL: Cannot resolve plugin root. CLAUDE_PLUGIN_ROOT is not set and $_PLUGIN_ROOT_CANDIDATE/pipeline/haiku.py does not exist."
    _resolve_paths_fail "$_msg" "${CLAUDE_PROJECT_DIR:-.}/.remember/logs" || return 1
fi

# --- Resolve PROJECT_DIR (the user's project root) ---
#
# Priority:
#   1. CLAUDE_PROJECT_DIR (set by Claude Code — always correct)
#   2. If PIPELINE_DIR is inside a .claude/remember/ structure, derive from that
#   3. Fail — we cannot guess the project root from a marketplace cache path
if [ -n "$CLAUDE_PROJECT_DIR" ]; then
    PROJECT_DIR="$CLAUDE_PROJECT_DIR"
elif [[ "$PIPELINE_DIR" == *"/.claude/remember" ]]; then
    # Local install: plugin is at $PROJECT/.claude/remember
    PROJECT_DIR="$(cd "$PIPELINE_DIR/../.." && pwd)"
else
    _msg="FATAL: Cannot resolve project root. CLAUDE_PROJECT_DIR is not set and plugin is not in a local .claude/remember/ layout (PIPELINE_DIR=$PIPELINE_DIR)."
    _resolve_paths_fail "$_msg" "${PROJECT_DIR:-.}/.remember/logs" || return 1
fi

# --- Windows shell normalization (Git Bash / MSYS / Cygwin) ----------------
# Claude Code stores sessions under a Windows-native slug (e.g.
# "C--Users-dev-project") computed from the Win32 path "C:\Users\dev\project".
# But on Windows shells, $CLAUDE_PROJECT_DIR arrives as a POSIX-style path
# ("/c/Users/dev/project") and our sed-based slug produces "-c-Users-dev-..."
# which never matches. The plugin's `ls $SESSION_DIR/*.jsonl` then returns
# nothing and the entire save pipeline silently no-ops.
#
# Convert /c/Users/... → C:\Users\... here so all downstream slug computations
# (3 shell sites + Python `_session_dir`) align with Claude Code's storage.
# On Linux/macOS bash $OSTYPE is "linux-gnu" or "darwin*"; the case below
# never matches and PROJECT_DIR is left untouched.
case "$OSTYPE" in
    msys|cygwin)
        if [[ "$PROJECT_DIR" =~ ^/([a-zA-Z])/(.*)$ ]]; then
            _drive=$(printf '%s' "${BASH_REMATCH[1]}" | tr '[:lower:]' '[:upper:]')
            _rest="${BASH_REMATCH[2]//\//\\}"
            PROJECT_DIR="${_drive}:\\${_rest}"
        fi
        ;;
esac

# --- Validate both paths exist ---
if [ ! -d "$PROJECT_DIR" ]; then
    _msg="FATAL: PROJECT_DIR does not exist: $PROJECT_DIR"
    _resolve_paths_fail "$_msg" || return 1
fi

if [ ! -d "$PIPELINE_DIR" ]; then
    _msg="FATAL: PIPELINE_DIR does not exist: $PIPELINE_DIR"
    _resolve_paths_fail "$_msg" || return 1
fi

# --- Export for subprocesses (critical for nohup) ---
export CLAUDE_PROJECT_DIR="$PROJECT_DIR"
export CLAUDE_PLUGIN_ROOT="$PIPELINE_DIR"
export PROJECT_DIR
export PIPELINE_DIR
