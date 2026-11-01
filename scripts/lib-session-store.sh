#!/usr/bin/env bash

_remember_session_store_dir() {
    printf '%s' "${HOME}/.remember/tmp/session-store"
}

_remember_session_store_shape_ok() {
    local _val="$1"
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    [ -n "$_val" ] || return 1
    [[ "$_val" == *[$'\n\r']* ]] && return 1
    [[ "$_val" == /* || "$_val" == [A-Za-z]:[/\\]* || "$_val" == [\\][\\][^\\]* ]] && return 0
    return 1
}

_remember_session_store_read() {
    local _sid="$1" _ptr _val
    [ -n "$_sid" ] || return 1
    _ptr="$(_remember_session_store_dir)/$_sid"
    [ -f "$_ptr" ] || return 1
    _val=$(head -n 1 "$_ptr" 2>/dev/null)
    _remember_session_store_shape_ok "$_val" || return 1
    printf '%s' "$_val"
    return 0
}

_remember_session_store_trial_dir() {
    local _lib_dir
    _lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    (
        CLAUDE_PROJECT_DIR="$1"
        export CLAUDE_PROJECT_DIR
        REMEMBER_PATHS_SOFT_FAIL=1 source "$_lib_dir/resolve-paths.sh" >/dev/null 2>&1 || exit 1
        source "$_lib_dir/lib-memory-dir.sh" >/dev/null 2>&1 || exit 1
        [ -n "${REMEMBER_DIR:-}" ] || exit 1
        printf '%s\n' "$REMEMBER_DIR"
    )
}

_remember_session_store_ancestor_candidate() {
    local _git_root="$1" _session_id="$2"
    _REMEMBER_SESSION_STORE_RESULT="$_git_root"
    _REMEMBER_SESSION_STORE_AMBIGUOUS=0
    if [ -z "$_git_root" ] || [ "$_git_root" = "$(pwd)" ] || [ -z "$_session_id" ]; then
        return 0
    fi
    local _walk_dir="$(pwd)" _hint_dir="" _hint_count=0 _rd _hint_file _walk_next
    while :; do
        _rd=$(_remember_session_store_trial_dir "$_walk_dir") || _rd=""
        if [ -n "$_rd" ]; then
            _hint_file="$_rd/tmp/handoff-path.$_session_id"
            if [ -f "$_hint_file" ]; then
                _hint_count=$((_hint_count + 1))
                _hint_dir="$_walk_dir"
            fi
        fi
        [ "$_walk_dir" -ef "$_git_root" ] && break
        _walk_next=$(dirname "$_walk_dir")
        [ "$_walk_next" = "$_walk_dir" ] && break
        _walk_dir="$_walk_next"
    done
    if [ "$_hint_count" -gt 1 ]; then
        _REMEMBER_SESSION_STORE_AMBIGUOUS=1
        return 0
    fi
    if [ "$_hint_count" -eq 1 ] && [ -n "$_hint_dir" ]; then
        _REMEMBER_SESSION_STORE_RESULT="$_hint_dir"
    fi
    return 0
}
