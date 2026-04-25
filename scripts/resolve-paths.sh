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
#   source "$(dirname "$0")/resolve-paths.sh"
#   # Now PROJECT_DIR and PIPELINE_DIR are set and validated
#
# ENVIRONMENT (inputs)
#   CLAUDE_PROJECT_DIR    Project root (set by Claude Code hooks)
#   CLAUDE_PLUGIN_ROOT    Plugin install directory (set by Claude Code hooks)
#
# ENVIRONMENT (outputs)
#   PROJECT_DIR           Resolved project root (validated to exist)
#   PIPELINE_DIR          Resolved plugin root (validated to exist)
#
# EXIT CODES
#   1   Path resolution failed (PROJECT_DIR or PIPELINE_DIR not found)
#
# ============================================================================

# --- Resolve PIPELINE_DIR (where the plugin code lives) ---
#
# Priority:
#   1. CLAUDE_PLUGIN_ROOT (set by Claude Code for marketplace installs)
#   2. Walk up from this script's real location to find the plugin root
#      (works for local installs where scripts/ is inside the plugin dir)
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PLUGIN_ROOT_CANDIDATE="$(cd "$_SCRIPT_DIR/.." && pwd)"

if [ -n "$CLAUDE_PLUGIN_ROOT" ]; then
    PIPELINE_DIR="$CLAUDE_PLUGIN_ROOT"
elif [ -f "$_PLUGIN_ROOT_CANDIDATE/pipeline/haiku.py" ]; then
    # Local install: scripts/ is one level below the plugin root
    PIPELINE_DIR="$_PLUGIN_ROOT_CANDIDATE"
else
    _msg="FATAL: Cannot resolve plugin root. CLAUDE_PLUGIN_ROOT is not set and $_PLUGIN_ROOT_CANDIDATE/pipeline/haiku.py does not exist."
    echo "$_msg" >&2
    # Try to log if we can find a log directory
    _log_dir="${CLAUDE_PROJECT_DIR:-.}/.remember/logs"
    if [ -d "$_log_dir" ]; then
        echo "$(date '+%H:%M:%S') [resolve] $_msg" >> "$_log_dir/memory-$(date '+%Y-%m-%d').log" 2>/dev/null
    fi
    exit 1
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
    echo "$_msg" >&2
    _log_dir="${PROJECT_DIR:-.}/.remember/logs"
    if [ -d "$_log_dir" ]; then
        echo "$(date '+%H:%M:%S') [resolve] $_msg" >> "$_log_dir/memory-$(date '+%Y-%m-%d').log" 2>/dev/null
    fi
    exit 1
fi

# --- Validate both paths exist ---
if [ ! -d "$PROJECT_DIR" ]; then
    _msg="FATAL: PROJECT_DIR does not exist: $PROJECT_DIR"
    echo "$_msg" >&2
    exit 1
fi

if [ ! -d "$PIPELINE_DIR" ]; then
    _msg="FATAL: PIPELINE_DIR does not exist: $PIPELINE_DIR"
    echo "$_msg" >&2
    exit 1
fi

# --- Export for subprocesses (critical for nohup) ---
export CLAUDE_PROJECT_DIR="$PROJECT_DIR"
export CLAUDE_PLUGIN_ROOT="$PIPELINE_DIR"
export PROJECT_DIR
export PIPELINE_DIR
