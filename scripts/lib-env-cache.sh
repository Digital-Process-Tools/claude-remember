#!/bin/bash
# ============================================================================
# lib-env-cache.sh — replay a completed path resolution instead of redoing it
# ============================================================================
#
# DESCRIPTION
#   `user-prompt-hook.sh` needs exactly two facts to print its line: where the
#   memory store is (REMEMBER_DIR, for the #200 capture-gap notice) and which
#   timezone to print (REMEMBER_TZ, from config.json). Deriving those runs
#   resolve-paths.sh → bootstrap-dirs.sh → log.sh: `git rev-parse`, a slug, a
#   three-layer config merge, ~19 processes measured on macOS and 27 on the
#   Windows/QEMU box in #227, where it cost a p50 of 8.7s on every prompt.
#
#   Neither fact can change while the config files that produce them do not. So
#   the hook that already paid for the answer writes it down, and the next one
#   reads it — no second resolver, and that distinction is the whole design.
#   #158 is the scar: a hook that re-derived REMEMBER_DIR its own way resolved a
#   DIFFERENT store than every other, which is split-brain memory rather than a
#   clean failure. This file never computes anything. It replays what
#   lib-memory-dir.sh and log.sh produced, or it declines and the caller runs
#   the real chain.
#
#   Every check below is a bash builtin — `[ -f ]`, `[ -O ]`, `[ -nt ]`,
#   parameter expansion, `read`. A validation step that forked would spend the
#   savings it exists to protect. The one exception (#504) is
#   `_remember_env_cache_normalize_into`'s Windows drive-letter uppercasing,
#   which forks `tr` -- but only on `OSTYPE=msys|cygwin`, and only once a
#   drive-letter pattern has actually matched, the same carve-out
#   resolve-paths.sh's own copy of this logic already makes. Everywhere else,
#   including that same function's own substitution site, `printf -v` is used
#   specifically so the key computation itself never forks a subshell.
#
# USAGE
#   source "$_HOOK_DIR/lib-env-cache.sh"
#   if _remember_env_cache_load; then
#       : # REMEMBER_DIR / REMEMBER_TZ / PROJECT_DIR / PIPELINE_DIR are set
#   else
#       source resolve-paths.sh; source bootstrap-dirs.sh; source log.sh
#       _remember_env_cache_publish
#   fi
#
# INVALIDATION
#   The cache is refused unless it is newer than all three config layers, and
#   unless CLAUDE_PROJECT_DIR / CLAUDE_PLUGIN_ROOT / HOME are the same values it
#   was written for. Editing config.json therefore takes effect on the next
#   prompt, exactly as it does today when every hook re-reads it. `-nt` against
#   a file that does not exist is true, which is the right answer: a config
#   layer that is still absent cannot have changed. Creating one makes it newer
#   than the cache, so the cache loses.
#
#   Not covered: a project that becomes (or stops being) a linked git worktree
#   while a cache is live, which would move REMEMBER_DIR. SessionStart
#   republishes unconditionally, so the window is one session.
#
# ENVIRONMENT
#   REMEMBER_ENV_CACHE=0   Disable entirely — every hook resolves from scratch.
#
# SECURITY
#   The file lives in the system temp dir, which is world-writable on Linux, and
#   it names a directory this plugin then writes memory into. It is therefore
#   read only when it is a regular file, not a symlink, and owned by the current
#   user; every line must match one known `KEY=value`, and one that does not
#   rejects the whole file rather than the line. Written 0600 via a private temp
#   file and renamed, so a reader never sees a half-written resolution.
#
# ============================================================================

[ -n "${_REMEMBER_LIB_ENV_CACHE_LOADED:-}" ] && return 0
_REMEMBER_LIB_ENV_CACHE_LOADED=1

# Sets _REMEMBER_ENV_CACHE_FILE and _REMEMBER_ENV_CACHE_KEY. Returns 1 when
# there is no project to key on.
#
# Keyed on the RAW CLAUDE_PROJECT_DIR when it is set, falling back to
# REMEMBER_HOOK_CWD (#469): Codex and Gemini CLI set neither CLAUDE_PROJECT_DIR
# nor any variable this cache could key on before resolve-paths.sh runs, and
# that absence is the entire premise of #407/#411/#444 -- the same hosts
# #411/#444 gave every hook a REMEMBER_HOOK_CWD fallback for. Without this,
# resolve-paths.sh:270 still exports the RESOLVED CLAUDE_PROJECT_DIR before
# _remember_env_cache_publish runs, so a cache file is written every time and
# never found by _remember_env_cache_load, which runs in a fresh process
# before that export exists. The per-process pin inside the function below is
# what keeps the key stable across a call that runs before resolve-paths.sh's
# normalisation and one that runs after, WITHIN one process -- but #504 found
# that pin is not enough on its own: user-prompt-hook.sh's fast path calls
# this BEFORE resolve-paths.sh has run, in a FRESH process, so it pins the RAW
# CLAUDE_PROJECT_DIR the host set (unnormalised on Windows/Git-Bash --
# forward-slash or POSIX drive form); session-start-hook.sh's publish call
# runs AFTER resolve-paths.sh, in ITS OWN process, and pins the NORMALISED
# form resolve-paths.sh re-exported. Two different processes, two different
# strings for the same project, two different cache files -- the fast path
# can never hit what the slow path wrote. _remember_env_cache_normalize_into
# below closes that: same normalisation resolve-paths.sh's own
# _remember_normalize_win_path applies, scoped to the same `case "$OSTYPE" in
# msys|cygwin)` guard, so its BODY is a genuine no-op (no `tr` fork, no
# regex work) on every platform except the one where raw and resolved can
# actually disagree. Written into a named variable via `printf -v`, not
# printed for a caller to capture with `$( )` -- self-review on this same
# change (#511/#504, reviewed together) caught that `$(...)` forks a
# subshell for the SUBSTITUTION ITSELF regardless of what the function body
# does, which would have paid exactly the per-fork cost #511 exists to
# remove, on the fast path, on every single invocation, unconditionally on
# every platform -- not scoped to msys/cygwin the way the function body is.
# `_remember_normalize_win_path` in resolve-paths.sh keeps the older
# print-and-capture shape because that file's own callers are cold-path
# (once per hook invocation, after resolve-paths.sh has already forked far
# more than this); lib-env-cache.sh's copy exists specifically because this
# one runs on the hot path, so it gets the `_into` treatment from the start.
_remember_env_cache_normalize_into() {
    local _var="$1" _in="$2" _drive="" _rest=""
    local _re='^([a-zA-Z]):[/\](.*)$'
    case "$OSTYPE" in
        msys|cygwin)
            if [[ "$_in" =~ ^/cygdrive/([a-zA-Z])/(.*)$ ]]; then
                _drive="${BASH_REMATCH[1]}"
                _rest="${BASH_REMATCH[2]}"
            elif [[ "$_in" =~ ^/([a-zA-Z])/(.*)$ ]]; then
                _drive="${BASH_REMATCH[1]}"
                _rest="${BASH_REMATCH[2]}"
            elif [[ "$_in" =~ $_re ]]; then
                _drive="${BASH_REMATCH[1]}"
                _rest="${BASH_REMATCH[2]}"
            fi
            if [ -n "$_drive" ]; then
                _drive=$(printf '%s' "$_drive" | tr '[:lower:]' '[:upper:]')
                _rest="${_rest//\//\\}"
                printf -v "$_var" '%s:\\%s' "$_drive" "$_rest"
                return 0
            fi
            ;;
    esac
    printf -v "$_var" '%s' "$_in"
}

_remember_env_cache_path() {
    # Pinned once per process (#469): this function runs both BEFORE
    # resolve-paths.sh (from _remember_env_cache_load, when CLAUDE_PROJECT_DIR
    # is still unset on Codex/Gemini and REMEMBER_HOOK_CWD is the only
    # signal) and AFTER it (from _remember_env_cache_publish, by which point
    # resolve-paths.sh has exported the RESOLVED -- and on Windows/Git-Bash,
    # NORMALIZED -- CLAUDE_PROJECT_DIR). Recomputing on the second call would
    # let the now-set CLAUDE_PROJECT_DIR win over the raw REMEMBER_HOOK_CWD
    # this same invocation's earlier (failed) load call already keyed on --
    # on a platform where normalization actually changes the string (drive-
    # letter rewriting; a no-op on macOS/Linux, where raw and resolved always
    # agree), write and read would target different files, permanently,
    # exactly the #469 symptom relocated to Windows. resolve-paths.sh only
    # resolves once per hook invocation, so once a key is known in THIS
    # process it is reused rather than asked of the environment again.
    if [ -n "${_REMEMBER_ENV_CACHE_KEY:-}" ]; then
        return 0
    fi
    local _key="${CLAUDE_PROJECT_DIR:-${REMEMBER_HOOK_CWD:-}}"
    [ -n "$_key" ] || return 1
    # #504: normalise BEFORE pinning, so a raw (pre-resolve-paths.sh) and an
    # already-normalised (post-resolve-paths.sh) spelling of the same project
    # collapse to the same key. Idempotent on an already-normalised string --
    # the drive-letter regex matches a backslash-separated input just as
    # readily as a forward-slash one, and re-uppercasing an already-uppercase
    # drive letter is a no-op. `_into` (writes `_key` directly via
    # `printf -v`), not `_key=$(_remember_env_cache_normalize "$_key")` --
    # see the comment above the function for why that distinction matters
    # here specifically.
    _remember_env_cache_normalize_into _key "$_key"
    _REMEMBER_ENV_CACHE_KEY="$_key"
    _key="${_key//[!a-zA-Z0-9]/-}"
    # Filename length limits are real (255 bytes on most filesystems) and a deep
    # project path exceeds them. Keep the TAIL: the end of a path is what
    # distinguishes it from its siblings, and a collision only ever costs a
    # rejected cache, because the full path is stored inside and compared.
    [ "${#_key}" -gt 120 ] && _key="${_key: -120}"
    _REMEMBER_ENV_CACHE_FILE="${TMPDIR:-/tmp}/remember-env-${_key}"
    return 0
}

# _remember_env_cache_load
# On success: REMEMBER_DIR, REMEMBER_TZ, REMEMBER_PROMPT_STAMP,
# REMEMBER_SAVE_COOLDOWN, REMEMBER_DELTA_THRESHOLD, MEMORY_PROJECT_DIR,
# PROJECT_DIR and PIPELINE_DIR are set and exported, and the caller can skip
# the chain.
# On failure: nothing is set and the caller must resolve normally.
_remember_env_cache_load() {
    [ "${REMEMBER_ENV_CACHE:-1}" = "1" ] || return 1
    _remember_env_cache_path || return 1
    local _f="$_REMEMBER_ENV_CACHE_FILE"
    [ -f "$_f" ] || return 1
    [ -L "$_f" ] && return 1
    [ -O "$_f" ] || return 1
    [ -r "$_f" ] || return 1

    local _line _dir="" _tz="" _mem="" _proj="" _pipe="" _stamp=""
    local _cooldown="" _delta=""
    local _env_proj="" _env_pipe="" _env_home=""
    local _cfgs=()
    while IFS= read -r _line || [ -n "$_line" ]; do
        # Python on Windows writes \r\n and a stray CR would end up inside a
        # path — the #84 class of bug, and invisible in every error message.
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        case "$_line" in
            REMEMBER_DIR=*)          _dir="${_line#*=}" ;;
            REMEMBER_TZ=*)           _tz="${_line#*=}" ;;
            REMEMBER_PROMPT_STAMP=*) _stamp="${_line#*=}" ;;
            REMEMBER_SAVE_COOLDOWN=*) _cooldown="${_line#*=}" ;;
            REMEMBER_DELTA_THRESHOLD=*) _delta="${_line#*=}" ;;
            MEMORY_PROJECT_DIR=*)    _mem="${_line#*=}" ;;
            PROJECT_DIR=*)           _proj="${_line#*=}" ;;
            PIPELINE_DIR=*)          _pipe="${_line#*=}" ;;
            CACHE_ENV_PROJECT_DIR=*) _env_proj="${_line#*=}" ;;
            CACHE_ENV_PLUGIN_ROOT=*) _env_pipe="${_line#*=}" ;;
            CACHE_ENV_HOME=*)        _env_home="${_line#*=}" ;;
            CACHE_CONFIG=*)          _cfgs[${#_cfgs[@]}]="${_line#*=}" ;;
            # An unknown line means this is not our file, or not our version of
            # it. Distrust the whole thing; the cost of being wrong is one slow
            # prompt, and the cost of guessing is memory in the wrong place.
            *) return 1 ;;
        esac
    done < "$_f"

    [ -n "$_dir" ] && [ -n "$_proj" ] && [ -n "$_pipe" ] || return 1
    # Required, not defaulted, and that is the point (#301). The cache is
    # invalidated by config mtime, never by plugin version — so a file written
    # by the release before this key existed carries no answer for it, and
    # reading that absence as "the default" would serve a resolution that never
    # considered the option. Writers always write a value, so an empty one means
    # an older writer. Costs one slow prompt, once, at upgrade.
    [ -n "$_stamp" ] || return 1
    # Same rule, same reason (#350). post-tool-hook.sh feeds both of these
    # straight into `$(( ))` and `[ -lt ]`, so a value that is not digits is
    # not a slow answer, it is a broken one -- and log.sh, the only writer,
    # already refuses to publish anything else. A cache from a release before
    # these keys existed carries no answer for them and loses to the chain,
    # once, at upgrade.
    case "$_cooldown" in '' | *[!0-9]*) return 1 ;; esac
    case "$_delta" in '' | *[!0-9]*) return 1 ;; esac
    # Compared against the SAME identity _remember_env_cache_path just keyed
    # on (CLAUDE_PROJECT_DIR, falling back to REMEMBER_HOOK_CWD, #469) rather
    # than raw CLAUDE_PROJECT_DIR directly -- on Codex/Gemini CLAUDE_PROJECT_DIR
    # is unset in the fresh process reading this cache, so comparing against
    # it directly would reject every cache this fallback lets the path
    # function find, defeating the fix at this one remaining line.
    [ "$_env_proj" = "${_REMEMBER_ENV_CACHE_KEY:-}" ] || return 1
    [ "$_env_pipe" = "${CLAUDE_PLUGIN_ROOT:-}" ] || return 1
    [ "$_env_home" = "${HOME:-}" ] || return 1
    [ -d "$_pipe" ] || return 1

    local _cfg
    for _cfg in ${_cfgs[@]+"${_cfgs[@]}"}; do
        [ -n "$_cfg" ] || continue
        # Newer config than cache — including a layer created since — wins.
        [ "$_f" -nt "$_cfg" ] || return 1
    done

    PROJECT_DIR="$_proj"
    PIPELINE_DIR="$_pipe"
    REMEMBER_DIR="$_dir"
    REMEMBER_TZ="$_tz"
    REMEMBER_PROMPT_STAMP="$_stamp"
    REMEMBER_SAVE_COOLDOWN="$_cooldown"
    REMEMBER_DELTA_THRESHOLD="$_delta"
    MEMORY_PROJECT_DIR="${_mem:-$_proj}"
    export PROJECT_DIR PIPELINE_DIR REMEMBER_DIR REMEMBER_TZ MEMORY_PROJECT_DIR
    export REMEMBER_PROMPT_STAMP REMEMBER_SAVE_COOLDOWN REMEMBER_DELTA_THRESHOLD
    return 0
}

# _remember_env_cache_publish
# Record a resolution that just completed. Never fails the caller: a hook that
# could not write a cache file has still done its actual job.
_remember_env_cache_publish() {
    [ "${REMEMBER_ENV_CACHE:-1}" = "1" ] || return 0
    # Same fallback as the key itself (#469): by the time any real caller
    # reaches here, resolve-paths.sh has already exported the resolved
    # CLAUDE_PROJECT_DIR (resolve-paths.sh:270), so this is normally
    # redundant with that export -- but requiring it directly, rather than
    # accepting REMEMBER_HOOK_CWD alone, would silently refuse to publish a
    # cache whose only identity source is REMEMBER_HOOK_CWD, defeating the
    # fix on any future caller that publishes before that export runs.
    [ -n "${CLAUDE_PROJECT_DIR:-}${REMEMBER_HOOK_CWD:-}" ] || return 0
    [ -n "${REMEMBER_DIR:-}" ] || return 0
    [ -n "${PROJECT_DIR:-}" ] || return 0
    [ -n "${PIPELINE_DIR:-}" ] || return 0
    # log.sh returns early — before it ever sets these two — on a store whose
    # logs/ dir it cannot create (#358). Two of this function's three callers
    # (user-prompt-hook.sh, post-tool-hook.sh's slow path) source log.sh with
    # stderr suppressed and call this unconditionally, so an early return left
    # both variables unset here, not merely absent from config. Defaulting them
    # below in that case would publish 120/50 as though config had said so, and
    # the load side cannot tell that apart from a real answer — both are
    # digits. Required here, not defaulted, same rule _remember_env_cache_load
    # already applies to the same two keys and for the same reason (#301):
    # skip the whole publish rather than write a cache the load side would
    # accept but is really a stand-in for a chain that never ran. A cache one
    # write cycle stale is the ordinary case anywhere in this file; a cache
    # that lies about config is the thing #358 exists to refuse.
    [ -n "${REMEMBER_SAVE_COOLDOWN:-}" ] || return 0
    [ -n "${REMEMBER_DELTA_THRESHOLD:-}" ] || return 0
    _remember_env_cache_path || return 0

    local _f="$_REMEMBER_ENV_CACHE_FILE" _t
    # mktemp, not a PID-suffixed literal path (#429): $_f itself is built from
    # CLAUDE_PROJECT_DIR (predictable to anyone who knows the project path),
    # and appending "$$" to it is no better -- both name a path in a SHARED
    # tmp dir before this process has created anything there. The shell's `>`
    # follows a symlink when opening its target and truncates on open, before
    # a byte is written, so a symlink pre-seeded at that name would receive
    # whatever this function goes on to write. mktemp creates the file
    # atomically at an unpredictable name and already 0600 on every mktemp
    # this repo relies on (GNU and BSD/macOS alike) -- no umask needed, and no
    # trailing suffix after the X's (BSD mktemp only substitutes a run of X's
    # at the very end of the template; anything after is left literal).
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    {
        # The same identity the file is keyed and validated on (#469), not
        # raw CLAUDE_PROJECT_DIR: on a caller whose only identity source was
        # REMEMBER_HOOK_CWD, printing CLAUDE_PROJECT_DIR here would record a
        # value _remember_env_cache_load's later comparison can never match.
        printf 'CACHE_ENV_PROJECT_DIR=%s\n' "$_REMEMBER_ENV_CACHE_KEY"
        printf 'CACHE_ENV_PLUGIN_ROOT=%s\n' "${CLAUDE_PLUGIN_ROOT:-}"
        printf 'CACHE_ENV_HOME=%s\n' "${HOME:-}"
        printf 'PROJECT_DIR=%s\n' "$PROJECT_DIR"
        printf 'PIPELINE_DIR=%s\n' "$PIPELINE_DIR"
        printf 'REMEMBER_DIR=%s\n' "$REMEMBER_DIR"
        printf 'REMEMBER_TZ=%s\n' "${REMEMBER_TZ:-}"
        printf 'REMEMBER_PROMPT_STAMP=%s\n' "${REMEMBER_PROMPT_STAMP:-full}"
        # No `:-120`/`:-50` fallback here — the guard above already refused to
        # reach this line unless both were actually set by log.sh, and a
        # default at the point of writing is exactly the silent stand-in
        # #358 was filed about.
        printf 'REMEMBER_SAVE_COOLDOWN=%s\n' "$REMEMBER_SAVE_COOLDOWN"
        printf 'REMEMBER_DELTA_THRESHOLD=%s\n' "$REMEMBER_DELTA_THRESHOLD"
        printf 'MEMORY_PROJECT_DIR=%s\n' "${MEMORY_PROJECT_DIR:-$PROJECT_DIR}"
        printf 'CACHE_CONFIG=%s\n' "${PIPELINE_DIR}/config.json"
        printf 'CACHE_CONFIG=%s\n' "${HOME:-}/.remember/config.json"
        printf 'CACHE_CONFIG=%s\n' "${REMEMBER_DIR}/config.json"
    } > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
    # Rename, so no reader ever parses a partial file and rejects a resolution
    # that was merely mid-write.
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t" 2>/dev/null
    return 0
}
