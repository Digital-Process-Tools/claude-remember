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
#
#   Two environment facts it does not trust:
#   - The language of git. "Not a repository" is recognised from the message
#     itself, so git runs with LC_ALL=C in its OWN environment (a local LC_ALL in
#     this shell is not exported to it). Without that a French or German user reads
#     every non-git project as "could not tell".
#   - The age of git. Before 2.31 git does not know --path-format: it echoes the
#     option back on stdout, exits 0, and answers with a RELATIVE path. Lines
#     starting with -- are discarded, and a relative answer is resolved against
#     <dir>, never against the working directory of the caller.
#   Outer GIT_* variables are cleared for the call: a leaked one makes every
#   lookup answer about the leaked repository instead of <dir>.

_remember_git_rev_parse() {
    local _d="$1"
    shift
    (
        cd "$_d" 2>/dev/null || exit 1
        unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE
        LC_ALL=C git rev-parse "$@" 2>&1
    )
}

# Sets _RGC_LINE to the last output line that is a path (not empty, not an
# echoed option). Assigns rather than prints: no subshell on the path of the caller.
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
        # shellcheck disable=SC2086  # the two spellings are deliberately word-split
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
