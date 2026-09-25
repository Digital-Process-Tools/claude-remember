#!/usr/bin/env bash
# ============================================================================
# write-handoff.sh — write a handoff note to the ONE path this session
# resolved, never a path chosen by the model or parsed out of a transcript
# ============================================================================
#
# DESCRIPTION
#   The /remember skill used to instruct the model to parse its Write target
#   out of "the most recent === HANDOFF === block in this session's context"
#   -- text that any untrusted content the session ingested (a Read of a
#   hostile README, a fetched page, an issue body, or a repo-committed
#   .remember/remember.md the SessionStart hook itself cats into context,
#   #721) can supply, with Write pre-approved for any path (#720). This
#   script closes that off structurally: it takes no destination argument at
#   all. The note comes in on stdin; the destination is derived the same way
#   session-start-hook.sh derives REMEMBER_DIR, from config, never from
#   anything the model said or read.
#
# RESOLUTION
#   1. Source resolve-paths.sh + lib-memory-dir.sh to get REMEMBER_DIR, the
#      same way the hook does. CLAUDE_PROJECT_DIR is not exported to the
#      Bash tool (#207, see doctor.sh). Claude Code runs the Bash tool with
#      the project directory as its INITIAL cwd, but that cwd is the same
#      shell state across every command in one Bash-tool call -- a `cd`
#      earlier in the call persists, so a bare `$PWD` fallback can be a
#      subdirectory of the project by the time this script runs, not the
#      project root itself (#743). This prefers `git rev-parse
#      --show-toplevel` when the cwd is inside a git repo -- the common
#      case, and immune to an earlier `cd` -- falling back to the plain
#      $PWD default only outside a git repo, same as before.
#   2. If the SessionStart hook this session left a resolved path at
#      $REMEMBER_DIR/tmp/handoff-path (written every session start, #720),
#      and that path's shape passes the check below, use it -- this is what
#      makes per_session mode's remember.<id>.md work without this script
#      ever seeing the session id itself.
#   3. Otherwise fall back to $REMEMBER_DIR/remember.md, the same hardcoded
#      default the skill used to fall back to.
#
# SHAPE CHECK (defense in depth, applied to BOTH paths above)
#   The resolved path's parent directory must be exactly $REMEMBER_DIR, and
#   its basename must be `remember.md` or `remember.<token>.md` where
#   <token> is restricted to [A-Za-z0-9._-]+ -- the same allowlist
#   session-start-hook.sh already applies to CURRENT_SESSION_ID before ever
#   using it in a filename. A hint file that fails this check is refused,
#   not silently widened to "write wherever it says".
#
# USAGE
#   The note is untrusted content (#742): the caller must pipe it in behind
#   a fresh, unpredictable heredoc terminator each time -- never the literal
#   `EOF` -- since a note containing a line matching the terminator exactly
#   would end the heredoc early and let the remainder be parsed as shell in
#   this same call.
#
#     bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-handoff.sh" <<'HANDOFF_<random>'
#     <handoff note>
#     HANDOFF_<random>
#
#   Always prints exactly one of:
#     Wrote handoff to: <path>
#     REFUSED: <reason>
#   never silence -- an unattended overwrite with no visible destination is
#   the exposure #720 and #721 exist to close, not something to keep in a
#   different form.
#
# EXIT CODES
#   0  written
#   1  refused (bad shape, unresolvable REMEMBER_DIR, or write failed)
# ============================================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
    # #743: prefer the git top level over a possibly-stale $PWD -- immune to
    # a `cd` that happened earlier in the same Bash-tool call. Falls back to
    # $PWD, unchanged, when the cwd is not inside a git repo at all.
    _WH_GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || _WH_GIT_ROOT=""
    if [ -n "$_WH_GIT_ROOT" ]; then
        CLAUDE_PROJECT_DIR="$_WH_GIT_ROOT"
    else
        CLAUDE_PROJECT_DIR="$PWD"
    fi
    export CLAUDE_PROJECT_DIR
fi

_WH_RESOLVE_ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/remember-write-handoff-resolve-XXXXXX" 2>/dev/null) || _WH_RESOLVE_ERR_FILE=""
if [ -n "$_WH_RESOLVE_ERR_FILE" ]; then
    REMEMBER_PATHS_SOFT_FAIL=1 source "$SCRIPT_DIR/resolve-paths.sh" 2>"$_WH_RESOLVE_ERR_FILE"
else
    REMEMBER_PATHS_SOFT_FAIL=1 source "$SCRIPT_DIR/resolve-paths.sh" 2>/dev/null
fi
_WH_RESOLVE_STATUS=$?
if [ -n "$_WH_RESOLVE_ERR_FILE" ]; then
    _WH_RESOLVE_ERR=$(cat "$_WH_RESOLVE_ERR_FILE" 2>/dev/null)
    rm -f "$_WH_RESOLVE_ERR_FILE" 2>/dev/null
fi

if [ "$_WH_RESOLVE_STATUS" -ne 0 ]; then
    echo "REFUSED: path resolution failed: ${_WH_RESOLVE_ERR:-unknown error}" >&2
    exit 1
fi

source "$SCRIPT_DIR/lib-memory-dir.sh"

if [ -z "${REMEMBER_DIR:-}" ]; then
    echo "REFUSED: REMEMBER_DIR did not resolve" >&2
    exit 1
fi

# _wh_shape_ok <candidate-path>
# The parent must be exactly REMEMBER_DIR (string compare, both already
# forward-slash-normalized by resolve-paths.sh's own convention) and the
# basename must be remember.md or remember.<safe-token>.md.
_wh_shape_ok() {
    local _cand="$1" _parent _base
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    _parent="${_cand%/*}"
    _base="${_cand##*/}"
    [ "$_parent" = "$REMEMBER_DIR" ] || return 1
    case "$_base" in
        remember.md) return 0 ;;
        remember.*.md)
            _token="${_base#remember.}"
            _token="${_token%.md}"
            case "$_token" in
                ''|*[!A-Za-z0-9._-]*) return 1 ;;
                *) return 0 ;;
            esac
            ;;
        *) return 1 ;;
    esac
}

_WH_TARGET=""

# Session-keyed hint, checked FIRST (#738). $REMEMBER_DIR/tmp/handoff-path is
# published by EVERY SessionStart, project-wide -- in per_session mode the
# last session to start owns that one file, so an earlier session's later
# /remember read a pointer some other, more-recently-started session had
# already overwritten and clobbered that session's own remember.<id>.md.
# The value being published (a resolved handoff path) is per-SESSION; the
# channel it was published over was per-PROJECT -- one shared, last-writer-
# wins file. session-start-hook.sh now ALSO publishes a copy keyed by that
# session's own id, $REMEMBER_DIR/tmp/handoff-path.<session_id>, which
# nothing else can overwrite. This script can only read its OWN copy of
# that file if it knows which session it is running as -- CLAUDE_CODE_SESSION_ID
# is the answer: an env var Claude Code itself sets for the Bash tool
# (observed live, macOS, this session; distinct from CLAUDE_PROJECT_DIR,
# which #207 already established is hook-only), never a value the model
# reads, asserts or can forge the way transcript text could pre-#720.
#
# Same character allowlist CURRENT_SESSION_ID is sanitized against in
# session-start-hook.sh (#270) -- this value did not arrive through that
# hook's stdin JSON here, so it gets the same point-of-entry validation
# before it is ever used to build a path.
_WH_SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"
case "$_WH_SESSION_ID" in
    ''|.|..|*[!A-Za-z0-9._-]*) _WH_SESSION_ID="" ;;
esac

if [ -n "$_WH_SESSION_ID" ]; then
    _WH_SESSION_HINT_FILE="$REMEMBER_DIR/tmp/handoff-path.$_WH_SESSION_ID"
    if [ -f "$_WH_SESSION_HINT_FILE" ]; then
        _WH_HINTED=$(head -n 1 "$_WH_SESSION_HINT_FILE" 2>/dev/null)
        if [ -n "$_WH_HINTED" ] && _wh_shape_ok "$_WH_HINTED"; then
            _WH_TARGET="$_WH_HINTED"
        fi
    fi
fi

# Fallback: no usable session id reached this script, or no session-keyed
# hint file exists yet (single/external mode, or a session that predates
# this fix) -- same shared-pointer lookup as before #738, unchanged.
if [ -z "$_WH_TARGET" ]; then
    _WH_HINT_FILE="$REMEMBER_DIR/tmp/handoff-path"
    if [ -f "$_WH_HINT_FILE" ]; then
        _WH_HINTED=$(head -n 1 "$_WH_HINT_FILE" 2>/dev/null)
        if [ -n "$_WH_HINTED" ] && _wh_shape_ok "$_WH_HINTED"; then
            _WH_TARGET="$_WH_HINTED"
        fi
    fi
fi

if [ -z "$_WH_TARGET" ]; then
    _WH_TARGET="$REMEMBER_DIR/remember.md"
fi

if ! _wh_shape_ok "$_WH_TARGET"; then
    echo "REFUSED: resolved target does not have the expected shape: $_WH_TARGET" >&2
    exit 1
fi

[ -d "$REMEMBER_DIR" ] || mkdir -p "$REMEMBER_DIR" 2>/dev/null

# mktemp, not a $$-suffixed literal path -- the exact hazard
# lib-memory-dir.sh's own merged-config write already documents at length
# for this SAME directory: a name built from the PID is predictable from
# the outside the instant this process starts, and a symlink pre-seeded at
# that predictable name would have the note's content written straight
# through it by `cat >`, then `mv -f` would move the symlink itself (not
# its target) onto $_WH_TARGET. mktemp both creates the file atomically
# and names it unpredictably. No trailing content after the X's: BSD/macOS
# mktemp only randomizes a run of X's at the very end of the template.
_WH_TMP=$(mktemp "${_WH_TARGET%/*}/.write-handoff-XXXXXX" 2>/dev/null) || _WH_TMP=""
if [ -z "$_WH_TMP" ]; then
    echo "REFUSED: could not create a temp file to write $_WH_TARGET" >&2
    exit 1
fi
if ! cat > "$_WH_TMP" 2>/dev/null; then
    rm -f "$_WH_TMP" 2>/dev/null
    echo "REFUSED: could not write $_WH_TARGET" >&2
    exit 1
fi
if ! mv -f "$_WH_TMP" "$_WH_TARGET" 2>/dev/null; then
    rm -f "$_WH_TMP" 2>/dev/null
    echo "REFUSED: could not write $_WH_TARGET" >&2
    exit 1
fi

echo "Wrote handoff to: $_WH_TARGET"
exit 0
