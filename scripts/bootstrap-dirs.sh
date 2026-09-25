#!/bin/bash
# ============================================================================
# bootstrap-dirs.sh — Single source of truth for .remember/ directory layout
# ============================================================================
#
# DESCRIPTION
#   Creates the memory directory structure and sets up stderr logging.
#   Every hook script sources this after resolve-paths.sh and detect-tools.sh
#   to guarantee the directory tree exists before any file I/O.
#
#   When REMEMBER_DIR points outside the project (external mode), the legacy
#   ${PROJECT_DIR}/.remember/ is migrated automatically on first run and a
#   MIGRATED-TO.txt marker is left behind.
#
# USAGE
#   source "$(dirname "$0")/resolve-paths.sh"
#   source "$(dirname "$0")/detect-tools.sh"
#   source "$(dirname "$0")/bootstrap-dirs.sh"
#
# REQUIRES
#   PROJECT_DIR      must be set (by resolve-paths.sh)
#   PIPELINE_DIR     must be set (by resolve-paths.sh)
#   session_dir_slug must be defined (by detect-tools.sh)
#
# EXPORTS
#   REMEMBER_DIR    — absolute path to memory data directory (via lib-memory-dir.sh)
#   SYS_TMPDIR      — portable system temp directory
#
# ============================================================================

# Resolve REMEMBER_DIR via the shared helper (no-op if already loaded).
_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
# A path with no slash in it (`source log.sh` from the scripts dir) leaves the
# filename behind, not a directory — `dirname` answered "." and this must too.
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="."
source "$_REMEMBER_SRC_DIR/lib-memory-dir.sh"
unset _REMEMBER_SRC_DIR

# --- System temp directory (portable: macOS, Linux, Windows/Git Bash) ---
SYS_TMPDIR="${TMPDIR:-/tmp}"

# --- One-shot migration: legacy .remember → external REMEMBER_DIR ---
# Keyed to MEMORY_PROJECT_DIR (the main checkout when in a worktree) so the
# legacy dir we migrate/gitignore matches where REMEMBER_DIR now resolves.
_mem_proj="${MEMORY_PROJECT_DIR:-$PROJECT_DIR}"
_legacy_dir="${_mem_proj}/.remember"
# -L before -d: -d follows a symlink, so a LEGACY DIRECTORY ITSELF that
# is a symlink (a repository can commit one, pointing anywhere on disk)
# would otherwise satisfy this condition and get `mv`'d wholesale into
# REMEMBER_DIR -- moving the link, not the data, and leaving every later
# session reading and writing through it, indistinguishable at read time
# from a legitimate operator-configured external directory. Refused below
# instead, in its own branch.
if [ "$REMEMBER_DIR" != "$_legacy_dir" ] && [ ! -L "$_legacy_dir" ] && [ -d "$_legacy_dir" ] && [ ! -e "$REMEMBER_DIR" ]; then
    # Never migrate ~/.remember. It is not a legacy project store — it is the
    # user-global config home that lib-memory-dir.sh reads to resolve
    # REMEMBER_DIR in the first place. Open a session with cwd = $HOME and the
    # three conditions above all hold, so the whole directory (config.json
    # included) was moved into the external store: the config that directs the
    # migration was consumed by it. Every later session in every project then
    # found no user config, fell back to data_dir=".remember", and leaked
    # memory into working trees while the central store went stale — and the
    # now-existing home-slug dir meant it never re-fired to reveal itself
    # (issue #132). Only external-mode users could hit it, which is to say
    # exactly the users the config exists to serve.
    #
    # Compared canonically as well as textually: $HOME and PROJECT_DIR can name
    # the same directory by different paths (a symlinked home, /tmp vs
    # /private/tmp on macOS), and a textual miss here costs the user their
    # config. The subshells only run when a migration would otherwise happen,
    # which is once per project at most.
    _migrating_user_config_home=false
    if [ -n "$HOME" ]; then
        if [ "${_mem_proj%/}" = "${HOME%/}" ]; then
            _migrating_user_config_home=true
        else
            _mem_proj_real=$(cd "$_mem_proj" 2>/dev/null && pwd -P)
            _home_real=$(cd "$HOME" 2>/dev/null && pwd -P)
            if [ -n "$_mem_proj_real" ] && [ "$_mem_proj_real" = "$_home_real" ]; then
                _migrating_user_config_home=true
            fi
            unset _mem_proj_real _home_real
        fi
    fi

    if [ "$_migrating_user_config_home" = false ]; then
        mkdir -p "$(dirname "$REMEMBER_DIR")" 2>/dev/null

        # #782: the config.json holdout just below is the ONLY tracked-
        # content protection this migration has ever applied -- every OTHER
        # file the legacy directory holds, including a planted now.md,
        # still moved wholesale into REMEMBER_DIR. The injection guard
        # exempts external storage from its tracked-file check entirely BY
        # DESIGN (`_remember_in_project_store || return 0`,
        # lib-memory-context.sh, #764) because that store can legitimately
        # be the plugin's OWN git_backup repository -- so once a
        # repository-tracked memory file lands inside REMEMBER_DIR via this
        # migration, nothing downstream ever refuses it again. Scanning for
        # tracked content BEYOND config.json (already held out on its own
        # below) and refusing the WHOLE migration when any is found is
        # simpler and safer than generalizing the single-file holdout into a
        # per-file loop: REMEMBER_DIR is never created here, so this
        # session's live render still has nothing to inject FROM the
        # external store, and the legacy directory (with its tracked
        # content) is left exactly where it was for the operator to sort
        # out by hand.
        # Same repo-existence-first shape as _remember_config_tracked_status
        # (lib-memory-dir.sh, #766): "no git binary" and "not a git
        # repository at all" must never read the same way. Only an
        # ENCLOSING repository that git cannot be asked about fails closed
        # (could-not-tell, treated the same as tracked below) -- no
        # repository anywhere above _mem_proj means there is nothing to
        # check, same as the ordinary non-git project.
        # Every assignment from a `git ...` call below is deliberately
        # written `CMD && RC=0 || RC=$?` rather than the plainer `CMD; RC=$?`
        # -- this file can be sourced by a CALLER running under `set -e`
        # (see the nullglob comment further down in this same file for the
        # established precedent), and `git rev-parse --is-inside-work-tree`
        # is EXPECTED to exit non-zero for the ordinary non-repo case. A bare
        # failing command substitution assignment aborts the whole script
        # under `set -e` before this function ever gets to inspect the exit
        # code -- observed directly: `test_an_ordinary_legacy_directory_
        # still_migrates` and `test_migration_moves_legacy_to_external`
        # (both plain non-git tmp dirs) failed with returncode 128 and empty
        # output until this shape was used. Per POSIX, a command that is not
        # the LAST element of an AND-OR list is exempt from triggering
        # errexit, so the first `git ...` call here never aborts the caller.
        _legacy_other_tracked="clean"
        if command -v git >/dev/null 2>&1; then
            _legacy_repo_check=$( (unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                                    LC_ALL=C LANGUAGE=C git -C "$_mem_proj" rev-parse --is-inside-work-tree) 2>&1 ) && _legacy_repo_rc=0 || _legacy_repo_rc=$?
            if [ "$_legacy_repo_rc" -ne 0 ]; then
                case "$_legacy_repo_check" in
                    *"not a git repository"*) : ;;
                    (*) _legacy_other_tracked="could-not-tell" ;;
                esac
            elif [ "$_legacy_repo_check" = "true" ]; then
                _legacy_ls_list=$(unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                                   git -c core.quotePath=false -C "$_mem_proj" ls-files -- ".remember/" 2>/dev/null) && _legacy_ls_rc=0 || _legacy_ls_rc=$?
                if [ "$_legacy_ls_rc" -ne 0 ]; then
                    _legacy_other_tracked="could-not-tell"
                else
                    while IFS= read -r _legacy_ls_line; do
                        [ -n "$_legacy_ls_line" ] || continue
                        case "$_legacy_ls_line" in
                            (.remember/config.json) : ;;
                            (*) _legacy_other_tracked="tracked" ;;
                        esac
                    done <<EOF
$_legacy_ls_list
EOF
                fi
            fi
            # else: a real repository, but $_mem_proj is not inside its work
            # tree (a bare repo) -- nothing to check, stays "clean".
        else
            _legacy_walk=$(cd "$_mem_proj" 2>/dev/null && pwd -P) || _legacy_walk="$_mem_proj"
            while [ -n "$_legacy_walk" ]; do
                if [ -e "$_legacy_walk/.git" ]; then
                    _legacy_other_tracked="could-not-tell"
                    break
                fi
                [ "$_legacy_walk" = "/" ] && break
                _legacy_walk="${_legacy_walk%/*}"
                [ -z "$_legacy_walk" ] && _legacy_walk="/"
            done
            unset _legacy_walk
        fi
        unset _legacy_ls_list _legacy_ls_line _legacy_repo_check _legacy_repo_rc _legacy_ls_rc

        if [ "$_legacy_other_tracked" != "clean" ]; then
            printf 'remember: %s contains git-tracked content beyond config.json (%s) -- refusing to migrate it into the external memory store, which would launder repository-committed content into a location the injection guard trusts unconditionally (#782). Left in place, untouched; move your own files out of it by hand, or `git rm --cached` whatever the repository should not have committed, then start a new session to retry.\n' \
                "$_legacy_dir" "$_legacy_other_tracked" >&2
        fi
    fi
    if [ "$_migrating_user_config_home" = false ] && [ "${_legacy_other_tracked:-clean}" = "clean" ]; then
        # #757: lib-memory-dir.sh trusts ${REMEMBER_DIR}/config.json outright
        # once REMEMBER_DIR is external (~293-300) -- that is right for a
        # config an OPERATOR wrote, but a legacy .remember/config.json a
        # REPOSITORY committed is exactly the untrusted input #726/#740
        # already strip out of the ordinary project layer. Carrying it across
        # this one-shot `mv` unchanged would launder it into the trusted
        # layer permanently, on every session after the first. So: hold a
        # git-tracked config.json out of the move, and leave it behind,
        # still tracked, doing nothing for the new external store -- logged,
        # not silent, so the operator can see why their repo's config.json
        # no longer takes effect and where their own settings now belong.
        # _remember_config_tracked_status (lib-memory-dir.sh, already
        # sourced above) walks up to any ENCLOSING work tree rather than
        # checking only "$_mem_proj/.git" -- a project started from a repo
        # SUBDIRECTORY has no .git of its own, and the repository can still
        # have committed the file two levels up (#754). It also tells
        # "definitely untracked" apart from "could not tell" (a git spawn
        # failing for a reason other than a confirmed answer) -- and
        # could-not-tell fails CLOSED here too: left behind, same as
        # tracked (#760 -- a git failure must never read as a confirmed
        # absence and let the file migrate as trusted by default).
        _legacy_cfg="$_legacy_dir/config.json"
        _legacy_cfg_holdout=""
        # -e alone misses a SYMLINKED config.json whose target does not
        # exist (a dangling link still needs to be held out untouched,
        # never treated as absent) -- -L catches that case without ever
        # resolving the link.
        if [ -e "$_legacy_cfg" ] || [ -L "$_legacy_cfg" ]; then
            case "$(_remember_config_tracked_status "$_mem_proj" ".remember/config.json")" in
                untracked) : ;;
                *)
                    # mv, never cp+rm: a git-tracked config.json can be a
                    # SYMLINK pointing anywhere on disk (outside the repo
                    # entirely), and `cp` follows a symlink by default --
                    # the target file's own bytes would land as an ordinary
                    # regular file inside the repo's working tree, ready to
                    # be committed. `mv` renames the link itself and never
                    # opens what it points to, so a symlink held out here
                    # comes back exactly as it went in, never read, never
                    # copied, never migrated (hardens #757).
                    _legacy_cfg_holdout=$(mktemp "${SYS_TMPDIR:-/tmp}/remember-legacy-cfg-XXXXXX" 2>/dev/null) || _legacy_cfg_holdout=""
                    if [ -n "$_legacy_cfg_holdout" ]; then
                        mv "$_legacy_cfg" "$_legacy_cfg_holdout" 2>/dev/null || _legacy_cfg_holdout=""
                    fi
                    ;;
            esac
        fi

        if mv "$_legacy_dir" "$REMEMBER_DIR" 2>/dev/null; then
            mkdir -p "$_legacy_dir"
            if [ -n "$_legacy_cfg_holdout" ] && { [ -e "$_legacy_cfg_holdout" ] || [ -L "$_legacy_cfg_holdout" ]; }; then
                # Every step from here on is checked: a config held out of
                # a successful migration is the operator's only copy, and
                # an unchecked restore `mv` here (#757's own original
                # shape) silently loses it if the destination is not
                # writable -- with nothing to say so and the holdout then
                # deleted unconditionally regardless.
                if mv "$_legacy_cfg_holdout" "$_legacy_cfg" 2>/dev/null; then
                    printf 'Memory data migrated to:\n  %s\nThis directory is now empty; you may delete it.\n\nconfig.json was NOT migrated: it is tracked by this repository git\nindex (or its git status could not be determined, which this treats the\nsame way) -- treating it as your own trusted config would let a cloned\nrepo choose the summarizer credential, model, or refusal-gate settings\nyour memory pipeline runs with (#757). Left behind here, still doing\nnothing for the external store above. Put your own settings in\n%s/config.json instead.\n' \
                        "$REMEMBER_DIR" "$REMEMBER_DIR" > "$_legacy_dir/MIGRATED-TO.txt"
                    printf 'remember: %s is tracked by this repository git index (or its git status could not be determined); left behind rather than migrated into the trusted external config store (#757)\n' \
                        "$_legacy_cfg" >&2
                    _legacy_cfg_holdout=""
                else
                    # Restore failed -- the operator's config is NOT lost,
                    # it is still sitting at the holdout path below, and
                    # this deliberately does NOT delete it. Left set so the
                    # cleanup at the end of this block skips it too.
                    printf 'Memory data migrated to:\n  %s\nconfig.json could NOT be restored to %s after migration --\nsee the error logged to stderr; it still exists at the path named there\nand was not deleted.\n' \
                        "$REMEMBER_DIR" "$_legacy_cfg" > "$_legacy_dir/MIGRATED-TO.txt"
                    printf 'remember: FAILED to restore %s from its holdout copy -- your config.json was NOT deleted, it is still sitting at %s; move it back to %s by hand. (#757)\n' \
                        "$_legacy_cfg" "$_legacy_cfg_holdout" "$_legacy_cfg" >&2
                fi
            else
                printf 'Memory data migrated to:\n  %s\nThis directory is now empty; you may delete it.\n' \
                    "$REMEMBER_DIR" > "$_legacy_dir/MIGRATED-TO.txt"
            fi
        elif [ -n "$_legacy_cfg_holdout" ] && { [ -e "$_legacy_cfg_holdout" ] || [ -L "$_legacy_cfg_holdout" ]; }; then
            # The dir move itself failed after config.json was already pulled
            # out -- put it back rather than leave the legacy dir missing a
            # file nothing else removed on purpose. Checked the same way as
            # the success path above: an unchecked restore here is the same
            # silent-loss bug, just on the other branch.
            if mv "$_legacy_cfg_holdout" "$_legacy_cfg" 2>/dev/null; then
                _legacy_cfg_holdout=""
            else
                printf 'remember: FAILED to restore %s after the migration move itself failed -- your config.json was NOT deleted, it is still sitting at %s; move it back to %s by hand. (#757)\n' \
                    "$_legacy_cfg" "$_legacy_cfg_holdout" "$_legacy_cfg" >&2
            fi
        fi
        # No unconditional cleanup of the holdout here (#757's own original
        # shape had one, `rm -f "$_legacy_cfg_holdout"` run no matter what):
        # every path above either moves the holdout back out from under
        # itself (nothing left to remove) or leaves it in place ON PURPOSE
        # because the restore failed -- an unconditional rm after either
        # outcome would have deleted the operator's only remaining copy in
        # exactly the failure case this exists to protect.
        unset _legacy_cfg _legacy_cfg_holdout
    fi
    unset _migrating_user_config_home _legacy_other_tracked
elif [ "$REMEMBER_DIR" != "$_legacy_dir" ] && [ -L "$_legacy_dir" ] && [ ! -e "$REMEMBER_DIR" ]; then
    # The legacy .remember DIRECTORY itself is a symlink -- never
    # migrated, left exactly where it is, logged loudly rather than
    # silently (#757).
    printf 'remember: %s is a symlink -- refusing to migrate it; left in place untouched. Move or replace it by hand if this was not intentional. (#757)\n' \
        "$_legacy_dir" >&2
fi
unset _legacy_dir

# --- Create directory structure ---
# Gated on the tree already existing (#230). `mkdir -p` over three directories
# that are already there is a process spent to change nothing, and every hook
# pays it — PostToolUse on every single tool call. The deepest path implies its
# parents, so two tests settle all three, and a partially-removed tree still
# falls through to the unchanged mkdir.
if [ ! -d "$REMEMBER_DIR/logs/autonomous" ] || [ ! -d "$REMEMBER_DIR/tmp" ]; then
    mkdir -p \
        "$REMEMBER_DIR/tmp" \
        "$REMEMBER_DIR/logs" \
        "$REMEMBER_DIR/logs/autonomous" \
        2>/dev/null
fi

# --- Relocate the per-invocation merged config out of the shared OS temp
# root, and sweep what a killed process left behind (#362) ---
#
# lib-memory-dir.sh (sourced above) writes REMEMBER_CONFIG to an
# unpredictable mktemp-named path under $SYS_TMPDIR (#429) and relies solely
# on its own EXIT trap to remove it. On Windows/Git Bash that trap does not reliably fire for this
# plugin's short-lived hook processes -- the harness kills the process rather
# than letting it exit through a path that runs the trap -- so the file
# leaked forever. One machine accumulated 23,908 of them directly in %TEMP%,
# a directory shared with every other app on the box.
#
# This has to happen here, not in lib-memory-dir.sh, for two independent
# reasons. First, that file is pinned byte-for-byte against origin/main by
# tests/test_case_divergence_298.py specifically so nothing can reach into it
# and add cost to the per-tool-call path -- so it cannot change at all, not
# even to add a builtin-only check. Second, REMEMBER_DIR does not exist on
# disk yet at the point lib-memory-dir.sh runs; creating it there (to hold
# the relocated file) would short-circuit the migration guard above
# (`[ ! -e "$REMEMBER_DIR" ]`), which depends on REMEMBER_DIR being absent
# until migration has had its chance to run -- confirmed by
# tests/test_migration.py and tests/test_home_dir_migration.py both failing
# "not migrated" against an earlier version of this fix that created
# REMEMBER_DIR from inside lib-memory-dir.sh.
#
# $REMEMBER_DIR/tmp -- just created above -- is a directory this plugin
# already owns and already uses for its own per-invocation scratch files
# (post-tool-hook.sh's hook-stdin.$$, save.lock, ...). A leftover
# remember-config-*.json there is unambiguously this plugin's own leak,
# never another user's or another app's file on a shared machine, and the
# directory is bounded by what this plugin itself has ever written there
# rather than by everything on the OS -- so sweeping it costs nothing like
# the multi-minute %TEMP% scan #362 reports against a 57k-entry directory.
if [ -d "$REMEMBER_DIR/tmp" ]; then
    # Opportunistic sweep: a remember-config-*.json here whose mtime is
    # older than the threshold belongs to a process that is long gone -- this
    # plugin's own hook scripts never run anywhere near this long, so this
    # cannot collide with a legitimately still-running invocation. This is
    # the backstop for the case the EXIT trap never fires at all.
    #
    # Gated on there being any candidate at all (#666): `find` ran
    # unconditionally on EVERY hook invocation before this, even on a store
    # where the EXIT trap has never once failed to fire and the glob below
    # matches nothing. A glob array costs no fork; `find` still does the
    # real mtime filtering once something is actually there to check.
    # `shopt -p nullglob` exits 1 (even though it prints correctly) whenever
    # the option is currently OFF -- which it is by default -- so capturing
    # it via `var=$(...)` would abort any caller running under `set -e`.
    # `shopt -q` in a plain `&&` conditional never has that problem.
    _remember_stale_cfg_was_nullglob=0
    shopt -q nullglob && _remember_stale_cfg_was_nullglob=1
    shopt -s nullglob
    _remember_stale_cfg_candidates=("$REMEMBER_DIR/tmp"/remember-config-*.json)
    [ "$_remember_stale_cfg_was_nullglob" = 1 ] || shopt -u nullglob
    if [ "${#_remember_stale_cfg_candidates[@]}" -gt 0 ]; then
        find "$REMEMBER_DIR/tmp" -maxdepth 1 -name 'remember-config-*.json' \
            -mmin +30 -exec rm -f {} + 2>/dev/null || true
    fi
    unset _remember_stale_cfg_candidates _remember_stale_cfg_was_nullglob

    # Move THIS invocation's file in, and repoint REMEMBER_CONFIG and the EXIT
    # trap at its new home. Best-effort: a failed mv (cross-device, the
    # directory disappearing under us) leaves REMEMBER_CONFIG at its original
    # $SYS_TMPDIR path, exactly as before this change -- never a merge that
    # used to succeed starting to fail.
    _remember_relocated_cfg="$REMEMBER_DIR/tmp/remember-config-$$.json"
    if [ -n "${REMEMBER_CONFIG:-}" ] && [ -f "$REMEMBER_CONFIG" ] \
        && mv -f "$REMEMBER_CONFIG" "$_remember_relocated_cfg" 2>/dev/null; then
        REMEMBER_CONFIG="$_remember_relocated_cfg"
        export REMEMBER_CONFIG
        # #375: $_remember_relocated_cfg is under $REMEMBER_DIR, which in
        # legacy mode is the raw, non-slugified project directory -- a
        # user-controlled string, unlike lib-memory-dir.sh's own copy of
        # this idiom (see there) whose path lives under $SYS_TMPDIR, which
        # no user names. Embedding a user-controlled path inside a
        # single-quoted span in a string that `trap` re-parses at exit is
        # exactly the composition that broke: an apostrophe in the project
        # path terminates that span early and the remainder is evaluated as
        # shell source at exit, rather than as the filename it names.
        # `printf %q` turns the path into a form the SAME shell can safely
        # re-parse as exactly one word, however many quote characters it
        # contains, so it is embedded UNquoted below -- it already carries
        # its own quoting.
        _remember_relocated_cfg_q=$(printf %q "$_remember_relocated_cfg")
        # Same subshell-safe append lib-memory-dir.sh uses for its own trap --
        # bash keeps a single EXIT trap, and its trap (still targeting the
        # old, now-moved-away path -- a harmless no-op `rm -f` once it is
        # gone) must not be the one this replaces.
        #
        # One more substitution than lib-memory-dir.sh's own copy of this
        # idiom needs: `trap -p` re-quotes its output for safe re-sourcing,
        # rewriting each embedded `'` as `'\''`. lib-memory-dir.sh only ever
        # chains onto a caller's bare function name (no embedded quotes), so
        # that never mattered there -- but the trap we are reading back HERE
        # is lib-memory-dir.sh's own `rm -f '$path'`, which is exactly the
        # case that breaks: stripping only the outer wrapper quotes leaves a
        # dangling, unbalanced `'\''` at the tail (the content itself ends in
        # a quote), and embedding that into a new double-quoted trap body
        # produced an EXIT-time "unexpected EOF while looking for matching
        # `'" on every single invocation once this was measured against the
        # real chain rather than a standalone snippet. Undoing the
        # requoting -- collapsing each `'\''` back to a literal `'` -- makes
        # the extracted text the exact original command again, safe to
        # re-embed. This reverses exactly ONE level of `trap -p`'s
        # requoting, which is all today's chain ever needs (nothing in this
        # codebase installs an EXIT trap before bootstrap-dirs.sh sources
        # lib-memory-dir.sh, so what we read back here is always exactly one
        # `rm -f '$path'`, never something that has already been through
        # this same substitution itself). It is not a general un-quoter --
        # chaining onto a trap value that has itself already passed through
        # one round of this idiom would need a second pass. That extracted
        # text is $SYS_TMPDIR-rooted and therefore not user-controlled, so
        # it is chained here exactly as before -- only the path THIS block
        # owns goes through the %q fix above.
        # #679 (part of #660): `trap -p` is a builtin -- `sed` was the only
        # fork this line paid, at a fixed offset plus one quote-collapse
        # (undoing `trap -p`'s own re-quoting of an embedded `'`).
        # Parameter expansion does the identical strip+collapse with zero
        # forks -- proven byte-identical to the old sed output in
        # tests/test_session_start_spawn_reduction_679.py.
        _remember_trap_raw=$(trap -p EXIT 2>/dev/null)
        _remember_existing_trap="${_remember_trap_raw#trap -- \'}"
        _remember_existing_trap="${_remember_existing_trap%\' EXIT}"
        _remember_existing_trap="${_remember_existing_trap//\'\\\'\'/\'}"
        unset _remember_trap_raw
        if [ -n "$_remember_existing_trap" ]; then
            # shellcheck disable=SC2064
            trap "${_remember_existing_trap}; rm -f ${_remember_relocated_cfg_q}" EXIT
        else
            # shellcheck disable=SC2064
            trap "rm -f ${_remember_relocated_cfg_q}" EXIT
        fi
        unset _remember_existing_trap
        unset _remember_relocated_cfg_q
    fi
    unset _remember_relocated_cfg
fi

# --- Install marker (#401) ---
#
# doctor.sh's SessionEnd liveness check (#370) needs a "remember became
# active here" baseline so a transcript that went quiet BEFORE the store
# existed is not misread as SessionEnd's own silent failure (#392). It used
# to read $REMEMBER_DIR/.gitignore's mtime for that -- the one file under
# REMEMBER_DIR ordinary hook activity never rewrites -- but that file is
# deleted, by design, the first time a legacy-to-external migration is
# backed up with git: hooks.d/after_save/50-git-backup.sh's cleanup of the
# per-slug ".gitignore" bootstrap artifact ("removed per-slug .gitignore
# (legacy bootstrap artifact)"). The two behaviours are individually correct
# and were written years apart; composed, the cleanup silently removed the
# diagnostic's only baseline, and that store's SessionEnd check degraded to
# a permanent WARN, unable to ever reach FAIL again.
#
# This marker is a dedicated replacement nothing else in this codebase ever
# touches, or has any reason to: written once, gated on it not already
# existing, exactly like the .gitignore write below -- but unconditional of
# storage mode, unlike .gitignore, which is only ever written when
# REMEMBER_DIR is inside the project tree. An external-mode store that was
# never migrated from legacy never had a .gitignore baseline at all; this
# marker gives it one for the first time, not only a migrated one.
#
# Gated on REMEMBER_DIR existing for the same reason the .gitignore write
# below is (#204): the mkdir above is best-effort, and writing into a
# directory that failed to materialize would surface the shell's own
# "No such file or directory" as a non-blocking hook failure at every
# session start.
#
# An existing store that upgrades into this fix and has no marker yet gets
# one written the next time any hook sources this file -- the next tool
# call, the next session start -- dated from that moment, not from the
# store's true original install. A store already degraded to WARN-only by
# the .gitignore-deletion composition above self-heals on that next hook
# run rather than staying broken forever; the cost is that quiet transcripts
# between the store's true install and this upgrade cannot be attributed to
# either side of a baseline that did not exist for them, exactly the same
# "nothing has had the chance to prove or disprove this yet" third state
# doctor.sh already renders as the honest answer for a fresh install.
if [ -d "$REMEMBER_DIR" ]; then
    [ -f "$REMEMBER_DIR/.install-marker" ] \
        || { echo 'This file marks when remember was first bootstrapped here. Read only by scripts/doctor.sh (#401); do not delete it.' \
            > "$REMEMBER_DIR/.install-marker"; } 2>/dev/null
fi

# --- Gitignore: only write when REMEMBER_DIR is inside the project tree ---
# In external mode (REMEMBER_DIR outside PROJECT_DIR) there is no gitignore
# to write — the user manages that tree themselves (typically as a private git
# repo at ~/.remember/).
#
# Gated on the store actually existing (#204). The mkdir above is best-effort
# and its status was never checked, so on an unwritable root — the reported
# case is a session opened at `/`, where legacy mode makes REMEMBER_DIR
# `//.remember` on macOS's read-only root volume — this walked into file I/O
# against a directory that was never created. The `2>/dev/null` did not hide
# the result: the shell opens a redirection target BEFORE running the command,
# so it is the shell that reports the failure, outside the scope of the
# redirect meant to silence it, and the user saw
#   bootstrap-dirs.sh: line NN: //.remember/.gitignore: No such file or directory
# surfaced as a non-blocking hook failure at every session start.
#
# The braces close the same hole for the residual race — the store removed
# between this test and the write — where there is no directory check left to
# make. Redirecting the whole group puts the shell's own diagnostic inside the
# suppressed scope.
if [ -d "$REMEMBER_DIR" ]; then
    # #519: both sides forward-slashed before the case-glob match --
    # REMEMBER_DIR and _mem_proj both arrive backslash-separated on
    # msys/cygwin (resolve-paths.sh's own _remember_normalize_win_path),
    # and a bash `case` pattern only ever recognises '/' as a path
    # separator, so an in-project store's REMEMBER_DIR silently never
    # matched "$_mem_proj"/* there -- no .gitignore was ever written. This
    # is NOT the #401 doctor.sh baseline (that reads .install-marker,
    # written unconditionally, just above, unaffected by this gate): the
    # real, still-live consequence is hooks.d/after_save/50-git-backup.sh's
    # own protective .gitignore going missing for that store, so its
    # memory content is not excluded from `git add -A`/`git status`
    # inside the user's own project repository the way the file's comment
    # near the top of this block documents. The write itself still targets
    # the real (unmodified) REMEMBER_DIR; only the comparison is
    # normalized.
    #
    # The gate is duplicated INLINE here rather than calling the shared
    # `_remember_forward_slash` (scripts/resolve-paths.sh, #517) directly
    # (self-review/CI finding, job 100934963344, ubuntu-latest 3.9):
    # this file's own USAGE header says every caller sources resolve-paths.sh
    # first, but that is not true of every REAL caller -- tests/test_external_data_dir.py
    # and tests/test_worktree_memory.py's own `_run_bootstrap()` harnesses
    # source only detect-tools.sh and bootstrap-dirs.sh, never resolve-paths.sh,
    # and neither of those two sources it transitively either. There,
    # `_remember_forward_slash` is undefined, the command substitution below
    # would run it as an unknown external command (bash: ... command not
    # found, exit 127) and silently substitute an EMPTY string, so the case
    # pattern would never match and .gitignore would never be written for
    # ANY REMEMBER_DIR, forward-slash or not -- exactly the regression CI
    # caught (test_gitignore_written_in_legacy_mode /
    # test_worktree_remember_is_gitignored, both asserting on real, ordinary
    # POSIX paths with no backslash involved at all). Same reasoning
    # hooks.d/before_session_start/50-git-restore.sh's own #519 fix already
    # documents for the identical reason.
    _mem_bd_glob_dir="$REMEMBER_DIR"
    _mem_bd_glob_proj="$_mem_proj"
    case "$OSTYPE" in
        msys|cygwin)
            _mem_bd_glob_dir="${_mem_bd_glob_dir//\\//}"
            _mem_bd_glob_proj="${_mem_bd_glob_proj//\\//}"
            ;;
    esac
    case "$_mem_bd_glob_dir" in
        "$_mem_bd_glob_proj"/*)
            [ -f "$REMEMBER_DIR/.gitignore" ] || { echo '*' > "$REMEMBER_DIR/.gitignore"; } 2>/dev/null
            ;;
    esac
fi
unset _mem_proj _mem_bd_glob_dir _mem_bd_glob_proj

# --- Redirect stderr to hook-errors.log ---
# This replaces the 2>> that was in hooks.json. Now the directory is
# guaranteed to exist before we open the file.
# Guard: only redirect if the logs dir was actually created (read-only
# filesystems, Docker read-only mounts, etc. will skip this gracefully).
#
# Second guard (#690): bash's own xtrace stream is on fd 2 unless
# BASH_XTRACEFD says otherwise, so this one line also swallowed every
# `bash -x` profile of these hooks -- from this point on, into a log file the
# profiler never reads, with nothing anywhere saying the rest was lost. A
# truncated trace does not look truncated; it looks like a complete profile of
# a fast hook, and every number derived from it (fork counts, per-step
# attributions, "the largest gap is X") then describes a fraction of the run
# while reading as though it described all of it. Measured on macOS while
# building the #660 diagnostic: wall 0.71s, traced span 0.07s.
#
# So the redirect stands EXCEPT where doing it would swallow a trace somebody
# deliberately started:
#   - xtrace is on, and going to fd 2 (BASH_XTRACEFD unset or 2);
#   - or REMEMBER_TRACE=1, for a profiler that is not bash's own xtrace.
# A trace on its own fd (BASH_XTRACEFD=9) is untouched by the redirect, so
# that case keeps it -- the stderr leak this redirect exists to stop (#643)
# is not worth trading for a problem that is already solved.
#
# Third guard, below BOTH of the above (round-2 #690, found on GitHub's own
# macOS runners): BASH_XTRACEFD was added in bash 4.1. Stock macOS ships bash
# 3.2 as `/bin/bash` -- the same floor tests/test_session_start_fork_tax_665.py
# already names for $BASHPID -- and 3.2 silently ignores the variable, so
# xtrace stays on real fd 2 no matter what BASH_XTRACEFD says. Trusting the
# variable's TEXT on that floor reproduces the exact #690 symptom this file
# exists to fix: the guard reads "9", concludes the trace is safely
# elsewhere, and redirects fd 2 into hook-errors.log anyway -- while the
# trace, which never left fd 2, goes with it. Checked directly against
# BASH_VERSINFO, the same version test lib-clock.sh and lib-lock.sh already
# run before relying on a bash-4+ feature, rather than assumed.
_remember_bd_has_xtracefd=0
if [ "${BASH_VERSINFO[0]:-0}" -gt 4 ] 2>/dev/null; then
    _remember_bd_has_xtracefd=1
elif [ "${BASH_VERSINFO[0]:-0}" -eq 4 ] 2>/dev/null && [ "${BASH_VERSINFO[1]:-0}" -ge 1 ] 2>/dev/null; then
    _remember_bd_has_xtracefd=1
fi
if [ -d "$REMEMBER_DIR/logs" ]; then
    _remember_bd_keep_fd2=""
    case "$-" in
        (*x*)
            if [ "$_remember_bd_has_xtracefd" = "0" ]; then
                _remember_bd_keep_fd2="an xtrace is running (this bash predates BASH_XTRACEFD, added in 4.1, so xtrace stays on fd 2 regardless of the variable)"
            elif [ "${BASH_XTRACEFD:-2}" = "2" ]; then
                _remember_bd_keep_fd2="an xtrace is running on fd 2"
            fi
            ;;
    esac
    [ "${REMEMBER_TRACE:-}" = "1" ] && _remember_bd_keep_fd2="REMEMBER_TRACE=1"
    if [ -n "$_remember_bd_keep_fd2" ]; then
        # Said out loud, on the stream being kept: this hook's stderr is now in
        # the operator's terminal rather than in the log they would otherwise
        # go and read, and silence about that is its own small version of the
        # defect above.
        printf 'remember: %s, so stderr is NOT being redirected to %s -- point BASH_XTRACEFD at its own fd to get both (#690)\n' \
            "$_remember_bd_keep_fd2" "$REMEMBER_DIR/logs/hook-errors.log" >&2
    else
        exec 2>> "$REMEMBER_DIR/logs/hook-errors.log"
    fi
    unset _remember_bd_keep_fd2
fi
unset _remember_bd_has_xtracefd
