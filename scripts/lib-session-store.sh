#!/usr/bin/env bash
# ============================================================================
# lib-session-store.sh — the ONE resolver write-handoff.sh and doctor.sh both
# source, so a project's store is never guessed twice, by two independently
# drifting copies (#776/#827/#1058/#1082 history: doctor.sh carried a hand-
# ported duplicate of write-handoff.sh's own ancestor-walk fix).
# ============================================================================
#
# #1083: an ancestor walk, however tuned, can only ever see directories on
# the chain from $(pwd) up to the git toplevel -- a session-keyed hint
# anywhere else (a resumed session's hint at a SIBLING directory, a nested
# repo/submodule one level IN, a non-git sibling the walk never even enters
# for) is categorically invisible to it, no matter which tie-break rule picks
# among the ancestors it DOES see. #1058/PR #1081 tuned that tie-break twice
# (nearest-wins, then freshest-mtime-wins) and each tuning broke on a
# different counterexample in review.
#
# The fix pins the actual answer at SessionStart, when the project directory
# is still known for certain, into a pointer OUTSIDE every project store --
# $HOME/.remember/tmp/session-store/<session-id> -- so write-handoff.sh and
# doctor.sh never have to infer it from $(pwd) at all for any session that
# started after this file existed. The ancestor walk below survives only as
# a FALLBACK for a session that predates the pointer (no file reaches
# _remember_session_store_read for it) -- and even there, it no longer
# guesses among multiple candidates: it REFUSES, on the same principle the
# pointer itself is built on (an answer that is sometimes wrong is worse
# than a refusal that is always honest). A same-second write defeats mtime
# comparison outright (mtime is whole seconds, worse on FAT), so "pick the
# freshest" was never more than a plausible-looking guess.
# ============================================================================

# _remember_session_store_dir
# $HOME/.remember/tmp/session-store -- a single, cross-project directory,
# never under any REMEMBER_DIR: a pointer naming store A must be readable
# even when the cwd that triggered this read resolves, via the ancestor
# walk below, to a completely different store B.
_remember_session_store_dir() {
    printf '%s' "${HOME}/.remember/tmp/session-store"
}

# _remember_session_store_shape_ok <value>
# Same byte-wise shape resolve-paths.sh/lib-memory-dir.sh's own
# _remember_lmd_set_dir_unsafe applies to REMEMBER_DIR itself (#920): an
# absolute POSIX path, a drive-letter path, or a UNC path, with no embedded
# CR/LF. A pointer that fails this is treated exactly like a missing one --
# never trusted, never guessed around (#920's guard extends to this value
# too, not only to the REMEMBER_DIR each script resolves on its own).
_remember_session_store_shape_ok() {
    local _val="$1"
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    [ -n "$_val" ] || return 1
    [[ "$_val" == *[$'\n\r']* ]] && return 1
    [[ "$_val" == /* || "$_val" == [A-Za-z]:[/\\]* || "$_val" == [\\][\\][^\\]* ]] && return 0
    return 1
}

# _remember_session_store_read <session-id>
# Prints the REMEMBER_DIR this session's own SessionStart pinned, and
# returns 0, when the pointer exists, is non-empty, and passes the shape
# check above. Returns 1 -- never guesses -- otherwise: the caller falls
# back to the ancestor walk below.
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

# _remember_session_store_trial_dir <candidate-project-dir>
# Prints the REMEMBER_DIR that candidate would resolve to, by running the
# SAME resolve-paths.sh + lib-memory-dir.sh chain the real resolution uses,
# in a subshell -- so it can be tried repeatedly with neither trial able to
# leak an export, a cd, or a set -e/-u state change into the caller's own
# environment. Prints nothing and exits nonzero if the chain itself fails
# for that candidate. Shared by write-handoff.sh and doctor.sh (#1083) --
# previously each kept its own pasted copy of this exact function
# (_wh_trial_remember_dir / _doctor_trial_remember_dir, #776/#827).
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

# _remember_session_store_ancestor_candidate <git-root> <session-id>
# FALLBACK ONLY -- callers invoke this only once _remember_session_store_read
# above has already failed (a session that started before the pointer
# existed). Walks every directory from $(pwd) up to <git-root>, counting
# every ancestor whose own store carries this session's pre-existing
# per-project hint (tmp/handoff-path.<session-id>, #738/#1058).
#
# NOT called via command substitution -- a caller needs BOTH the resolved
# directory and the ambiguous flag back out, and `$(...)` forks a subshell
# that can only hand back one value on stdout; a flag set inside it is
# invisible to the caller (self-review: the first version of this function
# tried exactly that and the ambiguous flag silently never propagated).
# Call it directly and read its two globals afterward:
#   _REMEMBER_SESSION_STORE_RESULT      the resolved directory
#   _REMEMBER_SESSION_STORE_AMBIGUOUS   1 when more than one ancestor had a
#                                       hint, 0 otherwise
#
# #1083 review: the pre-#1083 version of this walk tie-broke an ambiguous
# result by taking the FRESHEST hint by mtime, which a same-second write
# defeats outright. The caller decides what a refusal looks like for its
# own output shape (write-handoff.sh's stderr convention is not doctor.sh's
# WARN/FAIL lines) -- this function only counts and reports, it never
# prints a user-facing message itself.
#
# _REMEMBER_SESSION_STORE_RESULT is the single matching ancestor when
# EXACTLY ONE has a hint, and <git-root> unchanged (ambiguous=0) when NONE
# do -- both unchanged from the pre-#1083 walk's own behaviour in those two
# cases.
#
# Termination does not depend on reaching <git-root>: it stops as soon as
# `dirname` makes no further progress, covering both POSIX "/" and an
# MSYS/Windows UNC root like "//server" (reasoned, not observed -- no MSYS
# environment was available to exercise the UNC shape directly).
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
