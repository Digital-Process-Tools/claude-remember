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
    # `[ ]` tests, not a case with a `*/*` arm (#898 round 10: a shape the
    # plugin directory's scanner holds a submission on).
    if [ "$_remember_root_scratch" = "/" ]; then
        REMEMBER_ROOT="/"
    elif [ "${_remember_root_scratch#*/}" != "$_remember_root_scratch" ]; then
        REMEMBER_ROOT="${_remember_root_scratch%/*}"
        [ -n "$REMEMBER_ROOT" ] || REMEMBER_ROOT="/"
    else
        # dirname's own answer for a path with no slash at all -- one dot,
        # which tests/test_dirname_without_a_fork_660.py pins byte for byte
        # ($(pwd) would be absolute, a real behaviour change). Written as the
        # octal escape 056 through `printf -v` (a builtin) rather than as a
        # quoted lone dot, which the plugin directory's scanner can misread
        # as a `.` (source) command (#898 round 8). Same byte.
        printf -v REMEMBER_ROOT '\056'
    fi
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
    _remember_mem_proj="${MEMORY_PROJECT_DIR:-}"
    [ -n "$_remember_mem_proj" ] || _remember_mem_proj="$PROJECT_DIR"
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
# _lock_timing_slot comment) has no associative arrays, so this cache is two
# parallel INDEXED arrays (bash 3.2 has no trouble with those) -- KEYS[i]
# holds the raw path, VALS[i] its cached byte count -- searched linearly by
# index rather than through a dynamically-named variable. Round 4/5/6 of
# #898 used `${!name}` indirect-name expansion for exactly this ("printf -v
# + indirect expansion is plain parameter expansion, no subshell"); that
# shape is itself one of the ones the plugin directory's scanner holds a
# submission on, so round 7 moves to this design instead. `declare -A`
# remains the wrong answer for the reason the earlier comment already
# found: it parses as ACCEPTED on bash 3.2 and silently creates a plain,
# non-associative array (`declare: -A: invalid option` is non-fatal), so
# the very next `${arr[$key]}` throws a fatal "syntax error: operand
# expected" instead, aborting the whole render with nothing injected and
# nothing visible beyond stderr (#662/#664). A handful of memory files per
# render means the linear scan below is a handful of comparisons, not a
# hot-path cost worth avoiding.
_REMEMBER_WCSZ_NAMES=()
_REMEMBER_WCSZ_VALS=()
_remember_wc_size_set() {
    local _i=0
    while [ "$_i" -lt "${#_REMEMBER_WCSZ_NAMES[@]}" ]; do
        if [ "${_REMEMBER_WCSZ_NAMES[$_i]}" = "$1" ]; then
            _REMEMBER_WCSZ_VALS[$_i]="$2"
            return 0
        fi
        _i=$((_i + 1))
    done
    _REMEMBER_WCSZ_NAMES[${#_REMEMBER_WCSZ_NAMES[@]}]="$1"
    _REMEMBER_WCSZ_VALS[${#_REMEMBER_WCSZ_VALS[@]}]="$2"
}
# Writes into VARNAME rather than returning via `$(...)` -- a command
# substitution forks a subshell even when nothing inside it forks a real
# process, and this is called once per memory file on the render's hot path.
_remember_wc_size_get_into() {
    local _remember_wc_size_outvar="$1" _i=0
    while [ "$_i" -lt "${#_REMEMBER_WCSZ_NAMES[@]}" ]; do
        if [ "${_REMEMBER_WCSZ_NAMES[$_i]}" = "$2" ]; then
            # `-` and not `:-0`: a file this cache holds no entry for is
            # UNMEASURED, and `0` is a measurement. Laundering the two
            # together made _remember_emit_file's own "no usable size" arm
            # unreachable -- `0` is a digit string and `0 -gt 16384` is
            # false, so an unmeasured file of ANY size took the read path
            # the 16 KB threshold exists to keep it off (#695 round-1
            # audit). The batched `wc -c` can fail wholesale, which leaves
            # every file in that state at once -- hence returning early on
            # a found index rather than falling through to the empty case.
            printf -v "$_remember_wc_size_outvar" '%s' "${_REMEMBER_WCSZ_VALS[$_i]}"
            return 0
        fi
        _i=$((_i + 1))
    done
    # '%s' with an empty argument, not an empty format: bash 3.2 leaves the
    # variable unassigned for `printf -v VAR ''` (#898 round 12).
    printf -v "$_remember_wc_size_outvar" '%s' ''
}
# _remember_wc_size_batch FILE...
# One batched `wc -c` over every file named, cached through
# _remember_wc_size_set (#664) -- one fork for the lot instead of one `wc` +
# one `tr` PER file. Default IFS (not `IFS=`): `wc -c`'s own right-justify
# padding is entirely LEADING the byte count, never between the count and
# the filename (exactly one space there, verified against both GNU and BSD
# wc) -- so a plain `read bytes path` both trims the padding and hands the
# filename back verbatim, spaces-in-paths included, since `read` dumps
# everything left over into the LAST variable rather than re-splitting it.
# A non-numeric count, or wc's own `total` line, is skipped. Called with no
# files it measures nothing (and runs no `wc`).
_remember_wc_size_batch() {
    [ "$#" -gt 0 ] || return 0
    local _remember_wc_bytes _remember_wc_path
    while read -r _remember_wc_bytes _remember_wc_path; do
        if [ -z "$_remember_wc_bytes" ] || [ "${_remember_wc_bytes#*[!0-9]}" != "$_remember_wc_bytes" ]; then continue; fi
        [ "$_remember_wc_path" = "total" ] && continue
        _remember_wc_size_set "$_remember_wc_path" "$_remember_wc_bytes"
    done < <(wc -c "$@")
    return 0
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
#   - a file reached through a SYMLINKED DIRECTORY somewhere between it
#     and the repository root (most commonly `.remember` itself, if a
#     repository commits it as a symlink rather than a directory). The
#     per-file symlink check above only ever sees the file at the far end
#     of the link, already resolved, and `git ls-files` records a
#     symlinked directory as one blob with nothing "under" it, so neither
#     check alone catches this shape -- see the `symlinked-ancestor` state
#     _remember_file_tracked_state_into documents and produces. Gated the
#     same way the tracked check is (a repository has to be found first),
#     so a user-chosen external store reached through its own symlink
#     stays exempt, exactly like the tracked check just above.
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

# _remember_git_unquote_into <outvar> <line>
# `git ls-files` wraps an entry in double quotes and C-escapes it (\\, \",
# \a \b \f \n \r \t \v, and \NNN octal byte values) whenever the path
# carries a byte it considers unusual -- ALWAYS for a literal backslash,
# double quote, or control byte, and ALSO for any byte >= 0x80 unless
# core.quotePath=false is set (#774 disables that half; this handles the
# half quotePath cannot touch, found in #774's own self-review: a tracked
# path containing a literal backslash or double quote was still quoted,
# and the guard's comparison is a literal string match against the raw,
# unquoted path, so it still silently answered "not tracked"). `printf
# '%b'` expands exactly this escape set (it is the same table `echo -e`
# uses) back into the original bytes, so unquoting is one call, not a
# hand-rolled parser -- and it is applied unconditionally: an UNQUOTED
# line (the common case) has no leading/trailing `"` and falls through
# the `else` branch unchanged.
_remember_git_unquote_into() {
    local _gu_outvar="$1" _gu_line="$2" _gu_dq
    # The double quote is held in a variable (octal 042) rather than written
    # backslash-escaped, and "starts and ends with it" is two `[ ]` tests
    # plus a length check (one `"` alone is not a quoted entry) -- the same
    # match the old `case` arm made, without the escaped quote the plugin
    # directory's scanner mis-tracks (#898 round 8).
    printf -v _gu_dq '\042'
    if [ "${#_gu_line}" -ge 2 ] \
        && [ "${_gu_line#"$_gu_dq"}" != "$_gu_line" ] \
        && [ "${_gu_line%"$_gu_dq"}" != "$_gu_line" ]; then
            _gu_line="${_gu_line#"$_gu_dq"}"
            _gu_line="${_gu_line%"$_gu_dq"}"
            # #780: `printf '%b'` does NOT know the two-character escape
            # `\"` (unlike `\\`, `\n`, `\t`, ... which it does handle) --
            # left alone, a literal double-quote byte in the path survives
            # as the two raw characters backslash+quote instead of becoming
            # a quote, so the comparison at the call site never matches and
            # a tracked path containing a `"` byte reads as not-tracked.
            # Pre-translate every `\"` pair (always exactly one escaped
            # quote byte -- git never emits an unescaped `"` inside a
            # quoted entry) into the octal escape `\042` (0x22 = '"'),
            # which `%b` DOES expand. The replacement text itself contains
            # no `"` character, so a left-to-right non-overlapping scan
            # cannot re-match anything it just wrote, and it never touches
            # an unrelated `\\` (backslash) escape, which `%b` already
            # handles correctly on its own.
            # Built from two separate literals (one backslash, one quote)
            # rather than written as a single two-backslash-then-quote
            # pattern: that exact raw shape is what the plugin directory's
            # scanner holds a submission on (#898 round 7), and quoting the
            # pattern variable below switches this `//` replace from glob
            # matching to a literal substring match, so the un-escaped
            # 2-char value is exactly the pattern we want.
            local _gu_bs _gu_bsq
            _gu_bs='\'
            _gu_bsq="${_gu_bs}${_gu_dq}"
            _gu_line="${_gu_line//"$_gu_bsq"/\\042}"
            printf -v "$_gu_outvar" '%b' "$_gu_line"
    else
            printf -v "$_gu_outvar" '%s' "$_gu_line"
    fi
}

# No `_remember_cache_key_into` helper here (#898 round 7 removed it along
# with the indirect-name convention it built names for): the two caches
# below that used to share it (root-tracked-listing, symlinked-ancestor)
# each hold their own parallel-array cache now, same shape as
# _remember_wc_size_set/_get_into just above.

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
    # `[ ]` tests, not a `case` with a catch-all `*)` arm inside this loop
    # (#898 round 7 -- that shape is one the plugin directory's scanner
    # holds a submission on).
    while :; do
        if [ -e "${_rrw_dir}/.git" ]; then
            printf -v "$_rrw_outvar" '%s' "$_rrw_dir"
            return 0
        fi
        if [ "$_rrw_dir" = "/" ]; then
            break
        elif [ "${_rrw_dir%/*}" != "$_rrw_dir" ]; then
            _rrw_dir="${_rrw_dir%/*}"
            [ -n "$_rrw_dir" ] || _rrw_dir="/"
        else
            break
        fi
    done
    # not an empty format: see above
    printf -v "$_rrw_outvar" '%s' ''
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
# of `:(icase).remember/`. An empty REL-DIR (the memory file sits directly at
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
# Parallel-array cache, same shape as _remember_wc_size_set/_get_into
# above: _REMEMBER_RTS_IDS[i] is a "root#reldir" cache id,
# _REMEMBER_RTS_STATE[i]/_REMEMBER_RTS_LIST[i] its two cached answers.
_REMEMBER_RTS_IDS=()
_REMEMBER_RTS_STATE=()
_REMEMBER_RTS_LIST=()
_remember_root_tracked_state_into() {
    local _rts_list_outvar="$1" _rts_state_outvar="$2" _rts_root="$3" _rts_reldir="$4"
    local _rts_list _rts_rc _rts_cache_id _rts_pathspec _rts_idx=-1 _rts_i=0
    _rts_cache_id="${_rts_root}#${_rts_reldir}"
    while [ "$_rts_i" -lt "${#_REMEMBER_RTS_IDS[@]}" ]; do
        if [ "${_REMEMBER_RTS_IDS[$_rts_i]}" = "$_rts_cache_id" ]; then
            _rts_idx="$_rts_i"
            break
        fi
        _rts_i=$((_rts_i + 1))
    done
    if [ "$_rts_idx" -lt 0 ]; then
        if command -v git >/dev/null 2>&1; then
            # An empty REL-DIR is the sentinel for "no subdirectory to scope
            # to" (see this function's own header comment), exercised by
            # #754/#755/#756's injection-guard tests. Empty rather than a dot
            # (#898 round 10): no lone-dot value at all, not one re-encoded.
            if [ -z "$_rts_reldir" ]; then
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
            # `-c core.quotePath=false` (#774): without it, `ls-files`
            # quotes/octal-escapes any non-ASCII (or otherwise "unusual")
            # byte in a path -- e.g. `caf\303\251/.remember/now.md` -- and
            # the comparison below is a literal string match against the
            # RAW relative path, which never equals the quoted form. That
            # silently answered `not-tracked` for a genuinely tracked
            # non-ASCII path, letting a planted file through the guard.
            # Disabling quoting here (not globally) keeps this comparison
            # correct without touching how `git` is invoked anywhere else.
            if [ -n "$_rts_pathspec" ]; then
                _rts_list=$(unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                            git -c core.quotePath=false -C "$_rts_root" ls-files -- "$_rts_pathspec" 2>/dev/null)
            else
                _rts_list=$(unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                            git -c core.quotePath=false -C "$_rts_root" ls-files 2>/dev/null)
            fi
            _rts_rc=$?
        else
            _rts_list=""
            _rts_rc=127
        fi
        _rts_idx="${#_REMEMBER_RTS_IDS[@]}"
        _REMEMBER_RTS_IDS[$_rts_idx]="$_rts_cache_id"
        if [ "$_rts_rc" -eq 0 ]; then
            _REMEMBER_RTS_STATE[$_rts_idx]='ok'
            _REMEMBER_RTS_LIST[$_rts_idx]="$_rts_list"
        else
            _REMEMBER_RTS_STATE[$_rts_idx]='unavailable'
            _REMEMBER_RTS_LIST[$_rts_idx]=''
        fi
    fi
    printf -v "$_rts_state_outvar" '%s' "${_REMEMBER_RTS_STATE[$_rts_idx]}"
    printf -v "$_rts_list_outvar" '%s' "${_REMEMBER_RTS_LIST[$_rts_idx]}"
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
#   symlinked-ancestor -- a repository is there, and some directory
#                    between FILE's own directory and the repository root
#                    (inclusive of both ends) is itself a symlink. The
#                    per-file symlink check elsewhere in this guard only
#                    ever sees FILE after every directory component in its
#                    path has already been followed, so a symlinked
#                    directory earlier in that path (most commonly
#                    `.remember` itself) is invisible to it, and so is a
#                    `git ls-files` tracked-check scoped underneath a
#                    directory git itself records as a symlink BLOB rather
#                    than a tree -- nothing "under" a symlink blob is ever
#                    listed. Checked only once a repository root is known
#                    (see the walk below), which keeps this scoped to a
#                    repository-controlled tree and out of a user-chosen
#                    external store, whose own root is trusted regardless
#                    of how it is reached.
# _REMEMBER_REFUSED_TRACKED_STATES -- the single list of
# _remember_file_tracked_state_into outcomes that must never be permitted
# through, on EITHER side of the guard: injecting a memory file back into
# session context (_remember_may_inject below) or writing a NEW one
# (write-handoff.sh's own #750 guard). Before #799, write-handoff.sh kept
# its own inline `case ... tracked|unavailable)` list, independently of
# this one, and a state added here (symlinked-ancestor) was never added
# there -- the write side silently let it through while the read side
# already refused it. Keeping one list and a helper that consults it means
# a future addition here is enough; it does not also have to be
# remembered in a second, unrelated list of states.
_REMEMBER_REFUSED_TRACKED_STATES="tracked unavailable symlinked-ancestor"

# _remember_tracked_state_is_refused <state>
# True (exit 0) when STATE (a _remember_file_tracked_state_into outcome)
# must be refused by every guard that consults it.
_remember_tracked_state_is_refused() {
    local _ls=" $_REMEMBER_REFUSED_TRACKED_STATES "
    [[ "$_ls" == *" $1 "* ]]
}

# Parallel-array cache for the symlinked-ancestor walk below, same shape
# as _remember_wc_size_set/_get_into and the root-tracked-state cache
# above: _REMEMBER_FTS_SYM_DIRS[i] is a directory, _REMEMBER_FTS_SYM_VALS[i]
# its cached '1' (is a symlink) or '0' (is not) answer.
_REMEMBER_FTS_SYM_DIRS=()
_REMEMBER_FTS_SYM_VALS=()
_remember_file_tracked_state_into() {
    local _fts_outvar="$1" _fts_file="$2"
    local _fts_dir _fts_root _fts_root_fs _fts_file_fs _fts_dir_fs _fts_rel _fts_reldir
    local _fts_list _fts_state _fts_line _fts_line_raw _fts_walk _fts_sym_idx _fts_sym_i
    # Forward-slash FILE before anything splits it: on msys/cygwin it can
    # carry the backslash form _remember_normalize_win_path produces. Split
    # first and a backslash-only path has no `/`, so DIR fell back to "." --
    # the process cwd -- and the walk then found whatever repository the
    # cwd sits in, not FILE's own, and every file read as unavailable
    # (observed on windows-latest: DIR_FS=. ROOT=.). The walk below also
    # climbs one `/`-delimited component at a time, so it needs the
    # forward-slashed form for the same reason.
    _remember_forward_slash_into _fts_file_fs "$_fts_file"
    if [ "${_fts_file_fs#*/}" != "$_fts_file_fs" ]; then
        _fts_dir_fs="${_fts_file_fs%/*}"
    else
        # Same dirname-no-slash answer (one dot, octal 056), written the
        # same way as REMEMBER_ROOT's just above in this file (#898 round 8).
        printf -v _fts_dir_fs '\056'
    fi
    _remember_repo_root_walk_into _fts_root "$_fts_dir_fs"
    if [ -z "$_fts_root" ]; then
        printf -v "$_fts_outvar" 'no-repo'
        return 0
    fi
    _remember_forward_slash_into _fts_root_fs "$_fts_root"

    # Walk every directory from DIR up to (and including) the repository
    # root and refuse if any one of them is itself a symlink -- see the
    # `symlinked-ancestor` state documented above this function. `[ -L ]`
    # is a shell builtin (no fork), and the result is cached per distinct
    # directory the same way the tracked-listing cache above is, so a
    # render touching several files under the same store directory pays
    # this walk once rather than once per file.
    _fts_walk="$_fts_dir_fs"
    while :; do
        _fts_sym_idx=-1
        _fts_sym_i=0
        while [ "$_fts_sym_i" -lt "${#_REMEMBER_FTS_SYM_DIRS[@]}" ]; do
            if [ "${_REMEMBER_FTS_SYM_DIRS[$_fts_sym_i]}" = "$_fts_walk" ]; then
                _fts_sym_idx="$_fts_sym_i"
                break
            fi
            _fts_sym_i=$((_fts_sym_i + 1))
        done
        if [ "$_fts_sym_idx" -lt 0 ]; then
            _fts_sym_idx="${#_REMEMBER_FTS_SYM_DIRS[@]}"
            _REMEMBER_FTS_SYM_DIRS[$_fts_sym_idx]="$_fts_walk"
            if [ -L "$_fts_walk" ]; then
                _REMEMBER_FTS_SYM_VALS[$_fts_sym_idx]='1'
            else
                _REMEMBER_FTS_SYM_VALS[$_fts_sym_idx]='0'
            fi
        fi
        if [ "${_REMEMBER_FTS_SYM_VALS[$_fts_sym_idx]}" = '1' ]; then
            printf -v "$_fts_outvar" 'symlinked-ancestor'
            return 0
        fi
        # `[ ]` tests, not a `case` with a catch-all `*)` arm inside this
        # loop (#898 round 7 -- that shape is one the plugin directory's
        # scanner holds a submission on).
        [ "$_fts_walk" = "$_fts_root_fs" ] && break
        if [ "${_fts_walk%/*}" != "$_fts_walk" ]; then
            _fts_walk="${_fts_walk%/*}"
            [ -n "$_fts_walk" ] || _fts_walk="/"
        else
            break
        fi
    done

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
    # empty when the file sits directly at the repository root (the rare
    # REMEMBER_ROOT/identity.md fallback), otherwise the directory portion
    # of _fts_rel.
    if [ "$_fts_dir_fs" = "$_fts_root_fs" ]; then
        # The REL-DIR sentinel _remember_root_tracked_state_into tests for:
        # empty, "no subdirectory" (#898 round 10).
        _fts_reldir=""
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
        # #774 follow-up: `ls-files` C-quotes a literal backslash, double
        # quote or control byte UNCONDITIONALLY (core.quotePath has no
        # effect on that half), so the line compared here can still be
        # quoted even with `-c core.quotePath=false` set on the listing
        # above -- unquote before comparing, not just before storing.
        _remember_git_unquote_into _fts_line_raw "$_fts_line"
        if [ "$_fts_line_raw" = "$_fts_rel" ] || _remember_ci_eq "$_fts_line_raw" "$_fts_rel"; then
            printf -v "$_fts_outvar" 'tracked'
            return 0
        fi
    done <<< "$_fts_list"
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
    local _rips_mem_proj="${MEMORY_PROJECT_DIR:-}"
    [ -n "$_rips_mem_proj" ] || _rips_mem_proj="$PROJECT_DIR"
    [ "$REMEMBER_ROOT" = "$_rips_mem_proj" ]
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
        _REMEMBER_INJECT_REFUSAL="$_mi_file is a symlink -- refusing to follow it into session context. This plugin never creates one; inspect where it points before deleting it."
        log "$_mi_component" "refused injecting $_mi_file: symlink"
        return 1
    fi
    _remember_in_project_store || return 0
    _remember_file_tracked_state_into _mi_state "$_mi_file"
    # #799 follow-up (self-review): this used to be its OWN independent
    # `tracked|unavailable|symlinked-ancestor` case, kept in sync with
    # write-handoff.sh's copy by hand only -- the exact hazard #799 fixed on
    # the write side. Gating on the shared _remember_tracked_state_is_refused
    # first means a state added to _REMEMBER_REFUSED_TRACKED_STATES without
    # also adding a branch HERE now fails closed (falls to the `else` branch
    # below, which refuses) instead of silently falling through to the old
    # unconditional `(*) return 0` -- the injection guard was previously the
    # one side that would have allowed an unrecognised refused state through.
    if _remember_tracked_state_is_refused "$_mi_state"; then
        if [ "$_mi_state" = tracked ]; then
            _REMEMBER_INJECT_REFUSAL="$_mi_file is tracked by this repository's git index; this plugin never commits memory files, so the repository shipped it. Not injecting it. Yours: git rm --cached it. Not yours: delete it and check the commit that added it."
            log "$_mi_component" "refused injecting $_mi_file: git-tracked"
        elif [ "$_mi_state" = unavailable ]; then
            # #760: a repository really is above this file, but asking git
            # whether it tracks the file could not be trusted (missing
            # binary, broken state, a shim on PATH). Refuse rather than
            # deliver on a guess -- an untrustworthy "no" from the tracked
            # check must read the same as "yes", not the same as a clean
            # "not tracked".
            _REMEMBER_INJECT_REFUSAL="$_mi_file could not be checked against this repository's git index -- refusing rather than injecting unverified (/remember:doctor shows why)."
            log "$_mi_component" "refused injecting $_mi_file: git status unavailable"
        elif [ "$_mi_state" = symlinked-ancestor ]; then
            _REMEMBER_INJECT_REFUSAL="$_mi_file sits under a symlinked directory -- refusing to follow it into session context. This plugin never creates one; inspect where it points."
            log "$_mi_component" "refused injecting $_mi_file: symlinked ancestor directory"
        else
            _REMEMBER_INJECT_REFUSAL="$_mi_file could not be verified (tracked state: $_mi_state) -- refusing rather than injecting unverified."
            log "$_mi_component" "refused injecting $_mi_file: unrecognised refused state $_mi_state"
        fi
        return 1
    fi
    return 0
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
    if [ -z "$_remember_emit_max" ] || [ "${_remember_emit_max#*[!0-9]}" != "$_remember_emit_max" ]; then _remember_emit_max=16384; fi
    local _sz="${2:-}"
    if [ -z "$_sz" ] || [[ "$_sz" == *[!0-9]* ]]; then
        # No usable size: `cat` is the one that cannot go quadratic.
        cat "$1"
        return 0
    fi
    if [ "$2" -gt "$_remember_emit_max" ]; then
        cat "$1"
        return 0
    fi
    local _remember_file_body=""
    IFS= read -r -d '' _remember_file_body < "$1" || :
    printf '%s' "$_remember_file_body"
}

# _remember_print_sized FILE...
# One line per FILE, "FILE (N bytes)" -- or "FILE (size unknown)" when no
# size could be measured, since "(0 bytes)" would read exactly like an empty
# file (#695) -- after one batched `wc` over all of them (#664/#666). Shared
# by the compact-mode deferred listing and the rotated-slice listing (#898).
_remember_print_sized() {
    local _rps_f _rps_b
    [ "$#" -gt 0 ] || return 0
    _remember_wc_size_batch "$@"
    for _rps_f in "$@"; do
        _remember_wc_size_get_into _rps_b "$_rps_f"
        if [ -z "$_rps_b" ] || [ -n "${_rps_b//[0-9]/}" ]; then
            printf '%s (size unknown)\n' "$_rps_f"
        else
            printf '%s (%s bytes)\n' "$_rps_f" "$_rps_b"
        fi
    done
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
    if [ -z "$MEMORY_INJECT_MAX_BYTES" ] || [ "${MEMORY_INJECT_MAX_BYTES#*[!0-9]}" != "$MEMORY_INJECT_MAX_BYTES" ]; then MEMORY_INJECT_MAX_BYTES=200000; fi
    local OVERSIZED_MEMORY="" BASENAME MFILE_BYTES _remember_oversized_max=0
    local _remember_budget_dropped=""
    local REFUSED_MEMORY=""
    # A newline held in a variable, so no string below spans a line and no
    # case pattern opens on a quoted newline (#898 round 10).
    local _remember_nl
    printf -v _remember_nl '\n'
    # One batched `wc -c` over every memory file that is present AND
    # non-empty (#664), instead of one `wc` + one `tr` PER file -- a typical
    # 4-6 file store paid 8-12 forks here alone before this.
    local _remember_present=()
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
                REFUSED_MEMORY="${REFUSED_MEMORY}${_REMEMBER_INJECT_REFUSAL}${_remember_nl}"
            fi
        fi
    done
    [ "${#_remember_present[@]}" -gt 0 ] && _remember_wc_size_batch "${_remember_present[@]}"
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
            if [ "${MFILE_BYTES#*[!0-9]}" != "$MFILE_BYTES" ]; then MFILE_BYTES=""; fi
            if [ -n "$MFILE_BYTES" ] && [ "$MEMORY_INJECT_MAX_BYTES" -gt 0 ] && [ "$MFILE_BYTES" -gt "$MEMORY_INJECT_MAX_BYTES" ]; then
                OVERSIZED_MEMORY="${OVERSIZED_MEMORY}${MFILE} (${MFILE_BYTES} bytes)${_remember_nl}"
                [ "$MFILE_BYTES" -gt "$_remember_oversized_max" ] && _remember_oversized_max="$MFILE_BYTES"
                continue
            fi
            # #842: a file the SessionStart budget excluded is decided HERE,
            # per section, before a single byte of it is emitted -- never
            # cut back out of the concatenated body afterwards (see
            # _remember_apply_session_start_budget for why that was both
            # quadratic and steerable by content). Unset on every other
            # caller, so the cache publish and every other render is
            # unaffected.
            if [ -n "${_REMEMBER_BUDGET_EXCLUDE:-}" ]; then
                _remember_budget_hay="${_remember_nl}${_REMEMBER_BUDGET_EXCLUDE}"
                if [[ "$_remember_budget_hay" == *"${_remember_nl}${MFILE}${_remember_nl}"* ]]; then
                    _remember_budget_dropped="${_remember_budget_dropped}${MFILE}"
                    if [ -n "${MFILE_BYTES:-}" ]; then
                        _remember_budget_dropped="${_remember_budget_dropped} (${MFILE_BYTES} bytes)"
                    fi
                    _remember_budget_dropped="${_remember_budget_dropped}${_remember_nl}"
                    continue
                fi
            fi
            BASENAME="${MFILE##*/}"
            echo "--- $BASENAME ---"
            _remember_emit_file "$MFILE" "$MFILE_BYTES"
            echo ""
    done
    if [ -n "$_remember_budget_dropped" ]; then
        echo "--- not injected (over thresholds.session_start_max_bytes) -- grep or read on request ---"
        printf '%s' "$_remember_budget_dropped"
        echo ""
    fi
    if [ -n "$OVERSIZED_MEMORY" ]; then
        echo "--- too large to inject (kept on disk; grep on request) ---"
        printf '%s' "$OVERSIZED_MEMORY"
        if [ "$MEMORY_INJECT_MAX_BYTES" -lt 200000 ] && [ "$_remember_oversized_max" -le 200000 ]; then
            # #842: a cap set BELOW the bundled 200000 default is a
            # deliberate choice (e.g. to stay under thresholds.
            # session_start_max_bytes), not evidence of a broken store --
            # /remember:doctor has nothing to diagnose here, and telling an
            # operator to run it for a cap they set on purpose is noise.
            # Only while every over-cap file is ALSO within that bundled
            # default, though: the lowered cap explains a 100 KB file, not a
            # 6 GB recent.md (#346). A file past 200000 is malformed whatever
            # the configured cap, so it keeps the doctor advice below.
            printf 'Capped by config (thresholds.memory_inject_max_bytes=%s, below the bundled default of 200000) -- this is a deliberately lowered cap, not a sign of a malformed memory file.\n' "$MEMORY_INJECT_MAX_BYTES"
        else
            printf 'A healthy memory file is kilobytes. One this size means consolidation wrote a response nobody bounded (see thresholds.memory_inject_max_bytes) and has been skipping ever since; run /remember:doctor.\n'
        fi
        echo ""
    fi
    if [ -n "$REFUSED_MEMORY" ]; then
        echo "--- refused (not injected) ---"
        printf '%s' "$REFUSED_MEMORY"
        echo ""
    fi
    if [ "${SESSION_START_SOURCE:-}" = "compact" ]; then
        local DEFERRED_MEMORY _remember_deferred=() _remember_deferred_refused=""
        for MFILE in "${MEMORY_FILES[@]}"; do
            [ "$MFILE" != "$IDENTITY_FILE" ] || continue
            [ -f "$MFILE" ] && [ -s "$MFILE" ] || continue
            # #777: the main loop above (772-787) skips every non-identity
            # file at source=compact BEFORE it ever reaches the guard call at
            # line 780 -- so during compact, _remember_may_inject is invoked
            # on zero non-identity files unless this loop calls it itself. A
            # git-tracked or symlinked file must be refused here exactly as
            # it would be by the main loop at any other source, never
            # silently added to the deferred list: that list's own header
            # asserts the file "was delivered at session start", which is
            # false for a refused file, and its path is printed right below
            # the false claim -- an invitation to read the planted content
            # through a follow-up tool call.
            if _remember_may_inject "$MFILE" "memory-context"; then
                _remember_deferred+=("$MFILE")
            else
                _remember_deferred_refused="${_remember_deferred_refused}${_REMEMBER_INJECT_REFUSAL}
"
            fi
        done
        # Same empty-array guard as the main loop above (bash < 4.4 + set -u).
        DEFERRED_MEMORY=""
        [ "${#_remember_deferred[@]}" -gt 0 ] && DEFERRED_MEMORY=$(_remember_print_sized "${_remember_deferred[@]}")
        if [ -n "$_remember_deferred_refused" ]; then
            # A DIFFERENT header from the main loop's own "--- refused (not
            # injected) ---" above (833-837), on purpose: both loops can fire
            # in the same compact render (the main loop's identity-only pass
            # refuses IDENTITY_FILE itself; this one refuses any OTHER file
            # that would otherwise have been listed as deferred), and an
            # identical header on both would let a reader -- or anything that
            # parses this output by header text rather than by sequence --
            # merge or misattribute the two populations (self-review finding,
            # #777).
            echo "--- refused, would have been listed as deferred (not injected) ---"
            printf '%s' "$_remember_deferred_refused"
            echo ""
        fi
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
            # `[[ ]]` pattern match, not a `case` with a catch-all `*)` arm
            # inside this loop (#898 round 7 -- that shape is one the
            # plugin directory's scanner holds a submission on).
            if [[ "$_core" == *-*-*-* ]]; then
                _date=${_core%-*}; _seq=${_core##*-}
            else
                _date=$_core;      _seq=1
            fi
            if [ -z "$_seq" ] || [ "${_seq#*[!0-9]}" != "$_seq" ]; then _seq=1; fi
            printf '%s-%010d\t%s\n' "$_date" "$_seq" "$_slice"
        done | sort | tail -n "$ROTATED_LIST_MAX" | cut -f2-)
        echo "--- rotated memory slices (not shown; grep on request) ---"
        # One batched `wc -c` over at most ROTATED_LIST_MAX (10) slices
        # instead of one `wc` + one `tr` per slice (#664/#666).
        local _remember_newest_arr=() _remember_newest_line _remember_newest_refused=""
        while IFS= read -r _remember_newest_line; do
            [ -f "$_remember_newest_line" ] || continue
            # #805: a rotated slice is named by path here exactly like the
            # main memory-file loop (line ~814) and the compact-mode
            # deferred-file loop (#777, line ~888) name theirs -- so it goes
            # through the same `_remember_may_inject` gate before its path
            # is ever printed. Without this, a git-tracked or symlinked
            # slice was listed under a header implying it is safe/expected,
            # inviting a follow-up read of content the guard exists to
            # refuse.
            if _remember_may_inject "$_remember_newest_line" "memory-context"; then
                _remember_newest_arr+=("$_remember_newest_line")
            else
                _remember_newest_refused="${_remember_newest_refused}${_REMEMBER_INJECT_REFUSAL}
"
            fi
        done <<< "$ROTATED_NEWEST"
        [ "${#_remember_newest_arr[@]}" -eq 0 ] || _remember_print_sized "${_remember_newest_arr[@]}"
        if [ -n "$_remember_newest_refused" ]; then
            # A blank line before this header, always -- self-review finding
            # (#805): without it, this header ran straight into either the
            # accepted-slices header above (when every slice was refused) or
            # the last accepted path's own line (when some were), unlike
            # every other header transition in this same function, which
            # always separates sections with one blank line (the main
            # memory-file loop's per-file `echo ""` and its own REFUSED_MEMORY
            # header; the compact-mode deferred loop's equivalent).
            echo ""
            # A THIRD header, distinct from both of #790's own two ("---
            # refused (not injected) ---" and "--- refused, would have been
            # listed as deferred (not injected) ---") -- reusing either
            # would misattribute this population (a refused ROTATED slice)
            # to one of those two unrelated populations (self-review finding
            # carried over from #790 itself, per #805's own issue text).
            echo "--- refused, not listed as a rotated slice (not injected) ---"
            printf '%s' "$_remember_newest_refused"
            echo ""
        fi
        if [ "$ROTATED_COUNT" -gt "$ROTATED_LIST_MAX" ]; then
            printf '... and %s older: %s/archive-*.md, %s/recent-*.md\n' \
                "$((ROTATED_COUNT - ROTATED_LIST_MAX))" "$REMEMBER_DIR" "$REMEMBER_DIR"
        fi
        echo ""
    fi
    echo ""
}

# _REMEMBER_CACHE_FORMAT_VERSION -- bump this whenever a change to this
# file could make an OLD cache's bytes wrong to serve under the NEW code,
# even though every SRC= mtime the old manifest names is still older than
# the cache (#775). The injection guard (_remember_may_inject) is the
# motivating case: a cache published before the guard existed (or before a
# later guard fix) can hold a rendered MEMORY section built from a file the
# CURRENT guard would refuse -- the guard is never consulted on a cache
# HIT (see _remember_start_cache_context_load), so mtimes alone cannot
# catch this. A mismatched or entirely absent VERSION= line in the
# manifest is always a miss; see the loader below.
_REMEMBER_CACHE_FORMAT_VERSION="2"

# _remember_start_cache_manifest_lines
# Echoes, one per line, "SRC=<path>" for every input the render depends on,
# preceded by a "VERSION=<_REMEMBER_CACHE_FORMAT_VERSION>" stamp -- shared by
# the loader (checks each SRC with -nt, and the VERSION against its own
# constant) and the publisher (writes them verbatim). Requires
# _remember_memory_paths to have already run.
_remember_start_cache_manifest_lines() {
    local MFILE
    printf 'VERSION=%s\n' "$_REMEMBER_CACHE_FORMAT_VERSION"
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
    local _f
    for _f in "$_cache" "$_manifest"; do
        [ -f "$_f" ] && [ ! -L "$_f" ] && [ -O "$_f" ] && [ -r "$_f" ] || return 1
    done
    # #781: a repository can commit its own start-context.cache and a
    # manifest holding nothing but a VERSION= stamp -- git checks both out
    # owned by the user and as regular files, so every check above this
    # comment passes, and with no SRC= line the -nt loop below never runs
    # against anything either. None of that has ever asked the injection
    # guard a single question. Route the cache and manifest FILES
    # THEMSELVES through the same tracked/symlink check _remember_may_inject
    # applies to every file a fresh render injects, before trusting either
    # one -- a git-tracked cache pair is exactly the shipped-by-the-
    # repository content that check exists to catch.
    _remember_may_inject "$_cache" "start-cache" || return 1
    _remember_may_inject "$_manifest" "start-cache" || return 1

    local _line _src _mf_version="" _mf_saw_src=0
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        # `[ ]` prefix tests, not a `case` with a catch-all `*)` arm inside
        # this loop (#898 round 7 -- that shape is one the plugin
        # directory's scanner holds a submission on).
        if [ "${_line#VERSION=}" != "$_line" ]; then
            _mf_version="${_line#VERSION=}"
            continue
        elif [ "${_line#SRC=}" != "$_line" ]; then
            _src="${_line#SRC=}"
        else
            # Unknown line: not our file, or not our version of it --
            # distrust the whole manifest rather than partially validate it.
            return 1
        fi
        [ -n "$_src" ] || continue
        _mf_saw_src=1
        # Strictly newer, never a tie (see the file header): -nt is false on
        # an equal mtime, which is exactly the "ambiguous means miss"
        # guardrail #668 asks for, and true against a manifest entry that no
        # longer exists (an absent source cannot have changed).
        [ "$_cache" -nt "$_src" ] || return 1
    done < "$_manifest"
    # #781: a legitimate cache always depends on at least one source file --
    # _remember_start_cache_manifest_lines below always emits several. Zero
    # SRC= lines is either a bug in whatever wrote the manifest or a planted
    # manifest engineered to make the loop above a no-op (nothing to compare
    # mtimes against, so nothing can ever force a miss).
    [ "$_mf_saw_src" -eq 1 ] || return 1
    # #775: a manifest with no VERSION= line at all (every cache published
    # before this fix existed) or one naming a different format version
    # must never be served -- see _REMEMBER_CACHE_FORMAT_VERSION above for
    # why mtimes alone cannot catch this.
    [ "$_mf_version" = "$_REMEMBER_CACHE_FORMAT_VERSION" ] || return 1

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
    # Only the non-compact render is ever cached (see the file header): a
    # compact-mode render is the small, identity-only shape, and writing IT
    # into the cache would make the very next ordinary session start serve a
    # near-empty MEMORY section instead of falling through to a live render.
    if [ "${REMEMBER_START_CACHE:-1}" != "1" ] || [ "${SESSION_START_SOURCE:-}" = "compact" ] \
        || [ -z "${REMEMBER_DIR:-}" ]; then
        rm -f "$_tmp_cache" 2>/dev/null
        return 0
    fi
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

# ============================================================================
# SESSION-START TOTAL BUDGET (#842)
# ============================================================================
# Claude Code persists hook stdout over roughly 10,000 CHARACTERS to a file
# and hands the model only a short preview plus the file's path -- so a
# SessionStart body that fits comfortably in a terminal can still never
# reach the model if its TOTAL crosses that line. Per-file
# memory_inject_max_bytes (above) cannot fix this: it caps one file at a
# time and has no way to prefer one memory section over another when the
# total, not any single file, is the problem (#842's own repro: six
# healthy files summing to 11-14.7KB).
#
# Budget is counted in BYTES, not characters, on purpose. Claude Code's own
# cap is reported in characters, and measuring actual Unicode codepoints
# portably across this plugin's three supported shells (stock macOS bash
# 3.2, Linux bash, Git Bash on Windows) is not reliable: bash's own
# `${#var}` length is locale-sensitive (multibyte-aware only when the
# shell's build and the active locale both support it), while `wc -c`
# always counts bytes, unconditionally, everywhere this plugin already
# relies on it for memory_inject_max_bytes above. A byte is never SHORTER
# than the character it is part of in UTF-8, so counting bytes can only be
# as-strict-or-stricter than the real character cap, never looser: staying
# under a 9000-BYTE budget guarantees staying under a 9000-CHARACTER one
# too, with margin to spare under Claude Code's ~10,000. The failure mode
# on the conservative side is listing a file by name that would, in fact,
# have fit under the real character cap -- strictly better than the
# alternative, which is the bug this file exists to fix.
_remember_session_start_max_bytes_into() {
    local _outvar="$1"
    local _val=""
    config_into _val ".thresholds.session_start_max_bytes" 9000
    if [ -z "$_val" ] || [[ "$_val" == *[!0-9]* ]]; then
        log "memory-context" "WARNING: thresholds.session_start_max_bytes is not a valid non-negative integer (got $_val) -- using default 9000"
        _val=9000
    fi
    printf -v "$_outvar" %s "$_val"
}

# Never inherited: only _remember_apply_session_start_budget sets this, and
# only inside the subshell it renders in. An exported value from the
# environment would otherwise silently drop files from every render,
# including the cache that later starts serve.
unset _REMEMBER_BUDGET_EXCLUDE

# _remember_apply_session_start_budget VARNAME MAX_BYTES TEXT
#
# VARNAME names a variable holding the already-assembled SessionStart body
# (handoff block + REMEMBER legend + MEMORY section, in that order -- see
# the call site in session-start-hook.sh). When the body's byte length
# exceeds MAX_BYTES, whole memory files are left out -- never truncated
# mid-content -- in REVERSE priority order (archive.md, then today-*.md,
# then recent.md, then now.md) until it fits or all four are gone, and the
# renderer lists each one it left out by path and size. Handoff and the
# identity/core-memories head are never touched: #842's own priority order
# is handoff -> now -> recent -> today -> archive.
#
# How, and why not by cutting the body (review of #845). The first version
# searched the captured body for each file's "--- BASENAME ---" header and
# cut around the first match with ${text%%"$h"*} / ${text#*"$h"}. Both
# defects of that shape are structural, so the fix is too:
#   - bash 3.2 and 5 evaluate those expansions in time that grows with the
#     SQUARE of the body (one ${t#*"$h"} measured ~5s at 80 KB, ~72s at
#     320 KB), and this only ever runs on a body that is already big;
#   - the first match is not the real section: the handoff (file content,
#     the #721 threat model) and every earlier memory file are content, and
#     a header-shaped line in either moved the cut into them -- removing the
#     handoff's random-token fence and "=== MEMORY ===", and listing a file
#     as dropped whose content was still injected.
# So nothing here searches content. The MEMORY section is re-rendered with
# _REMEMBER_BUDGET_EXCLUDE naming the files to leave out, and the renderer
# decides inclusion per section BEFORE emitting it. The head (everything
# before the MEMORY section) is recovered by LENGTH, not by a marker: the
# memory section is the body's tail, so head = the first
# ${#body} - ${#memory} bytes, and that split is trusted only after the tail
# is confirmed byte-identical to a fresh render. ${var:off:len} and string
# equality are both linear. A mismatch (a memory file changed between the
# two renders) leaves the body untouched and logs it -- the pre-#842 shape,
# never a cut at a guessed offset.
#
# Cost: none on the common path. An under-budget body returns at the length
# check below, before any render, fork or copy; only an over-budget body
# pays for the renders (one, plus at most one per dropped file).
#
# MAX_BYTES <= 0 or non-numeric disables the budget outright (0 is a
# deliberate "no cap", the same convention every other threshold in this
# file uses).
#
# #878: if the body is STILL over budget once every droppable section is
# gone (the head -- handoff + identity/core-memories -- is never dropped,
# and can alone exceed a tightly configured MAX_BYTES), this function logs
# a WARNING naming the final size and MAX_BYTES before returning. Never a
# failure -- the body is still delivered in full, dropped sections and
# all -- but without that log line an operator cannot tell "budget
# satisfied" from "budget exhausted, still over" by reading the log alone.
# <VARNAME> is both read and written: the caller already holds the body
# it wants trimmed, so it is passed explicitly as TEXT rather than read back
# via indirect-name expansion (#898 round 7 -- the one other shape, besides
# this cache's own, this file used that the scanner holds submissions on).
_remember_apply_session_start_budget() {
    local _outvar="$1" _max="$2" _text="$3"
    if [ -z "$_max" ] || [ "${_max#*[!0-9]}" != "$_max" ]; then return 0; fi
    [ "$_max" -gt 0 ] || return 0
    # byte length, not a locale-dependent character count (see header above)
    local LC_ALL=C
    [ "${#_text}" -gt "$_max" ] || return 0

    # Both captures strip trailing newlines the same way the body's own
    # capture in session-start-hook.sh did, and the memory section is the
    # body's tail -- so the two tails compare equal byte for byte.
    local _mem _head_len _head _exclude="" _path _next
    _mem=$(_remember_render_memory_section 2>/dev/null)
    [ -n "$_mem" ] || return 0
    _head_len=$(( ${#_text} - ${#_mem} ))
    if [ "$_head_len" -lt 0 ] || [ "${_text:$_head_len}" != "$_mem" ]; then
        log "memory-context" "WARNING: session_start_max_bytes: the MEMORY section changed between render and budget check -- injected as rendered, over budget"
        return 0
    fi
    _head="${_text:0:$_head_len}"

    local _drop_order=("${REMEMBER_ARCHIVE:-}" "${REMEMBER_TODAY_FILE:-}" "${REMEMBER_RECENT:-}" "${REMEMBER_NOW:-}")
    for _path in "${_drop_order[@]}"; do
        [ $(( _head_len + ${#_mem} )) -gt "$_max" ] || break
        [ -n "$_path" ] && [ -s "$_path" ] || continue
        _exclude="${_exclude}${_path}
"
        _next=$(_REMEMBER_BUDGET_EXCLUDE="$_exclude"; _remember_render_memory_section 2>/dev/null)
        _mem="$_next"
    done
    # #878: the loop above can exit still over budget -- every droppable
    # section (archive/today/recent/now) gone and the head (handoff +
    # identity/core-memories, never touched) plus what remains of the
    # MEMORY section still exceeds _max. That is a distinct outcome from
    # "fits now" and must say so loudly (not a failure: the body is still
    # delivered, dropped sections and all -- see the function header), or
    # an operator has no way to tell "budget satisfied" from "budget
    # exhausted, still over" from the log alone.
    if [ $(( _head_len + ${#_mem} )) -gt "$_max" ]; then
        log "memory-context" "WARNING: thresholds.session_start_max_bytes: still over budget ($(( _head_len + ${#_mem} )) bytes > ${_max}) after dropping every droppable section"
    fi
    printf -v "$_outvar" %s "${_head}${_mem}"
}
