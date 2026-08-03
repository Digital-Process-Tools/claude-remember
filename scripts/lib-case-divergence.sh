#!/usr/bin/env bash
# ============================================================================
# lib-case-divergence.sh — is this store known by a second spelling? (#298)
# ============================================================================
#
# DESCRIPTION
#   Answers one question and repairs nothing: does the store directory this
#   session resolved differ, ONLY BY CASE, from the name the filesystem gives it
#   or the name its own git repository tracks?
#
#   #298 was filed on a premise that did not survive measurement. It claimed a
#   store created under the pre-#263 drive-letter spelling (C--Users-…) is
#   orphaned by the post-#263 resolver (c--Users-…). @jqit-ricky measured it on
#   the only Windows box any of this can be pointed at: Test-Path on both
#   spellings returns True for the SAME directory object, 88 files through
#   either name. NTFS is case-insensitive, so two stores differing only in case
#   is not a state that filesystem can be in. Nothing is stranded there, and the
#   orphaning cannot happen on the platform #263 came from.
#
#   What survived is smaller and sits one layer over. Git's index is
#   case-sensitive where NTFS is not:
#
#       on disk : C--Users-ricky-dev-jqit-project-b
#       in git  : c--Users-ricky-dev-jqit-project-b
#
#   That costs nothing while the store stays on NTFS. The git backup exists
#   precisely so the store can be restored elsewhere, and on a case-sensitive
#   volume — verified on a real case-sensitive APFS volume, not predicted — one
#   repository carrying both spellings clones to TWO directories with two
#   remember.md files, of which the resolver reads exactly one.
#
#   So this discloses, and only discloses. No rename, no merge, no migration:
#   a merge inside a git repository the user owns, for a case nobody has
#   reproduced, is not something to do speculatively. If the user should act,
#   the message says so in prose.
#
# WHAT IS COMPARED, AND WHAT IS NOT
#   Two probes, over the two places that can name this store:
#
#   * DISK — the directory names in the store root that case-fold to the one we
#     resolved. NOT "does a sibling store root exist": on NTFS there is never a
#     sibling, so a sibling check would be dead code on the platform it was
#     written for. There is one directory, and the question is whether it is
#     spelled the way we resolved it. This probe costs no fork at all.
#
#   * GIT — the top-level names in HEAD's tree of the store's own repository.
#     HEAD's tree, and deliberately not the history: a clone checks out HEAD, so
#     a history that carries both spellings while HEAD carries one is inert.
#     Whether any real store's history holds both is still an open question on
#     #298 and nothing here assumes an answer. One `git ls-tree`, once per
#     session start, and only for a store that has its own repository.
#
# STATES
#   ok             — compared, and they agree.
#   diverged       — a second spelling exists. Harmless today on a
#                    case-insensitive filesystem; it is a heads-up about a
#                    restore.
#   unavailable    — could not compare, with a reason. An absence produced by
#                    this check must never read as agreement; that conflation is
#                    this repo's recurring defect (#296, #299, and the 0.12.0
#                    entry's "three states, not two").
#   not-applicable — there is no comparison to make. The legacy layout and any
#                    store whose directory is not named by the slug: a literal
#                    name cannot have a second spelling, and the user's own
#                    project repository is not ours to inspect.
#
#   `unavailable` never outranks a `diverged` another probe found.
#
# USAGE
#   source lib-case-divergence.sh
#   remember_case_divergence          # ASSIGNS, never called as $(...)
#
# REQUIRES
#   REMEMBER_DIR, PROJECT_DIR, REMEMBER_STORE_ROOT (all from lib-memory-dir.sh)
#
# SETS
#   REMEMBER_CASE_STATUS      ok | diverged | unavailable | not-applicable
#   REMEMBER_CASE_RESOLVED    the store directory name this session resolved
#   REMEMBER_CASE_ROOT        the directory that store sits in
#   REMEMBER_CASE_DISK_STATE  ok | diverged | unavailable
#   REMEMBER_CASE_DISK_REASON set when the disk probe is unavailable
#   REMEMBER_CASE_DISK_NAMES  comma-separated, set when the disk probe diverged
#   REMEMBER_CASE_GIT_STATE   ok | diverged | unavailable
#   REMEMBER_CASE_GIT_REASON  set when the git probe is unavailable
#   REMEMBER_CASE_GIT_NAMES   comma-separated, set when the git probe diverged
#   REMEMBER_CASE_MESSAGE     one line of prose, set only when diverged
#
#   Comma is safe as a separator because every name compared here case-folds to
#   a session slug, and session_dir_slug maps every non-alphanumeric byte to a
#   dash — a comma cannot survive it.
#
# ============================================================================

[ -n "${_REMEMBER_LIB_CASE_DIVERGENCE_LOADED:-}" ] && return 0
_REMEMBER_LIB_CASE_DIVERGENCE_LOADED=1

# _remember_case_fold_eq <a> <b>
# Case-insensitive string equality with no fork. bash 3.2 is the floor here —
# macOS ships it and this repo supports it — so ${x,,} is not available and
# `tr` would be a fork per name compared. `shopt -s nocasematch` makes [[ = ]]
# fold case, the RHS is quoted so it is a literal rather than a glob, and the
# previous setting is restored because this is a global shell option and a
# library has no business leaving it changed.
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

    # Only a store whose directory is NAMED BY THE SLUG can have a second
    # spelling — a literal `.remember` or a literal single-directory store
    # cannot. REMEMBER_STORE_ROOT is #297's existing signal for exactly that
    # layout, and its emptiness is documented as the interface, so this reuses
    # it rather than re-deriving the same fact from the template.
    [ -n "${REMEMBER_STORE_ROOT:-}" ] || return 0
    [ -n "${REMEMBER_DIR:-}" ] || return 0

    local _root="${REMEMBER_DIR%/*}" _name="${REMEMBER_DIR##*/}"
    [ -n "$_root" ] && [ "$_root" != "$REMEMBER_DIR" ] || return 0
    [ -n "$_name" ] || return 0

    # The same refusal the git backup makes at its top: when the store's parent
    # IS the project, this is the user's own repository and neither hook touches
    # it (#138). Nothing here may go looking inside a repo we do not manage.
    [ "$_root" != "${PROJECT_DIR:-}" ] || return 0

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

# The disk probe. A readdir of the store root and no subprocess: the store root
# holds one directory per project, so this is a handful of entries even on the
# reporter's machine, and it is the only probe that can fire on NTFS.
_remember_case_probe_disk() {
    local _root="$1" _name="$2" _entry _base

    # An unreadable store root would make the glob below expand to nothing,
    # which is indistinguishable from "no second spelling" — so it is refused
    # rather than read as agreement.
    if [ ! -d "$_root" ] || [ ! -r "$_root" ] || [ ! -x "$_root" ]; then
        REMEMBER_CASE_DISK_STATE="unavailable"
        REMEMBER_CASE_DISK_REASON="store-root-unreadable"
        return 0
    fi

    # A slug never begins with a dot — session_dir_slug maps a leading `/` to a
    # dash and a Windows path to a drive letter — so the plain glob covers every
    # name that could case-fold to one, and a no-match expands to the literal
    # pattern that `-d` then discards.
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

# The git probe. HEAD's top-level tree, one invocation, and only for a store
# root that is itself the toplevel of a repository.
_remember_case_probe_git() {
    local _root="$1" _name="$2" _out _line _matched=0

    # `.git` present at the store root is the POSITIVE answer to "is this the
    # toplevel of a repository", with a shell builtin and no fork — the same
    # trick lib-memory-dir.sh uses for the worktree question. Its absence proves
    # only that we cannot answer: the store may sit inside a larger repository,
    # which the backup hook declines to manage anyway.
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

    # A leaked GIT_DIR from a bare-repo dotfiles setup, or from running inside
    # another git hook, would resolve this against a different repository
    # entirely — the sanitisation 50-git-backup.sh does for the same reason,
    # done here in a subshell so the caller's environment is left alone.
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
    done <<EOF
$_out
EOF

    if [ -n "$REMEMBER_CASE_GIT_NAMES" ]; then
        REMEMBER_CASE_GIT_STATE="diverged"
    elif [ "$_matched" -eq 1 ]; then
        REMEMBER_CASE_GIT_STATE="ok"
    else
        # The repository tracks other stores but not this one — it has never
        # been backed up, or not yet. There is no tracked path to compare
        # against, so there is no agreement to report.
        REMEMBER_CASE_GIT_STATE="unavailable"
        REMEMBER_CASE_GIT_REASON="store-not-tracked"
    fi
    return 0
}

# The message a human reads. The constraint it has to hold: on the reporter's
# machine this condition is TRUE and completely harmless, so a reader must not
# be able to conclude the opposite of the truth from it. It says what is true
# now (nothing is wrong), what would change it (a restore onto a case-sensitive
# filesystem), and that nothing will be done for them.
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

    REMEMBER_CASE_MESSAGE="remember: ${_lead} — spellings that differ only in case. On a case-insensitive filesystem those are one and the same, which is why memory is being read and written normally and there is nothing to fix today. It would matter on a restore: checked out onto a case-sensitive filesystem they become separate directories, and this plugin would use only ${REMEMBER_CASE_RESOLVED}. Nothing here will rename or merge anything for you — if you keep a backup of this store, check which spellings its HEAD holds before restoring it somewhere case-sensitive. Run /remember:doctor to see this again."
    return 0
}
