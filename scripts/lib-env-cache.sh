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
#   savings it exists to protect.
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

# Sets _REMEMBER_ENV_CACHE_FILE. Returns 1 when there is no project to key on.
#
# Keyed on the RAW CLAUDE_PROJECT_DIR, before resolve-paths.sh normalises it:
# the reader has not run that normalisation yet (it forks `tr` on Git Bash), and
# both sides comparing the same environment variable is what makes the key
# agree without either side computing anything.
_remember_env_cache_path() {
    local _key="${CLAUDE_PROJECT_DIR:-}"
    [ -n "$_key" ] || return 1
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
# On success: REMEMBER_DIR, REMEMBER_TZ, MEMORY_PROJECT_DIR, PROJECT_DIR and
# PIPELINE_DIR are set and exported, and the caller can skip the chain.
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
    [ "$_env_proj" = "${CLAUDE_PROJECT_DIR:-}" ] || return 1
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
    MEMORY_PROJECT_DIR="${_mem:-$_proj}"
    export PROJECT_DIR PIPELINE_DIR REMEMBER_DIR REMEMBER_TZ MEMORY_PROJECT_DIR
    export REMEMBER_PROMPT_STAMP
    return 0
}

# _remember_env_cache_publish
# Record a resolution that just completed. Never fails the caller: a hook that
# could not write a cache file has still done its actual job.
_remember_env_cache_publish() {
    [ "${REMEMBER_ENV_CACHE:-1}" = "1" ] || return 0
    [ -n "${CLAUDE_PROJECT_DIR:-}" ] || return 0
    [ -n "${REMEMBER_DIR:-}" ] || return 0
    [ -n "${PROJECT_DIR:-}" ] || return 0
    [ -n "${PIPELINE_DIR:-}" ] || return 0
    _remember_env_cache_path || return 0

    local _f="$_REMEMBER_ENV_CACHE_FILE" _t="${_REMEMBER_ENV_CACHE_FILE}.$$"
    # 0600 before a single byte of it exists. Every entry point sets umask 077
    # (#68), but this decides where memory gets written and its mode must not
    # depend on the caller having done that.
    (umask 077; : > "$_t") 2>/dev/null || return 0
    {
        printf 'CACHE_ENV_PROJECT_DIR=%s\n' "$CLAUDE_PROJECT_DIR"
        printf 'CACHE_ENV_PLUGIN_ROOT=%s\n' "${CLAUDE_PLUGIN_ROOT:-}"
        printf 'CACHE_ENV_HOME=%s\n' "${HOME:-}"
        printf 'PROJECT_DIR=%s\n' "$PROJECT_DIR"
        printf 'PIPELINE_DIR=%s\n' "$PIPELINE_DIR"
        printf 'REMEMBER_DIR=%s\n' "$REMEMBER_DIR"
        printf 'REMEMBER_TZ=%s\n' "${REMEMBER_TZ:-}"
        printf 'REMEMBER_PROMPT_STAMP=%s\n' "${REMEMBER_PROMPT_STAMP:-full}"
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
