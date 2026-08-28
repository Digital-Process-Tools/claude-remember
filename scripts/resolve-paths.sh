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
#   REMEMBER_HOOK_CWD     Fallback project root (#411), consulted when
#                         CLAUDE_PROJECT_DIR is unset -- Codex and Gemini CLI
#                         never set the latter. Exported by
#                         session-start-hook.sh / session-end-hook.sh from the
#                         SessionStart/SessionEnd stdin payload's `cwd` field;
#                         not read from stdin here (see the caller comments).
#   PLUGIN_ROOT           Plugin install directory, vendor-neutral name (#407).
#                         Read before CLAUDE_PLUGIN_ROOT, which is honoured
#                         when PLUGIN_ROOT is absent -- see pipeline/host.py.
#   CLAUDE_PLUGIN_ROOT    Plugin install directory (set by Claude Code hooks;
#                         also set by Codex as a compatibility alias)
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
#   1. PLUGIN_ROOT (vendor-neutral name; Codex sets it natively) falling back
#      to CLAUDE_PLUGIN_ROOT (set by Claude Code for marketplace installs, and
#      by Codex as a compatibility alias it can withdraw -- pipeline/host.py's
#      PLUGIN_ROOT_VARS is the same precedence, mirrored by hand, and
#      test_host_shell_parity asserts the two agree)
#   2. Walk up from this script's real location to find the plugin root
#      (works for local installs where scripts/ is inside the plugin dir)
_SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
[ "$_SCRIPT_DIR" = "${BASH_SOURCE[0]}" ] && _SCRIPT_DIR="."
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

_REMEMBER_PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -n "$_REMEMBER_PLUGIN_ROOT" ]; then
    PIPELINE_DIR="$_REMEMBER_PLUGIN_ROOT"
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
#   1. CLAUDE_PROJECT_DIR (set by Claude Code — always correct, and the more
#      specific signal on the host that sets it, so it is tried first and a
#      disagreeing stdin cwd never overrides it)
#   2. REMEMBER_HOOK_CWD (#411) — the SessionStart/SessionEnd payload's `cwd`
#      field, exported by the hooks that already read stdin for `session_id`
#      and `transcript_path` (#206, #407). Codex and Gemini CLI both put `cwd`
#      on that payload but neither sets CLAUDE_PROJECT_DIR (Codex documents no
#      such variable at all; Gemini documents no hook environment variables
#      whatsoever), so this is the fallback that makes resolution possible on
#      either host. Not every caller of this file is a hook with stdin to
#      read -- doctor.sh and a bare `source` from a shell have none -- so an
#      unset or unusable value here is silently skipped, same as an unset
#      CLAUDE_PROJECT_DIR above.
#   3. If PIPELINE_DIR is inside a .claude/remember/ structure, derive from that
#   4. Fail — we cannot guess the project root from a marketplace cache path
if [ -n "$CLAUDE_PROJECT_DIR" ]; then
    PROJECT_DIR="$CLAUDE_PROJECT_DIR"
elif [ -n "${REMEMBER_HOOK_CWD:-}" ] && [ -d "${REMEMBER_HOOK_CWD:-}" ]; then
    PROJECT_DIR="$REMEMBER_HOOK_CWD"
elif [[ "$PIPELINE_DIR" == *"/.claude/remember" ]]; then
    # Local install: plugin is at $PROJECT/.claude/remember
    PROJECT_DIR="$(cd "$PIPELINE_DIR/../.." && pwd)"
else
    _msg="FATAL: Cannot resolve project root. CLAUDE_PROJECT_DIR is not set, REMEMBER_HOOK_CWD is not set or not a directory, and plugin is not in a local .claude/remember/ layout (PIPELINE_DIR=$PIPELINE_DIR)."
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
# All FOUR shapes, not just the POSIX one (#263). $CLAUDE_PROJECT_DIR does not
# always arrive in the same form on the same machine: the reporter's log carries
# `/c/Users/...` and `c:/Users/...` within a single day. Only the first matched,
# so only the first was normalised, and one directory produced two different
# slugs. NTFS is case-insensitive and hid that everywhere except git, whose
# pathspecs are not — `git add -- "$SLUG/"` matched nothing for twelve days and
# the backup reported an empty store.
#
# A path carrying no drive letter at all falls through untouched, which is what
# a genuine POSIX path under MSYS (/tmp, /usr) needs.
#
# The drive-form regex lives in a variable: a bracket expression containing a
# backslash is not portable to write inline on the right of `=~`.
_REMEMBER_WIN_DRIVE_RE='^([a-zA-Z]):[/\](.*)$'
case "$OSTYPE" in
    msys|cygwin)
        _drive=""
        _rest=""
        # Cygwin's mount prefix first: /cygdrive/c/... cannot match the MSYS
        # form below, because "cygdrive" is not one character.
        if [[ "$PROJECT_DIR" =~ ^/cygdrive/([a-zA-Z])/(.*)$ ]]; then
            _drive="${BASH_REMATCH[1]}"
            _rest="${BASH_REMATCH[2]}"
        elif [[ "$PROJECT_DIR" =~ ^/([a-zA-Z])/(.*)$ ]]; then
            _drive="${BASH_REMATCH[1]}"
            _rest="${BASH_REMATCH[2]}"
        elif [[ "$PROJECT_DIR" =~ $_REMEMBER_WIN_DRIVE_RE ]]; then
            _drive="${BASH_REMATCH[1]}"
            _rest="${BASH_REMATCH[2]}"
        fi
        if [ -n "$_drive" ]; then
            _drive=$(printf '%s' "$_drive" | tr '[:lower:]' '[:upper:]')
            _rest="${_rest//\//\\}"
            PROJECT_DIR="${_drive}:\\${_rest}"
        fi
        unset _drive _rest
        ;;
esac
unset _REMEMBER_WIN_DRIVE_RE

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
