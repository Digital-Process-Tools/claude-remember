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
#   session_dir_slug — sourced from lib-slug.sh (no longer needs detect-tools.sh)
#
# EXPORTS
#   REMEMBER_DIR         — absolute path to memory data directory
#   REMEMBER_STORE_ROOT  — the directory the per-project stores sit in, and empty
#                          unless data_dir is absolute AND carries {slug} (#297)
#   REMEMBER_CONFIG      — absolute path to merged config (tmp file)
#
# ============================================================================

# Guard against double-sourcing. Use default-expansion so set -u callers don't error.
[ -n "${_LIB_MEMORY_DIR_LOADED:-}" ] && return 0
_LIB_MEMORY_DIR_LOADED=1

# session_dir_slug, from the one file that defines it. This used to be a naive
# inline fallback declared at the point of use — the pre-#144 implementation,
# carrying every bug #156 fixed, and live for user-prompt-hook.sh, which reaches
# here without sourcing detect-tools.sh (#158). Sourcing detect-tools.sh instead
# is not an option: it exits 1 when it finds no Python, taking its caller down.
_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
# A path with no slash in it (`source log.sh` from the scripts dir) leaves the
# filename behind, not a directory — `dirname` answered "." and this must too.
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="."
source "$_REMEMBER_SRC_DIR/lib-slug.sh"
unset _REMEMBER_SRC_DIR

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

# _resolve_memory_project_dir <project_dir>
# Returns the directory memory should be keyed to. Normally this is PROJECT_DIR
# itself. When PROJECT_DIR is a *linked git worktree*, Claude Code has set
# CLAUDE_PROJECT_DIR to the worktree path — but memory should live with the main
# checkout so it survives `git worktree remove` and is shared across worktrees
# of the same repo (issue #56). A linked worktree is detected via git's common
# dir differing from its git dir; the main checkout is the parent of the shared
# common dir.
#
# Fail-safe by design: it only redirects when it positively identifies a linked
# worktree whose main checkout is a real work tree. Ordinary checkouts, non-git
# directories, bare-repo worktrees, and old git without --path-format all fall
# through to PROJECT_DIR unchanged — identical to pre-fix behaviour. Only
# REMEMBER_DIR is affected; PROJECT_DIR stays the worktree path so session
# recovery still finds transcripts under the worktree slug.
_resolve_memory_project_dir() {
    local proj="$1"

    # A linked worktree's `.git` is a FILE (a `gitdir:` pointer); a main
    # checkout's is a DIRECTORY. A `.git` directory therefore settles "this is
    # not a linked worktree" with a shell builtin, and the ~200ms `git rev-parse`
    # below is not paid at all in the overwhelmingly common case (#230). That
    # matters because PostToolUse pays this on EVERY tool call.
    #
    # The POSITIVE answer only. The ABSENCE of `.git` proves nothing — a
    # subdirectory of a worktree has no `.git` entry and still needs the redirect
    # — so everything that is not a confirmed main checkout falls through to the
    # unchanged chain below. Written as an `if` rather than `[ … ] && { … }` so
    # the false branch cannot hand a non-zero status to a `set -e` caller.
    if [ -d "$proj/.git" ]; then
        echo "$proj"
        return 0
    fi

    command -v git >/dev/null 2>&1 || { echo "$proj"; return 0; }

    # One rev-parse yields both paths (common-dir first, git-dir second).
    # --path-format=absolute requires git >= 2.31; on older git this fails and
    # we fall through to the unchanged PROJECT_DIR.
    local _out _gcd _gd
    _out=$(git -C "$proj" rev-parse --path-format=absolute \
                --git-common-dir --git-dir 2>/dev/null) || _out=""
    { IFS= read -r _gcd; IFS= read -r _gd; } <<EOF
$_out
EOF

    # Not a git repo, unsupported flag, or an ordinary checkout (common == git):
    # leave PROJECT_DIR untouched.
    if [ -z "$_gcd" ] || [ -z "$_gd" ] || [ "$_gcd" = "$_gd" ]; then
        echo "$proj"
        return 0
    fi

    # Linked worktree: the main checkout is the parent of the shared git dir.
    # Guard against bare-repo worktrees (parent is not a work tree) by only
    # redirecting to a directory git confirms is inside a work tree.
    local _main
    _main=$(dirname "$_gcd")
    if [ -d "$_main" ] && \
       git -C "$_main" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "$_main"
    else
        echo "$proj"
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

# _set_store_root <data_dir_value>
# Sets REMEMBER_STORE_ROOT to the directory the per-project stores sit in: the
# data_dir template truncated at {slug}, with ~ expanded and trailing separators
# removed. Sets it EMPTY in every other case, and the emptiness is the interface
# (#297).
#
# A FUNCTION THAT ASSIGNS, not one that echoes, and it must never be called as
# `$(...)`. This file is sourced by bootstrap-dirs.sh and by log.sh, and
# post-tool-hook.sh reaches both on the tool call that resolves — the first of a
# session and the first after any config edit (#350 made the rest of them replay
# instead). session-start-hook.sh and save-session.sh reach it unconditionally.
# A command substitution here is a fork on every one of those, which is the cost
# #230 went to trouble removing and #296 refused to add back. Everything below
# is parameter expansion: no subshell, no external command, no measurable cost
# on that path.
#
# This exists because of a circularity #296 left open. The slug record lives at
# <REMEMBER_DIR>/tmp/session-slug, and in the layout config.user.example.json
# ships — "data_dir": "~/.remember/{slug}", under a _purpose that says to copy
# it — REMEMBER_DIR is itself named by the slug, so a caller had to know the
# answer to open the file holding it. The store root is the one path in that
# layout a caller CAN name: it is the part of the template before the
# placeholder, so the template alone yields it.
#
# It is deliberately empty when there is no {slug}. In the legacy layout
# (<project>/.remember) and in a single-directory external store, REMEMBER_DIR
# and the store root are the same directory and the record is already reachable
# from project_dir and the template — so there is nothing to publish, and the
# session-start index that reads this stays off. The common layout does not pay
# for the external one.
#
# A prefix of "/" or a bare drive is refused rather than accepted. It would put
# a plugin-owned file at /tmp/sessions, which on a shared world-writable
# directory is a hijack waiting to happen, and no one keeps a memory store at
# the filesystem root.
_set_store_root() {
    local data_dir="$1" prefix
    REMEMBER_STORE_ROOT=""

    # Same absolute/home-relative test as _resolve_remember_dir, including the
    # Windows drive-letter forms: a relative data_dir has no store root.
    case "$data_dir" in
        /*|~*|[A-Za-z]:/*|[A-Za-z]:\\*) ;;
        *) return 0 ;;
    esac
    case "$data_dir" in
        *'{slug}'*) ;;
        *) return 0 ;;
    esac

    prefix="${data_dir%%\{slug\}*}"
    # shellcheck disable=SC2016  # we want literal ~ expansion here
    prefix="${prefix/#\~/$HOME}"

    # Trailing separators, never down to nothing: the ?* guard keeps "/" whole
    # so the refusal below is what rejects it, rather than this loop emptying it.
    while :; do
        case "$prefix" in
            ?*/|?*\\) prefix="${prefix%?}" ;;
            *) break ;;
        esac
    done

    case "$prefix" in
        ''|/|[A-Za-z]:|[A-Za-z]:/|[A-Za-z]:\\) return 0 ;;
    esac

    REMEMBER_STORE_ROOT="$prefix"
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

# Key memory to the main checkout when PROJECT_DIR is a linked worktree, so it
# survives `git worktree remove` and is shared across worktrees (issue #56).
# For non-worktree / non-git projects this is exactly PROJECT_DIR.
MEMORY_PROJECT_DIR=$(_resolve_memory_project_dir "$PROJECT_DIR")
export MEMORY_PROJECT_DIR

REMEMBER_DIR=$(_resolve_remember_dir "$_data_dir_raw" "$MEMORY_PROJECT_DIR")
export REMEMBER_DIR

# Empty in every layout where REMEMBER_DIR is nameable without the slug (#297).
# Assigned, never `$(...)`: this runs on the per-tool-call path.
_set_store_root "$_data_dir_raw"
export REMEMBER_STORE_ROOT

# ── Pass 2: layered config merge ─────────────────────────────────────────────
# Now that REMEMBER_DIR is known, merge all three layers.

_project_cfg="${REMEMBER_DIR}/config.json"
SYS_TMPDIR="${TMPDIR:-/tmp}"
_merged_cfg="${SYS_TMPDIR}/remember-config-$$.json"

# Create it private BEFORE any layer is written into it. Every entry point
# sources resolve-paths.sh (umask 077, #68) first, so this is belt-and-braces
# there — but this file documents itself as sourceable on its own, and since
# the merged config can carry `haiku.oauth_token` (a live OAuth credential) its
# mode must not depend on the caller having set a umask. jq's `>`, the Python
# fallback's open(), and `cp` all write into the existing file and keep its mode.
(umask 077; : > "$_merged_cfg") 2>/dev/null || true

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
elif [ "${#_cfg_sources[@]}" -gt 0 ]; then
    # No jq — do the same deep-merge in Python instead of silently dropping
    # the user-global/per-project layers and copying only the bundled
    # defaults. Every override in ~/.remember/config.json or
    # ${REMEMBER_DIR}/config.json (time_format, model, cooldowns.*,
    # thresholds.*, git_backup.*) was previously invisible on any machine
    # without jq — this made config() (log.sh) irrelevant to those users.
    "${PYTHON:-python3}" - "$_merged_cfg" "${_cfg_sources[@]}" > /dev/null 2>&1 <<'PYMERGE' || cp "$_bundled_cfg" "$_merged_cfg" 2>/dev/null
import json
import sys


def deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = deep_merge(out[k], v) if k in out else v
        return out
    return b


out_path = sys.argv[1]
merged = {}
for path in sys.argv[2:]:
    with open(path) as f:
        merged = deep_merge(merged, json.load(f))
# Strip `_`-prefixed doc keys, top-level only — same convention as the jq path.
merged = {k: v for k, v in merged.items() if not str(k).startswith("_")}
with open(out_path, "w") as f:
    json.dump(merged, f)
PYMERGE
else
    # No config files at all — fall back to the bundled defaults.
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
