#!/bin/bash
# ============================================================================
# bootstrap-dirs.sh — Single source of truth for .remember/ directory layout
# ============================================================================
#
# DESCRIPTION
#   Creates the .remember/ directory structure and sets up stderr logging.
#   Every hook script sources this after resolve-paths.sh to guarantee the
#   directory tree exists before any file I/O.
#
#   This replaces the inline 2>> redirect that was in hooks.json, which
#   failed on fresh projects because bash opens the redirect target before
#   the script runs (chicken-and-egg bug: GitHub issues #23, #27, #31, #32).
#
# USAGE
#   source "$(dirname "$0")/resolve-paths.sh"
#   source "$(dirname "$0")/bootstrap-dirs.sh"
#
# REQUIRES
#   PROJECT_DIR   must be set (by resolve-paths.sh)
#
# ============================================================================

REMEMBER_DIR="${PROJECT_DIR}/.remember"

# --- System temp directory (portable: macOS, Linux, Windows/Git Bash) ---
SYS_TMPDIR="${TMPDIR:-/tmp}"

# --- Create directory structure ---
mkdir -p \
    "$REMEMBER_DIR/tmp" \
    "$REMEMBER_DIR/logs" \
    "$REMEMBER_DIR/logs/autonomous" \
    2>/dev/null

# --- Gitignore so .remember/ never gets committed ---
[ -f "$REMEMBER_DIR/.gitignore" ] || echo '*' > "$REMEMBER_DIR/.gitignore" 2>/dev/null

# --- Redirect stderr to hook-errors.log ---
# This replaces the 2>> that was in hooks.json. Now the directory is
# guaranteed to exist before we open the file.
# Guard: only redirect if the logs dir was actually created (read-only
# filesystems, Docker read-only mounts, etc. will skip this gracefully).
if [ -d "$REMEMBER_DIR/logs" ]; then
    exec 2>> "$REMEMBER_DIR/logs/hook-errors.log"
fi
