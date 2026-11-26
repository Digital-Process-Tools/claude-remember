#!/bin/bash
# ============================================================================
# lib-git-common-dir.sh -- which git repository does this directory belong to (#1134)
# ============================================================================
#
# The one place that question is answered, for save-session.sh (which asks while
# the project is still live and hands the answer to the after_save hooks) and the
# detached after_save git-backup hook. Kept OUT of lib-memory-dir.sh on purpose:
# that file is sourced on every tool call and is pinned byte for byte against the
# per-tool-call cost tests (tests/test_case_divergence_298.py), and none of this
# belongs on that path.
#
# _remember_git_common_dir <dir>
#   Three answers, never two -- an empty string must not stand for both "not a
#   repository" and "could not look":
#     rc 0  prints the resolved absolute common dir
#     rc 3  <dir> exists and git says it is NOT inside a repository
#     rc 1  could not tell (no such dir, git failed for another reason)
#   Outer GIT_* variables are cleared for the call: a leaked one makes every
#   lookup answer about the leaked repository instead of <dir>.

_remember_git_rev_parse() {
    local _d="$1"
    shift
    ( cd "$_d" 2>/dev/null && env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR -u GIT_INDEX_FILE git rev-parse "$@" 2>&1 )
}

_remember_git_common_dir() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local _d="$1" _out
    [ -d "$_d" ] || return 1
    if ! _out=$(_remember_git_rev_parse "$_d" --path-format=absolute --git-common-dir); then
        case "$_out" in *"not a git repository"*) return 3 ;; esac
        # Old git without --path-format: resolve the relative form ourselves.
        if ! _out=$(_remember_git_rev_parse "$_d" --git-common-dir); then
            case "$_out" in *"not a git repository"*) return 3 ;; esac
            return 1
        fi
        _out="${_out##*$'\n'}"
        [ -n "$_out" ] || return 1
        if [ "${_out#/}" = "$_out" ] && [ "${_out#[A-Za-z]:[/\\]}" = "$_out" ]; then
            _out="$_d/$_out"
        fi
    fi
    _out="${_out##*$'\n'}"
    [ -n "$_out" ] || return 1
    ( cd "$_out" 2>/dev/null && pwd -P ) || printf '%s' "$_out"
}
