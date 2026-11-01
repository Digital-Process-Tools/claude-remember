#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/lib-session-store.sh"

_WH_SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"
if [ -z "${_WH_SESSION_ID#.}" ] || [ -z "${_WH_SESSION_ID#..}" ] \
    || [[ "$_WH_SESSION_ID" == *[!A-Za-z0-9._-]* ]]; then
    _WH_SESSION_ID=""
fi

_wh_trial_remember_dir() {
    _remember_session_store_trial_dir "$1"
}

_WH_POINTER_DIR=""
if [ -n "$_WH_SESSION_ID" ]; then
    _WH_POINTER_VAL=$(_remember_session_store_read "$_WH_SESSION_ID") && _WH_POINTER_DIR="$_WH_POINTER_VAL"
    unset _WH_POINTER_VAL
fi

_WH_AMBIGUOUS=0
if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
    _WH_GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || _WH_GIT_ROOT=""

    if [ -z "$_WH_POINTER_DIR" ] && [ -n "$_WH_GIT_ROOT" ] && [ "$_WH_GIT_ROOT" != "$(pwd)" ] && [ -n "$_WH_SESSION_ID" ]; then
        _remember_session_store_ancestor_candidate "$_WH_GIT_ROOT" "$_WH_SESSION_ID"
        _WH_GIT_ROOT="$_REMEMBER_SESSION_STORE_RESULT"
        _WH_AMBIGUOUS="$_REMEMBER_SESSION_STORE_AMBIGUOUS"
    fi

    if [ -n "$_WH_GIT_ROOT" ]; then
        CLAUDE_PROJECT_DIR="$_WH_GIT_ROOT"
    else
        CLAUDE_PROJECT_DIR="$(pwd)"
    fi
    export CLAUDE_PROJECT_DIR
fi

if [ -z "$_WH_POINTER_DIR" ] && [ "$_WH_AMBIGUOUS" = 1 ]; then
    echo "REFUSED: more than one candidate store matched this session, and no session-start pointer exists to disambiguate -- restart this session (or let its next SessionStart run) rather than risk writing the handoff note to the wrong one." >&2
    exit 1
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

if [ -n "$_WH_POINTER_DIR" ]; then
    REMEMBER_DIR="$_WH_POINTER_DIR"
    REMEMBER_DIR_UNSAFE=0
fi

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
    if [ "$_base" = remember.md ]; then
        return 0
    fi
    local _rest="${_base#remember.}"
    if [ "$_rest" = "$_base" ] || [ "${_rest%.md}" = "$_rest" ]; then
        return 1
    fi
    _variant="${_rest%.md}"
    if [ -z "$_variant" ] || [[ "$_variant" == *[!A-Za-z0-9._-]* ]]; then
        return 1
    fi
    return 0
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
        if [ "$_WH_TRACKED_STATE" = tracked ]; then
            echo "REFUSED: $_WH_TARGET is tracked by this repository's own git index -- this plugin never commits a memory file itself, so writing your note over it would report success and then have the next SessionStart refuse to show it back to you, blaming a commit you never made. If this file is genuinely yours: git rm --cached it, then retry." >&2
        elif [ "$_WH_TRACKED_STATE" = unavailable ]; then
            echo "REFUSED: $_WH_TARGET could not be checked against this repository's git index (git is missing, or the check itself failed) -- refusing to write unverified." >&2
        elif [ "$_WH_TRACKED_STATE" = symlinked-ancestor ]; then
            echo "REFUSED: $_WH_TARGET sits under a directory that is itself a symlink -- this plugin never creates a symlink inside a memory store, so writing through one would land your note somewhere outside the store you think you are writing to. If you did not create this symlink, treat it as planted and inspect what it points at before deleting it." >&2
        else
            echo "REFUSED: $_WH_TARGET could not be verified (tracked state: $_WH_TRACKED_STATE) -- refusing to write unverified." >&2
        fi
        exit 1
    fi
fi

[ "${REMEMBER_DIR_UNSAFE:-0}" = 1 ] || [ -d "$REMEMBER_DIR" ] || mkdir -p "$REMEMBER_DIR" 2>/dev/null

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
