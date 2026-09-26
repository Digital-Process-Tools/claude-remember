#!/bin/bash
# ============================================================================
# lib-memory-dir.sh — Resolve REMEMBER_DIR and produce a merged REMEMBER_CONFIG
# ============================================================================
#
# DESCRIPTION
#   Single source of truth for two closely coupled concerns:
#
#   1. REMEMBER_DIR — where memory data files live.
#      Normally "${PROJECT_DIR}/.remember" (legacy default).
#      When config.json carries a data_dir starting with "/" or "~", the path
#      is expanded and the {slug} placeholder is replaced with the
#      session_dir_slug of PROJECT_DIR, matching Claude Code's own naming for
#      ~/.claude/projects/<slug>/.
#
#   2. REMEMBER_CONFIG — the merged config.json that every caller reads via
#      config(). Built by deep-merging three layers (highest priority wins):
#        1. ${PIPELINE_DIR}/config.json          (plugin-bundled defaults)
#        2. ${HOME}/.remember/config.json         (user-global, survives updates)
#        3. ${REMEMBER_DIR}/config.json           (per-project override)
#
# USAGE
#   source "$(dirname "$0")/resolve-paths.sh"   # sets PROJECT_DIR, PIPELINE_DIR
#   source "$(dirname "$0")/detect-tools.sh"    # sets session_dir_slug
#   source "$(dirname "$0")/lib-memory-dir.sh"  # exports REMEMBER_DIR, REMEMBER_CONFIG
#
# REQUIRES
#   PROJECT_DIR    — set by resolve-paths.sh
#   PIPELINE_DIR   — set by resolve-paths.sh
#   session_dir_slug — sourced from lib-slug.sh (no longer needs detect-tools.sh)
#
# EXPORTS
#   REMEMBER_DIR         — absolute path to memory data directory
#   REMEMBER_STORE_ROOT  — the directory the per-project stores sit in, and empty
#                          unless data_dir is absolute AND carries {slug} (#297)
#   REMEMBER_CONFIG      — absolute path to merged config (tmp file)
#
# ============================================================================

# Guard against double-sourcing. Use default-expansion so set -u callers don't error.
[ -n "${_LIB_MEMORY_DIR_LOADED:-}" ] && return 0
_LIB_MEMORY_DIR_LOADED=1

# session_dir_slug, from the one file that defines it. This used to be a naive
# inline fallback declared at the point of use — the pre-#144 implementation,
# carrying every bug #156 fixed, and live for user-prompt-hook.sh, which reaches
# here without sourcing detect-tools.sh (#158). Sourcing detect-tools.sh instead
# is not an option: it exits 1 when it finds no Python, taking its caller down.
_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
# A path with no slash in it (`source log.sh` from the scripts dir) leaves the
# filename behind, not a directory — `dirname` answered "." and this must too.
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="."
source "$_REMEMBER_SRC_DIR/lib-slug.sh"
unset _REMEMBER_SRC_DIR

# ── Helpers ──────────────────────────────────────────────────────────────────

# _read_data_dir <config-file>
# Prints the raw data_dir value from a single config file, empty if absent.
_read_data_dir() {
    local cfg="$1"
    [ -f "$cfg" ] || return 0
    if command -v jq >/dev/null 2>&1; then
        jq -r '.data_dir // empty' "$cfg" 2>/dev/null || true
    else
        # Minimal grep fallback — handles simple string values only.
        grep -o '"data_dir"[[:space:]]*:[[:space:]]*"[^"]*"' "$cfg" 2>/dev/null \
            | sed 's/.*"data_dir"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/'
    fi
}

# _resolve_memory_project_dir <project_dir>
# Returns the directory memory should be keyed to. Normally this is PROJECT_DIR
# itself. When PROJECT_DIR is a *linked git worktree*, Claude Code has set
# CLAUDE_PROJECT_DIR to the worktree path — but memory should live with the main
# checkout so it survives `git worktree remove` and is shared across worktrees
# of the same repo (issue #56). A linked worktree is detected via git's common
# dir differing from its git dir; the main checkout is the parent of the shared
# common dir.
#
# Fail-safe by design: it only redirects when it positively identifies a linked
# worktree whose main checkout is a real work tree. Ordinary checkouts, non-git
# directories, bare-repo worktrees, and old git without --path-format all fall
# through to PROJECT_DIR unchanged — identical to pre-fix behaviour. Only
# REMEMBER_DIR is affected; PROJECT_DIR stays the worktree path so session
# recovery still finds transcripts under the worktree slug.
_resolve_memory_project_dir() {
    local proj="$1"

    # A linked worktree's `.git` is a FILE (a `gitdir:` pointer); a main
    # checkout's is a DIRECTORY. A `.git` directory therefore settles "this is
    # not a linked worktree" with a shell builtin, and the ~200ms `git rev-parse`
    # below is not paid at all in the overwhelmingly common case (#230). That
    # matters because PostToolUse pays this on EVERY tool call.
    #
    # The POSITIVE answer only. The ABSENCE of `.git` proves nothing — a
    # subdirectory of a worktree has no `.git` entry and still needs the redirect
    # — so everything that is not a confirmed main checkout falls through to the
    # unchanged chain below. Written as an `if` rather than `[ … ] && { … }` so
    # the false branch cannot hand a non-zero status to a `set -e` caller.
    if [ -d "$proj/.git" ]; then
        echo "$proj"
        return 0
    fi

    command -v git >/dev/null 2>&1 || { echo "$proj"; return 0; }

    # One rev-parse yields both paths (common-dir first, git-dir second).
    # --path-format=absolute requires git >= 2.31; on older git this fails and
    # we fall through to the unchanged PROJECT_DIR.
    local _out _gcd _gd
    _out=$(git -C "$proj" rev-parse --path-format=absolute \
                --git-common-dir --git-dir 2>/dev/null) || _out=""
    { IFS= read -r _gcd; IFS= read -r _gd; } <<EOF
$_out
EOF

    # Not a git repo, unsupported flag, or an ordinary checkout (common == git):
    # leave PROJECT_DIR untouched.
    if [ -z "$_gcd" ] || [ -z "$_gd" ] || [ "$_gcd" = "$_gd" ]; then
        echo "$proj"
        return 0
    fi

    # Linked worktree: the main checkout is the parent of the shared git dir.
    # Guard against bare-repo worktrees (parent is not a work tree) by only
    # redirecting to a directory git confirms is inside a work tree.
    local _main
    _main=$(dirname "$_gcd")
    if [ -d "$_main" ] && \
       git -C "$_main" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "$_main"
    else
        echo "$proj"
    fi
}

# _resolve_remember_dir <data_dir_value> <project_dir>
# Resolves the final absolute REMEMBER_DIR.
# If data_dir starts with / or ~ treat as absolute; expand ~ and {slug}.
# Otherwise treat as a path relative to PROJECT_DIR (legacy behaviour).
_resolve_remember_dir() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local data_dir="$1"
    local proj="$2"

    case "$data_dir" in
        /*|~*|[A-Za-z]:/*|[A-Za-z]:\\*)
            # Absolute / home-relative: expand ~ and substitute {slug}.
            # Drive-letter forms (C:/... and C:\...) are absolute on Windows /
            # Git Bash — without them a Windows data_dir is wrongly treated as
            # relative and prepended to PROJECT_DIR (path doubling).
            local slug
            slug=$(session_dir_slug "$proj")
            # shellcheck disable=SC2016  # we want literal ~ expansion here
            local expanded="${data_dir/#\~/$HOME}"
            echo "${expanded//\{slug\}/$slug}"
            ;;
        *)
            # Relative (legacy): resolve against PROJECT_DIR.
            echo "${proj}/${data_dir}"
            ;;
    esac
}

# _set_store_root <data_dir_value>
# Sets REMEMBER_STORE_ROOT to the directory the per-project stores sit in: the
# data_dir template truncated at {slug}, with ~ expanded and trailing separators
# removed. Sets it EMPTY in every other case, and the emptiness is the interface
# (#297).
#
# A FUNCTION THAT ASSIGNS, not one that echoes, and it must never be called as
# `$(...)`. This file is sourced by bootstrap-dirs.sh and by log.sh, and
# post-tool-hook.sh reaches both on the tool call that resolves — the first of a
# session and the first after any config edit (#350 made the rest of them replay
# instead). session-start-hook.sh and save-session.sh reach it unconditionally.
# A command substitution here is a fork on every one of those, which is the cost
# #230 went to trouble removing and #296 refused to add back. Everything below
# is parameter expansion: no subshell, no external command, no measurable cost
# on that path.
#
# This exists because of a circularity #296 left open. The slug record lives at
# <REMEMBER_DIR>/tmp/session-slug, and in the layout config.user.example.json
# ships — "data_dir": "~/.remember/{slug}", under a _purpose that says to copy
# it — REMEMBER_DIR is itself named by the slug, so a caller had to know the
# answer to open the file holding it. The store root is the one path in that
# layout a caller CAN name: it is the part of the template before the
# placeholder, so the template alone yields it.
#
# It is deliberately empty when there is no {slug}. In the legacy layout
# (<project>/.remember) and in a single-directory external store, REMEMBER_DIR
# and the store root are the same directory and the record is already reachable
# from project_dir and the template — so there is nothing to publish, and the
# session-start index that reads this stays off. The common layout does not pay
# for the external one.
#
# A prefix of "/" or a bare drive is refused rather than accepted. It would put
# a plugin-owned file at /tmp/sessions, which on a shared world-writable
# directory is a hijack waiting to happen, and no one keeps a memory store at
# the filesystem root.
_set_store_root() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local data_dir="$1" prefix
    REMEMBER_STORE_ROOT=""

    # Same absolute/home-relative test as _resolve_remember_dir, including the
    # Windows drive-letter forms: a relative data_dir has no store root.
    case "$data_dir" in
        /*|~*|[A-Za-z]:/*|[A-Za-z]:\\*) ;;
        *) return 0 ;;
    esac
    case "$data_dir" in
        *'{slug}'*) ;;
        *) return 0 ;;
    esac

    prefix="${data_dir%%\{slug\}*}"
    # shellcheck disable=SC2016  # we want literal ~ expansion here
    prefix="${prefix/#\~/$HOME}"

    # Trailing separators, never down to nothing: the ?* guard keeps "/" whole
    # so the refusal below is what rejects it, rather than this loop emptying it.
    while :; do
        case "$prefix" in
            ?*/|?*\\) prefix="${prefix%?}" ;;
            *) break ;;
        esac
    done

    case "$prefix" in
        ''|/|[A-Za-z]:|[A-Za-z]:/|[A-Za-z]:\\) return 0 ;;
    esac

    REMEMBER_STORE_ROOT="$prefix"
}

# ── Pass 1: resolve REMEMBER_DIR ─────────────────────────────────────────────
# Read data_dir from the plugin-bundled config and the user-global config only
# (the per-project config lives inside REMEMBER_DIR, which we don't know yet).

_bundled_cfg="${PIPELINE_DIR}/config.json"
_user_cfg="${HOME}/.remember/config.json"

# Highest-priority source that has data_dir wins.
_data_dir_raw=""
for _cfg_candidate in "$_user_cfg" "$_bundled_cfg"; do
    _val=$(_read_data_dir "$_cfg_candidate")
    if [ -n "$_val" ]; then
        _data_dir_raw="$_val"
        break
    fi
done

# Default to legacy layout if nothing found.
_data_dir_raw="${_data_dir_raw:-.remember}"

# Key memory to the main checkout when PROJECT_DIR is a linked worktree, so it
# survives `git worktree remove` and is shared across worktrees (issue #56).
# For non-worktree / non-git projects this is exactly PROJECT_DIR.
MEMORY_PROJECT_DIR=$(_resolve_memory_project_dir "$PROJECT_DIR")
export MEMORY_PROJECT_DIR

REMEMBER_DIR=$(_resolve_remember_dir "$_data_dir_raw" "$MEMORY_PROJECT_DIR")
export REMEMBER_DIR

# Empty in every layout where REMEMBER_DIR is nameable without the slug (#297).
# Assigned, never `$(...)`: this runs on the per-tool-call path.
_set_store_root "$_data_dir_raw"
export REMEMBER_STORE_ROOT

# ── Pass 2: layered config merge ─────────────────────────────────────────────
# Now that REMEMBER_DIR is known, merge all three layers.

_project_cfg="${REMEMBER_DIR}/config.json"

# The per-project config layer is untrusted input when REMEMBER_DIR sits
# inside the project checkout (the default/legacy layout) -- a repository an
# operator clones can ship .remember/config.json, and its own `haiku.*` block
# would otherwise choose the credential the nested `claude -p` / `codex exec`
# summarizer authenticates with, and can flip whether the operator's own
# ANTHROPIC_API_KEY is stripped (#726). External storage mode (data_dir
# absolute or home-relative, e.g. ~/.remember/{slug}) resolves outside any
# checkout the project itself controls, so that layer's `haiku` block IS
# trusted there -- the same absolute/home-relative case switch
# _resolve_remember_dir already uses to tell the two layouts apart.
# A function, not a bare top-level `case`: `local LC_ALL=C` needs a function
# body to attach to, and this is the only way to force byte-wise bracket-range
# matching here without a subshell fork -- same convention as
# _resolve_remember_dir/_set_store_root above, which are exempted from
# tests/test_locale_ranges_695.py's scan for the same reason (#695). Assigns
# the caller's `_project_cfg_haiku_untrusted` directly, same as
# _set_store_root does for REMEMBER_STORE_ROOT.
_classify_project_cfg_haiku_trust() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    case "$_data_dir_raw" in
        /*|~*|[A-Za-z]:/*|[A-Za-z]:\\*) _project_cfg_haiku_untrusted=0 ;;
        *) _project_cfg_haiku_untrusted=1 ;;
    esac
}
_classify_project_cfg_haiku_trust

# _remember_config_tracked_status <dir> <name>
# Whether <dir>/<name> is tracked by <dir>'s own git index, walking up to
# any ENCLOSING work tree -- not just a same-directory .git (#754: a
# project started from a repo SUBDIRECTORY has no .git of its own, and the
# repository can still have committed the file two levels up). Prints
# exactly one of three words: `tracked`, `untracked`, `could-not-tell`.
#
# `could-not-tell` is not `untracked` (#760): a git spawn that fails for a
# reason OTHER than "no repository reachable from here" or "this path is
# not tracked" -- a corrupted index, a permissions error, a PATH shim that
# answers with an unrelated fatal error -- must never read the same as a
# confirmed absence. Every caller of this treats `could-not-tell` the SAME
# as `tracked` (fail CLOSED): the class of input this guards is more
# expensive to leak than to occasionally over-protect.
#
# Up to three spawns, and the common "no git at all" case pays only one:
# `rev-parse --is-inside-work-tree` answers "is there a work tree reachable
# from here" on its exit status alone (no output parsing, so no locale/
# translation risk from git's own error text) -- a plain non-git project
# fails it immediately and this returns `untracked` without ever running
# `ls-files`. Only once inside a real work tree does this run `rev-parse
# --show-toplevel` (#761's symlinked-.git guard) and then `ls-files
# --error-unmatch`, to ask about the file itself; ITS exit status (0 /
# 1 / anything else) is what separates the three states from there.
_remember_config_tracked_status() {
    local _dir="$1" _name="$2" _out _rc _toplevel

    if ! command -v git >/dev/null 2>&1; then
        # #766: "no git binary" used to fall straight through to
        # "untracked", trusting the config -- the opposite of the
        # documented fail-closed behaviour, and indistinguishable from the
        # genuine "there is no repository here" case. No git spawn is
        # possible either way, so this walks the filesystem by hand instead:
        # an enclosing .git found on disk means there COULD be a tracked
        # file here that this has no way to ask git about (could-not-tell,
        # fails closed same as `tracked`); no .git anywhere above means
        # there is nothing to check, same as the ordinary non-git case
        # (untracked).
        local _walk
        _walk=$(cd "$_dir" 2>/dev/null && pwd -P) || _walk="$_dir"
        while [ -n "$_walk" ]; do
            if [ -e "$_walk/.git" ]; then
                echo "could-not-tell"
                return 0
            fi
            [ "$_walk" = "/" ] && break
            _walk="${_walk%/*}"
            [ -z "$_walk" ] && _walk="/"
        done
        echo "untracked"
        return 0
    fi

    # LC_ALL=C/LANGUAGE=C: the ONE place this reads a git error message as
    # text rather than an exit code alone, so it forces git's own fatal
    # text to the untranslated form rather than trusting the ambient
    # locale. A plain non-git project fails here with EXACTLY "fatal: not
    # a git repository (or any of the parent directories): .git" (exit
    # 128, verified against this file's own platform) -- the legitimate,
    # common "nothing to check" case. Any OTHER fatal text at this step
    # (a corrupted repo, a permissions error, a PATH shim answering with
    # an unrelated failure) is NOT that case and must not read as it.
    _out=$( (unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
             LC_ALL=C LANGUAGE=C git -C "$_dir" rev-parse --is-inside-work-tree) 2>&1 )
    _rc=$?
    if [ "$_rc" -ne 0 ]; then
        case "$_out" in
            *"not a git repository"*) echo "untracked" ;;
            *) echo "could-not-tell" ;;
        esac
        return 0
    fi
    if [ "$_out" != "true" ]; then
        # A real repository, but $_dir itself is not inside its work tree
        # (a bare repo) -- nothing to check either.
        echo "untracked"
        return 0
    fi

    # #761: everything above resolved `.git` the ORDINARY way -- following
    # wherever it points, with no check on what it actually is. A `.git`
    # SYMLINK is not a shape either a normal checkout (`.git` a directory)
    # or a linked worktree (`.git` a plain FILE holding a `gitdir:`
    # pointer -- see _resolve_memory_project_dir above) ever produces, so
    # one found here is only ever a pre-planted swap pointing this whole
    # tracked-check at an UNRELATED repository's own, independent objects
    # and index. Answering "untracked" against that repository would be
    # answering the wrong question entirely -- fail CLOSED here exactly
    # like every other ambiguous case in this function (could-not-tell ==
    # tracked for every caller). A linked worktree's `.git` FILE is left
    # untouched: that shape is already trusted elsewhere in this codebase
    # (#56) and is not what this check is guarding against.
    _toplevel=$( (unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                  git -C "$_dir" rev-parse --show-toplevel) 2>/dev/null )
    if [ -n "$_toplevel" ] && [ -L "${_toplevel}/.git" ]; then
        echo "could-not-tell"
        return 0
    fi

    # #757/#754's `ls-files --error-unmatch` compared the exact-case path,
    # which misses a config the repository committed under a DIFFERENTLY
    # CASED name on a case-insensitive filesystem (macOS APFS default): a
    # repo shipping `.Remember/config.json` reads as untracked here and
    # migrates as trusted. `:(icase)` pathspec magic asks git the
    # case-insensitive question directly, still in one spawn -- non-empty
    # output means something matching that name case-insensitively is
    # tracked; empty output (exit 0) means nothing does.
    _out=$( (unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
             git -C "$_dir" ls-files -- ":(icase)$_name") 2>/dev/null )
    _rc=$?
    if [ "$_rc" -ne 0 ]; then
        echo "could-not-tell"
        return 0
    fi
    if [ -n "$_out" ]; then
        echo "tracked"
    else
        echo "untracked"
    fi
}

# #757: `model` and `reject_pattern` are not credential-equivalent to
# `haiku` -- they cannot redirect where transcripts go, only which model
# is billed or whether the refusal gate runs at all -- so unlike `haiku`
# (stripped from EVERY legacy-layout project layer, tracked or not: #726
# already decided that's worth the loss of a legitimate per-project
# override) they are stripped only when the file is GIT-TRACKED, i.e.
# committed by the repository rather than written by the operator running
# it. An untracked in-project config.json -- the ordinary, common case --
# keeps both, exactly as before #757. Computed only when there is
# something to check: legacy layout AND a project config file present.
_project_cfg_model_reject_untrusted=0
if [ "$_project_cfg_haiku_untrusted" = "1" ] && [ -f "$_project_cfg" ]; then
    case "$(_remember_config_tracked_status "${_project_cfg%/*}" "${_project_cfg##*/}")" in
        untracked) _project_cfg_model_reject_untrusted=0 ;;
        *) _project_cfg_model_reject_untrusted=1 ;;  # tracked or could-not-tell -> fail CLOSED
    esac
fi

# #757 hardening: a SYMLINKED project config.json is left exactly as it
# is -- never read through the link during the merge below. jq (and the
# no-jq python fallback's own open()) opens what a symlink points to, not
# the link itself, so treating a symlinked config.json as an ordinary file
# here would read whatever it targets -- anywhere on disk -- into the
# merged config that later feeds a subprocess. -L never dereferences; this
# check runs BEFORE anything below opens "$_project_cfg" for reading, and
# clearing the variable to a path that cannot exist removes it from every
# `[ -f "$_project_cfg" ]` gate downstream in one place rather than at each
# of the several merge call sites individually.
if [ -L "$_project_cfg" ]; then
    printf 'remember: %s is a symlink -- refusing to read it through the link; this project config layer is skipped entirely for this session (#757)\n' \
        "$_project_cfg" >&2
    _project_cfg="${REMEMBER_DIR}/.remember-symlinked-config-refused"
fi

SYS_TMPDIR="${TMPDIR:-/tmp}"
# mktemp, not a PID-suffixed literal path (#429). ${SYS_TMPDIR} is a SHARED,
# often world-writable directory, and a name built from `$$` is predictable
# from the outside the instant this process starts. The shell's `>`
# redirection, jq's `>`, and Python's `open(path, "w")` all follow a symlink
# when opening their target and truncate on open, before a byte is written —
# so a symlink pre-seeded at the predictable name does not just get
# truncated, it receives the actual write that follows: the merged config,
# which per the comment below can carry a live `haiku.oauth_token`, lands at
# whatever path the attacker's symlink pointed to. mktemp both creates the
# file atomically (closing the create/open race a separate `: >` leaves open)
# and names it unpredictably, and is already 0600 on every mktemp this repo
# relies on (GNU and BSD/macOS alike) — no umask needed, and matching
# save-session.sh's six existing uses and #427's fix to doctor.sh.
# No trailing content after the X's: BSD/macOS mktemp only substitutes a run
# of X's at the very END of the template (verified against this file's own
# platform), so "...XXXXXX.json" is left LITERAL there -- not randomized at
# all, defeating the fix while looking identical to the working GNU form.
_merged_cfg=$(mktemp "${SYS_TMPDIR}/remember-config-XXXXXX" 2>/dev/null) || _merged_cfg=""

# Build an array of files that actually exist.
_cfg_sources=()
[ -f "$_bundled_cfg"  ] && _cfg_sources+=("$_bundled_cfg")
[ -f "$_user_cfg"     ] && _cfg_sources+=("$_user_cfg")
[ -f "$_project_cfg"  ] && _cfg_sources+=("$_project_cfg")

# An empty $_merged_cfg means mktemp itself failed (an unwritable/unusable
# SYS_TMPDIR -- rare, but no longer impossible now that this is mktemp
# instead of a literal path #429 could always name). `jq ... > "$_merged_cfg"`
# and `cp ... "$_merged_cfg"` with an EMPTY path are a shell redirect/target
# error the caller never asked for and 2>/dev/null does not catch (that
# suppresses the invoked COMMAND's stderr, not the shell's own
# redirection-setup failure), so skip the merge attempts entirely rather than
# let that leak: REMEMBER_CONFIG ends up empty either way, which every
# consumer already treats the same as a genuinely-absent config file.
if [ -z "$_merged_cfg" ]; then
    :
elif [ "${#_cfg_sources[@]}" -gt 0 ] && command -v jq >/dev/null 2>&1; then
    # Deep-merge: later files override earlier ones. Strip `_`-prefixed keys —
    # convention: `_*` are user-facing docs (_comments/_purpose/_notes), never runtime data.
    # #744: the previous two designs (`.[-1] |= del(.haiku)` on a `-s`
    # slurp, then `--slurpfile`/`input_filename`) were REASONED to be safe
    # on Windows, never OBSERVED there -- PR #744's CI showed the
    # `--slurpfile` invocation itself erroring under a native jq.exe, and
    # the `|| cp "$_bundled_cfg" ...` fallback silently dropped EVERY
    # layer, project AND trusted user-global, with no error surfaced to
    # the user at all. This design uses NOTHING that was not already
    # proven, on every CI platform, before #740 ever touched this file:
    # the untrusted project layer is sanitized in a SEPARATE, plain call
    # (`jq -c 'del(.haiku)' "$_project_cfg"`, no `-s`, no `-n`, no
    # `--slurpfile`, no `/dev/null` placeholder, no path comparison of any
    # kind) into its own temp file, applying `del(.haiku)` to every
    # top-level JSON value the untrusted file contains -- jq's ordinary
    # (non-slurp) mode already treats each one as a separate input, so
    # this handles a multi-document project config the same way #740's
    # first fix did, and an empty file the same way its second fix did,
    # without either design's own platform-dependent moving part. Once
    # sanitized, the ORIGINAL `jq -s` reduce below (unchanged since
    # before #726) runs over the SAME plain file arguments it always did
    # -- the sanitized temp file standing in for the untrusted one -- so
    # the one thing #744 needed proof of (does this shape work on
    # Windows) is answered by history rather than reasoning.
    # #757: when the project layer is ALSO git-tracked
    # (_project_cfg_model_reject_untrusted), the same filter drops `model`
    # and `reject_pattern` too -- not destinations, but a repo-committed
    # project layer could otherwise pick the summarizer's model or turn
    # the refusal gate off/into a ReDoS candidate (`reject_pattern` is a
    # user-supplied regex run over model output). An UNTRACKED project
    # config keeps both, same as before #757 -- only `haiku` is stripped
    # from every legacy layer unconditionally. Still one jq call either
    # way: the filter argument varies, the spawn count does not.
    _strip_project_haiku="false"
    [ "$_project_cfg_haiku_untrusted" = "1" ] && [ -f "$_project_cfg" ] && _strip_project_haiku="true"
    _project_del_filter=".haiku"
    [ "$_project_cfg_model_reject_untrusted" = "1" ] && _project_del_filter="${_project_del_filter}, .model, .reject_pattern"
    _jq_merge_sources=()
    [ -f "$_bundled_cfg" ] && _jq_merge_sources+=("$_bundled_cfg")
    [ -f "$_user_cfg"    ] && _jq_merge_sources+=("$_user_cfg")
    _project_sanitized_tmp=""
    if [ -f "$_project_cfg" ]; then
        if [ "$_strip_project_haiku" = "true" ]; then
            _project_sanitized_tmp=$(mktemp "${SYS_TMPDIR}/remember-config-sanitized-XXXXXX" 2>/dev/null) || _project_sanitized_tmp=""
            if [ -n "$_project_sanitized_tmp" ] && jq -c "del($_project_del_filter)" "$_project_cfg" > "$_project_sanitized_tmp" 2>/dev/null; then
                _jq_merge_sources+=("$_project_sanitized_tmp")
            else
                # Sanitizing failed (mktemp, an unreadable project file, or
                # jq itself) -- fail CLOSED: the project layer is dropped
                # entirely rather than merged unsanitized. The trusted
                # bundled/user layers above are unaffected; only the
                # untrusted one's own contribution is lost.
                #
                # #748: that drop used to happen with nothing logged
                # anywhere -- a project config's own `handoff_mode` (or any
                # other setting) disappeared with no explanation. This file
                # is sourced by log.sh BEFORE log.sh defines `log`/
                # `report_error` (log.sh sources this file at line 49, long
                # before its own `log()`/`report_error()` at lines
                # 941/1360), so neither can be assumed to exist here --
                # `declare -F` (not `command -v`; see lib-lock.sh's own
                # comment on why) asks whether THIS shell already has the
                # function, falling back to a plain stderr line when it
                # does not.
                if declare -F report_error >/dev/null 2>&1; then
                    report_error "lib-memory-dir" "sanitizing the project config layer failed (mktemp, an unreadable project file, or jq itself) -- that layer was dropped; bundled/user-global config still applies"
                else
                    printf '%s\n' "[lib-memory-dir] WARNING: sanitizing the project config layer failed (mktemp, an unreadable project file, or jq itself) -- that layer was dropped; bundled/user-global config still applies" >&2
                fi
                [ -n "$_project_sanitized_tmp" ] && rm -f "$_project_sanitized_tmp"
                _project_sanitized_tmp=""
            fi
        else
            _jq_merge_sources+=("$_project_cfg")
        fi
    fi
    # The filter is one line, not one per clause: a literal newline inside
    # this quoted argument reaches the process's own argv byte-for-byte, and
    # every spawn-counting test in this repo (tests/spawn_counting.py's
    # `spawns()`) records a process as `printf "%s %s\n" "$name" "$*"` then
    # SPLITS the log on newlines -- so a multi-line filter here does not cost
    # one more process, it costs the SAME process several extra phantom
    # lines in every budget this file's merge appears in (#discovered
    # investigating a macOS-only spawn-budget CI failure on this same #726
    # change: 4 phantom lines, one real process, jq itself is whitespace-
    # insensitive so this is a pure counting fix with no behavior change).
    if [ "${#_jq_merge_sources[@]}" -eq 0 ]; then
        # Bundled and user configs both absent, and the untrusted project
        # layer either doesn't exist either or was just sanitize-dropped
        # above -- `jq -s '...'` with ZERO positional file arguments falls
        # back to reading STDIN, and a hook blocked on STDIN never returns.
        # Same "no config files at all" answer the final `else` below gives
        # when jq isn't even on PATH.
        echo '{}' > "$_merged_cfg"
    else
        jq -s 'reduce .[] as $x ({}; . * $x) | with_entries(select(.key | startswith("_") | not))' "${_jq_merge_sources[@]}" > "$_merged_cfg" 2>/dev/null \
            || cp "$_bundled_cfg" "$_merged_cfg" 2>/dev/null
    fi
    [ -n "$_project_sanitized_tmp" ] && rm -f "$_project_sanitized_tmp"
elif [ "${#_cfg_sources[@]}" -gt 0 ]; then
    # No jq — do the same deep-merge in Python instead of silently dropping
    # the user-global/per-project layers and copying only the bundled
    # defaults. Every override in ~/.remember/config.json or
    # ${REMEMBER_DIR}/config.json (time_format, model, cooldowns.*,
    # thresholds.*, git_backup.*) was previously invisible on any machine
    # without jq — this made config() (log.sh) irrelevant to those users.
    # Resolves PYTHON on first use (#662) when detect-tools.sh was sourced in
    # lazy mode; a no-op everywhere else (PYTHON already set, or the
    # resolver was never defined because this ran without detect-tools.sh at
    # all -- both tolerated by the ${PYTHON:-python3} fallback below).
    declare -f _remember_python >/dev/null 2>&1 && _remember_python
    _untrusted_haiku_source=""
    [ "$_project_cfg_haiku_untrusted" = "1" ] && _untrusted_haiku_source="$_project_cfg"
    # #757: whether the SAME untrusted source is ALSO git-tracked, so
    # model/reject_pattern get dropped alongside haiku for it -- see the
    # jq branch's own comment above for why this is conditional where
    # haiku's own strip is not.
    _strip_model_reject="0"
    [ "$_project_cfg_model_reject_untrusted" = "1" ] && _strip_model_reject="1"
    # #748: the Python fallback's own fail-CLOSED drop of the untrusted
    # layer (the `except (OSError, ValueError): continue` below) used to
    # happen with nothing logged anywhere either. There is no fd left to
    # signal it on -- the interpreter's stdout/stderr are already thrown
    # away (`> /dev/null 2>&1`) so a real subprocess exit or a printed
    # marker cannot be told apart from success -- so the except branch
    # drops a one-byte marker file instead, and this shell checks for it
    # once the interpreter has exited.
    _project_drop_marker=$(mktemp "${SYS_TMPDIR}/remember-config-drop-marker-XXXXXX" 2>/dev/null) || _project_drop_marker=""
    rm -f "$_project_drop_marker" 2>/dev/null
    "${PYTHON:-python3}" - "$_merged_cfg" "$_untrusted_haiku_source" "$_strip_model_reject" "$_project_drop_marker" "${_cfg_sources[@]}" > /dev/null 2>&1 <<'PYMERGE' || cp "$_bundled_cfg" "$_merged_cfg" 2>/dev/null
import json
import sys


def deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = deep_merge(out[k], v) if k in out else v
        return out
    return b


out_path = sys.argv[1]
# Empty string when the project layer's `haiku` block is trusted (external
# storage mode, or no project cfg at all) -- never equal to a real path then,
# so nothing is stripped (#726, see the case switch this mirrors above).
untrusted_haiku_path = sys.argv[2]
# #757: "1" only when the SAME untrusted source is also git-tracked --
# model/reject_pattern are stripped alongside haiku only then, never for
# an untracked (the operator's own) in-project config.
strip_model_reject = sys.argv[3] == "1"
# #748: empty when mktemp itself failed above -- tolerated the same way
# every other mktemp-failure path in this file is (fall through, don't
# crash the merge over the logging side-channel itself).
drop_marker_path = sys.argv[4]


def load_documents(path):
    """Parse every whitespace-concatenated JSON document in `path` (#740):
    the untrusted project layer may ship more than one, and a plain
    json.load() raises `JSONDecodeError` on any file with more than one --
    which used to take the WHOLE merge down with it (the `|| cp
    "$_bundled_cfg" ...` fallback below), dropping the trusted user-global
    layer too rather than just stripping `haiku` from this file's own
    documents and keeping everything else."""
    with open(path) as f:
        raw = f.read()
    decoder = json.JSONDecoder()
    idx, n, docs = 0, len(raw), []
    while idx < n:
        while idx < n and raw[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, idx = decoder.raw_decode(raw, idx)
        docs.append(obj)
    return docs


merged = {}
for path in sys.argv[5:]:
    if untrusted_haiku_path and path == untrusted_haiku_path:
        # #744: fail CLOSED -- if the untrusted file can't even be loaded
        # (unreadable, a permissions error, malformed JSON, anything
        # load_documents() itself doesn't already tolerate), drop just this
        # layer rather than let the exception propagate and crash the whole
        # merge down to the bundled-only fallback below, taking the trusted
        # user-global layer's own overrides with it for no reason connected
        # to them. `json.JSONDecodeError` (raised by decoder.raw_decode() on
        # invalid JSON) and `UnicodeDecodeError` (raised by f.read() on a
        # file that isn't valid text in the expected encoding) are both
        # ValueError subclasses -- catching only OSError let either one
        # through uncaught.
        try:
            docs = load_documents(path)
        except (OSError, ValueError):
            # #748: drop a marker for the shell to notice and report --
            # this process's own stdout/stderr are discarded by the caller.
            if drop_marker_path:
                try:
                    with open(drop_marker_path, "w") as _marker:
                        _marker.write("1")
                except OSError:
                    pass
            continue
        for data in docs:
            if isinstance(data, dict):
                drop = {"haiku"}
                if strip_model_reject:
                    drop |= {"model", "reject_pattern"}
                data = {k: v for k, v in data.items() if k not in drop}
            merged = deep_merge(merged, data)
        continue
    with open(path) as f:
        data = json.load(f)
    merged = deep_merge(merged, data)
# Strip `_`-prefixed doc keys, top-level only — same convention as the jq path.
merged = {k: v for k, v in merged.items() if not str(k).startswith("_")}
with open(out_path, "w") as f:
    json.dump(merged, f)
PYMERGE
    if [ -n "$_project_drop_marker" ] && [ -f "$_project_drop_marker" ]; then
        rm -f "$_project_drop_marker" 2>/dev/null
        if declare -F report_error >/dev/null 2>&1; then
            report_error "lib-memory-dir" "sanitizing the project config layer failed (unreadable project file or malformed JSON) -- that layer was dropped; bundled/user-global config still applies"
        else
            printf '%s\n' "[lib-memory-dir] WARNING: sanitizing the project config layer failed (unreadable project file or malformed JSON) -- that layer was dropped; bundled/user-global config still applies" >&2
        fi
    fi
    [ -n "$_project_drop_marker" ] && rm -f "$_project_drop_marker" 2>/dev/null
else
    # No config files at all — fall back to the bundled defaults.
    cp "$_bundled_cfg" "$_merged_cfg" 2>/dev/null || echo '{}' > "$_merged_cfg"
fi

REMEMBER_CONFIG="$_merged_cfg"
export REMEMBER_CONFIG

# Register cleanup of the tmp file when the outermost script exits.
# Use a subshell-safe append to avoid overwriting any existing trap.
# #679 (part of #660): `trap -p EXIT | sed "s/trap -- '//;s/' EXIT//"` forked
# a `sed` to strip four characters at a known, fixed offset -- `trap -p`
# itself is a builtin (no fork either way), so parameter expansion removes
# the ONLY fork this line ever paid, with no behaviour change: stripping a
# prefix/suffix a string does not have leaves it unchanged, matching sed on
# empty input the same way (no existing trap -> both leave _existing_trap
# empty). Sanctioned in tests/test_case_divergence_298.py's
# _SANCTIONED_DIVERGENCE, same mechanism #429/#662/#665 already used for
# this exact file.
_t=$(trap -p EXIT 2>/dev/null)
_existing_trap="${_t#trap -- \'}"
_existing_trap="${_existing_trap%\' EXIT}"
if [ -n "$_existing_trap" ]; then
    # shellcheck disable=SC2064
    trap "${_existing_trap}; rm -f '${_merged_cfg}'" EXIT
else
    # shellcheck disable=SC2064
    trap "rm -f '${_merged_cfg}'" EXIT
fi
unset _existing_trap _t

# Clean up local variables to avoid polluting the caller's namespace.
unset _bundled_cfg _user_cfg _project_cfg _cfg_sources _data_dir_raw _val _merged_cfg _cfg_candidate
