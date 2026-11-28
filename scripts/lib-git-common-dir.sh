#!/bin/bash

_remember_git_rev_parse() {
    local _d="$1"
    shift
    (
        cd "$_d" 2>/dev/null || exit 1
        unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE
        LC_ALL=C git rev-parse "$@" 2>&1
    )
}

_remember_git_answer_line() {
    local _line
    _RGC_LINE=""
    while IFS= read -r _line; do
        if [ -n "$_line" ] && [ "${_line#--}" = "$_line" ]; then
            _RGC_LINE="$_line"
        fi
    done <<< "$1"
}

_remember_git_common_dir() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local _d="$1" _out _arg
    [ -d "$_d" ] || return 1
    _RGC_LINE=""
    for _arg in "--path-format=absolute --git-common-dir" "--git-common-dir"; do
        if _out=$(_remember_git_rev_parse "$_d" $_arg); then
            _remember_git_answer_line "$_out"
            [ -n "$_RGC_LINE" ] && break
        elif [ "${_out#*not a git repository}" != "$_out" ]; then
            return 3
        fi
    done
    [ -n "$_RGC_LINE" ] || return 1
    _out="$_RGC_LINE"
    if [ "${_out#/}" = "$_out" ] && [ "${_out#[A-Za-z]:[/\\]}" = "$_out" ]; then
        _out="$_d/$_out"
    fi
    ( cd "$_out" 2>/dev/null && pwd -P ) || printf '%s' "$_out"
}
