#!/bin/bash
# ============================================================================
# lib-memory-dir.sh — Resolve REMEMBER_DIR and produce a merged REMEMBER_CONFIG
# ============================================================================
#
# DESCRIPTION
#   Single source of truth for two closely coupled concerns:
#
#   1. REMEMBER_DIR — where memory data files live.
#      Normally "${PROJECT_DIR}/.remember" (legacy default).
#      When config.json carries a data_dir starting with "/" or "~", the path
#      is expanded and the {slug} placeholder is replaced with the
#      session_dir_slug of PROJECT_DIR, matching Claude Code's own naming for
#      ~/.claude/projects/<slug>/.
#
#   2. REMEMBER_CONFIG — the merged config.json that every caller reads via
#      config(). Built by deep-merging three layers (highest priority wins):
#        1. ${PIPELINE_DIR}/config.json          (plugin-bundled defaults)
#        2. ${HOME}/.remember/config.json         (user-global, survives updates)
#        3. ${REMEMBER_DIR}/config.json           (per-project override)
#
# USAGE
#   source "$(dirname "$0")/resolve-paths.sh"   # sets PROJECT_DIR, PIPELINE_DIR
#   source "$(dirname "$0")/detect-tools.sh"    # sets session_dir_slug
#   source "$(dirname "$0")/lib-memory-dir.sh"  # exports REMEMBER_DIR, REMEMBER_CONFIG
#
# REQUIRES
#   PROJECT_DIR    — set by resolve-paths.sh
#   PIPELINE_DIR   — set by resolve-paths.sh
#   session_dir_slug — defined by detect-tools.sh
#
# EXPORTS
#   REMEMBER_DIR      — absolute path to memory data directory
#   REMEMBER_CONFIG   — absolute path to merged config (tmp file)
#
# ============================================================================

# Guard against double-sourcing. Use default-expansion so set -u callers don't error.
[ -n "${_LIB_MEMORY_DIR_LOADED:-}" ] && return 0
_LIB_MEMORY_DIR_LOADED=1

# ── Helpers ──────────────────────────────────────────────────────────────────

# _read_data_dir <config-file>
# Prints the raw data_dir value from a single config file, empty if absent.
_read_data_dir() {
    local cfg="$1"
    [ -f "$cfg" ] || return 0
    if command -v jq >/dev/null 2>&1; then
        jq -r '.data_dir // empty' "$cfg" 2>/dev/null || true
    else
        # Minimal grep fallback — handles simple string values only.
        grep -o '"data_dir"[[:space:]]*:[[:space:]]*"[^"]*"' "$cfg" 2>/dev/null \
            | sed 's/.*"data_dir"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/'
    fi
}

# _resolve_remember_dir <data_dir_value> <project_dir>
# Resolves the final absolute REMEMBER_DIR.
# If data_dir starts with / or ~ treat as absolute; expand ~ and {slug}.
# Otherwise treat as a path relative to PROJECT_DIR (legacy behaviour).
_resolve_remember_dir() {
    local data_dir="$1"
    local proj="$2"

    case "$data_dir" in
        /*|~*|[A-Za-z]:/*|[A-Za-z]:\\*)
            # Absolute / home-relative: expand ~ and substitute {slug}.
            # Drive-letter forms (C:/... and C:\...) are absolute on Windows /
            # Git Bash — without them a Windows data_dir is wrongly treated as
            # relative and prepended to PROJECT_DIR (path doubling).
            # Guard: session_dir_slug may not be defined if detect-tools.sh was
            # not sourced yet (e.g. log.sh sourced directly in tests). Define a
            # minimal inline fallback so the slug is never silently empty.
            if ! type session_dir_slug >/dev/null 2>&1; then
                session_dir_slug() {
                    local _p="$1"
                    command -v cygpath >/dev/null 2>&1 && _p=$(cygpath -m "$_p")
                    echo "$_p" | sed 's/[^a-zA-Z0-9]/-/g'
                }
            fi
            local slug
            slug=$(session_dir_slug "$proj")
            # shellcheck disable=SC2016  # we want literal ~ expansion here
            local expanded="${data_dir/#\~/$HOME}"
            echo "${expanded//\{slug\}/$slug}"
            ;;
        *)
            # Relative (legacy): resolve against PROJECT_DIR.
            echo "${proj}/${data_dir}"
            ;;
    esac
}

# ── Pass 1: resolve REMEMBER_DIR ─────────────────────────────────────────────
# Read data_dir from the plugin-bundled config and the user-global config only
# (the per-project config lives inside REMEMBER_DIR, which we don't know yet).

_bundled_cfg="${PIPELINE_DIR}/config.json"
_user_cfg="${HOME}/.remember/config.json"

# Highest-priority source that has data_dir wins.
_data_dir_raw=""
for _cfg_candidate in "$_user_cfg" "$_bundled_cfg"; do
    _val=$(_read_data_dir "$_cfg_candidate")
    if [ -n "$_val" ]; then
        _data_dir_raw="$_val"
        break
    fi
done

# Default to legacy layout if nothing found.
_data_dir_raw="${_data_dir_raw:-.remember}"

REMEMBER_DIR=$(_resolve_remember_dir "$_data_dir_raw" "$PROJECT_DIR")
export REMEMBER_DIR

# ── Pass 2: layered config merge ─────────────────────────────────────────────
# Now that REMEMBER_DIR is known, merge all three layers.

_project_cfg="${REMEMBER_DIR}/config.json"
SYS_TMPDIR="${TMPDIR:-/tmp}"
_merged_cfg="${SYS_TMPDIR}/remember-config-$$.json"

# Build an array of files that actually exist.
_cfg_sources=()
[ -f "$_bundled_cfg"  ] && _cfg_sources+=("$_bundled_cfg")
[ -f "$_user_cfg"     ] && _cfg_sources+=("$_user_cfg")
[ -f "$_project_cfg"  ] && _cfg_sources+=("$_project_cfg")

if [ "${#_cfg_sources[@]}" -gt 0 ] && command -v jq >/dev/null 2>&1; then
    # Deep-merge: later files override earlier ones. Strip `_`-prefixed keys —
    # convention: `_*` are user-facing docs (_comments/_purpose/_notes), never runtime data.
    jq -s 'reduce .[] as $x ({}; . * $x) | with_entries(select(.key | startswith("_") | not))' "${_cfg_sources[@]}" > "$_merged_cfg" 2>/dev/null \
        || cp "$_bundled_cfg" "$_merged_cfg" 2>/dev/null
else
    # No jq, or no config files — fall back to the bundled defaults.
    cp "$_bundled_cfg" "$_merged_cfg" 2>/dev/null || echo '{}' > "$_merged_cfg"
fi

REMEMBER_CONFIG="$_merged_cfg"
export REMEMBER_CONFIG

# Register cleanup of the tmp file when the outermost script exits.
# Use a subshell-safe append to avoid overwriting any existing trap.
_existing_trap=$(trap -p EXIT 2>/dev/null | sed "s/trap -- '//;s/' EXIT//")
if [ -n "$_existing_trap" ]; then
    # shellcheck disable=SC2064
    trap "${_existing_trap}; rm -f '${_merged_cfg}'" EXIT
else
    # shellcheck disable=SC2064
    trap "rm -f '${_merged_cfg}'" EXIT
fi
unset _existing_trap

# Clean up local variables to avoid polluting the caller's namespace.
unset _bundled_cfg _user_cfg _project_cfg _cfg_sources _data_dir_raw _val _merged_cfg _cfg_candidate
