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
if [ "$REMEMBER_DIR" != "$_legacy_dir" ] && [ -d "$_legacy_dir" ] && [ ! -e "$REMEMBER_DIR" ]; then
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
        if mv "$_legacy_dir" "$REMEMBER_DIR" 2>/dev/null; then
            mkdir -p "$_legacy_dir"
            printf 'Memory data migrated to:\n  %s\nThis directory is now empty; you may delete it.\n' \
                "$REMEMBER_DIR" > "$_legacy_dir/MIGRATED-TO.txt"
        fi
    fi
    unset _migrating_user_config_home
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
# lib-memory-dir.sh (sourced above) writes REMEMBER_CONFIG to
# $SYS_TMPDIR/remember-config-$$.json and relies solely on its own EXIT trap
# to remove it. On Windows/Git Bash that trap does not reliably fire for this
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
    find "$REMEMBER_DIR/tmp" -maxdepth 1 -name 'remember-config-*.json' \
        -mmin +30 -exec rm -f {} + 2>/dev/null || true

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
        _remember_existing_trap=$(trap -p EXIT 2>/dev/null | sed "s/trap -- '//;s/' EXIT//;s/'\\\\''/'/g")
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
    case "$REMEMBER_DIR" in
        "$_mem_proj"/*)
            [ -f "$REMEMBER_DIR/.gitignore" ] || { echo '*' > "$REMEMBER_DIR/.gitignore"; } 2>/dev/null
            ;;
    esac
fi
unset _mem_proj

# --- Redirect stderr to hook-errors.log ---
# This replaces the 2>> that was in hooks.json. Now the directory is
# guaranteed to exist before we open the file.
# Guard: only redirect if the logs dir was actually created (read-only
# filesystems, Docker read-only mounts, etc. will skip this gracefully).
if [ -d "$REMEMBER_DIR/logs" ]; then
    exec 2>> "$REMEMBER_DIR/logs/hook-errors.log"
fi
