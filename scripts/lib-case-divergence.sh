#!/usr/bin/env bash

[ -n "${_REMEMBER_LIB_CASE_DIVERGENCE_LOADED:-}" ] && return 0
_REMEMBER_LIB_CASE_DIVERGENCE_LOADED=1

_remember_case_fold_eq() {
    local _was=0 _rc
    shopt -q nocasematch && _was=1
    shopt -s nocasematch
    [[ "$1" == "$2" ]]
    _rc=$?
    [ "$_was" -eq 1 ] || shopt -u nocasematch
    return $_rc
}

remember_case_divergence() {
    REMEMBER_CASE_STATUS="not-applicable"
    REMEMBER_CASE_RESOLVED=""
    REMEMBER_CASE_ROOT=""
    REMEMBER_CASE_DISK_STATE=""
    REMEMBER_CASE_DISK_REASON=""
    REMEMBER_CASE_DISK_NAMES=""
    REMEMBER_CASE_GIT_STATE=""
    REMEMBER_CASE_GIT_REASON=""
    REMEMBER_CASE_GIT_NAMES=""
    REMEMBER_CASE_MESSAGE=""

    [ -n "${REMEMBER_STORE_ROOT:-}" ] || return 0
    [ -n "${REMEMBER_DIR:-}" ] || return 0

    local _remember_case_div_dir _root _name
    _remember_case_div_dir=$(_remember_forward_slash "$REMEMBER_DIR")
    _root="${_remember_case_div_dir%/*}" _name="${_remember_case_div_dir##*/}"
    [ -n "$_root" ] && [ "$_root" != "$_remember_case_div_dir" ] || return 0
    [ -n "$_name" ] || return 0

    [ "$_root" != "$(_remember_forward_slash "${PROJECT_DIR:-}")" ] || return 0

    REMEMBER_CASE_RESOLVED="$_name"
    REMEMBER_CASE_ROOT="$_root"

    _remember_case_probe_disk "$_root" "$_name"
    _remember_case_probe_git "$_root" "$_name"

    if [ "$REMEMBER_CASE_DISK_STATE" = "diverged" ] || [ "$REMEMBER_CASE_GIT_STATE" = "diverged" ]; then
        REMEMBER_CASE_STATUS="diverged"
        _remember_case_compose_message
    elif [ "$REMEMBER_CASE_DISK_STATE" = "ok" ] && [ "$REMEMBER_CASE_GIT_STATE" = "ok" ]; then
        REMEMBER_CASE_STATUS="ok"
    else
        REMEMBER_CASE_STATUS="unavailable"
    fi
    return 0
}

_remember_case_probe_disk() {
    local _root="$1" _name="$2" _entry _base

    if [ ! -d "$_root" ] || [ ! -r "$_root" ] || [ ! -x "$_root" ]; then
        REMEMBER_CASE_DISK_STATE="unavailable"
        REMEMBER_CASE_DISK_REASON="store-root-unreadable"
        return 0
    fi

    for _entry in "$_root"/*; do
        [ -d "$_entry" ] || continue
        _base="${_entry##*/}"
        [ "$_base" = "$_name" ] && continue
        _remember_case_fold_eq "$_base" "$_name" || continue
        REMEMBER_CASE_DISK_NAMES="${REMEMBER_CASE_DISK_NAMES:+$REMEMBER_CASE_DISK_NAMES,}$_base"
    done

    if [ -n "$REMEMBER_CASE_DISK_NAMES" ]; then
        REMEMBER_CASE_DISK_STATE="diverged"
    else
        REMEMBER_CASE_DISK_STATE="ok"
    fi
    return 0
}

_remember_case_probe_git() {
    local _root="$1" _name="$2" _out _line _matched=0

    if [ ! -e "$_root/.git" ]; then
        REMEMBER_CASE_GIT_STATE="unavailable"
        REMEMBER_CASE_GIT_REASON="not-a-repository"
        return 0
    fi
    if ! command -v git >/dev/null 2>&1; then
        REMEMBER_CASE_GIT_STATE="unavailable"
        REMEMBER_CASE_GIT_REASON="git-not-installed"
        return 0
    fi

    _out=$(unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
           git -C "$_root" ls-tree --name-only HEAD 2>/dev/null) || _out=""

    if [ -z "$_out" ]; then
        REMEMBER_CASE_GIT_STATE="unavailable"
        REMEMBER_CASE_GIT_REASON="nothing-committed"
        return 0
    fi

    while IFS= read -r _line; do
        [ -n "$_line" ] || continue
        if [ "$_line" = "$_name" ]; then
            _matched=1
            continue
        fi
        if _remember_case_fold_eq "$_line" "$_name"; then
            _matched=1
            REMEMBER_CASE_GIT_NAMES="${REMEMBER_CASE_GIT_NAMES:+$REMEMBER_CASE_GIT_NAMES,}$_line"
        fi
    done <<< "$_out"

    if [ -n "$REMEMBER_CASE_GIT_NAMES" ]; then
        REMEMBER_CASE_GIT_STATE="diverged"
    elif [ "$_matched" -eq 1 ]; then
        REMEMBER_CASE_GIT_STATE="ok"
    else
        REMEMBER_CASE_GIT_STATE="unavailable"
        REMEMBER_CASE_GIT_REASON="store-not-tracked"
    fi
    return 0
}

_remember_case_compose_message() {
    local _lead=""

    if [ "$REMEMBER_CASE_DISK_STATE" = "diverged" ]; then
        _lead="this store's directory is spelled ${REMEMBER_CASE_DISK_NAMES} where this plugin resolves it as ${REMEMBER_CASE_RESOLVED}"
    fi
    if [ "$REMEMBER_CASE_GIT_STATE" = "diverged" ]; then
        if [ -n "$_lead" ]; then
            _lead="$_lead, and its backup repository tracks it as ${REMEMBER_CASE_GIT_NAMES}"
        else
            _lead="this store's backup repository tracks it as ${REMEMBER_CASE_GIT_NAMES} where this plugin resolves it as ${REMEMBER_CASE_RESOLVED}"
        fi
    fi

    REMEMBER_CASE_MESSAGE="remember: ${_lead} -- spellings that differ only in case. On a case-insensitive filesystem those are one and the same, which is why memory is being read and written normally and there is nothing to fix today. It would matter on a restore: checked out onto a case-sensitive filesystem they become separate directories, and this plugin would use only ${REMEMBER_CASE_RESOLVED}. Nothing here will rename or merge anything for you -- if you keep a backup of this store, check which spellings its HEAD holds before restoring it somewhere case-sensitive. Run /remember:doctor to see this again."
    return 0
}
