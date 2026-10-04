#!/bin/bash

[ -n "${_REMEMBER_LIB_MEMORY_CONTEXT_LOADED:-}" ] && return 0
_REMEMBER_LIB_MEMORY_CONTEXT_LOADED=1

_remember_memory_paths() {
    [ -n "${TODAY:-}" ] || TODAY=$(_remember_date '+%Y-%m-%d')

    _remember_root_scratch="$REMEMBER_DIR"
    while [ "${_remember_root_scratch%/}" != "$_remember_root_scratch" ] \
        && [ "$_remember_root_scratch" != "/" ]; do
        _remember_root_scratch="${_remember_root_scratch%/}"
    done
    if [ "$_remember_root_scratch" = "/" ]; then
        REMEMBER_ROOT="/"
    elif [ "${_remember_root_scratch#*/}" != "$_remember_root_scratch" ]; then
        REMEMBER_ROOT="${_remember_root_scratch%/*}"
        [ -n "$REMEMBER_ROOT" ] || REMEMBER_ROOT="/"
    else
        printf -v REMEMBER_ROOT '\056'
    fi
    unset _remember_root_scratch
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
_remember_wc_size_get_into() {
    local _remember_wc_size_outvar="$1" _i=0
    while [ "$_i" -lt "${#_REMEMBER_WCSZ_NAMES[@]}" ]; do
        if [ "${_REMEMBER_WCSZ_NAMES[$_i]}" = "$2" ]; then
            printf -v "$_remember_wc_size_outvar" '%s' "${_REMEMBER_WCSZ_VALS[$_i]}"
            return 0
        fi
        _i=$((_i + 1))
    done
    printf -v "$_remember_wc_size_outvar" '%s' ''
}


_remember_ci_eq() {
    local _was=0 _rc
    shopt -q nocasematch && _was=1
    shopt -s nocasematch
    [[ "$1" == "$2" ]]
    _rc=$?
    [ "$_was" -eq 1 ] || shopt -u nocasematch
    return $_rc
}

_remember_git_unquote_into() {
    local _gu_outvar="$1" _gu_line="$2" _gu_dq
    printf -v _gu_dq '\042'
    if [ "${#_gu_line}" -ge 2 ] \
        && [ "${_gu_line#"$_gu_dq"}" != "$_gu_line" ] \
        && [ "${_gu_line%"$_gu_dq"}" != "$_gu_line" ]; then
            _gu_line="${_gu_line#"$_gu_dq"}"
            _gu_line="${_gu_line%"$_gu_dq"}"
            local _gu_bs _gu_bsq
            _gu_bs='\'
            _gu_bsq="${_gu_bs}${_gu_dq}"
            _gu_line="${_gu_line//"$_gu_bsq"/\\042}"
            printf -v "$_gu_outvar" '%b' "$_gu_line"
    else
            printf -v "$_gu_outvar" '%s' "$_gu_line"
    fi
}


_remember_repo_root_walk_into() {
    local _rrw_outvar="$1" _rrw_dir="$2"
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
    printf -v "$_rrw_outvar" '%s' ''  # not an empty format: see above
    return 1
}

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
            if [ -z "$_rts_reldir" ]; then
                _rts_pathspec=""
            else
                _rts_pathspec=":(icase)${_rts_reldir}/"
            fi
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

_REMEMBER_REFUSED_TRACKED_STATES="tracked unavailable symlinked-ancestor"

_remember_tracked_state_is_refused() {
    case " $_REMEMBER_REFUSED_TRACKED_STATES " in
        (*" $1 "*) return 0 ;;
        (*) return 1 ;;
    esac
}

_REMEMBER_FTS_SYM_DIRS=()
_REMEMBER_FTS_SYM_VALS=()
_remember_file_tracked_state_into() {
    local _fts_outvar="$1" _fts_file="$2"
    local _fts_dir _fts_root _fts_root_fs _fts_file_fs _fts_dir_fs _fts_rel _fts_reldir
    local _fts_list _fts_state _fts_line _fts_line_raw _fts_walk _fts_sym_idx _fts_sym_i
    _remember_forward_slash_into _fts_file_fs "$_fts_file"
    if [ "${_fts_file_fs#*/}" != "$_fts_file_fs" ]; then
        _fts_dir_fs="${_fts_file_fs%/*}"
    else
        printf -v _fts_dir_fs '\056'
    fi
    _remember_repo_root_walk_into _fts_root "$_fts_dir_fs"
    if [ -z "$_fts_root" ]; then
        printf -v "$_fts_outvar" 'no-repo'
        return 0
    fi
    _remember_forward_slash_into _fts_root_fs "$_fts_root"

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
        printf -v "$_fts_outvar" 'unavailable'
        return 0
    fi
    if [ "$_fts_dir_fs" = "$_fts_root_fs" ]; then
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
        _remember_git_unquote_into _fts_line_raw "$_fts_line"
        if [ "$_fts_line_raw" = "$_fts_rel" ] || _remember_ci_eq "$_fts_line_raw" "$_fts_rel"; then
            printf -v "$_fts_outvar" 'tracked'
            return 0
        fi
    done <<< "$_fts_list"
    printf -v "$_fts_outvar" 'not-tracked'
}

_remember_in_project_store() {
    local _rips_mem_proj="${MEMORY_PROJECT_DIR:-}"
    [ -n "$_rips_mem_proj" ] || _rips_mem_proj="$PROJECT_DIR"
    [ "$REMEMBER_ROOT" = "$_rips_mem_proj" ]
}

_remember_may_inject() {
    local _mi_file="$1" _mi_component="${2:-memory-context}" _mi_state
    _REMEMBER_INJECT_REFUSAL=""
    if [ -L "$_mi_file" ]; then
        _REMEMBER_INJECT_REFUSAL="$_mi_file is a symlink -- refusing to follow it into session context. This plugin never creates a symlink inside a memory store; if you did not create this one, treat it as planted and inspect what it points at before deleting it."
        log "$_mi_component" "refused injecting $_mi_file: symlink"
        return 1
    fi
    _remember_in_project_store || return 0
    _remember_file_tracked_state_into _mi_state "$_mi_file"
    if _remember_tracked_state_is_refused "$_mi_state"; then
        case "$_mi_state" in
            (tracked)
                _REMEMBER_INJECT_REFUSAL="$_mi_file is tracked by this repository's own git index. This plugin never commits a memory file itself (.remember/.gitignore excludes the whole directory), so a tracked one was shipped by the repository, not written by your own /remember. Not injecting it. If it is genuinely yours: git rm --cached it. If you did not add it: delete it and consider what else the commit that added it changed."
                log "$_mi_component" "refused injecting $_mi_file: git-tracked"
                ;;
            (unavailable)
                _REMEMBER_INJECT_REFUSAL="$_mi_file could not be checked against this repository's git index (git is missing, or the check itself failed) -- refusing rather than injecting unverified. Run /remember:doctor to see why git could not be asked."
                log "$_mi_component" "refused injecting $_mi_file: git status unavailable"
                ;;
            (symlinked-ancestor)
                _REMEMBER_INJECT_REFUSAL="$_mi_file sits under a directory that is itself a symlink -- refusing to follow it into session context. This plugin never creates a symlink inside a memory store; if you did not create this one, treat it as planted and inspect what it points at before deleting it."
                log "$_mi_component" "refused injecting $_mi_file: symlinked ancestor directory"
                ;;
            (*)
                _REMEMBER_INJECT_REFUSAL="$_mi_file could not be verified (tracked state: $_mi_state) -- refusing rather than injecting unverified."
                log "$_mi_component" "refused injecting $_mi_file: unrecognised refused state $_mi_state"
                ;;
        esac
        return 1
    fi
    return 0
}

_remember_emit_file() {
    local _remember_emit_max="${REMEMBER_EMIT_READ_MAX:-16384}"
    if [ -z "$_remember_emit_max" ] || [ "${_remember_emit_max#*[!0-9]}" != "$_remember_emit_max" ]; then _remember_emit_max=16384; fi
    case "${2:-}" in
        (''|*[!0-9]*)
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
    config_into MEMORY_INJECT_MAX_BYTES ".thresholds.memory_inject_max_bytes" 200000
    if [ -z "$MEMORY_INJECT_MAX_BYTES" ] || [ "${MEMORY_INJECT_MAX_BYTES#*[!0-9]}" != "$MEMORY_INJECT_MAX_BYTES" ]; then MEMORY_INJECT_MAX_BYTES=200000; fi
    local OVERSIZED_MEMORY="" BASENAME MFILE_BYTES _remember_oversized_max=0
    local _remember_budget_dropped=""
    local REFUSED_MEMORY=""
    local _remember_nl
    printf -v _remember_nl '\n'
    local _remember_present=() _remember_wc_bytes _remember_wc_path
    for MFILE in "${MEMORY_FILES[@]}"; do
        if [ -f "$MFILE" ] && [ -s "$MFILE" ]; then
            if [ "${SESSION_START_SOURCE:-}" = "compact" ] && [ "$MFILE" != "$IDENTITY_FILE" ]; then
                continue
            fi
            if _remember_may_inject "$MFILE" "memory-context"; then
                _remember_present+=("$MFILE")
            else
                REFUSED_MEMORY="${REFUSED_MEMORY}${_REMEMBER_INJECT_REFUSAL}${_remember_nl}"
            fi
        fi
    done
    if [ "${#_remember_present[@]}" -gt 0 ]; then
        while read -r _remember_wc_bytes _remember_wc_path; do
            if [ -z "$_remember_wc_bytes" ] || [ "${_remember_wc_bytes#*[!0-9]}" != "$_remember_wc_bytes" ]; then continue; fi
            [ "$_remember_wc_path" = "total" ] && continue
            _remember_wc_size_set "$_remember_wc_path" "$_remember_wc_bytes"
        done < <(wc -c "${_remember_present[@]}")
    fi
    [ "${#_remember_present[@]}" -gt 0 ] && for MFILE in "${_remember_present[@]}"; do
            _remember_wc_size_get_into MFILE_BYTES "$MFILE"
            if [ "${MFILE_BYTES#*[!0-9]}" != "$MFILE_BYTES" ]; then MFILE_BYTES=""; fi
            if [ -n "$MFILE_BYTES" ] && [ "$MEMORY_INJECT_MAX_BYTES" -gt 0 ] && [ "$MFILE_BYTES" -gt "$MEMORY_INJECT_MAX_BYTES" ]; then
                OVERSIZED_MEMORY="${OVERSIZED_MEMORY}${MFILE} (${MFILE_BYTES} bytes)${_remember_nl}"
                [ "$MFILE_BYTES" -gt "$_remember_oversized_max" ] && _remember_oversized_max="$MFILE_BYTES"
                continue
            fi
            if [ -n "${_REMEMBER_BUDGET_EXCLUDE:-}" ]; then
                case "${_remember_nl}${_REMEMBER_BUDGET_EXCLUDE}" in
                    (*"${_remember_nl}${MFILE}${_remember_nl}"*)
                        _remember_budget_dropped="${_remember_budget_dropped}${MFILE}"
                        if [ -n "${MFILE_BYTES:-}" ]; then
                            _remember_budget_dropped="${_remember_budget_dropped} (${MFILE_BYTES} bytes)"
                        fi
                        _remember_budget_dropped="${_remember_budget_dropped}${_remember_nl}"
                        continue
                        ;;
                esac
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
            if _remember_may_inject "$MFILE" "memory-context"; then
                _remember_deferred+=("$MFILE")
            else
                _remember_deferred_refused="${_remember_deferred_refused}${_REMEMBER_INJECT_REFUSAL}
"
            fi
        done
        if [ "${#_remember_deferred[@]}" -gt 0 ]; then
            while read -r _remember_wc_bytes _remember_wc_path; do
                if [ -z "$_remember_wc_bytes" ] || [ "${_remember_wc_bytes#*[!0-9]}" != "$_remember_wc_bytes" ]; then continue; fi
                [ "$_remember_wc_path" = "total" ] && continue
                _remember_wc_size_set "$_remember_wc_path" "$_remember_wc_bytes"
            done < <(wc -c "${_remember_deferred[@]}")
        fi
        DEFERRED_MEMORY=""
        [ "${#_remember_deferred[@]}" -gt 0 ] && DEFERRED_MEMORY=$(for MFILE in "${_remember_deferred[@]}"; do
            _remember_wc_size_get_into MFILE_BYTES "$MFILE"
            case "$MFILE_BYTES" in
                (''|*[!0-9]*) printf '%s (size unknown)\n' "$MFILE" ;;
                (*) printf '%s (%s bytes)\n' "$MFILE" "$MFILE_BYTES" ;;
            esac
        done)
        if [ -n "$_remember_deferred_refused" ]; then
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
        ROTATED_COUNT="${#_remember_rotated_arr[@]}"
        ROTATED_NEWEST=$(echo "$ROTATED_SLICES" | while read -r _slice; do
            [ -n "$_slice" ] || continue
            _core=${_slice##*/}
            _core=${_core#archive-}
            _core=${_core#recent-}
            _core=${_core%.md}
            if [[ "$_core" == *-*-*-* ]]; then
                _date=${_core%-*}; _seq=${_core##*-}
            else
                _date=$_core;      _seq=1
            fi
            if [ -z "$_seq" ] || [ "${_seq#*[!0-9]}" != "$_seq" ]; then _seq=1; fi
            printf '%s-%010d\t%s\n' "$_date" "$_seq" "$_slice"
        done | sort | tail -n "$ROTATED_LIST_MAX" | cut -f2-)
        echo "--- rotated memory slices (not shown; grep on request) ---"
        local _remember_newest_arr=() _remember_newest_line _remember_newest_refused=""
        while IFS= read -r _remember_newest_line; do
            [ -f "$_remember_newest_line" ] || continue
            if _remember_may_inject "$_remember_newest_line" "memory-context"; then
                _remember_newest_arr+=("$_remember_newest_line")
            else
                _remember_newest_refused="${_remember_newest_refused}${_REMEMBER_INJECT_REFUSAL}
"
            fi
        done <<< "$ROTATED_NEWEST"
        if [ "${#_remember_newest_arr[@]}" -gt 0 ]; then
            local _remember_newest_bytes
            while read -r _remember_wc_bytes _remember_wc_path; do
                if [ -z "$_remember_wc_bytes" ] || [ "${_remember_wc_bytes#*[!0-9]}" != "$_remember_wc_bytes" ]; then continue; fi
                [ "$_remember_wc_path" = "total" ] && continue
                _remember_wc_size_set "$_remember_wc_path" "$_remember_wc_bytes"
            done < <(wc -c "${_remember_newest_arr[@]}")
            for _remember_newest_line in "${_remember_newest_arr[@]}"; do
                _remember_wc_size_get_into _remember_newest_bytes "$_remember_newest_line"
                if [ -z "$_remember_newest_bytes" ] || [ -n "${_remember_newest_bytes//[0-9]/}" ]; then
                    printf '%s (size unknown)\n' "$_remember_newest_line"
                else
                    printf '%s (%s bytes)\n' "$_remember_newest_line" "$_remember_newest_bytes"
                fi
            done
        fi
        if [ -n "$_remember_newest_refused" ]; then
            echo ""
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

_REMEMBER_CACHE_FORMAT_VERSION="2"

_remember_start_cache_manifest_lines() {
    local MFILE
    printf 'VERSION=%s\n' "$_REMEMBER_CACHE_FORMAT_VERSION"
    for MFILE in "${MEMORY_FILES[@]}"; do
        printf 'SRC=%s\n' "$MFILE"
    done
    printf 'SRC=%s\n' "$REMEMBER_DIR"
    printf 'SRC=%s\n' "${PIPELINE_DIR:-}/config.json"
    printf 'SRC=%s\n' "${HOME:-}/.remember/config.json"
    printf 'SRC=%s\n' "${REMEMBER_DIR}/config.json"
}

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
    _remember_may_inject "$_cache" "start-cache" || return 1
    _remember_may_inject "$_manifest" "start-cache" || return 1

    local _line _src _mf_version="" _mf_saw_src=0
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        if [ "${_line#VERSION=}" != "$_line" ]; then
            _mf_version="${_line#VERSION=}"
            continue
        elif [ "${_line#SRC=}" != "$_line" ]; then
            _src="${_line#SRC=}"
        else
            return 1
        fi
        [ -n "$_src" ] || continue
        _mf_saw_src=1
        [ "$_cache" -nt "$_src" ] || return 1
    done < "$_manifest"
    [ "$_mf_saw_src" -eq 1 ] || return 1
    [ "$_mf_version" = "$_REMEMBER_CACHE_FORMAT_VERSION" ] || return 1

    cat "$_cache"
    return 0
}

_remember_start_cache_context_finish_publish() {
    local _tmp_cache="$1"
    [ "${REMEMBER_START_CACHE:-1}" = "1" ] || { rm -f "$_tmp_cache" 2>/dev/null; return 0; }
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
    mv -f "$_tmp_cache" "$_cache" 2>/dev/null || { rm -f "$_tmp_cache" "$_tmp_manifest" 2>/dev/null; return 0; }
    mv -f "$_tmp_manifest" "$_manifest" 2>/dev/null || rm -f "$_tmp_manifest" 2>/dev/null
    return 0
}

_remember_start_cache_context_publish() {
    [ "${REMEMBER_START_CACHE:-1}" = "1" ] || return 0
    [ -n "${REMEMBER_DIR:-}" ] || return 0
    local _dir="$REMEMBER_DIR/tmp"
    mkdir -p "$_dir" 2>/dev/null || return 0
    local _cache="$_dir/start-context.cache"
    local _tmp_cache
    _tmp_cache=$(mktemp "${_cache}.XXXXXX" 2>/dev/null) || return 0
    _remember_render_memory_section > "$_tmp_cache" 2>/dev/null
    _remember_start_cache_context_finish_publish "$_tmp_cache"
}

_remember_session_start_max_bytes_into() {
    local _outvar="$1"
    local _val=""
    config_into _val ".thresholds.session_start_max_bytes" 9000
    case "$_val" in
        (''|*[!0-9]*)
            log "memory-context" "WARNING: thresholds.session_start_max_bytes is not a valid non-negative integer (got $_val) -- using default 9000"
            _val=9000
            ;;
    esac
    printf -v "$_outvar" %s "$_val"
}

unset _REMEMBER_BUDGET_EXCLUDE

_remember_apply_session_start_budget() {
    local _outvar="$1" _max="$2" _text="$3"
    if [ -z "$_max" ] || [ "${_max#*[!0-9]}" != "$_max" ]; then return 0; fi
    [ "$_max" -gt 0 ] || return 0
    local LC_ALL=C  # byte length, not a locale-dependent character count (see header above)
    [ "${#_text}" -gt "$_max" ] || return 0

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
    if [ $(( _head_len + ${#_mem} )) -gt "$_max" ]; then
        log "memory-context" "WARNING: thresholds.session_start_max_bytes: still over budget ($(( _head_len + ${#_mem} )) bytes > ${_max}) after dropping every droppable section"
    fi
    printf -v "$_outvar" %s "${_head}${_mem}"
}
