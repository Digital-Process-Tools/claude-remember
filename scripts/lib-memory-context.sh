#!/bin/bash
# ============================================================================
# lib-memory-context.sh — render the injected MEMORY section, and cache it
# across SessionStart runs (#668, part of #660)
# ============================================================================
#
# DESCRIPTION
#   session-start-hook.sh used to read, size, head and concatenate six memory
#   files plus the rotated-slice listing on EVERY single session start, even
#   though the bytes only change when a memory file changes -- i.e. after a
#   save or a consolidation, both of which already run in a detached
#   background phase (nohup ... & disown) before the next session starts.
#
#   This file is the single source of truth for that render, used by:
#     - session-start-hook.sh, on the foreground path: a cache hit skips the
#       render entirely (one `cat`); a miss renders live, same as before #668,
#       and additionally leaves a fresh cache behind for the next start.
#     - save-session.sh / run-consolidation.sh, at the very end of their own
#       body: both already run fully detached (see the `nohup ... & disown`
#       call sites in session-start-hook.sh and agy-stop-hook.sh), so a call
#       placed at their tail costs the interactive session nothing -- the
#       parent hook has already exited by the time either script reaches it.
#
#   Kept in one function rather than two copies for the same reason #158
#   documents for session_dir_slug: a second, independently-maintained render
#   is how the cache and the live path silently drift apart.
#
# CACHE VALIDATION
#   `$REMEMBER_DIR/tmp/start-context.cache` (raw bytes to inject) plus
#   `$REMEMBER_DIR/tmp/start-context.manifest` (one `SRC=<path>` line per
#   input the render depends on: the six memory files, REMEMBER_DIR itself --
#   so a rotated slice appearing or disappearing invalidates the cache even if
#   whatever created it forgot to republish -- and the three config layers,
#   since MEMORY_INJECT_MAX_BYTES is read from config()).
#
#   A hit requires the cache to be STRICTLY NEWER (`-nt`) than every manifest
#   entry that exists. Not `-ge`: two mtimes that compare EQUAL are treated as
#   a miss, never a hit, because FAT/exFAT's 2s mtime granularity can make a
#   source edited a moment after the cache was written compare equal to it --
#   an ambiguous read must invalidate, not serve stale content. `-nt` already
#   has this property (bash's `-nt` is false on a tie), so no special-casing is
#   needed beyond using it consistently, the same convention lib-env-cache.sh
#   already established for this codebase. `-nt` against a manifest entry that
#   does not exist is true, which is correct: an absent memory file cannot have
#   changed.
#
#   Only the non-`compact` render is cached. At `source=compact` the injection
#   is a different, much smaller shape (identity only; everything else is
#   named, not shown) -- caching that too would need a second cache keyed on
#   SESSION_START_SOURCE, and `compact` is already the cheap branch (it skips
#   reading five of the six files), so there is little to save there and real
#   risk in conflating the two shapes under one cache key.
#
# SECURITY
#   Same convention as lib-env-cache.sh and the promo marker: the cache lives
#   under REMEMBER_DIR/tmp, is written 0600 via mktemp + rename (never a
#   truncating redirect to a predictable name), and is read only when it is a
#   regular file owned by the current user -- a symlink or another user's file
#   is never trusted, and the loader falls back to a live render instead.
#
# ============================================================================

[ -n "${_REMEMBER_LIB_MEMORY_CONTEXT_LOADED:-}" ] && return 0
_REMEMBER_LIB_MEMORY_CONTEXT_LOADED=1

# _remember_memory_paths
# Sets REMEMBER_ROOT, IDENTITY_FILE, CORE_MEMORIES, REMEMBER_RECENT,
# REMEMBER_ARCHIVE, REMEMBER_NOW, REMEMBER_TODAY_FILE and the MEMORY_FILES
# array. Requires REMEMBER_DIR, PROJECT_DIR and PLUGIN_ROOT already set;
# computes TODAY itself when the caller has not already set one (save-session.sh
# and run-consolidation.sh have their own differently-named "today" variables
# and never set this one).
_remember_memory_paths() {
    [ -n "${TODAY:-}" ] || TODAY=$(_remember_date '+%Y-%m-%d')

    # Parameter expansion, not a `dirname` fork (#660) -- the pattern #230
    # established for this repo and session-start-hook.sh:60 already uses.
    # `dirname` is reproduced exactly, edge cases included, and
    # tests/test_dirname_without_a_fork_660.py compares the two against a
    # table of paths rather than trusting this comment:
    #   trailing slashes are stripped first ("/a/b/" -> "/a")
    #   no slash at all answers "." ("x" -> ".")
    #   the root's parent is the root ("/a" -> "/", "/" -> "/")
    _remember_root_scratch="$REMEMBER_DIR"
    while [ "${_remember_root_scratch%/}" != "$_remember_root_scratch" ] \
        && [ "$_remember_root_scratch" != "/" ]; do
        _remember_root_scratch="${_remember_root_scratch%/}"
    done
    case "$_remember_root_scratch" in
        (/) REMEMBER_ROOT="/" ;;
        (*/*)
            REMEMBER_ROOT="${_remember_root_scratch%/*}"
            [ -n "$REMEMBER_ROOT" ] || REMEMBER_ROOT="/"
            ;;
        (*) REMEMBER_ROOT="." ;;
    esac
    unset _remember_root_scratch
    # Anchored on MEMORY_PROJECT_DIR, not PROJECT_DIR (#756, same anchoring
    # bug #747 fixed for the handoff tracked-check): from a linked git
    # worktree, PROJECT_DIR is the worktree path, but REMEMBER_DIR -- and
    # therefore REMEMBER_ROOT, its dirname -- is redirected into the MAIN
    # checkout's .remember/ (#56), the same repository just a different
    # path. Comparing against PROJECT_DIR made that redirect look like
    # "REMEMBER_ROOT is some other, unrelated directory" and picked up
    # REMEMBER_ROOT/identity.md unconditionally from a worktree, even when
    # it is genuinely the project root's own (possibly repo-shipped) file.
    # MEMORY_PROJECT_DIR already equals PROJECT_DIR outside a worktree (the
    # fail-safe default in _resolve_memory_project_dir), so this is
    # additive: non-worktree behaviour is unchanged. Whether the resulting
    # file may actually be injected is decided later, by _remember_may_inject
    # (#754/#755/#756), which is anchored on nothing but the file itself.
    _remember_mem_proj="${MEMORY_PROJECT_DIR:-$PROJECT_DIR}"
    if [ -f "$REMEMBER_DIR/identity.md" ]; then
        IDENTITY_FILE="$REMEMBER_DIR/identity.md"
    elif [ -f "$REMEMBER_ROOT/identity.md" ] && [ "$REMEMBER_ROOT" != "$_remember_mem_proj" ]; then
        IDENTITY_FILE="$REMEMBER_ROOT/identity.md"
    else
        IDENTITY_FILE="$PLUGIN_ROOT/identity.md"
    fi
    unset _remember_mem_proj

    CORE_MEMORIES="$REMEMBER_DIR/core-memories.md"
    REMEMBER_RECENT="$REMEMBER_DIR/recent.md"
    REMEMBER_ARCHIVE="$REMEMBER_DIR/archive.md"
    REMEMBER_NOW="$REMEMBER_DIR/now.md"
    REMEMBER_TODAY_FILE="$REMEMBER_DIR/today-${TODAY}.md"

    MEMORY_FILES=("$IDENTITY_FILE" "$CORE_MEMORIES" "$REMEMBER_TODAY_FILE" "$REMEMBER_NOW" "$REMEMBER_RECENT" "$REMEMBER_ARCHIVE")
}

# _remember_render_memory_section
# Prints the "=== MEMORY ===" block exactly as session-start-hook.sh printed
# it before #668 -- identical bytes, so a cache hit and a live render are
# indistinguishable to whatever reads the hook's stdout. Requires
# _remember_memory_paths to have already run, and `config()` (log.sh) to be
# available. Reads SESSION_START_SOURCE from the environment; unset/empty is
# treated as the non-compact (full) render, same as the original code.
# bash 3.2 (the documented floor -- lib-clock.sh's own header, lib-lock.sh's
# _lock_timing_key comment) has no associative arrays, so the per-file byte
# counts `_remember_render_memory_section` batches below live in a variable
# named after the sanitized path instead, exactly like lib-lock.sh's own
# `_lock_timing_key` does for the same reason. `declare -A` parses as a
# syntax error... no, it does not -- it is ACCEPTED and silently creates a
# plain, non-associative array on bash 3.2 (`declare: -A: invalid option` is
# non-fatal), so the very next `${arr[$key]}` lookup throws a fatal "syntax
# error: operand expected" instead, aborting the whole render with nothing
# injected and nothing visible beyond stderr (#662/#664 self-review finding).
# `printf -v` + indirect (`${!name}`) expansion is plain parameter expansion,
# no subshell, and has worked since bash 2.x -- confirmed directly against
# the real `/bin/bash` 3.2.57 this repo ships behind on stock macOS.
_remember_wc_size_set() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local _remember_wc_size_key="_remember_wcsz_${1//[!A-Za-z0-9]/_}"
    printf -v "$_remember_wc_size_key" '%s' "$2"
}
# Writes into VARNAME rather than returning via `$(...)` -- a command
# substitution forks a subshell even when nothing inside it forks a real
# process, and this is called once per memory file on the render's hot path.
_remember_wc_size_get_into() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local _remember_wc_size_outvar="$1"
    local _remember_wc_size_key="_remember_wcsz_${2//[!A-Za-z0-9]/_}"
    # `-` and not `:-0`: a file this cache holds no entry for is UNMEASURED,
    # and `0` is a measurement. Laundering the two together made
    # _remember_emit_file's own "no usable size" arm unreachable -- `0` is a
    # digit string and `0 -gt 16384` is false, so an unmeasured file of ANY
    # size took the read path that the 16 KB threshold exists to keep it off
    # (#695 round-1 audit). The batched `wc -c` can fail wholesale, which
    # leaves every file in that state at once.
    printf -v "$_remember_wc_size_outvar" '%s' "${!_remember_wc_size_key-}"
}

# ============================================================================
# INJECTION GUARD -- "may this memory file be injected?" (#721 follow-ups:
# GHSA-6q55-m29c-3xgj, #754, #755, #756, #760)
# ============================================================================
# One shared decision, used for EVERY file the context loader injects (and
# also the handoff, session-start-hook.sh's own separate render path --
# see _remember_may_inject below). Refuses:
#
#   - a SYMLINK (`[ -L ]`), in EVERY storage mode. A cloned repository can
#     commit `.remember/now.md` (or any other injected file) as a symlink
#     to a path outside the repo -- e.g. `~/.aws/credentials` -- and have
#     THAT file's bytes read through the link and injected as though they
#     were the user's own memory. Refused unconditionally, not "only when
#     the resolved target is outside the store": nothing in this plugin
#     ever creates a symlink inside a memory store (no `ln -s` anywhere
#     under scripts/*.sh), so there is no legitimate in-store symlink this
#     would need to spare. Not gated on legacy/in-project storage either
#     (unlike the tracked check below): #757's own first-run migration
#     (`bootstrap-dirs.sh`'s `mv "$_legacy_dir" "$REMEMBER_DIR"`) carries a
#     legacy store's files, symlink and all, into the external store, so
#     restricting this check to legacy mode would leave a migrated symlink
#     unrefused from that point on.
#   - a GIT-TRACKED file, asked about BY THE FILE ITSELF: whether the
#     nearest repository above the file's OWN directory tracks it, found
#     by walking the FILESYSTEM for `.git` (_remember_repo_root_walk_into,
#     no `git` spawn) rather than by comparing REMEMBER_ROOT /
#     MEMORY_PROJECT_DIR / PROJECT_DIR against each other. #747 and #756
#     were exactly that comparison anchoring on the wrong root from a
#     linked worktree; #754 was a fixed `.git`-in-one-directory lookup that
#     a repository SUBDIRECTORY has none of. Walking the filesystem needs
#     neither fact and resolves the nearest repository itself --
#     worktree, subdirectory or submodule alike. A repository can ship a
#     memory file committed; this plugin never commits one itself
#     (bootstrap-dirs.sh writes a .gitignore for the whole directory), so a
#     tracked one was shipped by the repository, not written by the user's
#     own /remember. Gated on legacy/in-project storage only
#     (_remember_in_project_store): external storage can legitimately be
#     the user's own git-backup repository, and this plugin commits into
#     THAT one on purpose -- see that function's own comment.
#   - a file a repository IS there to answer about, but git's answer could
#     not be trusted (#760: missing binary, corrupted `.git`, a broken
#     shim on PATH). This is a THIRD state, not folded into "not tracked":
#     see _remember_file_tracked_state_into's own comment.
#   - nothing else. A file with no repository anywhere above it (external-
#     storage mode's ordinary case) is passed through unchanged, exactly
#     as before this guard existed.
#
# Every refusal is LOGGED via log() (never silent); the caller is expected
# to also surface it in whatever it hands back to the model/user --
# _remember_may_inject only decides and records, it does not itself print
# anything user-visible.
#
# Spawn budget (#754/#755/#756/#760 note, #660/#665/#689 fork-count tests):
# repository existence costs NO `git` spawn at all (a filesystem walk, see
# _remember_repo_root_walk_into) and asking what is tracked costs at most
# ONE `git` spawn per DISTINCT (repository root, file's own directory)
# pair, cached, scoped to that one directory via an `:(icase)` pathspec
# rather than listing the whole repository -- `git -C root ls-files` with
# NO pathspec lists everything the repository has ever committed, which on
# a large monorepo is a listing many orders of magnitude bigger than the
# handful of memory files this guard actually needs an answer about. A
# typical render touches at most two distinct directories (REMEMBER_DIR,
# and REMEMBER_ROOT only for the identity.md fallback), so this is at most
# two scoped `git` spawns per render, cached via the same indirect-variable
# convention _remember_wc_size_set (above) uses for the same
# bash-3.2-has-no-associative-arrays reason. `:(icase)` (pathspec magic,
# present since git 1.8, well below anything a supported CI runner ships)
# is what keeps a repository-committed DIFFERENTLY-CASED directory (e.g.
# `.Remember/remember.md` on a case-insensitive filesystem) detectable
# without a second, unscoped listing: a pathspec of `:(icase).remember/`
# matches a tracked `.Remember/remember.md` even though a byte-exact
# pathspec would not.

# _remember_ci_eq <a> <b> -- case-insensitive string equality, no fork.
# Same trick session-start-hook.sh's own (now-removed) _remember_th_ci_eq
# and lib-case-divergence.sh's _remember_case_fold_eq use; kept here,
# duplicated rather than sourced, so this file does not need to pull in
# lib-case-divergence.sh (loaded on demand, deep inside
# _remember_write_case_divergence) just for one comparison.
_remember_ci_eq() {
    local _was=0 _rc
    shopt -q nocasematch && _was=1
    shopt -s nocasematch
    [[ "$1" == "$2" ]]
    _rc=$?
    [ "$_was" -eq 1 ] || shopt -u nocasematch
    return $_rc
}

# _remember_cache_key_into <outvar> <prefix> <value> -- same sanitized-
# indirect-name convention as _remember_wc_size_set, with a prefix so two
# different caches (repo root, tracked-file listing) keyed off the same
# raw string (a directory, or that directory's resolved root) do not
# collide with each other.
_remember_cache_key_into() {
    local LC_ALL=C  # bracket range below is byte-wise, not collated (#695)
    printf -v "$1" '%s' "_remember_${2}_${3//[!A-Za-z0-9]/_}"
}

# _remember_repo_root_walk_into <outvar> <dir>
# Ground truth for "is DIR inside any git repository at all" -- answered by
# walking up the FILESYSTEM (no fork, `.git` is checked with a shell
# builtin at each level) rather than by asking the `git` binary. `[ -e ]`
# matches both an ordinary checkout's `.git` DIRECTORY and a linked
# worktree's or submodule's own `.git` FILE (a `gitdir: ...` pointer), so
# worktrees and submodules resolve exactly like an ordinary checkout.
#
# This is deliberately independent of whatever `git` itself would report
# (#760): the next function asks git a question ("what does this
# repository track") that can fail for reasons that have nothing to do
# with whether a repository is really there -- a missing binary, a
# corrupted `.git`, a broken shim on PATH -- and #760's point is that a
# failure of THAT kind must never be read the same way as "there genuinely
# is no repository here". Establishing repo-existence independently, on
# disk, is what makes that distinction possible: empty OUTVAR (return 1)
# here means no `.git` was found anywhere above DIR up to `/`, a fact nothing
# about `git`'s own health can change.
_remember_repo_root_walk_into() {
    local _rrw_outvar="$1" _rrw_dir="$2"
    while :; do
        if [ -e "${_rrw_dir}/.git" ]; then
            printf -v "$_rrw_outvar" '%s' "$_rrw_dir"
            return 0
        fi
        case "$_rrw_dir" in
            (/) break ;;
            (*/*)
                _rrw_dir="${_rrw_dir%/*}"
                [ -n "$_rrw_dir" ] || _rrw_dir="/"
                ;;
            (*) break ;;
        esac
    done
    printf -v "$_rrw_outvar" ''
    return 1
}

# _remember_root_tracked_state_into <list-outvar> <state-outvar> <root> <rel-dir>
# STATE-OUTVAR is "ok" (LIST-OUTVAR holds a newline-joined `git -C root
# ls-files` listing, possibly empty for a genuinely empty repository) or
# "unavailable" (#760: `git` is missing, or the `ls-files` call itself
# exited non-zero -- a repository really is there, per
# _remember_repo_root_walk_into above, but asking it could not be
# trusted). Never conflates "asked, and nothing is tracked" with "could not
# ask" -- an unavailable answer is a REASON, not an empty LIST, so a
# genuinely-empty repository (a fresh `git init`, nothing committed yet)
# still reads as "ok, empty" rather than as a failure.
#
# REL-DIR scopes the listing to one directory of ROOT via a pathspec --
# without it, `git -C root ls-files` with no pathspec lists the WHOLE
# repository on every session start, which on a large monorepo is a
# listing of everything the user has ever committed, not the handful of
# memory files this guard actually needs to check. `:(icase)` (pathspec
# magic, git >= 1.8 -- confirmed against the git this host ships, 2.x;
# every CI runner's git is well past that floor) keeps the differently-
# cased-directory case correct WITHOUT a second, unscoped listing: a
# repository that commits `.Remember/remember.md` still matches a pathspec
# of `:(icase).remember/`. REL-DIR of "." (the memory file sits directly at
# the repository root -- the rare REMEMBER_ROOT/identity.md fallback, not
# the ordinary REMEMBER_DIR case) has no meaningful subdirectory to scope
# to, so the pathspec is omitted and the call is genuinely whole-repo --
# unavoidable in that one case, since "the file's own directory" and "the
# repository root" are the same directory there.
#
# One `git` spawn per DISTINCT (ROOT, REL-DIR) pair, cached: every file
# that shares both reuses this one result (#754/#755/#756 spawn-budget
# note). A typical render touches at most two distinct directories
# (REMEMBER_DIR, and REMEMBER_ROOT only for the identity.md fallback), so
# this is at most two `git` spawns per render, each scoped, never one
# unscoped whole-repo listing.
_remember_root_tracked_state_into() {
    local _rts_list_outvar="$1" _rts_state_outvar="$2" _rts_root="$3" _rts_reldir="$4"
    local _rts_list_key _rts_state_key _rts_list _rts_rc _rts_cache_id _rts_pathspec
    _rts_cache_id="${_rts_root}#${_rts_reldir}"
    _remember_cache_key_into _rts_list_key "list" "$_rts_cache_id"
    _remember_cache_key_into _rts_state_key "state" "$_rts_cache_id"
    if [ -z "${!_rts_state_key+x}" ]; then
        if command -v git >/dev/null 2>&1; then
            if [ "$_rts_reldir" = "." ]; then
                _rts_pathspec=""
            else
                _rts_pathspec=":(icase)${_rts_reldir}/"
            fi
            # Leaked GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE would resolve this
            # against a different repository entirely -- the same
            # sanitisation the case-divergence probe and the (now-removed)
            # handoff tracked-check already used for the same reason.
            # Newline-joined, not `-z`/NUL-delimited: memory filenames never
            # contain a literal newline, so capturing via `$(...)` (which
            # ALSO gives us $?, unlike a process-substitution pipeline) is
            # safe here even though the general "arbitrary repository path"
            # case is not -- the pre-existing NUL-delimited handling
            # elsewhere in this codebase reads exactly that general case and
            # stays NUL-delimited for it.
            if [ -n "$_rts_pathspec" ]; then
                _rts_list=$(unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                            git -C "$_rts_root" ls-files -- "$_rts_pathspec" 2>/dev/null)
            else
                _rts_list=$(unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                            git -C "$_rts_root" ls-files 2>/dev/null)
            fi
            _rts_rc=$?
        else
            _rts_list=""
            _rts_rc=127
        fi
        if [ "$_rts_rc" -eq 0 ]; then
            printf -v "$_rts_state_key" 'ok'
            printf -v "$_rts_list_key" '%s' "$_rts_list"
        else
            printf -v "$_rts_state_key" 'unavailable'
            printf -v "$_rts_list_key" ''
        fi
    fi
    printf -v "$_rts_state_outvar" '%s' "${!_rts_state_key}"
    printf -v "$_rts_list_outvar" '%s' "${!_rts_list_key}"
}

# _remember_file_tracked_state_into <outvar> <file>
# Sets OUTVAR to one of:
#   tracked      -- the nearest repository above FILE's own directory
#                    tracks it (exact-case match, or a case-insensitive
#                    fallback -- a case-insensitive filesystem, APFS
#                    default/NTFS, delivers the same bytes off disk
#                    regardless of which case a commit used).
#   not-tracked  -- a repository is there and answered; FILE is not in it.
#   no-repo      -- no repository anywhere above FILE's own directory
#                    (external storage's own ordinary case, and the
#                    legitimate ALLOWED state -- nothing could have
#                    committed this file).
#   unavailable  -- a repository IS there (per the filesystem walk) but
#                    asking it about FILE could not be trusted (#760).
#                    Callers must treat this as a REFUSAL, not as
#                    not-tracked: a git failure must never silently permit
#                    a memory file through un-checked.
_remember_file_tracked_state_into() {
    local _fts_outvar="$1" _fts_file="$2"
    local _fts_dir _fts_root _fts_root_fs _fts_file_fs _fts_dir_fs _fts_rel _fts_reldir
    local _fts_list _fts_state _fts_line
    case "$_fts_file" in
        (*/*) _fts_dir="${_fts_file%/*}" ;;
        (*)   _fts_dir="." ;;
    esac
    _remember_repo_root_walk_into _fts_root "$_fts_dir"
    if [ -z "$_fts_root" ]; then
        printf -v "$_fts_outvar" 'no-repo'
        return 0
    fi
    _remember_forward_slash_into _fts_root_fs "$_fts_root"
    _remember_forward_slash_into _fts_file_fs "$_fts_file"
    _remember_forward_slash_into _fts_dir_fs "$_fts_dir"
    _fts_rel="${_fts_file_fs#$_fts_root_fs/}"
    if [ "$_fts_rel" = "$_fts_file_fs" ]; then
        # FILE does not actually sit under the repository the walk found --
        # should not happen (the walk starts from FILE's own directory), but
        # if it ever does, "could not verify" is the honest answer, not a
        # silent allow.
        printf -v "$_fts_outvar" 'unavailable'
        return 0
    fi
    # REL-DIR (FILE's own directory, relative to ROOT) scopes the `ls-files`
    # pathspec below to one directory instead of the whole repository --
    # "." when the file sits directly at the repository root (the rare
    # REMEMBER_ROOT/identity.md fallback), otherwise the directory portion
    # of _fts_rel.
    if [ "$_fts_dir_fs" = "$_fts_root_fs" ]; then
        _fts_reldir="."
    else
        _fts_reldir="${_fts_dir_fs#$_fts_root_fs/}"
    fi
    _remember_root_tracked_state_into _fts_list _fts_state "$_fts_root" "$_fts_reldir"
    if [ "$_fts_state" != "ok" ]; then
        printf -v "$_fts_outvar" 'unavailable'
        return 0
    fi
    if [ -z "$_fts_list" ]; then
        printf -v "$_fts_outvar" 'not-tracked'
        return 0
    fi
    while IFS= read -r _fts_line; do
        [ -n "$_fts_line" ] || continue
        if [ "$_fts_line" = "$_fts_rel" ] || _remember_ci_eq "$_fts_line" "$_fts_rel"; then
            printf -v "$_fts_outvar" 'tracked'
            return 0
        fi
    done <<EOF
$_fts_list
EOF
    printf -v "$_fts_outvar" 'not-tracked'
}

# _remember_in_project_store -- true only when REMEMBER_DIR sits directly
# under the memory project's own directory (legacy/in-project storage --
# the only layout where a memory file can ALSO be a file the PROJECT's own
# git repository tracks, a repository the user may not have written
# themselves). Anchored on MEMORY_PROJECT_DIR, not PROJECT_DIR (#747/#756
# anchoring): from a linked worktree, PROJECT_DIR is the worktree path but
# REMEMBER_DIR is redirected into the MAIN checkout's .remember/ (#56).
#
# External-storage mode (REMEMBER_ROOT elsewhere entirely, e.g.
# ~/.remember/<slug>) is deliberately EXEMPT from the TRACKED check below
# (only): that store can be, and commonly is, its own PRIVATE git
# repository (the git_backup feature) that the plugin itself commits
# memory files into ON PURPOSE, to sync them across machines. A first
# version of this guard applied the tracked-check unconditionally and
# broke exactly that -- tests/test_delivery_record_per_machine_285.py
# reproduces the scenario (a handoff genuinely committed by git_backup,
# genuinely meant to be delivered on the second machine) and went red
# until this gate was added.
#
# The SYMLINK check does NOT go through this gate -- see _remember_may_inject
# below for why external storage is not exempt from it.
_remember_in_project_store() {
    [ "$REMEMBER_ROOT" = "${MEMORY_PROJECT_DIR:-$PROJECT_DIR}" ]
}

# _remember_may_inject <file> [<log-component>]
# The one decision every injection site calls. Sets _REMEMBER_INJECT_REFUSAL
# to a one-line, user-facing reason (empty string when allowed) and returns
# 0 (allowed) / 1 (refused). Logs every refusal; prints nothing itself.
_remember_may_inject() {
    local _mi_file="$1" _mi_component="${2:-memory-context}" _mi_state
    _REMEMBER_INJECT_REFUSAL=""
    # The symlink refusal applies in EVERY storage mode, including
    # external -- not gated on _remember_in_project_store. #757's own
    # first-run migration (`bootstrap-dirs.sh`'s `mv "$_legacy_dir"
    # "$REMEMBER_DIR"`) carries a legacy/in-project store's files, symlink
    # and all, straight into the external store; after that move, a
    # committed `now.md` symlink planted before migration would otherwise
    # sit in a directory this guard no longer scoped its checks to at all,
    # and the advisory's read-outside-the-repo bug would be back. Costs no
    # spawn (`[ -L ]` is a builtin) and nothing in this plugin ever creates
    # a symlink inside a memory store, so there is no legitimate case this
    # widening could break.
    if [ -L "$_mi_file" ]; then
        _REMEMBER_INJECT_REFUSAL="$_mi_file is a symlink -- refusing to follow it into session context. This plugin never creates a symlink inside a memory store; if you did not create this one, treat it as planted and inspect what it points at before deleting it."
        log "$_mi_component" "refused injecting $_mi_file: symlink"
        return 1
    fi
    _remember_in_project_store || return 0
    _remember_file_tracked_state_into _mi_state "$_mi_file"
    case "$_mi_state" in
        (tracked)
            _REMEMBER_INJECT_REFUSAL="$_mi_file is tracked by this repository's own git index. This plugin never commits a memory file itself (.remember/.gitignore excludes the whole directory), so a tracked one was shipped by the repository, not written by your own /remember. Not injecting it. If it is genuinely yours: git rm --cached it. If you did not add it: delete it and consider what else the commit that added it changed."
            log "$_mi_component" "refused injecting $_mi_file: git-tracked"
            return 1
            ;;
        (unavailable)
            # #760: a repository really is above this file, but asking git
            # whether it tracks the file could not be trusted (missing
            # binary, broken state, a shim on PATH). Refuse rather than
            # deliver on a guess -- an untrustworthy "no" from the tracked
            # check must read the same as "yes", not the same as a clean
            # "not tracked".
            _REMEMBER_INJECT_REFUSAL="$_mi_file could not be checked against this repository's git index (git is missing, or the check itself failed) -- refusing rather than injecting unverified. Run /remember:doctor to see why git could not be asked."
            log "$_mi_component" "refused injecting $_mi_file: git status unavailable"
            return 1
            ;;
        (*)
            return 0
            ;;
    esac
}

# Args: $1 -- a file. $2 -- its size in bytes. Writes its bytes to stdout,
# unchanged, using whichever of the two ways is actually cheaper AT THAT SIZE.
#
# Replaces an unconditional `cat "$MFILE"` (#660). A fork is expensive on Git
# Bash, so a small file is cheaper read by the shell -- but bash's `read`
# processes the delimiter byte by byte, and that is catastrophic at size.
# Measured on windows-latest, five iterations each (the workflow's own
# "cat vs read probe" step, which exists to keep this honest):
#
#     size     cat      read
#     4KB      0.197s   0.025s     <- read wins by 8x
#     256KB    0.092s   1.505s     <- read loses by 16x
#     4MB      0.099s   24.129s    <- read loses by 240x
#
# ubuntu-latest has the same shape (4MB: 0.008s vs 0.879s), so this is not a
# Windows quirk. The first version of this function used `read`
# unconditionally and made the big-store arms of the benchmark measurably
# SLOWER -- a fork-count optimisation that cost seconds, which is exactly what
# counting forks alone cannot see.
#
# The threshold is deliberately well below the ~34KB crossover implied by
# those numbers: being wrong towards `cat` costs one fork, being wrong towards
# `read` costs seconds.
#
# `read -d ''` reads to the first NUL -- i.e. the whole file, for text -- into
# a variable, using no subprocess at all, and `printf %s` writes it back
# verbatim. It returns non-zero at EOF having ALREADY set the variable, which
# is why the `|| :` is correct rather than sloppy.
#
# What this deliberately is NOT: `printf '%s\n' "$(<"$1")"`. Command
# substitution strips EVERY trailing newline and the printf adds exactly one
# back, so a file ending in two newlines, or in none, renders differently from
# what is on disk -- silently, in the model's injected context.
# tests/test_render_is_byte_identical_660.py pins all six shapes and carries a
# demonstration that the naive form fails them.
#
# A file containing a literal NUL would be truncated here where `cat` would
# have passed it through. Memory files are markdown this plugin wrote itself;
# a NUL in one is already a corrupted store, and injecting the bytes after it
# was never the more useful behaviour.
_remember_emit_file() {
    local _remember_emit_max="${REMEMBER_EMIT_READ_MAX:-16384}"
    case "${2:-}" in
        (''|*[!0-9]*)
            # No usable size: `cat` is the one that cannot go quadratic.
            cat "$1"
            return 0
            ;;
    esac
    if [ "$2" -gt "$_remember_emit_max" ]; then
        cat "$1"
        return 0
    fi
    local _remember_file_body=""
    IFS= read -r -d '' _remember_file_body < "$1" || :
    printf '%s' "$_remember_file_body"
}

_remember_render_memory_section() {
    local MFILE HAS_MEMORY="" ROTATED_SLICES _remember_rotated_glob_dir
    local _remember_rotated_arr=()

    for MFILE in "${MEMORY_FILES[@]}"; do
        [ -f "$MFILE" ] && HAS_MEMORY="true"
    done
    _remember_forward_slash_into _remember_rotated_glob_dir "$REMEMBER_DIR"
    # Glob array, not `ls` (#664/#666) -- `ls` over a pattern that matches
    # nothing prints nothing AND exits nonzero on some implementations, which
    # is exactly what nullglob expresses without forking a process to find
    # out. The sort below still forks (one process, not two) because the two
    # patterns must be merged in lexical order across both prefixes, which a
    # glob alone does not give -- `archive-*` and `recent-*` would otherwise
    # stay in two separate, unmerged runs.
    # `shopt -p nullglob` exits 1 (even though it prints correctly) whenever
    # the option is currently OFF -- which it is by default -- so capturing
    # it via `var=$(...)` would abort a caller sourcing this under `set -e`.
    # `shopt -q` in a plain `&&` conditional never has that problem.
    local _remember_was_nullglob=0
    shopt -q nullglob && _remember_was_nullglob=1
    shopt -s nullglob
    _remember_rotated_arr=("$_remember_rotated_glob_dir"/archive-*.md "$_remember_rotated_glob_dir"/recent-*.md)
    [ "$_remember_was_nullglob" = 1 ] || shopt -u nullglob
    if [ "${#_remember_rotated_arr[@]}" -gt 0 ]; then
        ROTATED_SLICES=$(printf '%s\n' "${_remember_rotated_arr[@]}" | sort)
    else
        ROTATED_SLICES=""
    fi
    [ -n "$ROTATED_SLICES" ] && HAS_MEMORY="true"

    [ -n "$HAS_MEMORY" ] || return 0

    echo "=== MEMORY ==="
    local MEMORY_INJECT_MAX_BYTES=""
    # (see _remember_emit_file, above, for why the render no longer forks cat)
    config_into MEMORY_INJECT_MAX_BYTES ".thresholds.memory_inject_max_bytes" 200000
    case "$MEMORY_INJECT_MAX_BYTES" in (''|*[!0-9]*) MEMORY_INJECT_MAX_BYTES=200000 ;; esac
    local OVERSIZED_MEMORY="" BASENAME MFILE_BYTES
    local REFUSED_MEMORY=""
    # One batched `wc -c` over every memory file that is present AND
    # non-empty (#664), instead of one `wc` + one `tr` PER file -- a typical
    # 4-6 file store paid 8-12 forks here alone before this. `tr -d ' '` is
    # gone too: `read` already splits on (and discards) leading/trailing
    # whitespace, which is all `wc -c`'s own leading-space padding is.
    local _remember_present=() _remember_wc_bytes _remember_wc_path
    for MFILE in "${MEMORY_FILES[@]}"; do
        if [ -f "$MFILE" ] && [ -s "$MFILE" ]; then
            if [ "${SESSION_START_SOURCE:-}" = "compact" ] && [ "$MFILE" != "$IDENTITY_FILE" ]; then
                continue
            fi
            # #721/#754/#755/#756/GHSA-6q55-m29c-3xgj: a symlink or a
            # git-tracked file is refused here, before its size is even
            # measured, so it never reaches _remember_emit_file.
            if _remember_may_inject "$MFILE" "memory-context"; then
                _remember_present+=("$MFILE")
            else
                REFUSED_MEMORY="${REFUSED_MEMORY}${_REMEMBER_INJECT_REFUSAL}
"
            fi
        fi
    done
    if [ "${#_remember_present[@]}" -gt 0 ]; then
        # Default IFS (not `IFS=`): `wc -c`'s own right-justify padding is
        # entirely LEADING the byte count, never between the count and the
        # filename (exactly one space there, verified against both GNU and
        # BSD wc) -- so a plain `read bytes path` both trims the padding and
        # hands the filename back verbatim, spaces-in-paths included, since
        # `read` dumps everything left over into the LAST variable rather
        # than re-splitting it.
        while read -r _remember_wc_bytes _remember_wc_path; do
            case "$_remember_wc_bytes" in (''|*[!0-9]*) continue ;; esac
            [ "$_remember_wc_path" = "total" ] && continue
            _remember_wc_size_set "$_remember_wc_path" "$_remember_wc_bytes"
        done < <(wc -c "${_remember_present[@]}")
    fi
    # `"${arr[@]}"` on an EMPTY array is an "unbound variable" error under
    # `set -u` on bash < 4.4 (3.2 included), while `${#arr[@]}` is not -- so
    # every iteration over an array that can be empty is count-guarded.
    # Compact mode with no identity file is exactly that case here, and
    # save-session.sh/run-consolidation.sh's caller chain runs under `set -u`
    # (CI's macOS legs caught it on #675; bash 5 hides it).
    [ "${#_remember_present[@]}" -gt 0 ] && for MFILE in "${_remember_present[@]}"; do
            _remember_wc_size_get_into MFILE_BYTES "$MFILE"
            # An unmeasured size stays unmeasured (empty), rather than being
            # rewritten to 0: `_remember_emit_file` reads it as "no usable
            # size" and picks `cat`, the arm that cannot go quadratic. The
            # oversize check below is skipped for it because there is nothing
            # to compare -- failing open to injecting the file, the same way a
            # file measured under the cap is injected (#695 round-1 audit).
            case "$MFILE_BYTES" in (*[!0-9]*) MFILE_BYTES="" ;; esac
            if [ -n "$MFILE_BYTES" ] && [ "$MEMORY_INJECT_MAX_BYTES" -gt 0 ] && [ "$MFILE_BYTES" -gt "$MEMORY_INJECT_MAX_BYTES" ]; then
                OVERSIZED_MEMORY="${OVERSIZED_MEMORY}${MFILE} (${MFILE_BYTES} bytes)
"
                continue
            fi
            BASENAME="${MFILE##*/}"
            echo "--- $BASENAME ---"
            _remember_emit_file "$MFILE" "$MFILE_BYTES"
            echo ""
    done
    if [ -n "$OVERSIZED_MEMORY" ]; then
        echo "--- too large to inject (kept on disk; grep on request) ---"
        printf '%s' "$OVERSIZED_MEMORY"
        printf 'A healthy memory file is kilobytes. One this size means consolidation wrote a response nobody bounded (see thresholds.memory_inject_max_bytes) and has been skipping ever since; run /remember:doctor.\n'
        echo ""
    fi
    if [ -n "$REFUSED_MEMORY" ]; then
        echo "--- refused (not injected) ---"
        printf '%s' "$REFUSED_MEMORY"
        echo ""
    fi
    if [ "${SESSION_START_SOURCE:-}" = "compact" ]; then
        local DEFERRED_MEMORY _remember_deferred=()
        for MFILE in "${MEMORY_FILES[@]}"; do
            [ "$MFILE" != "$IDENTITY_FILE" ] || continue
            [ -f "$MFILE" ] && [ -s "$MFILE" ] || continue
            _remember_deferred+=("$MFILE")
        done
        # Same one-batched-`wc` shape as the main loop above (#664): compact
        # mode is the one branch that did NOT already have these files'
        # sizes cached yet (the main loop above skips every non-identity
        # file once SESSION_START_SOURCE=compact). Same storage (indirect
        # variables, not an associative array -- see the comment above
        # _remember_wc_size_set) as the main loop's own batch.
        if [ "${#_remember_deferred[@]}" -gt 0 ]; then
            while read -r _remember_wc_bytes _remember_wc_path; do
                case "$_remember_wc_bytes" in (''|*[!0-9]*) continue ;; esac
                [ "$_remember_wc_path" = "total" ] && continue
                _remember_wc_size_set "$_remember_wc_path" "$_remember_wc_bytes"
            done < <(wc -c "${_remember_deferred[@]}")
        fi
        DEFERRED_MEMORY=""
        # Same empty-array guard as the main loop above (bash < 4.4 + set -u).
        [ "${#_remember_deferred[@]}" -gt 0 ] && DEFERRED_MEMORY=$(for MFILE in "${_remember_deferred[@]}"; do
            _remember_wc_size_get_into MFILE_BYTES "$MFILE"
            # "(0 bytes)" for a file nobody measured reads exactly like an
            # empty file. Say which one it is (#695 round-1 audit).
            case "$MFILE_BYTES" in
                (''|*[!0-9]*) printf '%s (size unknown)\n' "$MFILE" ;;
                (*) printf '%s (%s bytes)\n' "$MFILE" "$MFILE_BYTES" ;;
            esac
        done)
        if [ -n "$DEFERRED_MEMORY" ]; then
            echo "--- not re-injected at compact (delivered at session start); read or grep on request ---"
            printf '%s\n' "$DEFERRED_MEMORY"
            echo ""
        fi
    fi
    if [ -n "$ROTATED_SLICES" ]; then
        local ROTATED_LIST_MAX=10 ROTATED_COUNT ROTATED_NEWEST
        # Array length, not `echo | wc -l | tr -d ' '` (#664/#666): the
        # count is already known without forking anything -- it is exactly
        # how many elements the earlier glob matched.
        ROTATED_COUNT="${#_remember_rotated_arr[@]}"
        ROTATED_NEWEST=$(echo "$ROTATED_SLICES" | while read -r _slice; do
            [ -n "$_slice" ] || continue
            _core=${_slice##*/}
            _core=${_core#archive-}
            _core=${_core#recent-}
            _core=${_core%.md}
            case "$_core" in
                (*-*-*-*) _date=${_core%-*}; _seq=${_core##*-} ;;
                (*)       _date=$_core;      _seq=1 ;;
            esac
            case "$_seq" in (''|*[!0-9]*) _seq=1 ;; esac
            printf '%s-%010d\t%s\n' "$_date" "$_seq" "$_slice"
        done | sort | tail -n "$ROTATED_LIST_MAX" | cut -f2-)
        echo "--- rotated memory slices (not shown; grep on request) ---"
        # One batched `wc -c` over at most ROTATED_LIST_MAX (10) slices
        # instead of one `wc` + one `tr` per slice (#664/#666).
        local _remember_newest_arr=() _remember_newest_line
        while IFS= read -r _remember_newest_line; do
            [ -f "$_remember_newest_line" ] && _remember_newest_arr+=("$_remember_newest_line")
        done <<< "$ROTATED_NEWEST"
        if [ "${#_remember_newest_arr[@]}" -gt 0 ]; then
            local _remember_newest_bytes
            while read -r _remember_wc_bytes _remember_wc_path; do
                case "$_remember_wc_bytes" in (''|*[!0-9]*) continue ;; esac
                [ "$_remember_wc_path" = "total" ] && continue
                _remember_wc_size_set "$_remember_wc_path" "$_remember_wc_bytes"
            done < <(wc -c "${_remember_newest_arr[@]}")
            for _remember_newest_line in "${_remember_newest_arr[@]}"; do
                _remember_wc_size_get_into _remember_newest_bytes "$_remember_newest_line"
                # The third of this getter's three call sites, and the one the
                # round-1 repair missed: since that repair the getter can
                # answer with the empty string, so an unformatted `%s bytes`
                # here renders `( bytes)` -- a number-shaped slot holding
                # nothing. Same three states as the deferred listing above
                # (#695 round-2 audit).
                case "$_remember_newest_bytes" in
                    (''|*[!0-9]*) printf '%s (size unknown)\n' "$_remember_newest_line" ;;
                    (*) printf '%s (%s bytes)\n' "$_remember_newest_line" "$_remember_newest_bytes" ;;
                esac
            done
        fi
        if [ "$ROTATED_COUNT" -gt "$ROTATED_LIST_MAX" ]; then
            printf '... and %s older: %s/archive-*.md, %s/recent-*.md\n' \
                "$((ROTATED_COUNT - ROTATED_LIST_MAX))" "$REMEMBER_DIR" "$REMEMBER_DIR"
        fi
        echo ""
    fi
    echo ""
}

# _remember_start_cache_manifest_lines
# Echoes, one per line, "SRC=<path>" for every input the render depends on --
# shared by the loader (checks each with -nt) and the publisher (writes them
# verbatim). Requires _remember_memory_paths to have already run.
_remember_start_cache_manifest_lines() {
    local MFILE
    for MFILE in "${MEMORY_FILES[@]}"; do
        printf 'SRC=%s\n' "$MFILE"
    done
    # REMEMBER_DIR itself: catches a rotated slice appearing or disappearing
    # even if whatever created it never republished the cache (belt and
    # braces -- every writer of a new memory file in THIS codebase does
    # republish, via the same call this file exists to provide, but a
    # future one that forgets must not silently serve a stale listing).
    printf 'SRC=%s\n' "$REMEMBER_DIR"
    # The three config layers lib-memory-dir.sh merges -- MEMORY_INJECT_MAX_BYTES
    # comes from config(), and REMEMBER_CONFIG itself is a fresh mktemp path
    # every process (always "now"), so the SOURCE files are what must be
    # checked, not the merged scratch copy.
    printf 'SRC=%s\n' "${PIPELINE_DIR:-}/config.json"
    printf 'SRC=%s\n' "${HOME:-}/.remember/config.json"
    printf 'SRC=%s\n' "${REMEMBER_DIR}/config.json"
}

# _remember_start_cache_context_load
# On a hit: prints the cached MEMORY section bytes and returns 0. On a miss
# (disabled, absent, unreadable, untrusted, or any manifest entry not older
# than the cache): prints nothing and returns 1 -- the caller renders live.
_remember_start_cache_context_load() {
    [ "${REMEMBER_START_CACHE:-1}" = "1" ] || return 1
    [ "${SESSION_START_SOURCE:-}" != "compact" ] || return 1
    [ -n "${REMEMBER_DIR:-}" ] || return 1
    local _cache="$REMEMBER_DIR/tmp/start-context.cache"
    local _manifest="$REMEMBER_DIR/tmp/start-context.manifest"
    [ -f "$_cache" ] || return 1
    [ -f "$_manifest" ] || return 1
    [ -L "$_cache" ] && return 1
    [ -O "$_cache" ] || return 1
    [ -r "$_cache" ] || return 1
    [ -L "$_manifest" ] && return 1
    [ -O "$_manifest" ] || return 1
    [ -r "$_manifest" ] || return 1

    local _line _src
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        case "$_line" in
            SRC=*) _src="${_line#SRC=}" ;;
            # Unknown line: not our file, or not our version of it -- distrust
            # the whole manifest rather than partially validate it.
            *) return 1 ;;
        esac
        [ -n "$_src" ] || continue
        # Strictly newer, never a tie (see the file header): -nt is false on
        # an equal mtime, which is exactly the "ambiguous means miss"
        # guardrail #668 asks for, and true against a manifest entry that no
        # longer exists (an absent source cannot have changed).
        [ "$_cache" -nt "$_src" ] || return 1
    done < "$_manifest"

    cat "$_cache"
    return 0
}

# _remember_start_cache_context_finish_publish <tmp_cache_path>
# Second half of a publish whose CONTENT has already been rendered elsewhere
# (session-start-hook.sh's own miss path streams the live render to the
# caller's terminal via `tee` at the same time it fills a temp file, so this
# is what turns that temp file into the persisted cache without rendering a
# second time). Writes a fresh manifest and moves both into place. Always
# consumes (removes or renames) $1; never fails the caller.
_remember_start_cache_context_finish_publish() {
    local _tmp_cache="$1"
    [ "${REMEMBER_START_CACHE:-1}" = "1" ] || { rm -f "$_tmp_cache" 2>/dev/null; return 0; }
    # Only the non-compact render is ever cached (see the file header): a
    # compact-mode render is the small, identity-only shape, and writing IT
    # into the cache would make the very next ordinary session start serve a
    # near-empty MEMORY section instead of falling through to a live render.
    [ "${SESSION_START_SOURCE:-}" != "compact" ] || { rm -f "$_tmp_cache" 2>/dev/null; return 0; }
    [ -n "${REMEMBER_DIR:-}" ] || { rm -f "$_tmp_cache" 2>/dev/null; return 0; }
    [ -f "$_tmp_cache" ] || return 0
    local _dir="$REMEMBER_DIR/tmp"
    mkdir -p "$_dir" 2>/dev/null || { rm -f "$_tmp_cache" 2>/dev/null; return 0; }
    local _cache="$_dir/start-context.cache"
    local _manifest="$_dir/start-context.manifest"
    local _tmp_manifest
    _tmp_manifest=$(mktemp "${_manifest}.XXXXXX" 2>/dev/null) || { rm -f "$_tmp_cache" 2>/dev/null; return 0; }
    _remember_start_cache_manifest_lines > "$_tmp_manifest" 2>/dev/null
    # Cache written and renamed FIRST, manifest second: a reader that opens
    # the manifest only after this sees a cache that already exists, never a
    # manifest naming a cache file that has not landed yet.
    mv -f "$_tmp_cache" "$_cache" 2>/dev/null || { rm -f "$_tmp_cache" "$_tmp_manifest" 2>/dev/null; return 0; }
    mv -f "$_tmp_manifest" "$_manifest" 2>/dev/null || rm -f "$_tmp_manifest" 2>/dev/null
    return 0
}

# _remember_start_cache_context_publish
# Renders the memory section and writes it to the cache, along with a fresh
# manifest. Never fails the caller -- a hook or background script that could
# not write a cache has still done its actual job. Requires
# _remember_memory_paths to have already run and REMEMBER_DIR to be set. This
# is the shape save-session.sh and run-consolidation.sh use: they only ever
# want the cache written, never a copy printed to a terminal nobody is
# reading (both run fully detached -- see the file header).
_remember_start_cache_context_publish() {
    [ "${REMEMBER_START_CACHE:-1}" = "1" ] || return 0
    [ -n "${REMEMBER_DIR:-}" ] || return 0
    local _dir="$REMEMBER_DIR/tmp"
    mkdir -p "$_dir" 2>/dev/null || return 0
    local _cache="$_dir/start-context.cache"
    # mktemp, not a PID-suffixed literal name (#429, and this file's own
    # convention elsewhere in this codebase): the name would sit in
    # REMEMBER_DIR/tmp before the file exists there, and mktemp creates it
    # atomically, unpredictably-named, and already 0600.
    local _tmp_cache
    _tmp_cache=$(mktemp "${_cache}.XXXXXX" 2>/dev/null) || return 0
    _remember_render_memory_section > "$_tmp_cache" 2>/dev/null
    _remember_start_cache_context_finish_publish "$_tmp_cache"
}
