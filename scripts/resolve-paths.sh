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

# _REMEMBER_PLUGIN_ROOT keeps this EXACT single-assignment shape --
# test_host_shell_parity_407.py::_shell_mirrored_vars() reads the two
# variable names straight off this line via regex, rather than a
# hand-copied list that would drift from pipeline/host.PLUGIN_ROOT_VARS
# unnoticed. Validation (#471) below is layered on top of it, not folded
# into the assignment itself, so that regex keeps matching.
_REMEMBER_PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -n "$_REMEMBER_PLUGIN_ROOT" ] && [ -f "$_REMEMBER_PLUGIN_ROOT/pipeline/haiku.py" ]; then
    PIPELINE_DIR="$_REMEMBER_PLUGIN_ROOT"
elif [ -n "${PLUGIN_ROOT:-}" ] && [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] \
        && [ "$_REMEMBER_PLUGIN_ROOT" != "$CLAUDE_PLUGIN_ROOT" ] \
        && [ -f "${CLAUDE_PLUGIN_ROOT}/pipeline/haiku.py" ]; then
    # PLUGIN_ROOT is generic and unnamespaced (#471): an unrelated tool
    # exporting it into a hook's environment would otherwise become this
    # plugin's execution root with no check that it contains this plugin's
    # code -- PIPELINE_DIR is subsequently the pipeline.shell import root,
    # sourced as scripts/log.sh, executed as scripts/save-session.sh, and
    # dispatched as hooks.d. A PLUGIN_ROOT that fails the same [ -f ] marker
    # check the local-install branch below already makes falls through to
    # the vendor alias instead of being trusted anyway -- turning a name
    # collision into a fallback rather than a wrong execution root. This
    # arm only fires when PLUGIN_ROOT was actually set and rejected (so
    # _REMEMBER_PLUGIN_ROOT came from it, not already from
    # CLAUDE_PLUGIN_ROOT) and names a genuinely different directory than
    # CLAUDE_PLUGIN_ROOT, which is validated here on its own merits.
    PIPELINE_DIR="$CLAUDE_PLUGIN_ROOT"
elif [ -f "$_PLUGIN_ROOT_CANDIDATE/pipeline/haiku.py" ]; then
    # Local install: scripts/ is one level below the plugin root
    PIPELINE_DIR="$_PLUGIN_ROOT_CANDIDATE"
else
    _msg="FATAL: Cannot resolve plugin root. PLUGIN_ROOT/CLAUDE_PLUGIN_ROOT do not point at a valid plugin install (missing pipeline/haiku.py) and $_PLUGIN_ROOT_CANDIDATE/pipeline/haiku.py does not exist."
    _resolve_paths_fail "$_msg" "${CLAUDE_PROJECT_DIR:-.}/.remember/logs" || return 1
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
# never matches and the input is echoed back untouched.
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
# A FUNCTION (#448), not a one-shot block run after PROJECT_DIR was already
# chosen. The old ordering tested REMEMBER_HOOK_CWD's `[ -d ]` in whatever
# spelling the caller's payload happened to carry, then normalized only the
# already-selected PROJECT_DIR afterwards -- so a Windows-native cwd
# (backslash separators, or a bare drive letter) was tested in the form the
# shell cannot resolve and the branch was skipped before the code that would
# have fixed the spelling ever ran. On a host with no CLAUDE_PROJECT_DIR
# (Codex, Gemini CLI) that meant every hook silently fell through to the loud
# failure via its own `|| exit 0`.
#
# The fix normalizes every candidate BEFORE it is tested, uniformly:
# CLAUDE_PROJECT_DIR was never `-d`-tested before selection either (only
# trusted, then validated once at the very end below), so giving it the same
# normalize-then-trust treatment here is not a new exposure. REMEMBER_HOOK_CWD
# keeps its own existence test before selection -- now against the normalized
# form -- so a value that still doesn't resolve to a real directory falls
# through to the next candidate (the local-install derivation, or the loud
# failure) instead of being adopted and hard-failing later with a less
# specific message. Normalizing is pure string reshaping (drive letter case,
# separator direction) with no filesystem access of its own, so applying it to
# an unvalidated candidate before the existence test adds no new trust in a
# hostile REMEMBER_HOOK_CWD (#417, #424) -- the value still has to name a real
# directory to be selected, exactly as before.
#
# The drive-form regex lives in a variable: a bracket expression containing a
# backslash is not portable to write inline on the right of `=~`.
_remember_normalize_win_path() {
    local _in="$1" _drive="" _rest=""
    local _re='^([a-zA-Z]):[/\](.*)$'
    case "$OSTYPE" in
        msys|cygwin)
            # Cygwin's mount prefix first: /cygdrive/c/... cannot match the
            # MSYS form below, because "cygdrive" is not one character.
            if [[ "$_in" =~ ^/cygdrive/([a-zA-Z])/(.*)$ ]]; then
                _drive="${BASH_REMATCH[1]}"
                _rest="${BASH_REMATCH[2]}"
            elif [[ "$_in" =~ ^/([a-zA-Z])/(.*)$ ]]; then
                _drive="${BASH_REMATCH[1]}"
                _rest="${BASH_REMATCH[2]}"
            elif [[ "$_in" =~ $_re ]]; then
                _drive="${BASH_REMATCH[1]}"
                _rest="${BASH_REMATCH[2]}"
            fi
            if [ -n "$_drive" ]; then
                _drive=$(printf '%s' "$_drive" | tr '[:lower:]' '[:upper:]')
                _rest="${_rest//\//\\}"
                printf '%s' "${_drive}:\\${_rest}"
                return 0
            fi
            ;;
    esac
    printf '%s' "$_in"
}

# --- Resolve PROJECT_DIR (the user's project root) ---
#
# Priority:
#   1. CLAUDE_PROJECT_DIR (set by Claude Code — always correct, and the more
#      specific signal on the host that sets it, so it is tried first and a
#      disagreeing stdin cwd never overrides it)
#   2. REMEMBER_HOOK_CWD (#411, #444) — the host's own hook-event payload's
#      `cwd` field, exported by the hook that read this file, from its own
#      stdin -- every hook this plugin registers now offers one (session-start
#      and session-end since #411; user-prompt and post-tool since #444).
#      Codex and Gemini CLI both put `cwd` on that payload but neither sets
#      CLAUDE_PROJECT_DIR (Codex documents no such variable at all; Gemini
#      documents no hook environment variables whatsoever), so this is the
#      fallback that makes resolution possible on either host. Not every
#      caller of this file is a hook with stdin to read -- doctor.sh and a
#      bare `source` from a shell have none -- so an unset or unusable value
#      here is silently skipped, same as an unset CLAUDE_PROJECT_DIR above.
#      Normalized (#448) before the existence test immediately below, so a
#      Windows-native spelling is tested in the form the shell can resolve.
#
#      ASSUMPTION (#417): this file only SOURCES the variable, it never sets
#      it, so its correctness here depends on every caller either exporting a
#      freshly-validated value from its own stdin payload (every hook that
#      reaches this file does, as of #444, each rejecting embedded newlines)
#      or clearing it before sourcing this file when it has none to offer
#      (every non-hook caller, and any hook on a run where stdin carried no
#      usable `cwd` -- the `unset` at the top of each hook's file runs first,
#      before its own stdin read, for exactly that reason).
#      The assumption this whole chain rests on is that no supported host
#      reuses one process environment across separate hook invocations within
#      a project-agnostic dispatcher; on Claude Code CLAUDE_PROJECT_DIR always
#      wins first and this arm never runs. If that assumption is ever wrong,
#      a value exported by one session's SessionStart could be inherited by a
#      later PostToolUse/UserPromptSubmit invocation from a DIFFERENT project
#      that reused the same environment -- reachability was not established
#      either way when this comment was written; see #417.
#   3. If PIPELINE_DIR is inside a .claude/remember/ structure, derive from that
#   4. Fail — we cannot guess the project root from a marketplace cache path
if [ -n "$CLAUDE_PROJECT_DIR" ]; then
    PROJECT_DIR="$(_remember_normalize_win_path "$CLAUDE_PROJECT_DIR")"
elif [ -n "${REMEMBER_HOOK_CWD:-}" ] && [ -d "$(_remember_normalize_win_path "${REMEMBER_HOOK_CWD:-}")" ]; then
    PROJECT_DIR="$(_remember_normalize_win_path "$REMEMBER_HOOK_CWD")"
elif [[ "$PIPELINE_DIR" == *"/.claude/remember" ]]; then
    # Local install: plugin is at $PROJECT/.claude/remember
    PROJECT_DIR="$(cd "$PIPELINE_DIR/../.." && pwd)"
else
    _msg="FATAL: Cannot resolve project root. CLAUDE_PROJECT_DIR is not set, REMEMBER_HOOK_CWD is not set or not a directory, and plugin is not in a local .claude/remember/ layout (PIPELINE_DIR=$PIPELINE_DIR)."
    _resolve_paths_fail "$_msg" "${PROJECT_DIR:-.}/.remember/logs" || return 1
fi
unset -f _remember_normalize_win_path

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
