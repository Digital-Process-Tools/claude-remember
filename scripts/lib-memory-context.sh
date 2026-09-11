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

    REMEMBER_ROOT=$(dirname "$REMEMBER_DIR")
    if [ -f "$REMEMBER_DIR/identity.md" ]; then
        IDENTITY_FILE="$REMEMBER_DIR/identity.md"
    elif [ -f "$REMEMBER_ROOT/identity.md" ] && [ "$REMEMBER_ROOT" != "$PROJECT_DIR" ]; then
        IDENTITY_FILE="$REMEMBER_ROOT/identity.md"
    else
        IDENTITY_FILE="$PLUGIN_ROOT/identity.md"
    fi

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
_remember_render_memory_section() {
    local MFILE HAS_MEMORY="" ROTATED_SLICES _remember_rotated_glob_dir

    for MFILE in "${MEMORY_FILES[@]}"; do
        [ -f "$MFILE" ] && HAS_MEMORY="true"
    done
    _remember_rotated_glob_dir=$(_remember_forward_slash "$REMEMBER_DIR")
    ROTATED_SLICES=$(ls "$_remember_rotated_glob_dir"/archive-*.md "$_remember_rotated_glob_dir"/recent-*.md 2>/dev/null | sort)
    [ -n "$ROTATED_SLICES" ] && HAS_MEMORY="true"

    [ -n "$HAS_MEMORY" ] || return 0

    echo "=== MEMORY ==="
    local MEMORY_INJECT_MAX_BYTES
    MEMORY_INJECT_MAX_BYTES=$(config ".thresholds.memory_inject_max_bytes" 200000)
    case "$MEMORY_INJECT_MAX_BYTES" in (''|*[!0-9]*) MEMORY_INJECT_MAX_BYTES=200000 ;; esac
    local OVERSIZED_MEMORY="" BASENAME MFILE_BYTES
    for MFILE in "${MEMORY_FILES[@]}"; do
        if [ -f "$MFILE" ] && [ -s "$MFILE" ]; then
            if [ "${SESSION_START_SOURCE:-}" = "compact" ] && [ "$MFILE" != "$IDENTITY_FILE" ]; then
                continue
            fi
            MFILE_BYTES=$(wc -c < "$MFILE" | tr -d ' ')
            case "$MFILE_BYTES" in (''|*[!0-9]*) MFILE_BYTES=0 ;; esac
            if [ "$MEMORY_INJECT_MAX_BYTES" -gt 0 ] && [ "$MFILE_BYTES" -gt "$MEMORY_INJECT_MAX_BYTES" ]; then
                OVERSIZED_MEMORY="${OVERSIZED_MEMORY}${MFILE} (${MFILE_BYTES} bytes)
"
                continue
            fi
            BASENAME=$(basename "$MFILE")
            echo "--- $BASENAME ---"
            cat "$MFILE"
            echo ""
        fi
    done
    if [ -n "$OVERSIZED_MEMORY" ]; then
        echo "--- too large to inject (kept on disk; grep on request) ---"
        printf '%s' "$OVERSIZED_MEMORY"
        printf 'A healthy memory file is kilobytes. One this size means consolidation wrote a response nobody bounded (see thresholds.memory_inject_max_bytes) and has been skipping ever since; run /remember:doctor.\n'
        echo ""
    fi
    if [ "${SESSION_START_SOURCE:-}" = "compact" ]; then
        local DEFERRED_MEMORY
        DEFERRED_MEMORY=$(for MFILE in "${MEMORY_FILES[@]}"; do
            [ "$MFILE" != "$IDENTITY_FILE" ] || continue
            [ -f "$MFILE" ] && [ -s "$MFILE" ] || continue
            printf '%s (%s bytes)\n' "$MFILE" "$(wc -c < "$MFILE" | tr -d ' ')"
        done)
        if [ -n "$DEFERRED_MEMORY" ]; then
            echo "--- not re-injected at compact (delivered at session start); read or grep on request ---"
            printf '%s\n' "$DEFERRED_MEMORY"
            echo ""
        fi
    fi
    if [ -n "$ROTATED_SLICES" ]; then
        local ROTATED_LIST_MAX=10 ROTATED_COUNT ROTATED_NEWEST
        ROTATED_COUNT=$(echo "$ROTATED_SLICES" | wc -l | tr -d ' ')
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
        echo "$ROTATED_NEWEST" | while read -r _slice; do
            [ -f "$_slice" ] || continue
            printf '%s (%s bytes)\n' "$_slice" "$(wc -c < "$_slice" | tr -d ' ')"
        done
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
