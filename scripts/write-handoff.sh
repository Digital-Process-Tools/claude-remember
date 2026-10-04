#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_WH_SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"
case "$_WH_SESSION_ID" in
    ''|[.]|[.][.]|*[!A-Za-z0-9._-]*) _WH_SESSION_ID="" ;;
esac

_wh_trial_remember_dir() {
    (
        CLAUDE_PROJECT_DIR="$1"
        export CLAUDE_PROJECT_DIR
        REMEMBER_PATHS_SOFT_FAIL=1 source "$SCRIPT_DIR/resolve-paths.sh" >/dev/null 2>&1 || exit 1
        source "$SCRIPT_DIR/lib-memory-dir.sh" >/dev/null 2>&1 || exit 1
        [ -n "${REMEMBER_DIR:-}" ] || exit 1
        printf '%s\n' "$REMEMBER_DIR"
    )
}

if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
    _WH_GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || _WH_GIT_ROOT=""

    if [ -n "$_WH_GIT_ROOT" ] && [ "$_WH_GIT_ROOT" != "$(pwd)" ] && [ -n "$_WH_SESSION_ID" ]; then
        _WH_RD_CWD=$(_wh_trial_remember_dir "$(pwd)") || _WH_RD_CWD=""
        _WH_RD_GITROOT=$(_wh_trial_remember_dir "$_WH_GIT_ROOT") || _WH_RD_GITROOT=""
        _WH_CWD_HAS_HINT=0
        _WH_GITROOT_HAS_HINT=0
        [ -n "$_WH_RD_CWD" ] && [ -f "$_WH_RD_CWD/tmp/handoff-path.$_WH_SESSION_ID" ] && _WH_CWD_HAS_HINT=1
        [ -n "$_WH_RD_GITROOT" ] && [ -f "$_WH_RD_GITROOT/tmp/handoff-path.$_WH_SESSION_ID" ] && _WH_GITROOT_HAS_HINT=1
        if [ "$_WH_CWD_HAS_HINT" = 1 ] && [ "$_WH_GITROOT_HAS_HINT" = 0 ]; then
            _WH_GIT_ROOT=""
        fi
        unset _WH_RD_CWD _WH_RD_GITROOT _WH_CWD_HAS_HINT _WH_GITROOT_HAS_HINT
    fi

    if [ -n "$_WH_GIT_ROOT" ]; then
        CLAUDE_PROJECT_DIR="$_WH_GIT_ROOT"
    else
        CLAUDE_PROJECT_DIR="$(pwd)"
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

source "$SCRIPT_DIR/lib-memory-context.sh"

_wh_shape_ok() {
    local _cand="$1" _parent _base
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    _parent="${_cand%/*}"
    _base="${_cand##*/}"
    [ "$_parent" = "$REMEMBER_DIR" ] || return 1
    case "$_base" in
        remember.md) return 0 ;;
        remember.*.md)
            _variant="${_base#remember.}"
            _variant="${_variant%.md}"
            case "$_variant" in
                ''|*[!A-Za-z0-9._-]*) return 1 ;;
                *) return 0 ;;
            esac
            ;;
        *) return 1 ;;
    esac
}

_WH_TARGET=""


if [ -n "$_WH_SESSION_ID" ]; then
    _WH_SESSION_HINT_FILE="$REMEMBER_DIR/tmp/handoff-path.$_WH_SESSION_ID"
    if [ -f "$_WH_SESSION_HINT_FILE" ]; then
        _WH_HINTED=$(head -n 1 "$_WH_SESSION_HINT_FILE" 2>/dev/null)
        if [ -n "$_WH_HINTED" ] && _wh_shape_ok "$_WH_HINTED"; then
            _WH_TARGET="$_WH_HINTED"
        fi
    fi
fi

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

_WH_ROOT_SCRATCH="$REMEMBER_DIR"
while [ "${_WH_ROOT_SCRATCH%/}" != "$_WH_ROOT_SCRATCH" ] && [ "$_WH_ROOT_SCRATCH" != "/" ]; do
    _WH_ROOT_SCRATCH="${_WH_ROOT_SCRATCH%/}"
done
if [ "$_WH_ROOT_SCRATCH" = "/" ]; then
    _WH_REMEMBER_ROOT="/"
elif [ "${_WH_ROOT_SCRATCH#*/}" != "$_WH_ROOT_SCRATCH" ]; then
    _WH_REMEMBER_ROOT="${_WH_ROOT_SCRATCH%/*}"
    [ -n "$_WH_REMEMBER_ROOT" ] || _WH_REMEMBER_ROOT="/"
else
    printf -v _WH_REMEMBER_ROOT '\056'
fi
unset _WH_ROOT_SCRATCH

_WH_MEM_PROJ="${MEMORY_PROJECT_DIR:-}"
[ -n "$_WH_MEM_PROJ" ] || _WH_MEM_PROJ="$PROJECT_DIR"
if [ "$_WH_REMEMBER_ROOT" = "$_WH_MEM_PROJ" ]; then
    _WH_TRACKED_STATE=""
    _remember_file_tracked_state_into _WH_TRACKED_STATE "$_WH_TARGET"
    if _remember_tracked_state_is_refused "$_WH_TRACKED_STATE"; then
        case "$_WH_TRACKED_STATE" in
            tracked)
                echo "REFUSED: $_WH_TARGET is tracked by this repository's own git index -- this plugin never commits a memory file itself, so writing your note over it would report success and then have the next SessionStart refuse to show it back to you, blaming a commit you never made. If this file is genuinely yours: git rm --cached it, then retry." >&2
                ;;
            unavailable)
                echo "REFUSED: $_WH_TARGET could not be checked against this repository's git index (git is missing, or the check itself failed) -- refusing to write unverified." >&2
                ;;
            symlinked-ancestor)
                echo "REFUSED: $_WH_TARGET sits under a directory that is itself a symlink -- this plugin never creates a symlink inside a memory store, so writing through one would land your note somewhere outside the store you think you are writing to. If you did not create this symlink, treat it as planted and inspect what it points at before deleting it." >&2
                ;;
            *)
                echo "REFUSED: $_WH_TARGET could not be verified (tracked state: $_WH_TRACKED_STATE) -- refusing to write unverified." >&2
                ;;
        esac
        exit 1
    fi
fi

[ -d "$REMEMBER_DIR" ] || mkdir -p "$REMEMBER_DIR" 2>/dev/null

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
