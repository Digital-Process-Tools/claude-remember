#!/bin/bash
# ============================================================================
# bootstrap-dirs.sh — Single source of truth for .remember/ directory layout
# ============================================================================
#
# DESCRIPTION
#   Creates the memory directory structure and sets up stderr logging.
#   Every hook script sources this after resolve-paths.sh and detect-tools.sh
#   to guarantee the directory tree exists before any file I/O.
#
#   When REMEMBER_DIR points outside the project (external mode), the legacy
#   ${PROJECT_DIR}/.remember/ is migrated automatically on first run and a
#   MIGRATED-TO.txt marker is left behind.
#
# USAGE
#   source "$(dirname "$0")/resolve-paths.sh"
#   source "$(dirname "$0")/detect-tools.sh"
#   source "$(dirname "$0")/bootstrap-dirs.sh"
#
# REQUIRES
#   PROJECT_DIR      must be set (by resolve-paths.sh)
#   PIPELINE_DIR     must be set (by resolve-paths.sh)
#   session_dir_slug must be defined (by detect-tools.sh)
#
# EXPORTS
#   REMEMBER_DIR    — absolute path to memory data directory (via lib-memory-dir.sh)
#   SYS_TMPDIR      — portable system temp directory
#
# ============================================================================

# Resolve REMEMBER_DIR via the shared helper (no-op if already loaded).
_BOOTSTRAP_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_BOOTSTRAP_SCRIPT_DIR/lib-memory-dir.sh"
unset _BOOTSTRAP_SCRIPT_DIR

# --- System temp directory (portable: macOS, Linux, Windows/Git Bash) ---
SYS_TMPDIR="${TMPDIR:-/tmp}"

# --- One-shot migration: legacy ${PROJECT_DIR}/.remember → external REMEMBER_DIR ---
_legacy_dir="${PROJECT_DIR}/.remember"
if [ "$REMEMBER_DIR" != "$_legacy_dir" ] && [ -d "$_legacy_dir" ] && [ ! -e "$REMEMBER_DIR" ]; then
    mkdir -p "$(dirname "$REMEMBER_DIR")" 2>/dev/null
    if mv "$_legacy_dir" "$REMEMBER_DIR" 2>/dev/null; then
        mkdir -p "$_legacy_dir"
        printf 'Memory data migrated to:\n  %s\nThis directory is now empty; you may delete it.\n' \
            "$REMEMBER_DIR" > "$_legacy_dir/MIGRATED-TO.txt"
    fi
fi
unset _legacy_dir

# --- Create directory structure ---
mkdir -p \
    "$REMEMBER_DIR/tmp" \
    "$REMEMBER_DIR/logs" \
    "$REMEMBER_DIR/logs/autonomous" \
    2>/dev/null

# --- Gitignore: only write when REMEMBER_DIR is inside the project tree ---
# In external mode (REMEMBER_DIR outside PROJECT_DIR) there is no gitignore
# to write — the user manages that tree themselves (typically as a private git
# repo at ~/.remember/).
case "$REMEMBER_DIR" in
    "$PROJECT_DIR"/*)
        [ -f "$REMEMBER_DIR/.gitignore" ] || echo '*' > "$REMEMBER_DIR/.gitignore" 2>/dev/null
        ;;
esac

# --- Redirect stderr to hook-errors.log ---
# This replaces the 2>> that was in hooks.json. Now the directory is
# guaranteed to exist before we open the file.
# Guard: only redirect if the logs dir was actually created (read-only
# filesystems, Docker read-only mounts, etc. will skip this gracefully).
if [ -d "$REMEMBER_DIR/logs" ]; then
    exec 2>> "$REMEMBER_DIR/logs/hook-errors.log"
fi
