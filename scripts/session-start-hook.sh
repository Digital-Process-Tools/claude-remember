#!/bin/bash
__remember_src_resolve_paths() {
:

if [ -n "${REMEMBER_NESTED_SUMMARIZER:-}" ]; then
    if [ "${REMEMBER_PATHS_SOFT_FAIL:-0}" = "1" ]; then
        return 1
    fi
    exit 0
fi

umask 077

_SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
[ "$_SCRIPT_DIR" = "${BASH_SOURCE[0]}" ] && _SCRIPT_DIR="$(pwd)"
_PLUGIN_ROOT_CANDIDATE="$(cd "$_SCRIPT_DIR/.." && pwd)"

_resolve_paths_fail() {
    echo "$1" >&2
    if [ -n "${2:-}" ] && [ -d "$2" ]; then
        echo "$(date '+%H:%M:%S') [resolve] $1" >> "$2/memory-$(date '+%Y-%m-%d').log" 2>/dev/null
    fi
    [ "${REMEMBER_PATHS_SOFT_FAIL:-0}" = "1" ] && return 1
    exit 1
}

if [ -n "${PLUGIN_ROOT:-}" ]; then
    _REMEMBER_PLUGIN_ROOT="$PLUGIN_ROOT"
else
    _REMEMBER_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
fi
if [ -n "$_REMEMBER_PLUGIN_ROOT" ] && [ -f "$_REMEMBER_PLUGIN_ROOT/.claude-plugin/plugin.json" ]; then
    PIPELINE_DIR="$_REMEMBER_PLUGIN_ROOT"
elif [ -n "${PLUGIN_ROOT:-}" ] && [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] \
        && [ "$_REMEMBER_PLUGIN_ROOT" != "$CLAUDE_PLUGIN_ROOT" ] \
        && [ -f "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json" ]; then
    PIPELINE_DIR="${CLAUDE_PLUGIN_ROOT:-}"
elif [ -f "$_PLUGIN_ROOT_CANDIDATE/.claude-plugin/plugin.json" ]; then
    PIPELINE_DIR="$_PLUGIN_ROOT_CANDIDATE"
else
    _msg="FATAL: Cannot resolve plugin root. PLUGIN_ROOT/CLAUDE_PLUGIN_ROOT do not point at a valid plugin install (missing its install manifest) and $_PLUGIN_ROOT_CANDIDATE does not look like one either."
    _resolve_paths_fail "$_msg" "${CLAUDE_PROJECT_DIR:-.}/.remember/logs" || return 1
fi

_remember_normalize_win_path() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local _in="$1" _drive="" _rest=""
    local _re='^([a-zA-Z]):[/\](.*)$'
    if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
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
            _drive=$(printf '%s' "$_drive" | LC_ALL=C tr '[:lower:]' '[:upper:]')
            _rest="${_rest//\//\\}"
            printf '%s' "${_drive}:\\${_rest}"
            return 0
        fi
    fi
    printf '%s' "$_in"
}

_remember_forward_slash() {
    if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
        printf '%s' "${1//\\//}"
    else
        printf '%s' "$1"
    fi
}

_remember_forward_slash_into() {
    if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
        printf -v "$1" '%s' "${2//\\//}"
    else
        printf -v "$1" '%s' "$2"
    fi
}

if [ -n "$CLAUDE_PROJECT_DIR" ]; then
    PROJECT_DIR="$(_remember_normalize_win_path "$CLAUDE_PROJECT_DIR")"
elif [ -n "${REMEMBER_HOOK_CWD:-}" ] && [ -d "$(_remember_normalize_win_path "${REMEMBER_HOOK_CWD:-}")" ]; then
    PROJECT_DIR="$(_remember_normalize_win_path "$REMEMBER_HOOK_CWD")"
elif [[ "$PIPELINE_DIR" == *"/.claude/remember" ]]; then
    PROJECT_DIR="$(cd "$PIPELINE_DIR/../.." && pwd)"
else
    _msg="FATAL: Cannot resolve project root. CLAUDE_PROJECT_DIR is not set, REMEMBER_HOOK_CWD is not set or not a directory, and plugin is not in a local .claude/remember/ layout (PIPELINE_DIR=$PIPELINE_DIR)."
    _resolve_paths_fail "$_msg" "${PROJECT_DIR:-.}/.remember/logs" || return 1
fi
unset -f _remember_normalize_win_path

if [ ! -d "$PROJECT_DIR" ]; then
    _msg="FATAL: PROJECT_DIR does not exist: $PROJECT_DIR"
    _resolve_paths_fail "$_msg" || return 1
fi

if [ ! -d "$PIPELINE_DIR" ]; then
    _msg="FATAL: PIPELINE_DIR does not exist: $PIPELINE_DIR"
    _resolve_paths_fail "$_msg" || return 1
fi

export CLAUDE_PROJECT_DIR="$PROJECT_DIR"
export CLAUDE_PLUGIN_ROOT="$PIPELINE_DIR"
export PROJECT_DIR
export PIPELINE_DIR

}
__remember_src_detect_tools() {
:
_REMEMBER_TOOLS_CACHE="${TMPDIR:-/tmp}/remember-detect-tools-cache"

_jq_fallback() {
    local _jq_flags=""
    while [[ "$1" == -* ]]; do _jq_flags="$_jq_flags $1"; shift; done
    local _jq_query="$1"
    local _jq_file="$2"
    if declare -f _remember_python >/dev/null 2>&1; then
        _remember_python || return 1
    fi
    local _jq_fb_dir="${BASH_SOURCE[0]%/*}"
    [ "$_jq_fb_dir" = "${BASH_SOURCE[0]}" ] && _jq_fb_dir="$(pwd)"
    _remember_run_python "$_jq_fb_dir/jq_fallback_get.py" "$_jq_file" "$_jq_query" 2>/dev/null
}

_remember_tools_cache_load() {
    [ "${REMEMBER_TOOLS_CACHE:-1}" = "1" ] || return 1
    local _f="$_REMEMBER_TOOLS_CACHE"
    [ -f "$_f" ] || return 1
    [ -L "$_f" ] && return 1
    [ -O "$_f" ] || return 1
    [ -r "$_f" ] || return 1
    local _line _path="" _py="" _jq=""
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        if [ "${_line#CACHE_PATH=}" != "$_line" ]; then
            _path="${_line#*=}"
        elif [ "${_line#PYTHON=}" != "$_line" ]; then
            _py="${_line#*=}"
        elif [ "${_line#JQ=}" != "$_line" ]; then
            _jq="${_line#*=}"
        else
            return 1
        fi
    done < "$_f"
    [ -n "$_py" ] || return 1
    [ -n "$_jq" ] || return 1
    [ -n "$_path" ] || return 1
    [ "$_path" = "$PATH" ] || return 1
    if [ "$_jq" != jq ] && [ "$_jq" != _jq_fallback ]; then
        return 1
    fi
    PYTHON="$_py"
    JQ="$_jq"
    export PYTHON JQ
    return 0
}

_remember_tools_cache_publish() {
    [ "${REMEMBER_TOOLS_CACHE:-1}" = "1" ] || return 0
    local _f="$_REMEMBER_TOOLS_CACHE" _t
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    {
        printf 'CACHE_PATH=%s\n' "$PATH"
        printf 'PYTHON=%s\n' "$PYTHON"
        printf 'JQ=%s\n' "$JQ"
    } > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t" 2>/dev/null
    return 0
}

if _remember_tools_cache_load; then
    _remember_python() { [ -n "${PYTHON:-}" ]; }
else

if [ "${_REMEMBER_LAZY_PYTHON:-0}" = "1" ]; then
    PYTHON=""
    _remember_python() {
        [ -n "${PYTHON:-}" ] && return 0
        local _candidate _first _probe_status _probe_report=""
        for _candidate in "python3" "python" "py -3" "py"; do
            _first="${_candidate%% *}"
            if ! command -v "$_first" >/dev/null 2>&1; then
                _probe_report="$_probe_report
  $_candidate: not on PATH"
                continue
            fi
            if $_candidate -V >/dev/null 2>&1; then
                PYTHON="$_candidate"
                break
            else
                _probe_status=$?
            fi
            _probe_report="$_probe_report
  $_candidate: on PATH ($(command -v "$_first" 2>/dev/null)), '-V' exit $_probe_status"
        done
        unset _probe_status
        if [ -z "$PYTHON" ]; then
            echo "FATAL: No working Python found. Tried: python3, python, py -3, py. Windows users: install the official Python release (not the Microsoft Store one) and ensure 'python' or 'py' works from the shell Claude Code launches hooks in." >&2
            echo "  PATH searched: $PATH" >&2
            echo "  per-candidate (exit 49 = Microsoft Store placeholder, not a real interpreter):$_probe_report" >&2
            unset _probe_report
            return 1
        fi
        unset _probe_report
        export PYTHON
        _remember_tools_cache_publish
        return 0
    }
else
    PYTHON=""
    _probe_report=""
    for _candidate in "python3" "python" "py -3" "py"; do
        _first="${_candidate%% *}"
        if ! command -v "$_first" >/dev/null 2>&1; then
            _probe_report="$_probe_report
  $_candidate: not on PATH"
            continue
        fi
        if $_candidate -V >/dev/null 2>&1; then
            PYTHON="$_candidate"
            break
        else
            _probe_status=$?
        fi
        _probe_report="$_probe_report
  $_candidate: on PATH ($(command -v "$_first" 2>/dev/null)), '-V' exit $_probe_status"
    done
    unset _probe_status
    if [ -z "$PYTHON" ]; then
        echo "FATAL: No working Python found. Tried: python3, python, py -3, py. Windows users: install the official Python release (not the Microsoft Store one) and ensure 'python' or 'py' works from the shell Claude Code launches hooks in." >&2
        echo "  PATH searched: $PATH" >&2
        echo "  per-candidate (exit 49 = Microsoft Store placeholder, not a real interpreter):$_probe_report" >&2
        unset _probe_report
        exit 1
    fi
    unset _probe_report
    export PYTHON
    _remember_python() { [ -n "${PYTHON:-}" ]; }
fi

if command -v jq >/dev/null 2>&1; then
    JQ="jq"
else
    JQ="_jq_fallback"
fi
export JQ

if [ "${_REMEMBER_LAZY_PYTHON:-0}" != "1" ]; then
    _remember_tools_cache_publish
fi
fi

_remember_run_python() {
    if [ "$PYTHON" = python3 ]; then
        python3 "$@"
    elif [ "$PYTHON" = python ]; then
        python "$@"
    elif [ "$PYTHON" = "py -3" ]; then
        py -3 "$@"
    elif [ "$PYTHON" = py ]; then
        py "$@"
    else
        echo "FATAL: _remember_run_python: unrecognized PYTHON value '$PYTHON'" >&2
        return 127
    fi
}
_remember_run_jq() {
    if [ "$JQ" = jq ]; then
        jq "$@"
    elif [ "$JQ" = _jq_fallback ]; then
        _jq_fallback "$@"
    else
        echo "FATAL: _remember_run_jq: unrecognized JQ value '$JQ'" >&2
        return 127
    fi
}


_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
__remember_src_lib_slug ${1+"$@"}
unset _REMEMBER_SRC_DIR

}
__remember_src_lib_slug() {
:

[ -n "${_REMEMBER_LIB_SLUG_LOADED:-}" ] && return 0
_REMEMBER_LIB_SLUG_LOADED=1

_remember_slug_run_python() {
    if [ "${PYTHON:-python3}" = python3 ]; then
        python3 "$@"
    elif [ "${PYTHON:-python3}" = python ]; then
        python "$@"
    elif [ "${PYTHON:-python3}" = "py -3" ]; then
        py -3 "$@"
    elif [ "${PYTHON:-python3}" = py ]; then
        py "$@"
    else
        return 127
    fi
}

_remember_build_slug_sed() {
    local cont=$'\200-\277'
    local r220_277=$'\220-\277'
    local r361_363=$'\361-\363'
    local r200_217=$'\200-\217'
    local r240_277=$'\240-\277'
    local r341_354=$'\341-\354'
    local r200_237=$'\200-\237'
    local r356_357=$'\356-\357'
    local r302_337=$'\302-\337'
    _REMEMBER_SLUG_SED=(
        -e "s/"$'\360'"[$r220_277][$cont][$cont]/--/g"
        -e "s/[$r361_363][$cont][$cont][$cont]/--/g"
        -e "s/"$'\364'"[$r200_217][$cont][$cont]/--/g"
        -e "s/"$'\340'"[$r240_277][$cont]/-/g"
        -e "s/[$r341_354][$cont][$cont]/-/g"
        -e "s/"$'\355'"[$r200_237][$cont]/-/g"
        -e "s/[$r356_357][$cont][$cont]/-/g"
        -e "s/[$r302_337][$cont]/-/g"
        -e 's/[^a-zA-Z0-9]/-/g'
    )
}
_remember_build_slug_sed

claude_projects_dir() {
    local _root
    if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
        _root="$CLAUDE_CONFIG_DIR"
    else
        _root="$HOME/.claude"
    fi

    if command -v cygpath >/dev/null 2>&1; then
        local _converted
        _converted=$(cygpath -u "$_root" 2>/dev/null) && [ -n "$_converted" ] \
            && _root="$_converted"
    fi

    local _stripped="$_root"
    while [ "${_stripped%[/\\]}" != "$_stripped" ]; do
        _stripped="${_stripped%[/\\]}"
    done
    if [ -n "$_stripped" ]; then
        _root="$_stripped"
    else
        if [ "${_root#/}" != "$_root" ]; then
            _root=""
        fi
    fi

    printf '%s/projects' "$_root"
}

_remember_should_check_utf8() {
    [ "${REMEMBER_UTF8_STRICT:-0}" = "1" ] && return 0
    local _os="${OSTYPE:-}"
    [ "${_os#linux}" != "$_os" ] && return 0
    return 1
}

_REMEMBER_DRIVE_UPPER="ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_REMEMBER_DRIVE_LOWER="abcdefghijklmnopqrstuvwxyz"

session_dir_slug() {
    local path="$1"
    local _drive_at
    if command -v cygpath >/dev/null 2>&1; then
        local winpath
        winpath=$(cygpath -w "$path" 2>/dev/null) || winpath="$path"
        [ -n "$winpath" ] || winpath="$path"
        local _unc_pfx='\\?\UNC\' _long_pfx='\\?\'
        if [ "${winpath#"$_unc_pfx"}" != "$winpath" ]; then
            winpath='\\'"${winpath#"$_unc_pfx"}"
        elif [ "${winpath#"$_long_pfx"}" != "$winpath" ]; then
            winpath="${winpath#"$_long_pfx"}"
        fi
        path="$winpath"
    fi
    if [ "${path#?:}" != "$path" ]; then
        _drive_at="${_REMEMBER_DRIVE_UPPER%%"${path:0:1}"*}"
        if [ "$_drive_at" != "$_REMEMBER_DRIVE_UPPER" ]; then
            path="${_REMEMBER_DRIVE_LOWER:${#_drive_at}:1}${path:1}"
        fi
    fi
    local _orig="$path"

    if _remember_should_check_utf8; then
    local _high_byte=0 _lc_was_set="${LC_ALL+set}" _lc_prev="${LC_ALL:-}"
    LC_ALL=C
    local _hb_glob="[!"$'\001'"-"$'\177'"]"
    if [[ "$path" == *$_hb_glob* ]]; then
        _high_byte=1
    fi
    if [ -n "$_lc_was_set" ]; then LC_ALL="$_lc_prev"; else unset LC_ALL; fi

    if [ "$_high_byte" = 1 ]; then
        if command -v iconv >/dev/null 2>&1 \
            && ! printf '%s' "$path" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; then
            local _py_slug="${PIPELINE_DIR:-}/pipeline/slug.py"
            if [ -f "$_py_slug" ]; then
                local _decoded
                declare -f _remember_python >/dev/null 2>&1 && _remember_python
                _decoded=$(_remember_slug_run_python "$_py_slug" "$path" 2>/dev/null) \
                    && [ -n "$_decoded" ] && { printf '%s\n' "$_decoded"; return 0; }
            fi
        fi
    fi
    fi

    path=${path//$'\n'/-}
    local _slug
    _slug=$(printf '%s\n' "$path" | LC_ALL=C sed "${_REMEMBER_SLUG_SED[@]}")

    if [ ${#_slug} -le 200 ]; then
        printf '%s\n' "$_slug"
        return 0
    fi

    local _hash _slug_py="${PIPELINE_DIR:-}/pipeline/slug.py"
    if [ -f "$_slug_py" ]; then
        declare -f _remember_python >/dev/null 2>&1 && _remember_python
        _hash=$(_remember_slug_run_python "$_slug_py" --hash "$_orig" 2>/dev/null) || _hash=""
    else
        _hash=""
    fi

    if [[ "$_hash" == *[!0-9a-z]* ]]; then
        _hash=""
    fi

    if [ -z "$_hash" ]; then
        printf '%s\n' "$_slug"
        return 0
    fi
    printf '%s-%s\n' "${_slug:0:200}" "$_hash"
}

}
__remember_src_bootstrap_dirs() {
:

_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
__remember_src_lib_memory_dir ${1+"$@"}
unset _REMEMBER_SRC_DIR

SYS_TMPDIR="${TMPDIR:-/tmp}"

_mem_proj="${MEMORY_PROJECT_DIR:-}"
[ -n "$_mem_proj" ] || _mem_proj="$PROJECT_DIR"
_legacy_dir="${_mem_proj}/.remember"
_remember_legacy_migration_refused=""
if [ "$REMEMBER_DIR" != "$_legacy_dir" ] && [ ! -L "$_legacy_dir" ] && [ -d "$_legacy_dir" ] && [ ! -e "$REMEMBER_DIR" ]; then
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

        _legacy_other_tracked="clean"
        if command -v git >/dev/null 2>&1; then
            _legacy_repo_check=$( (unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                                    LC_ALL=C LANGUAGE=C git -C "$_mem_proj" rev-parse --is-inside-work-tree) 2>&1 ) && _legacy_repo_rc=0 || _legacy_repo_rc=$?
            if [ "$_legacy_repo_rc" -ne 0 ]; then
                if [ "${_legacy_repo_check#*not a git repository}" = "$_legacy_repo_check" ]; then
                    _legacy_other_tracked="could-not-tell"
                fi
            elif [ "$_legacy_repo_check" = "true" ]; then
                _legacy_ls_list=$(unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                                   git -c core.quotePath=false -C "$_mem_proj" ls-files -- ":(icase).remember/" 2>/dev/null) && _legacy_ls_rc=0 || _legacy_ls_rc=$?
                if [ "$_legacy_ls_rc" -ne 0 ]; then
                    _legacy_other_tracked="could-not-tell"
                else
                    while IFS= read -r _legacy_ls_line; do
                        [ -n "$_legacy_ls_line" ] || continue
                        _legacy_was_nocasematch=0
                        shopt -q nocasematch && _legacy_was_nocasematch=1
                        shopt -s nocasematch
                        if [[ "$_legacy_ls_line" == ".remember/config.json" ]]; then
                            _legacy_ci_match=1
                        else
                            _legacy_ci_match=0
                        fi
                        [ "$_legacy_was_nocasematch" -eq 1 ] || shopt -u nocasematch
                        [ "$_legacy_ci_match" -eq 1 ] && continue
                        _legacy_other_tracked="tracked"
                    done <<< "$_legacy_ls_list"
                fi
            fi
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
        unset _legacy_ls_list _legacy_ls_line _legacy_repo_check _legacy_repo_rc _legacy_ls_rc _legacy_was_nocasematch _legacy_ci_match

        if [ "$_legacy_other_tracked" != "clean" ]; then
            _remember_legacy_migration_refused="1"
            printf 'remember: %s contains git-tracked content beyond config.json (%s) -- refusing to migrate it into the external memory store, which would launder repository-committed content into a location the injection guard trusts unconditionally (#782). Left in place, untouched; move your own files out of it by hand, or `git rm --cached` whatever the repository should not have committed, then start a new session to retry.\n' \
                "$_legacy_dir" "$_legacy_other_tracked" >&2
        fi
    fi
    if [ "$_migrating_user_config_home" = false ] && [ "${_legacy_other_tracked:-clean}" = "clean" ]; then
        _legacy_cfg="$_legacy_dir/config.json"
        _legacy_cfg_holdout=""
        if [ -e "$_legacy_cfg" ] || [ -L "$_legacy_cfg" ]; then
            _legacy_cfg_tracked_answer=$(_remember_config_tracked_status "$_mem_proj" ".remember/config.json") || true
            if [ "$_legacy_cfg_tracked_answer" != untracked ]; then
                _legacy_cfg_holdout=$(mktemp "${SYS_TMPDIR:-/tmp}/remember-legacy-cfg-XXXXXX" 2>/dev/null) || _legacy_cfg_holdout=""
                if [ -n "$_legacy_cfg_holdout" ]; then
                    mv "$_legacy_cfg" "$_legacy_cfg_holdout" 2>/dev/null || _legacy_cfg_holdout=""
                fi
            fi
            unset _legacy_cfg_tracked_answer
        fi

        if mv "$_legacy_dir" "$REMEMBER_DIR" 2>/dev/null; then
            mkdir -p "$_legacy_dir"
            if [ -n "$_legacy_cfg_holdout" ] && { [ -e "$_legacy_cfg_holdout" ] || [ -L "$_legacy_cfg_holdout" ]; }; then
                if mv "$_legacy_cfg_holdout" "$_legacy_cfg" 2>/dev/null; then
                    printf 'Memory data migrated to:\n  %s\nThis directory is now empty; you may delete it.\n\nconfig.json was NOT migrated: it is tracked by this repository git\nindex (or its git status could not be determined, which this treats the\nsame way) -- treating it as your own trusted config would let a cloned\nrepo choose the summarizer credential, model, or refusal-gate settings\nyour memory pipeline runs with (#757). Left behind here, still doing\nnothing for the external store above. Put your own settings in\n%s/config.json instead.\n' \
                        "$REMEMBER_DIR" "$REMEMBER_DIR" > "$_legacy_dir/MIGRATED-TO.txt"
                    printf 'remember: %s is tracked by this repository git index (or its git status could not be determined); left behind rather than migrated into the trusted external config store (#757)\n' \
                        "$_legacy_cfg" >&2
                    _legacy_cfg_holdout=""
                else
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
            if mv "$_legacy_cfg_holdout" "$_legacy_cfg" 2>/dev/null; then
                _legacy_cfg_holdout=""
            else
                printf 'remember: FAILED to restore %s after the migration move itself failed -- your config.json was NOT deleted, it is still sitting at %s; move it back to %s by hand. (#757)\n' \
                    "$_legacy_cfg" "$_legacy_cfg_holdout" "$_legacy_cfg" >&2
            fi
        fi
        unset _legacy_cfg _legacy_cfg_holdout
    fi
    unset _migrating_user_config_home _legacy_other_tracked
elif [ "$REMEMBER_DIR" != "$_legacy_dir" ] && [ -L "$_legacy_dir" ] && [ ! -e "$REMEMBER_DIR" ]; then
    printf 'remember: %s is a symlink -- refusing to migrate it; left in place untouched. Move or replace it by hand if this was not intentional. (#757)\n' \
        "$_legacy_dir" >&2
fi
unset _legacy_dir

if [ -z "$_remember_legacy_migration_refused" ] && { [ ! -d "$REMEMBER_DIR/logs/autonomous" ] || [ ! -d "$REMEMBER_DIR/tmp" ]; }; then
    mkdir -p \
        "$REMEMBER_DIR/tmp" \
        "$REMEMBER_DIR/logs" \
        "$REMEMBER_DIR/logs/autonomous" \
        2>/dev/null
fi
unset _remember_legacy_migration_refused

if [ -d "$REMEMBER_DIR/tmp" ]; then
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

    _remember_relocated_cfg="$REMEMBER_DIR/tmp/remember-config-$$.json"
    if [ -n "${REMEMBER_CONFIG:-}" ] && [ -f "$REMEMBER_CONFIG" ] \
        && mv -f "$REMEMBER_CONFIG" "$_remember_relocated_cfg" 2>/dev/null; then
        REMEMBER_CONFIG="$_remember_relocated_cfg"
        export REMEMBER_CONFIG
        _remember_relocated_cfg_q=$(printf %q "$_remember_relocated_cfg")
        _remember_trap_raw=$(trap -p EXIT 2>/dev/null)
        _remember_existing_trap="${_remember_trap_raw#trap -- \'}"
        _remember_existing_trap="${_remember_existing_trap%\' EXIT}"
        _remember_existing_trap="${_remember_existing_trap//\'\\\'\'/\'}"
        unset _remember_trap_raw
        if [ -n "$_remember_existing_trap" ]; then
            trap "${_remember_existing_trap}; rm -f ${_remember_relocated_cfg_q}" EXIT
        else
            trap "rm -f ${_remember_relocated_cfg_q}" EXIT
        fi
        unset _remember_existing_trap
        unset _remember_relocated_cfg_q
    fi
    unset _remember_relocated_cfg
fi

if [ -d "$REMEMBER_DIR" ]; then
    [ -f "$REMEMBER_DIR/.install-marker" ] \
        || { echo 'This file marks when remember was first bootstrapped here. Read only by /remember:doctor (#401); do not delete it.' \
            > "$REMEMBER_DIR/.install-marker"; } 2>/dev/null
fi

if [ -d "$REMEMBER_DIR" ]; then
    _mem_bd_glob_dir="$REMEMBER_DIR"
    _mem_bd_glob_proj="$_mem_proj"
    if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
        _mem_bd_glob_dir="${_mem_bd_glob_dir//\\//}"
        _mem_bd_glob_proj="${_mem_bd_glob_proj//\\//}"
    fi
    if [ "${_mem_bd_glob_dir#"$_mem_bd_glob_proj"/}" != "$_mem_bd_glob_dir" ]; then
        [ -f "$REMEMBER_DIR/.gitignore" ] || { echo '*' > "$REMEMBER_DIR/.gitignore"; } 2>/dev/null
    fi
fi
unset _mem_proj _mem_bd_glob_dir _mem_bd_glob_proj

_remember_bd_has_xtracefd=0
if [ "${BASH_VERSINFO[0]:-0}" -gt 4 ] 2>/dev/null; then
    _remember_bd_has_xtracefd=1
elif [ "${BASH_VERSINFO[0]:-0}" -eq 4 ] 2>/dev/null && [ "${BASH_VERSINFO[1]:-0}" -ge 1 ] 2>/dev/null; then
    _remember_bd_has_xtracefd=1
fi
if [ -d "$REMEMBER_DIR/logs" ]; then
    _remember_bd_keep_fd2=""
    if [[ "$-" == *x* ]]; then
        if [ "$_remember_bd_has_xtracefd" = "0" ]; then
            _remember_bd_keep_fd2="an xtrace is running (this bash predates BASH_XTRACEFD, added in 4.1, so xtrace stays on fd 2 regardless of the variable)"
        elif [ "${BASH_XTRACEFD:-2}" = "2" ]; then
            _remember_bd_keep_fd2="an xtrace is running on fd 2"
        fi
    fi
    [ "${REMEMBER_TRACE:-}" = "1" ] && _remember_bd_keep_fd2="REMEMBER_TRACE=1"
    if [ -n "$_remember_bd_keep_fd2" ]; then
        printf 'remember: %s, so stderr is NOT being redirected to %s -- point BASH_XTRACEFD at its own fd to get both (#690)\n' \
            "$_remember_bd_keep_fd2" "$REMEMBER_DIR/logs/hook-errors.log" >&2
    else
        exec 2>> "$REMEMBER_DIR/logs/hook-errors.log"
    fi
    unset _remember_bd_keep_fd2
fi
unset _remember_bd_has_xtracefd

}
__remember_src_lib_memory_dir() {
:

[ -n "${_LIB_MEMORY_DIR_LOADED:-}" ] && return 0
_LIB_MEMORY_DIR_LOADED=1

_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
__remember_src_lib_slug ${1+"$@"}
unset _REMEMBER_SRC_DIR


_read_data_dir() {
    local cfg="$1"
    [ -f "$cfg" ] || return 0
    if command -v jq >/dev/null 2>&1; then
        jq -r '.data_dir // empty' "$cfg" 2>/dev/null || true
    else
        grep -o '"data_dir"[[:space:]]*:[[:space:]]*"[^"]*"' "$cfg" 2>/dev/null \
            | sed 's/.*"data_dir"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/'
    fi
}

_resolve_memory_project_dir() {
    local proj="$1"

    if [ -d "$proj/.git" ]; then
        echo "$proj"
        return 0
    fi

    command -v git >/dev/null 2>&1 || { echo "$proj"; return 0; }

    local _out _gcd _gd
    _out=$(git -C "$proj" rev-parse --path-format=absolute \
                --git-common-dir --git-dir 2>/dev/null) || _out=""
    { IFS= read -r _gcd; IFS= read -r _gd; } <<< "$_out"

    if [ -z "$_gcd" ] || [ -z "$_gd" ] || [ "$_gcd" = "$_gd" ]; then
        echo "$proj"
        return 0
    fi

    local _main
    _main=$(dirname "$_gcd")
    if [ -d "$_main" ] && \
       git -C "$_main" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "$_main"
    else
        echo "$proj"
    fi
}

_resolve_remember_dir() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local data_dir="$1"
    local proj="$2"

    if [ "${data_dir#/}" != "$data_dir" ] || [ "${data_dir#[~]}" != "$data_dir" ] \
        || [ "${data_dir#[A-Za-z]:[/\\]}" != "$data_dir" ]; then
        local slug
        slug=$(session_dir_slug "$proj")
        local expanded="${data_dir/#\~/$HOME}"
        echo "${expanded//\{slug\}/$slug}"
    else
        echo "${proj}/${data_dir}"
    fi
}

_set_store_root() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local data_dir="$1" prefix
    REMEMBER_STORE_ROOT=""

    if [ "${data_dir#/}" = "$data_dir" ] && [ "${data_dir#[~]}" = "$data_dir" ] \
        && [ "${data_dir#[A-Za-z]:[/\\]}" = "$data_dir" ]; then
        return 0
    fi
    [ "${data_dir#*\{slug\}}" != "$data_dir" ] || return 0

    prefix="${data_dir%%\{slug\}*}"
    prefix="${prefix/#\~/$HOME}"

    while :; do
        if [ "${#prefix}" -gt 1 ] && { [ "${prefix%/}" != "$prefix" ] || [ "${prefix%\\}" != "$prefix" ]; }; then
            prefix="${prefix%?}"
        else
            break
        fi
    done

    if [ -z "$prefix" ] || [ "$prefix" = / ] || [ -z "${prefix#[A-Za-z]:}" ] \
        || [ -z "${prefix#[A-Za-z]:[/\\]}" ]; then
        return 0
    fi

    REMEMBER_STORE_ROOT="$prefix"
}


_bundled_cfg="${PIPELINE_DIR}/config.json"
_user_cfg="${HOME}/.remember/config.json"

_data_dir_raw=""
for _cfg_candidate in "$_user_cfg" "$_bundled_cfg"; do
    _val=$(_read_data_dir "$_cfg_candidate")
    if [ -n "$_val" ]; then
        _data_dir_raw="$_val"
        break
    fi
done

_data_dir_raw="${_data_dir_raw:-.remember}"

MEMORY_PROJECT_DIR=$(_resolve_memory_project_dir "$PROJECT_DIR")
export MEMORY_PROJECT_DIR

REMEMBER_DIR=$(_resolve_remember_dir "$_data_dir_raw" "$MEMORY_PROJECT_DIR")
export REMEMBER_DIR

_set_store_root "$_data_dir_raw"
export REMEMBER_STORE_ROOT


_project_cfg="${REMEMBER_DIR}/config.json"

_classify_project_cfg_haiku_trust() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    if [ "${_data_dir_raw#/}" != "$_data_dir_raw" ] || [ "${_data_dir_raw#[~]}" != "$_data_dir_raw" ] \
        || [ "${_data_dir_raw#[A-Za-z]:[/\\]}" != "$_data_dir_raw" ]; then
        _project_cfg_haiku_untrusted=0
    else
        _project_cfg_haiku_untrusted=1
    fi
}
_classify_project_cfg_haiku_trust

_remember_config_tracked_status() {
    local _dir="$1" _name="$2" _out _rc _toplevel

    if ! command -v git >/dev/null 2>&1; then
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

    _out=$( (unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
             LC_ALL=C LANGUAGE=C git -C "$_dir" rev-parse --is-inside-work-tree) 2>&1 )
    _rc=$?
    if [ "$_rc" -ne 0 ]; then
        if [ "${_out#*not a git repository}" != "$_out" ]; then
            echo "untracked"
        else
            echo "could-not-tell"
        fi
        return 0
    fi
    if [ "$_out" != "true" ]; then
        echo "untracked"
        return 0
    fi

    _toplevel=$( (unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
                  git -C "$_dir" rev-parse --show-toplevel) 2>/dev/null )
    if [ -n "$_toplevel" ] && [ -L "${_toplevel}/.git" ]; then
        echo "could-not-tell"
        return 0
    fi

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

_project_cfg_model_reject_untrusted=0
if [ "$_project_cfg_haiku_untrusted" = "1" ] && [ -f "$_project_cfg" ]; then
    _project_cfg_tracked_answer=$(_remember_config_tracked_status "${_project_cfg%/*}" "${_project_cfg##*/}") || true
    if [ "$_project_cfg_tracked_answer" = untracked ]; then
        _project_cfg_model_reject_untrusted=0
    else
        _project_cfg_model_reject_untrusted=1  # tracked or could-not-tell -> fail CLOSED
    fi
    unset _project_cfg_tracked_answer
fi

if [ -L "$_project_cfg" ]; then
    printf 'remember: %s is a symlink -- refusing to read it through the link; this project config layer is skipped entirely for this session (#757)\n' \
        "$_project_cfg" >&2
    _project_cfg="${REMEMBER_DIR}/.remember-symlinked-config-refused"
fi

SYS_TMPDIR="${TMPDIR:-/tmp}"
_merged_cfg=$(mktemp "${SYS_TMPDIR}/remember-config-XXXXXX" 2>/dev/null) || _merged_cfg=""

_cfg_sources=()
[ -f "$_bundled_cfg"  ] && _cfg_sources+=("$_bundled_cfg")
[ -f "$_user_cfg"     ] && _cfg_sources+=("$_user_cfg")
[ -f "$_project_cfg"  ] && _cfg_sources+=("$_project_cfg")

if [ -z "$_merged_cfg" ]; then
    :
elif [ "${#_cfg_sources[@]}" -gt 0 ] && command -v jq >/dev/null 2>&1; then
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
    if [ "${#_jq_merge_sources[@]}" -eq 0 ]; then
        echo '{}' > "$_merged_cfg"
    else
        jq -s 'reduce .[] as $x ({}; getpath([]) * $x) | with_entries(select(.key | startswith("_") | not))' "${_jq_merge_sources[@]}" > "$_merged_cfg" 2>/dev/null \
            || cp "$_bundled_cfg" "$_merged_cfg" 2>/dev/null
    fi
    [ -n "$_project_sanitized_tmp" ] && rm -f "$_project_sanitized_tmp"
elif [ "${#_cfg_sources[@]}" -gt 0 ]; then
    declare -f _remember_python >/dev/null 2>&1 && _remember_python
    _untrusted_haiku_source=""
    [ "$_project_cfg_haiku_untrusted" = "1" ] && _untrusted_haiku_source="$_project_cfg"
    _strip_model_reject="0"
    [ "$_project_cfg_model_reject_untrusted" = "1" ] && _strip_model_reject="1"
    _project_drop_marker=$(mktemp "${SYS_TMPDIR}/remember-config-drop-marker-XXXXXX" 2>/dev/null) || _project_drop_marker=""
    rm -f "$_project_drop_marker" 2>/dev/null
    _py_merge_rc=0
    _lmd_run_python() {
        if [ "${PYTHON:-python3}" = python3 ]; then
            python3 "$@"
        elif [ "${PYTHON:-python3}" = python ]; then
            python "$@"
        elif [ "${PYTHON:-python3}" = "py -3" ]; then
            py -3 "$@"
        elif [ "${PYTHON:-python3}" = py ]; then
            py "$@"
        else
            return 127
        fi
    }
    _lmd_py_dir="${BASH_SOURCE[0]%/*}"
    [ "$_lmd_py_dir" = "${BASH_SOURCE[0]}" ] && _lmd_py_dir="$(pwd)"
    _lmd_run_python "$_lmd_py_dir/cfg_merge.py" "$_merged_cfg" "$_untrusted_haiku_source" "$_strip_model_reject" "$_project_drop_marker" "${_cfg_sources[@]}" > /dev/null 2>&1 || _py_merge_rc=$?
    unset _lmd_py_dir
    if [ "$_py_merge_rc" != "0" ] && [ "$_py_merge_rc" != "3" ] && [ "$_py_merge_rc" != "4" ] && [ "$_py_merge_rc" != "5" ]; then
        cp "$_bundled_cfg" "$_merged_cfg" 2>/dev/null
    fi
    if { [ -n "$_project_drop_marker" ] && [ -f "$_project_drop_marker" ]; } || [ "$_py_merge_rc" = "3" ] || [ "$_py_merge_rc" = "5" ]; then
        rm -f "$_project_drop_marker" 2>/dev/null
        if declare -F report_error >/dev/null 2>&1; then
            report_error "lib-memory-dir" "sanitizing the project config layer failed (unreadable project file or malformed JSON) -- that layer was dropped; bundled/user-global config still applies"
        else
            printf '%s\n' "[lib-memory-dir] WARNING: sanitizing the project config layer failed (unreadable project file or malformed JSON) -- that layer was dropped; bundled/user-global config still applies" >&2
        fi
    fi
    if [ "$_py_merge_rc" = "4" ] || [ "$_py_merge_rc" = "5" ]; then
        if declare -F report_error >/dev/null 2>&1; then
            report_error "lib-memory-dir" "sanitizing a trusted config layer failed (unreadable file or malformed JSON) -- bundled config, user-global config, and project config (when it is not the untrusted-haiku source) are all reached here, and one of them was dropped; the remaining layers still applied"
        else
            printf '%s\n' "[lib-memory-dir] WARNING: sanitizing a trusted config layer failed (unreadable file or malformed JSON) -- bundled config, user-global config, and project config (when it is not the untrusted-haiku source) are all reached here, and one of them was dropped; the remaining layers still applied" >&2
        fi
    fi
    [ -n "$_project_drop_marker" ] && rm -f "$_project_drop_marker" 2>/dev/null
else
    cp "$_bundled_cfg" "$_merged_cfg" 2>/dev/null || echo '{}' > "$_merged_cfg"
fi

REMEMBER_CONFIG="$_merged_cfg"
export REMEMBER_CONFIG

_t=$(trap -p EXIT 2>/dev/null)
_existing_trap="${_t#trap -- \'}"
_existing_trap="${_existing_trap%\' EXIT}"
if [ -n "$_existing_trap" ]; then
    trap "${_existing_trap}; rm -f '${_merged_cfg}'" EXIT
else
    trap "rm -f '${_merged_cfg}'" EXIT
fi
unset _existing_trap _t

unset _bundled_cfg _user_cfg _project_cfg _cfg_sources _data_dir_raw _val _merged_cfg _cfg_candidate

}
__remember_src_log() {
:

if [ -z "${PIPELINE_DIR:-}" ]; then
    if [ -n "${PROJECT_DIR:-}" ]; then
        PIPELINE_DIR="${PROJECT_DIR}/.claude/remember"
    else
        PIPELINE_DIR="./.claude/remember"
    fi
fi

_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
__remember_src_lib_memory_dir ${1+"$@"}
unset _REMEMBER_SRC_DIR


REMEMBER_LOG_DIR="${REMEMBER_DIR}/logs"
if [ ! -d "$REMEMBER_LOG_DIR" ] && ! mkdir -p "$REMEMBER_LOG_DIR" 2>/dev/null; then
    echo "FATAL: cannot create $REMEMBER_LOG_DIR" >&2
    return 1 2>/dev/null || true
fi

_REMEMBER_CFG_STATE=""
_REMEMBER_CFG_LOADED_FROM=""

_REMEMBER_CFG_NAMES=()
_REMEMBER_CFG_VALUES=()

_remember_cfg_table_set() {
    _REMEMBER_CFG_NAMES+=("$1")
    _REMEMBER_CFG_VALUES+=("$2")
}

_remember_cfg_table_get_into() {
    local _rcfgtg_i="${#_REMEMBER_CFG_NAMES[@]}"
    while [ "$_rcfgtg_i" -gt 0 ]; do
        _rcfgtg_i=$((_rcfgtg_i - 1))
        if [ "${_REMEMBER_CFG_NAMES[$_rcfgtg_i]}" = "$2" ]; then
            printf -v "$1" '%s' "${_REMEMBER_CFG_VALUES[$_rcfgtg_i]}"
            return 0
        fi
    done
    printf -v "$1" '%s' ""
    return 1
}

_config_is_private_path() {
    if [ "$1" = .haiku ] || [ "${1#.haiku.}" != "$1" ]; then
        return 0
    fi
    return 1
}



_remember_cfg_flatten_cache_path() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    [ -n "${REMEMBER_DIR:-}" ] || return 1
    local _slug="${REMEMBER_DIR//[!a-zA-Z0-9]/-}"
    [ "${#_slug}" -gt 120 ] && _slug="${_slug: -120}"
    printf '%s' "${TMPDIR:-/tmp}/remember-config-cache-v2-${_slug}"
}

_remember_cfg_flatten_cache_sources() {
    printf '%s\n' "${PIPELINE_DIR:-}/config.json"
    printf '%s\n' "${HOME:-}/.remember/config.json"
    printf '%s\n' "${REMEMBER_DIR:-}/config.json"
}

_remember_cfg_flatten_cache_is_standard_merge() {
    local _cfg_path="${REMEMBER_CONFIG:-}"
    if [[ "$_cfg_path" == */remember-config-* ]]; then
        return 0
    fi
    return 1
}

_remember_cfg_flatten_cache_valid_value() {
    local _value="$1"
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    [[ "$_value" =~ ^([^\\]|\\[\\nrt])*$ ]]
}

_remember_cfg_flatten_cache_valid_line() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local _line="$1"
    [[ "$_line" =~ ^_RCFG_[A-Za-z0-9_]+$'\t' ]] || return 1
    _remember_cfg_flatten_cache_valid_value "${_line#*$'\t'}"
}

_remember_cfg_flatten_q_encode() {
    local _rcfgqe_v="$2" _rcfgqe_b _rcfgqe_bb _rcfgqe_n _rcfgqe_r _rcfgqe_t
    printf -v _rcfgqe_b '\134'
    _rcfgqe_bb="$_rcfgqe_b$_rcfgqe_b"
    printf -v _rcfgqe_n '%sn' "$_rcfgqe_b"
    printf -v _rcfgqe_r '%sr' "$_rcfgqe_b"
    printf -v _rcfgqe_t '%st' "$_rcfgqe_b"
    _rcfgqe_v=${_rcfgqe_v//"$_rcfgqe_b"/"$_rcfgqe_bb"}
    _rcfgqe_v=${_rcfgqe_v//$'\n'/"$_rcfgqe_n"}
    _rcfgqe_v=${_rcfgqe_v//$'\r'/"$_rcfgqe_r"}
    _rcfgqe_v=${_rcfgqe_v//$'\t'/"$_rcfgqe_t"}
    printf -v "$1" '%s' "$_rcfgqe_v"
}

_remember_cfg_flatten_q_decode() {
    printf -v "$1" '%b' "$2"
}

_remember_cfg_flatten_cache_load() {
    [ "${REMEMBER_CONFIG_CACHE:-1}" = "1" ] || return 1
    _remember_cfg_flatten_cache_is_standard_merge || return 1
    local _f
    _f=$(_remember_cfg_flatten_cache_path) || return 1
    [ -f "$_f" ] || return 1
    [ -L "$_f" ] && return 1
    [ -O "$_f" ] || return 1
    [ -r "$_f" ] || return 1
    local _src _sources _exists_now=""
    _sources=$(_remember_cfg_flatten_cache_sources)
    while IFS= read -r _src; do
        [ -n "$_src" ] || continue
        if [ -e "$_src" ]; then
            _exists_now="${_exists_now}1"
            [ "$_f" -nt "$_src" ] || return 1
        else
            _exists_now="${_exists_now}0"
        fi
    done <<< "$_sources"

    local _line _lines=() _stage=0 _identity_raw="" _exists_raw=""
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        if [ "$_stage" = "0" ]; then
            _stage=1
            if [ "${_line#'#REMEMBER_DIR='}" != "$_line" ]; then
                _identity_raw="${_line#'#REMEMBER_DIR='}"
                _remember_cfg_flatten_cache_valid_value "$_identity_raw" || {
                    rm -f "$_f" 2>/dev/null
                    return 1
                }
                continue
            else
                rm -f "$_f" 2>/dev/null
                return 1
            fi
        fi
        if [ "$_stage" = "1" ]; then
            _stage=2
            if [ "${_line#'#RCFG_EXISTS='}" != "$_line" ]; then
                _exists_raw="${_line#'#RCFG_EXISTS='}"
                if [[ "$_exists_raw" == *[!01]* ]] || [ -z "$_exists_raw" ]; then
                    rm -f "$_f" 2>/dev/null
                    return 1
                fi
                continue
            else
                rm -f "$_f" 2>/dev/null
                return 1
            fi
        fi
        if ! _remember_cfg_flatten_cache_valid_line "$_line"; then
            rm -f "$_f" 2>/dev/null
            return 1
        fi
        _lines[${#_lines[@]}]="$_line"
    done < "$_f"
    [ "$_stage" = "2" ] || { rm -f "$_f" 2>/dev/null; return 1; }

    [ "$_exists_raw" = "$_exists_now" ] || return 1

    local _identity
    _remember_cfg_flatten_q_decode _identity "$_identity_raw" || {
        rm -f "$_f" 2>/dev/null
        return 1
    }
    [ "$_identity" = "${REMEMBER_DIR:-}" ] || {
        rm -f "$_f" 2>/dev/null
        return 1
    }

    local _assign _assign_name _assign_value _assign_decoded
    for _assign in ${_lines[@]+"${_lines[@]}"}; do
        _assign_name="${_assign%%$'\t'*}"
        _assign_value="${_assign#*$'\t'}"
        _remember_cfg_flatten_q_decode _assign_decoded "$_assign_value" || {
            rm -f "$_f" 2>/dev/null
            return 1
        }
        _remember_cfg_table_set "$_assign_name" "$_assign_decoded"
    done
    return 0
}

_remember_cfg_flatten_cache_publish() {
    [ "${REMEMBER_CONFIG_CACHE:-1}" = "1" ] || return 0
    _remember_cfg_flatten_cache_is_standard_merge || return 0
    local _dump="$1"
    local _f
    _f=$(_remember_cfg_flatten_cache_path) || return 0
    local _dir
    _dir="${_f%/*}"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0
    local _t
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    local _k _v _src _sources _exists_now=""
    _sources=$(_remember_cfg_flatten_cache_sources)
    while IFS= read -r _src; do
        [ -n "$_src" ] || continue
        if [ -e "$_src" ]; then
            _exists_now="${_exists_now}1"
        else
            _exists_now="${_exists_now}0"
        fi
    done <<< "$_sources"
    {
        local _encoded_dir
        _remember_cfg_flatten_q_encode _encoded_dir "${REMEMBER_DIR:-}"
        printf '#REMEMBER_DIR=%s\n' "$_encoded_dir"
        printf '#RCFG_EXISTS=%s\n' "$_exists_now"
        local _encoded_v
        while IFS=$'\t' read -r _k _v; do
            [ -n "$_k" ] || continue
            _remember_cfg_flatten_q_encode _encoded_v "$_v"
            printf '_RCFG_%s\t%s\n' "${_k//./_}" "$_encoded_v"
        done <<< "$_dump"
    } > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t" 2>/dev/null
    return 0
}

_config_load() {
    _REMEMBER_CFG_LOADED_FROM="${REMEMBER_CONFIG:-}"
    if [ ! -f "${REMEMBER_CONFIG:-}" ]; then
        _REMEMBER_CFG_STATE="ok"
        return 0
    fi

    if _remember_cfg_flatten_cache_load; then
        _REMEMBER_CFG_STATE="ok"
        return 0
    fi

    local _dump="" _rc=0
    if command -v jq >/dev/null 2>&1; then
        local _cfg_flatten_jq_dir="${BASH_SOURCE[0]%/*}"
        [ "$_cfg_flatten_jq_dir" = "${BASH_SOURCE[0]}" ] && _cfg_flatten_jq_dir="$(pwd)"
        _dump=$(jq -r -f "$_cfg_flatten_jq_dir/cfg_flatten.jq" "$REMEMBER_CONFIG" 2>/dev/null) || _rc=1
    else
        declare -f _remember_python >/dev/null 2>&1 && _remember_python
        local _cfg_flatten_dir="${BASH_SOURCE[0]%/*}"
        [ "$_cfg_flatten_dir" = "${BASH_SOURCE[0]}" ] && _cfg_flatten_dir="$(pwd)"
        _dump=$(_remember_log_run_python "$_cfg_flatten_dir/cfg_flatten.py" "$REMEMBER_CONFIG" 2>/dev/null) || _rc=1
    fi

    if [ "$_rc" -ne 0 ]; then
        echo "remember: could not read ${REMEMBER_CONFIG} -- is it valid JSON? falling back to per-key reads" >&2
        _REMEMBER_CFG_STATE="fallback"
        return 0
    fi

    if [ "${_dump#\#refuse}" != "$_dump" ]; then
        [ "${REMEMBER_DEBUG:-}" = "1" ] && \
            echo "remember: ${_dump#'#refuse' } -- reading config one key at a time" >&2
        _REMEMBER_CFG_STATE="fallback"
        return 0
    fi

    local _k _v
    while IFS=$'\t' read -r _k _v; do
        [ -n "$_k" ] || continue
        _remember_cfg_table_set "_RCFG_${_k//./_}" "$_v"
    done <<< "$_dump"
    _remember_cfg_flatten_cache_publish "$_dump"
    _REMEMBER_CFG_STATE="ok"
}

config() {
    local _cfg_result
    config_into _cfg_result "$1" "$2"
    printf '%s\n' "$_cfg_result"
}

_remember_log_run_python() {
    if [ "${PYTHON:-python3}" = python3 ]; then
        python3 "$@"
    elif [ "${PYTHON:-python3}" = python ]; then
        python "$@"
    elif [ "${PYTHON:-python3}" = "py -3" ]; then
        py -3 "$@"
    elif [ "${PYTHON:-python3}" = py ]; then
        py "$@"
    else
        return 127
    fi
}

config_into() {
    local _cfg_into_var="$1"
    local _cfg_into_name="$2"
    local _cfg_into_default="$3"

    if ! ( LC_ALL=C; [[ "$_cfg_into_name" =~ ^\.[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$ ]] ); then
        [ "${REMEMBER_DEBUG:-}" = "1" ] && \
            echo "remember: config() key '$_cfg_into_name' is not a plain dotted path -- returning the default rather than looking it up" >&2
        printf -v "$_cfg_into_var" '%s' "$_cfg_into_default"
        return
    fi

    if [ -z "$_REMEMBER_CFG_STATE" ] || \
       [ "$_REMEMBER_CFG_LOADED_FROM" != "${REMEMBER_CONFIG:-}" ]; then
        _config_load
    fi

    if [ "$_REMEMBER_CFG_STATE" = "ok" ] && ! _config_is_private_path "$_cfg_into_name"; then
        local _cfg_into_slot="_RCFG_${_cfg_into_name#.}"
        _cfg_into_slot="${_cfg_into_slot//./_}"
        local _cfg_into_hit
        _remember_cfg_table_get_into _cfg_into_hit "$_cfg_into_slot" || _cfg_into_hit=""
        [ -n "$_cfg_into_hit" ] || _cfg_into_hit="$_cfg_into_default"
        printf -v "$_cfg_into_var" '%s' "$_cfg_into_hit"
        return
    fi

    if [ ! -f "${REMEMBER_CONFIG:-}" ]; then
        printf -v "$_cfg_into_var" '%s' "$_cfg_into_default"
        return
    fi
    local _cfg_into_val=""
    if command -v jq >/dev/null 2>&1; then
        local _cfg_into_prog
        printf -v _cfg_into_prog 'if %s == null then "" else (%s | tostring) end' \
            "$_cfg_into_name" "$_cfg_into_name"
        _cfg_into_val=$(jq -r "$_cfg_into_prog" "$REMEMBER_CONFIG" 2>/dev/null)
    elif type _jq_fallback >/dev/null 2>&1; then
        _cfg_into_val=$(_jq_fallback -r "$_cfg_into_name" "$REMEMBER_CONFIG" 2>/dev/null)
    else
        local _cfg_py_dir="${BASH_SOURCE[0]%/*}"
        [ "$_cfg_py_dir" = "${BASH_SOURCE[0]}" ] && _cfg_py_dir="$(pwd)"
        _cfg_into_val=$(_remember_log_run_python "$_cfg_py_dir/jq_fallback_get.py" "$REMEMBER_CONFIG" "$_cfg_into_name")
    fi
    [ -n "$_cfg_into_val" ] || _cfg_into_val="$_cfg_into_default"
    printf -v "$_cfg_into_var" '%s' "$_cfg_into_val"
}

_config_load


config_into REMEMBER_TZ ".timezone" ""
export REMEMBER_TZ

config_into REMEMBER_PROMPT_STAMP ".prompt_stamp" "full"
if [ "$REMEMBER_PROMPT_STAMP" != stable ] && [ "$REMEMBER_PROMPT_STAMP" != off ]; then
    REMEMBER_PROMPT_STAMP="full"
fi
export REMEMBER_PROMPT_STAMP

config_into REMEMBER_SAVE_COOLDOWN ".cooldowns.save_seconds" 120
if [ -z "$REMEMBER_SAVE_COOLDOWN" ] || [ "${REMEMBER_SAVE_COOLDOWN#*[!0-9]}" != "$REMEMBER_SAVE_COOLDOWN" ]; then REMEMBER_SAVE_COOLDOWN=120; fi
export REMEMBER_SAVE_COOLDOWN

config_into REMEMBER_DELTA_THRESHOLD ".thresholds.delta_lines_trigger" 50
if [ -z "$REMEMBER_DELTA_THRESHOLD" ] || [ "${REMEMBER_DELTA_THRESHOLD#*[!0-9]}" != "$REMEMBER_DELTA_THRESHOLD" ]; then REMEMBER_DELTA_THRESHOLD=50; fi
export REMEMBER_DELTA_THRESHOLD

[ -n "${REMEMBER_MODEL:-}" ] || config_into REMEMBER_MODEL ".model" "haiku"
export REMEMBER_MODEL
[ -n "${REMEMBER_REJECT_PATTERN:-}" ] || config_into REMEMBER_REJECT_PATTERN ".reject_pattern" ""
export REMEMBER_REJECT_PATTERN

_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
__remember_src_lib_clock ${1+"$@"}
unset _REMEMBER_SRC_DIR

MEMORY_LOG_DATE=""
_remember_date_into MEMORY_LOG_DATE +%Y-%m-%d
MEMORY_LOG_FILE="${REMEMBER_LOG_DIR}/memory-${MEMORY_LOG_DATE}.log"

_REMEMBER_LOG_LAST_TIME=""
_remember_date_into _REMEMBER_LOG_LAST_TIME +%H:%M:%S

_REMEMBER_LOG_LAST_EPOCH=""
[ "${BASH_VERSINFO[0]:-0}" -ge 5 ] && _REMEMBER_LOG_LAST_EPOCH="$EPOCHSECONDS"
_REMEMBER_LOG_DAY_SECONDS="${_REMEMBER_LOG_DAY_SECONDS_TEST:-86400}"

log() {
    local component="$1"
    local message="$2"
    local timestamp
    local LC_ALL=C
    _remember_date_into timestamp +%H:%M:%S
    local _remember_log_rolled=0 _remember_log_now_epoch
    if [ -n "$_REMEMBER_LOG_LAST_TIME" ] && [[ "$timestamp" < "$_REMEMBER_LOG_LAST_TIME" ]]; then
        _remember_log_rolled=1
    elif [ -n "$_REMEMBER_LOG_LAST_EPOCH" ]; then
        _remember_log_now_epoch="$EPOCHSECONDS"
        if [ $(( _remember_log_now_epoch - _REMEMBER_LOG_LAST_EPOCH )) -ge "$_REMEMBER_LOG_DAY_SECONDS" ]; then
            _remember_log_rolled=1
        fi
    fi
    if [ "$_remember_log_rolled" = 1 ]; then
        _remember_date_into MEMORY_LOG_DATE +%Y-%m-%d
        MEMORY_LOG_FILE="${REMEMBER_LOG_DIR}/memory-${MEMORY_LOG_DATE}.log"
    fi
    _REMEMBER_LOG_LAST_TIME="$timestamp"
    [ -n "$_REMEMBER_LOG_LAST_EPOCH" ] && _REMEMBER_LOG_LAST_EPOCH="$EPOCHSECONDS"
    message="$(printf '%s' "$message" | LC_ALL=C tr '[:cntrl:]' ' ')"
    echo "${timestamp} [${component}] ${message}" >> "$MEMORY_LOG_FILE" 2>/dev/null \
        || echo "${timestamp} [${component}] ${message}" >&2
}



REMEMBER_HOOKS_DIR="$PIPELINE_DIR/hooks.d"

_DISPATCH_STDERR_LINES=5
_DISPATCH_STDERR_LINE_CHARS=400

_DISPATCH_STDOUT_LINES=200
_DISPATCH_STDOUT_LINE_CHARS=2000
_DISPATCH_STDOUT_PREFIX="[hook] "
_DISPATCH_FRAME="=== hooks.d: "

_DISPATCH_TIMEOUT_DEFAULT=15
_DISPATCH_TIMEOUT_DETACHED_DEFAULT=120

_DISPATCH_DETACHED_EVENTS=" before_save after_save before_consolidate after_consolidate "

_DISPATCH_KILL_GRACE_DEFAULT=5

_dispatch_stderr_excerpt() {
    local _file="$1"
    local _line _kept=0 _dropped=0 _out=""
    while IFS= read -r _line || [ -n "$_line" ]; do
        [ -n "$_line" ] || continue
        if [ "$_kept" -lt "$_DISPATCH_STDERR_LINES" ]; then
            if [ "${#_line}" -gt "$_DISPATCH_STDERR_LINE_CHARS" ]; then
                _line="${_line:0:$_DISPATCH_STDERR_LINE_CHARS} [line truncated]"
            fi
            _out="${_out:+$_out | }$_line"
            _kept=$((_kept + 1))
        else
            _dropped=$((_dropped + 1))
        fi
    done < "$_file"
    if [ "$_kept" -eq 0 ]; then
        printf '%s' "no stderr -- it exited without saying anything"
        return 0
    fi
    [ "$_dropped" -eq 0 ] || _out="$_out [+$_dropped more line(s) not shown]"
    printf '%s' "$_out"
}

_dispatch_stdout_relay() {
    local _file="$1" _event="$2" _name="$3"
    local _line _kept=0 _dropped=0 _framed=0
    while IFS= read -r _line || [ -n "$_line" ]; do
        if [ "$_kept" -lt "$_DISPATCH_STDOUT_LINES" ]; then
            if [ "$_framed" -eq 0 ]; then
                printf '%s%s/%s -- the "%s" lines below are output from a locally installed hook, not from the remember plugin ===\n' \
                    "$_DISPATCH_FRAME" "$_event" "$_name" "$_DISPATCH_STDOUT_PREFIX"
                _framed=1
            fi
            if [ "${#_line}" -gt "$_DISPATCH_STDOUT_LINE_CHARS" ]; then
                _line="${_line:0:$_DISPATCH_STDOUT_LINE_CHARS} [line truncated]"
            fi
            printf '%s%s\n' "$_DISPATCH_STDOUT_PREFIX" "$_line"
            _kept=$((_kept + 1))
        else
            _dropped=$((_dropped + 1))
        fi
    done < "$_file"
    [ "$_dropped" -eq 0 ] || printf '%s%s/%s -- %s line(s) not shown (hook stdout is capped at %s lines) ===\n' \
        "$_DISPATCH_FRAME" "$_event" "$_name" "$_dropped" "$_DISPATCH_STDOUT_LINES"
}

_dispatch_report_failure() {
    local _event="$1" _name="$2" _rc="$3" _why="$4"
    local _msg="ERROR: hook failed: $_event/$_name (exit $_rc): $_why"
    _msg="$(printf '%s' "$_msg" | LC_ALL=C tr '[:cntrl:]' ' ')"
    log "dispatch" "$_msg"
    [ -d "$REMEMBER_DIR/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [dispatch] $_msg" \
        >> "$REMEMBER_DIR/logs/hook-errors.log" 2>/dev/null || true
    return 0
}

_dispatch_report_skip() {
    local _event="$1" _name="$2" _why="$3"
    local _msg="WARNING: hook SKIPPED and did not run: $_event/$_name ($_why) -- it will not run on any later dispatch until this is fixed"
    _msg="$(printf '%s' "$_msg" | LC_ALL=C tr '[:cntrl:]' ' ')"
    log "dispatch" "$_msg"
    [ -d "$REMEMBER_DIR/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [dispatch] $_msg" \
        >> "$REMEMBER_DIR/logs/hook-errors.log" 2>/dev/null || true
    return 0
}

report_error() {
    local _component="$1"
    local _msg
    _msg="$(printf '%s' "$2" | LC_ALL=C tr '[:cntrl:]' ' ')"
    log "$_component" "$_msg"
    [ -d "$REMEMBER_DIR/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [$_component] $_msg" \
        >> "$REMEMBER_DIR/logs/hook-errors.log" 2>/dev/null || true
    return 0
}

_dispatch_report_timeout() {
    local _event="$1" _name="$2" _budget="$3" _how="$4" _said="$5"
    local _msg="WARNING: hook TIMED OUT: $_event/$_name did not return within ${_budget}s and was stopped ($_how). This is NOT a failure report from the hook -- it never answered, so whether it did its work is UNKNOWN, and anything it left half-done is its own to unwind. Raise hooks.dispatch_timeout_seconds if this listener is honestly slow, or 0 to disable the bound. It said: $_said"
    _msg="$(printf '%s' "$_msg" | LC_ALL=C tr '[:cntrl:]' ' ')"
    log "dispatch" "$_msg"
    [ -d "$REMEMBER_DIR/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [dispatch] $_msg" \
        >> "$REMEMBER_DIR/logs/hook-errors.log" 2>/dev/null || true
    return 0
}

_dispatch_supervise() {
    local _pid="$1" _budget="$2" _grace="$3" _sentinel="$4"
    local _wpid=""

    if [ "$_budget" -gt 0 ]; then
        (
            _w=0
            while [ "$_w" -lt "$_budget" ]; do
                kill -0 "$_pid" 2>/dev/null || exit 0
                sleep 1
                _w=$((_w + 1))
            done
            kill -0 "$_pid" 2>/dev/null || exit 0
            [ -z "$_sentinel" ] || : > "$_sentinel" 2>/dev/null || true
            kill -TERM "$_pid" 2>/dev/null || true
            _g=0
            while [ "$_g" -lt "$_grace" ]; do
                kill -0 "$_pid" 2>/dev/null || exit 0
                sleep 1
                _g=$((_g + 1))
            done
            kill -KILL "$_pid" 2>/dev/null || true
        ) </dev/null >/dev/null 2>&1 &
        _wpid=$!
    fi

    if wait "$_pid"; then _DISPATCH_RC=0; else _DISPATCH_RC=$?; fi

    if [ -n "$_wpid" ]; then
        kill "$_wpid" 2>/dev/null || true
        wait "$_wpid" 2>/dev/null || true
    fi

    _DISPATCH_TIMEDOUT=0
    if [ -n "$_sentinel" ]; then
        if [ -f "$_sentinel" ]; then
            _DISPATCH_TIMEDOUT=1
            rm -f "$_sentinel" 2>/dev/null || true
        fi
    elif [ "$_budget" -gt 0 ]; then
        if [ "$_DISPATCH_RC" = 143 ] || [ "$_DISPATCH_RC" = 137 ]; then
            _DISPATCH_TIMEDOUT=1
        fi
    fi
    return 0
}

dispatch() {
    local event="$1"
    local event_dir="$REMEMBER_HOOKS_DIR/$event"
    [ -d "$event_dir" ] || return 0
    local current_uid="$EUID"
    local _err_file="" _out_file="" _err_unavailable=""
    local _budget="" _grace="" _to_file=""
    for hook in "$event_dir"/*; do
        [ -x "$hook" ] || continue
        if [ -z "$_budget" ]; then
            if [ "${_DISPATCH_DETACHED_EVENTS#*" $event "}" != "$_DISPATCH_DETACHED_EVENTS" ]; then
                _budget=$(config '.hooks.dispatch_timeout_detached_seconds' "$_DISPATCH_TIMEOUT_DETACHED_DEFAULT")
                _DISPATCH_BUDGET_FALLBACK=$_DISPATCH_TIMEOUT_DETACHED_DEFAULT
            else
                _budget=$(config '.hooks.dispatch_timeout_seconds' "$_DISPATCH_TIMEOUT_DEFAULT")
                _DISPATCH_BUDGET_FALLBACK=$_DISPATCH_TIMEOUT_DEFAULT
            fi
            if [ -z "$_budget" ] || [ "${_budget#*[!0-9]}" != "$_budget" ]; then
                _budget=$_DISPATCH_BUDGET_FALLBACK
            fi
            _grace=$(config '.hooks.dispatch_kill_grace_seconds' "$_DISPATCH_KILL_GRACE_DEFAULT")
            if [ -z "$_grace" ] || [ "${_grace#*[!0-9]}" != "$_grace" ]; then
                _grace=$_DISPATCH_KILL_GRACE_DEFAULT
            fi
        fi
        local hook_stat hook_uid hook_perm
        hook_stat=$(stat -c '%u %a' "$hook" 2>/dev/null || stat -f '%u %Lp' "$hook" 2>/dev/null || echo "")
        hook_uid="${hook_stat%% *}"
        hook_perm="${hook_stat#* }"
        if [ -z "$hook_stat" ] || [ "$hook_uid" != "$current_uid" ]; then
            _dispatch_report_skip "$event" "${hook##*/}" "not owned by the current user"
            continue
        fi
        if [ -n "$hook_perm" ] && [ -z "${hook_perm//[0-7]/}" ]; then
            if [ $(( 8#$hook_perm & 2 )) -ne 0 ]; then
                _dispatch_report_skip "$event" "${hook##*/}" "world-writable"
                continue
            fi
        fi
        if [ -z "$_err_file" ] && [ -z "$_err_unavailable" ]; then
            _err_file="$REMEMBER_DIR/tmp/dispatch-stderr.$event.$$"
            _out_file="$REMEMBER_DIR/tmp/dispatch-stdout.$event.$$"
            [ -d "$REMEMBER_DIR/tmp" ] || mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            if ! : > "$_err_file" 2>/dev/null || ! : > "$_out_file" 2>/dev/null; then
                _err_file=""
                _out_file=""
                _err_unavailable="yes"
            else
                _to_file="$REMEMBER_DIR/tmp/dispatch-timeout.$$"
                rm -f "$_to_file" 2>/dev/null || true
            fi
        fi

        local _rc=0 _hpid=""
        _DISPATCH_RC=0
        _DISPATCH_TIMEDOUT=0
        if [ -n "$_err_file" ]; then
            REMEMBER_PROJECT="${PROJECT_DIR:-.}" "$hook" >"$_out_file" 2>"$_err_file" &
            _hpid=$!
            _dispatch_supervise "$_hpid" "$_budget" "$_grace" "$_to_file"
            _rc=$_DISPATCH_RC
            _dispatch_stdout_relay "$_out_file" "$event" "${hook##*/}"
        else
            REMEMBER_PROJECT="${PROJECT_DIR:-.}" "$hook" >/dev/null 2>/dev/null &
            _hpid=$!
            _dispatch_supervise "$_hpid" "$_budget" "$_grace" ""
            _rc=$_DISPATCH_RC
            printf '%s%s/%s -- output NOT SHOWN: stdout could not be captured (no writable %s/tmp), so it was discarded rather than delivered unattributed ===\n' \
                "$_DISPATCH_FRAME" "$event" "${hook##*/}" "$REMEMBER_DIR"
        fi

        if [ "$_DISPATCH_TIMEDOUT" -eq 1 ]; then
            local _how="SIGTERM, then SIGKILL after ${_grace}s if it was still there"
            [ -n "$_to_file" ] || _how="$_how; inferred from the exit status because $REMEMBER_DIR/tmp is not writable, so a hook that genuinely exited on this signal would look the same"
            local _said
            if [ -n "$_err_file" ]; then
                _said=$(_dispatch_stderr_excerpt "$_err_file")
            else
                _said="nothing captured -- no writable $REMEMBER_DIR/tmp"
            fi
            _dispatch_report_timeout "$event" "${hook##*/}" "$_budget" "$_how" "$_said"
            continue
        fi

        [ "$_rc" -eq 0 ] && continue

        local _why
        if [ -n "$_err_file" ]; then
            _why=$(_dispatch_stderr_excerpt "$_err_file")
        else
            _why="stderr not captured -- no writable $REMEMBER_DIR/tmp, so the reason is MISSING, not absent; rerun the hook by hand to see what it says"
        fi
        _dispatch_report_failure "$event" "${hook##*/}" "$_rc" "$_why"
    done
    [ -z "$_err_file" ] || rm -f "$_err_file" "$_out_file" 2>/dev/null
    [ -z "$_to_file" ] || rm -f "$_to_file" 2>/dev/null
    return 0
}

_ROTATE_ESCALATE_AFTER=3

_ROTATE_STATE_NAME=".rotate-failed"

_ROTATE_MAX_PARTS=100



}
__remember_src_lib_clock() {
:

[ -n "${_REMEMBER_LIB_CLOCK_LOADED:-}" ] && return 0
_REMEMBER_LIB_CLOCK_LOADED=1

if [ "${BASH_VERSINFO[0]:-0}" -gt 4 ] 2>/dev/null; then
    _REMEMBER_PRINTF_T=1
elif [ "${BASH_VERSINFO[0]:-0}" -eq 4 ] 2>/dev/null && [ "${BASH_VERSINFO[1]:-0}" -ge 2 ] 2>/dev/null; then
    _REMEMBER_PRINTF_T=1
else
    _REMEMBER_PRINTF_T=0
fi
[ "${REMEMBER_NO_PRINTF_T:-0}" = "1" ] && _REMEMBER_PRINTF_T=0

_remember_date_builtin_ok() {
    if [[ "$1" == *"%-"* ]] || [[ "$1" == *"%_"* ]] || [[ "$1" == *"%0"* ]] \
        || [[ "$1" == *"%^"* ]] || [[ "$1" == *"%#"* ]]; then
        return 1
    fi
    return 0
}

_remember_date() {
    if [ -n "${REMEMBER_TZ:-}" ]; then
        TZ="$REMEMBER_TZ" date "$@"
        return
    fi
    if [ "$_REMEMBER_PRINTF_T" = "1" ] && [ "$#" -eq 1 ] \
        && _remember_date_builtin_ok "$1"; then
        printf "%(${1#+})T\n" -1 && return
    fi
    date "$@"
}

_remember_date_into() {
    local _var="$1"
    shift
    if [ -z "${REMEMBER_TZ:-}" ] && [ "$_REMEMBER_PRINTF_T" = "1" ] && [ "$#" -eq 1 ] \
        && _remember_date_builtin_ok "$1"; then
        printf -v "$_var" "%(${1#+})T" -1
        return
    fi
    local _val
    _val=$(_remember_date "$@")
    printf -v "$_var" '%s' "$_val"
}

}
__remember_src_lib_env_cache() {
:

[ -n "${_REMEMBER_LIB_ENV_CACHE_LOADED:-}" ] && return 0
_REMEMBER_LIB_ENV_CACHE_LOADED=1

_remember_env_cache_normalize_into() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local _var="$1" _in="$2" _drive="" _rest=""
    local _re='^([a-zA-Z]):[/\](.*)$'
    if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
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
            _drive=$(printf '%s' "$_drive" | LC_ALL=C tr '[:lower:]' '[:upper:]')
            _rest="${_rest//\//\\}"
            printf -v "$_var" '%s:\\%s' "$_drive" "$_rest"
            return 0
        fi
    fi
    printf -v "$_var" '%s' "$_in"
}

_remember_env_cache_path() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    if [ -n "${_REMEMBER_ENV_CACHE_PROJECT_DIR:-}" ]; then
        return 0
    fi
    local _slug="${CLAUDE_PROJECT_DIR:-}"
    [ -n "$_slug" ] || _slug="${REMEMBER_HOOK_CWD:-}"
    [ -n "$_slug" ] || return 1
    _remember_env_cache_normalize_into _slug "$_slug"
    _REMEMBER_ENV_CACHE_PROJECT_DIR="$_slug"
    _slug="${_slug//[!a-zA-Z0-9]/-}"
    [ "${#_slug}" -gt 120 ] && _slug="${_slug: -120}"
    _REMEMBER_ENV_CACHE_FILE="${TMPDIR:-/tmp}/remember-env-${_slug}"
    return 0
}


_remember_env_cache_publish() {
    [ "${REMEMBER_ENV_CACHE:-1}" = "1" ] || return 0
    [ -n "${CLAUDE_PROJECT_DIR:-}${REMEMBER_HOOK_CWD:-}" ] || return 0
    [ -n "${REMEMBER_DIR:-}" ] || return 0
    [ -n "${PROJECT_DIR:-}" ] || return 0
    [ -n "${PIPELINE_DIR:-}" ] || return 0
    [ -n "${REMEMBER_SAVE_COOLDOWN:-}" ] || return 0
    [ -n "${REMEMBER_DELTA_THRESHOLD:-}" ] || return 0
    _remember_env_cache_path || return 0

    local _f="$_REMEMBER_ENV_CACHE_FILE" _t
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    {
        printf 'CACHE_ENV_PROJECT_DIR=%s\n' "$_REMEMBER_ENV_CACHE_PROJECT_DIR"
        printf 'CACHE_ENV_PLUGIN_ROOT=%s\n' "${CLAUDE_PLUGIN_ROOT:-}"
        printf 'CACHE_ENV_HOME=%s\n' "${HOME:-}"
        printf 'PROJECT_DIR=%s\n' "$PROJECT_DIR"
        printf 'PIPELINE_DIR=%s\n' "$PIPELINE_DIR"
        printf 'REMEMBER_DIR=%s\n' "$REMEMBER_DIR"
        printf 'REMEMBER_TZ=%s\n' "${REMEMBER_TZ:-}"
        printf 'REMEMBER_PROMPT_STAMP=%s\n' "${REMEMBER_PROMPT_STAMP:-full}"
        printf 'REMEMBER_SAVE_COOLDOWN=%s\n' "$REMEMBER_SAVE_COOLDOWN"
        printf 'REMEMBER_DELTA_THRESHOLD=%s\n' "$REMEMBER_DELTA_THRESHOLD"
        local _ecp_mem_proj="${MEMORY_PROJECT_DIR:-}"
        [ -n "$_ecp_mem_proj" ] || _ecp_mem_proj="$PROJECT_DIR"
        printf 'MEMORY_PROJECT_DIR=%s\n' "$_ecp_mem_proj"
        local _c
        for _c in "${PIPELINE_DIR}/config.json" "${HOME:-}/.remember/config.json" \
                  "${REMEMBER_DIR}/config.json"; do
            if [ -e "$_c" ]; then
                printf 'CACHE_CONFIG=1:%s\n' "$_c"
            else
                printf 'CACHE_CONFIG=0:%s\n' "$_c"
            fi
        done
    } > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t" 2>/dev/null
    return 0
}

}
__remember_src_lib_memory_context() {
:

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
    local _ls=" $_REMEMBER_REFUSED_TRACKED_STATES "
    [[ "$_ls" == *" $1 "* ]]
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
        if [ "$_mi_state" = tracked ]; then
            _REMEMBER_INJECT_REFUSAL="$_mi_file is tracked by this repository's own git index. This plugin never commits a memory file itself (.remember/.gitignore excludes the whole directory), so a tracked one was shipped by the repository, not written by your own /remember. Not injecting it. If it is genuinely yours: git rm --cached it. If you did not add it: delete it and consider what else the commit that added it changed."
            log "$_mi_component" "refused injecting $_mi_file: git-tracked"
        elif [ "$_mi_state" = unavailable ]; then
            _REMEMBER_INJECT_REFUSAL="$_mi_file could not be checked against this repository's git index (git is missing, or the check itself failed) -- refusing rather than injecting unverified. Run /remember:doctor to see why git could not be asked."
            log "$_mi_component" "refused injecting $_mi_file: git status unavailable"
        elif [ "$_mi_state" = symlinked-ancestor ]; then
            _REMEMBER_INJECT_REFUSAL="$_mi_file sits under a directory that is itself a symlink -- refusing to follow it into session context. This plugin never creates a symlink inside a memory store; if you did not create this one, treat it as planted and inspect what it points at before deleting it."
            log "$_mi_component" "refused injecting $_mi_file: symlinked ancestor directory"
        else
            _REMEMBER_INJECT_REFUSAL="$_mi_file could not be verified (tracked state: $_mi_state) -- refusing rather than injecting unverified."
            log "$_mi_component" "refused injecting $_mi_file: unrecognised refused state $_mi_state"
        fi
        return 1
    fi
    return 0
}

_remember_emit_file() {
    local _remember_emit_max="${REMEMBER_EMIT_READ_MAX:-16384}"
    if [ -z "$_remember_emit_max" ] || [ "${_remember_emit_max#*[!0-9]}" != "$_remember_emit_max" ]; then _remember_emit_max=16384; fi
    local _sz="${2:-}"
    if [ -z "$_sz" ] || [[ "$_sz" == *[!0-9]* ]]; then
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
            if [ -z "$MFILE_BYTES" ] || [[ "$MFILE_BYTES" == *[!0-9]* ]]; then
                printf '%s (size unknown)\n' "$MFILE"
            else
                printf '%s (%s bytes)\n' "$MFILE" "$MFILE_BYTES"
            fi
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

}
__remember_src_lib_lock() {
:

[ -n "${_REMEMBER_LIB_LOCK_SOURCED:-}" ] && return 0
_REMEMBER_LIB_LOCK_SOURCED=1

if sleep 0.001 2>/dev/null; then
    _LOCK_SLEEP=0.05
else
    _LOCK_SLEEP=1
fi

_lock_self_set() {
    if [ -n "${BASHPID:-}" ]; then
        _LOCK_SELF="$BASHPID"
        return 0
    fi
    local _probe
    _probe=$(mktemp "${TMPDIR:-/tmp}/remember-lockself-XXXXXX" 2>/dev/null) || {
        _LOCK_SELF="$$"
        return 0
    }
    sh -c 'echo $PPID' > "$_probe" 2>/dev/null
    _LOCK_SELF=$(cat "$_probe" 2>/dev/null) || true
    rm -f "$_probe" 2>/dev/null || true
    if [ -z "$_LOCK_SELF" ] || [ "${_LOCK_SELF#*[!0-9]}" != "$_LOCK_SELF" ]; then
        _LOCK_SELF="$$"
    fi
    return 0
}

_lock_try_steal() {
    local _dir="$1" _pid _seen _abandoned _owner _claim
    _lock_self_set
    _claim="${_dir}/pid.stealing.${_LOCK_SELF}"

    if [ ! -e "${_dir}/pid" ]; then
        for _abandoned in "${_dir}"/pid.stealing.*; do
            [ -e "$_abandoned" ] || continue
            _owner="${_abandoned##*.}"
            if [[ "$_owner" == *[!0-9]* ]]; then
                continue
            fi
            kill -0 "$_owner" 2>/dev/null && continue
            if [ ! -e "${_dir}/pid" ]; then
                mv "$_abandoned" "${_dir}/pid" 2>/dev/null || true
            else
                rm -f "$_abandoned" 2>/dev/null || true
            fi
        done
    fi

    _pid=$(cat "${_dir}/pid" 2>/dev/null) || true

    [ -z "$_pid" ] && return 1
    if [[ "$_pid" == *[!0-9]* ]]; then
        return 1
    fi
    kill -0 "$_pid" 2>/dev/null && return 1

    mv "${_dir}/pid" "$_claim" 2>/dev/null || return 1

    _seen=$(cat "$_claim" 2>/dev/null) || true
    if [ "$_seen" != "$_pid" ]; then
        mv "$_claim" "${_dir}/pid" 2>/dev/null || true
        return 1
    fi

    echo "$_LOCK_SELF" > "${_dir}/pid" 2>/dev/null || true
    rm -f "$_claim" 2>/dev/null || true
    return 0
}

_LOCK_ADOPT_AFTER="${_LOCK_ADOPT_AFTER:-30}"

_lock_dir_age() {
    local _mtime _now
    _mtime=$(stat -c %Y "$1" 2>/dev/null) || _mtime=""
    if [ -z "$_mtime" ] || [ "${_mtime#*[!0-9]}" != "$_mtime" ]; then
        _mtime=$(stat -f %m "$1" 2>/dev/null) || _mtime=""
    fi
    if [ -z "$_mtime" ] || [ "${_mtime#*[!0-9]}" != "$_mtime" ]; then
        echo 0; return 0
    fi
    _now=$(date +%s)
    echo $(( _now - 10#$_mtime ))
}

_lock_try_adopt() {
    local _dir="$1" _claim _owns_marker=0 _adopted=0
    [ -e "${_dir}/pid" ] && return 1
    for _claim in "${_dir}"/pid.stealing.*; do
        [ -e "$_claim" ] && return 1
    done
    [ "$(_lock_dir_age "$_dir")" -lt "$_LOCK_ADOPT_AFTER" ] && return 1

    if ! mkdir "${_dir}/adopt" 2>/dev/null; then
        [ "$(_lock_dir_age "${_dir}/adopt")" -lt "$_LOCK_ADOPT_AFTER" ] && return 1
        mv "${_dir}/adopt" "${_dir}/adopt.dead.$$" 2>/dev/null || return 1
        rm -rf "${_dir}/adopt.dead.$$" 2>/dev/null || true
    else
        _owns_marker=1
    fi

    _lock_self_set

    ( set -o noclobber; echo "$_LOCK_SELF" > "${_dir}/pid" ) 2>/dev/null && _adopted=1

    [ "$_owns_marker" = 1 ] && rmdir "${_dir}/adopt" 2>/dev/null
    [ "$_adopted" = 1 ] || return 1
    return 0
}

_LOCK_TIMING="${REMEMBER_LOCK_TIMING:-0}"
_LOCK_TIMING_MAX="${REMEMBER_LOCK_TIMING_MAX:-5000}"
_LOCK_TIMING_PRECISION=""
_LOCK_TIMING_FILE=""
_LOCK_TIMING_NOW=0
_LOCK_TIMING_DISCLOSED=0
_LOCK_TIMING_SLOTS=()
_LOCK_TIMING_T0S=()
_LOCK_TIMING_WAITS=()
_LOCK_TIMING_IDX=-1

_lock_timing_has_ns_date() {
    local _n
    _n=$(date +%s%N 2>/dev/null) || return 1
    if [ -z "$_n" ] || [ "${_n#*[!0-9]}" != "$_n" ]; then
        return 1
    fi
    [ "${#_n}" -ge 16 ] || return 1
    return 0
}

if [ "$_LOCK_TIMING" = 1 ]; then
    if [ "${BASH_VERSINFO[0]:-0}" -ge 5 ] && [ -n "${EPOCHREALTIME:-}" ]; then
        _LOCK_TIMING_PRECISION="us"
    elif _lock_timing_has_ns_date; then
        _LOCK_TIMING_PRECISION="ms"
    else
        _LOCK_TIMING_PRECISION="s"
    fi
fi


_lock_timing_us_to_ms() {
    local _r="$1" _s _f
    if [[ "$_r" == *[.,]* ]]; then
        _s="${_r%%[.,]*}"; _f="${_r#*[.,]}"
    else
        _s="$_r"; _f="000000"
    fi
    if [ -z "$_s" ] || [ "${_s#*[!0-9]}" != "$_s" ]; then
        _LOCK_TIMING_NOW=0; return 0
    fi
    if [[ "$_f" == *[!0-9]* ]]; then
        _f="000000"
    fi
    _f="${_f}000"
    _LOCK_TIMING_NOW=$(( 10#$_s * 1000 + 10#${_f:0:3} ))
    return 0
}

_lock_timing_ns_to_ms() {
    if [ -z "$1" ] || [ "${1#*[!0-9]}" != "$1" ]; then
        _LOCK_TIMING_NOW=0; return 0
    fi
    _LOCK_TIMING_NOW=$(( 10#$1 / 1000000 ))
    return 0
}

_lock_timing_s_to_ms() {
    if [ -z "$1" ] || [ "${1#*[!0-9]}" != "$1" ]; then
        _LOCK_TIMING_NOW=0; return 0
    fi
    _LOCK_TIMING_NOW=$(( 10#$1 * 1000 ))
    return 0
}

_lock_timing_now() {
    local _n
    if [ "$_LOCK_TIMING_PRECISION" = us ]; then
        _lock_timing_us_to_ms "$EPOCHREALTIME"
    elif [ "$_LOCK_TIMING_PRECISION" = ms ]; then
        _n=$(date +%s%N 2>/dev/null) || _n=""
        _lock_timing_ns_to_ms "$_n"
    else
        _n=$(date +%s 2>/dev/null) || _n=""
        _lock_timing_s_to_ms "$_n"
    fi
    return 0
}

_lock_timing_target() {
    if [ -n "${REMEMBER_LOCK_TIMING_FILE:-}" ]; then
        _LOCK_TIMING_FILE="$REMEMBER_LOCK_TIMING_FILE"
    elif [ -n "${REMEMBER_DIR:-}" ]; then
        _LOCK_TIMING_FILE="${REMEMBER_DIR}/logs/lock-timing.tsv"
    else
        _LOCK_TIMING_FILE=""
    fi
}

_lock_timing_disclose() {
    if [ "$_LOCK_TIMING_DISCLOSED" = 1 ]; then
        return 0
    fi
    _LOCK_TIMING_DISCLOSED=1
    if declare -F log >/dev/null 2>&1; then
        log "lock-timing" "$1" || printf 'remember lock-timing: %s\n' "$1" >&2
    else
        printf 'remember lock-timing: %s\n' "$1" >&2
    fi
    return 0
}

_lock_timing_slot() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    _LOCK_TIMING_SLOT="${1//[!A-Za-z0-9]/_}"
}

_lock_timing_find() {
    local _i _n="${#_LOCK_TIMING_SLOTS[@]}"
    _LOCK_TIMING_IDX=-1
    for ((_i = 0; _i < _n; _i++)); do
        if [ "${_LOCK_TIMING_SLOTS[_i]}" = "$_LOCK_TIMING_SLOT" ]; then
            _LOCK_TIMING_IDX=$_i
            return 0
        fi
    done
    return 1
}

_lock_timing_record() {
    local _name="${1##*/}" _dir _n _lock_timing_pid
    _lock_timing_target
    if [ -z "$_LOCK_TIMING_FILE" ]; then
        _lock_timing_disclose "REMEMBER_LOCK_TIMING=1 but neither REMEMBER_LOCK_TIMING_FILE nor REMEMBER_DIR is set -- nothing is being recorded"
        return 0
    fi
    if [ -e "${_LOCK_TIMING_FILE}.capped" ]; then
        return 0
    fi

    _dir="${_LOCK_TIMING_FILE%/*}"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || true

    if [ -f "$_LOCK_TIMING_FILE" ]; then
        _n=$(wc -l < "$_LOCK_TIMING_FILE" 2>/dev/null | tr -d ' ')
        if [ -z "$_n" ] || [ "${_n#*[!0-9]}" != "$_n" ]; then
            _n=0
        fi
        if [ "$_n" -ge "$_LOCK_TIMING_MAX" ]; then
            { printf '# CAPPED\t%s lines, REMEMBER_LOCK_TIMING_MAX=%s reached -- recording STOPPED here. Nothing was rolled or overwritten, so every record above is real; the distribution below this point is simply missing. Raise the cap or move this file to keep measuring.\n' \
                "$_n" "$_LOCK_TIMING_MAX" >> "$_LOCK_TIMING_FILE"; } 2>/dev/null \
                || _lock_timing_disclose "could not append the cap notice to $_LOCK_TIMING_FILE"
            { : > "${_LOCK_TIMING_FILE}.capped"; } 2>/dev/null || true
            _lock_timing_disclose "$_LOCK_TIMING_FILE reached REMEMBER_LOCK_TIMING_MAX=$_LOCK_TIMING_MAX lines -- recording stopped, nothing rolled"
            return 0
        fi
    else
        { printf '# ts_ms\tlock\tevent\toutcome\twait_ms\theld_ms\tprecision\tpid\n' >> "$_LOCK_TIMING_FILE"; } 2>/dev/null \
            || _lock_timing_disclose "could not create $_LOCK_TIMING_FILE"
    fi

    _lock_timing_pid="${BASHPID:-}"
    [ -n "$_lock_timing_pid" ] || _lock_timing_pid="$$"
    { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$_LOCK_TIMING_NOW" "$_name" "$2" "$3" "$4" "$5" \
        "$_LOCK_TIMING_PRECISION" "$_lock_timing_pid" >> "$_LOCK_TIMING_FILE"; } 2>/dev/null \
        || _lock_timing_disclose "could not append to $_LOCK_TIMING_FILE"
    return 0
}

lock_acquire() {
    local _t0 _waited
    if [ "$_LOCK_TIMING" != 1 ]; then
        _lock_acquire_impl "$@"
        return $?
    fi
    _lock_timing_now
    _t0="$_LOCK_TIMING_NOW"
    if _lock_acquire_impl "$@"; then
        _lock_timing_now
        _waited=$(( _LOCK_TIMING_NOW - _t0 ))
        _lock_timing_slot "$1"
        _lock_timing_find || _LOCK_TIMING_IDX="${#_LOCK_TIMING_SLOTS[@]}"
        _LOCK_TIMING_SLOTS[_LOCK_TIMING_IDX]="$_LOCK_TIMING_SLOT"
        _LOCK_TIMING_T0S[_LOCK_TIMING_IDX]="$_LOCK_TIMING_NOW"
        _LOCK_TIMING_WAITS[_LOCK_TIMING_IDX]="$_waited"
        return 0
    fi
    _lock_timing_now
    _lock_timing_record "$1" acquire timeout "$(( _LOCK_TIMING_NOW - _t0 ))" "-"
    return 1
}

_lock_acquire_impl() {
    local _dir="$1" _timeout="${2:-0}" _deadline _legacy
    _deadline=$(( $(date +%s) + _timeout ))

    mkdir -p "$(dirname "$_dir")" 2>/dev/null || true

    while :; do
        if mkdir "$_dir" 2>/dev/null; then
            _lock_self_set
            echo "$_LOCK_SELF" > "${_dir}/pid" 2>/dev/null || true
            return 0
        fi

        if [ -f "$_dir" ]; then
            _legacy=$(cat "$_dir" 2>/dev/null) || true
            if [ -z "$_legacy" ] || [ "${_legacy#*[!0-9]}" != "$_legacy" ]; then
                rm -f "$_dir" 2>/dev/null || true; continue
            fi
            if ! kill -0 "$_legacy" 2>/dev/null; then
                rm -f "$_dir" 2>/dev/null || true
                continue
            fi
        elif { [ -e "$_dir" ] || [ -L "$_dir" ]; } && [ ! -d "$_dir" ]; then
            rm -f "$_dir" 2>/dev/null || true
            continue
        elif _lock_try_steal "$_dir"; then
            return 0
        elif _lock_try_adopt "$_dir"; then
            return 0
        fi

        [ "$(date +%s)" -ge "$_deadline" ] && return 1
        sleep "$_LOCK_SLEEP"
    done
}

lock_release() {
    local _t0 _wait
    if [ "$_LOCK_TIMING" != 1 ]; then
        _lock_release_impl "$@"
        return $?
    fi
    _lock_release_impl "$@" || return 1
    _lock_timing_now
    _lock_timing_slot "$1"
    _t0=""
    _wait=""
    if _lock_timing_find; then
        _t0="${_LOCK_TIMING_T0S[_LOCK_TIMING_IDX]}"
        _wait="${_LOCK_TIMING_WAITS[_LOCK_TIMING_IDX]}"
    fi
    if [ -z "$_t0" ]; then
        _lock_timing_record "$1" release unpaired "-" "-"
        return 0
    fi
    _LOCK_TIMING_T0S[_LOCK_TIMING_IDX]=""
    _LOCK_TIMING_WAITS[_LOCK_TIMING_IDX]=""
    _lock_timing_record "$1" release ok "$_wait" "$(( _LOCK_TIMING_NOW - _t0 ))"
    return 0
}

_lock_release_impl() {
    local _dir="$1" _pid
    _pid=$(cat "${_dir}/pid" 2>/dev/null) || true

    _lock_self_set
    if [ -n "$_pid" ] && [ "$_pid" != "$_LOCK_SELF" ]; then
        return 1
    fi
    rm -rf "$_dir" 2>/dev/null || true
    return 0
}

}
__remember_src_lib_case_divergence() {
:

[ -n "${_REMEMBER_LIB_CASE_DIVERGENCE_LOADED:-}" ] && return 0
_REMEMBER_LIB_CASE_DIVERGENCE_LOADED=1

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

    [ -n "${REMEMBER_STORE_ROOT:-}" ] || return 0
    [ -n "${REMEMBER_DIR:-}" ] || return 0

    local _remember_case_div_dir _root _name
    _remember_case_div_dir=$(_remember_forward_slash "$REMEMBER_DIR")
    _root="${_remember_case_div_dir%/*}" _name="${_remember_case_div_dir##*/}"
    [ -n "$_root" ] && [ "$_root" != "$_remember_case_div_dir" ] || return 0
    [ -n "$_name" ] || return 0

    [ "$_root" != "$(_remember_forward_slash "${PROJECT_DIR:-}")" ] || return 0

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

_remember_case_probe_disk() {
    local _root="$1" _name="$2" _entry _base

    if [ ! -d "$_root" ] || [ ! -r "$_root" ] || [ ! -x "$_root" ]; then
        REMEMBER_CASE_DISK_STATE="unavailable"
        REMEMBER_CASE_DISK_REASON="store-root-unreadable"
        return 0
    fi

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

_remember_case_probe_git() {
    local _root="$1" _name="$2" _out _line _matched=0

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
    done <<< "$_out"

    if [ -n "$REMEMBER_CASE_GIT_NAMES" ]; then
        REMEMBER_CASE_GIT_STATE="diverged"
    elif [ "$_matched" -eq 1 ]; then
        REMEMBER_CASE_GIT_STATE="ok"
    else
        REMEMBER_CASE_GIT_STATE="unavailable"
        REMEMBER_CASE_GIT_REASON="store-not-tracked"
    fi
    return 0
}

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

    REMEMBER_CASE_MESSAGE="remember: ${_lead} -- spellings that differ only in case. On a case-insensitive filesystem those are one and the same, which is why memory is being read and written normally and there is nothing to fix today. It would matter on a restore: checked out onto a case-sensitive filesystem they become separate directories, and this plugin would use only ${REMEMBER_CASE_RESOLVED}. Nothing here will rename or merge anything for you -- if you keep a backup of this store, check which spellings its HEAD holds before restoring it somewhere case-sensitive. Run /remember:doctor to see this again."
    return 0
}

}

_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="$(pwd)"

[ -n "${REMEMBER_NESTED_SUMMARIZER:-}" ] && exit 0

_REMEMBER_HOOK_T0=""
if [ "${BASH_VERSINFO[0]:-0}" -ge 5 ] && [ "${_REMEMBER_HOOK_FORCE_DATE_FALLBACK:-0}" != "1" ]; then
    _REMEMBER_HOOK_T0="$EPOCHSECONDS"
else
    _REMEMBER_HOOK_T0=$(date +%s 2>/dev/null) || _REMEMBER_HOOK_T0=""
fi

HOOK_STDIN=""
if [ ! -t 0 ]; then
    _line=""
    while IFS= read -r -t 1 _line || [ -n "$_line" ]; do
        HOOK_STDIN="$HOOK_STDIN$_line"
        _line=""
    done
fi

_stdin_json_string() {
    local field="$1" raw="$2" rest prefix value dq
    printf -v dq '\042'
    [[ "$raw" == *"$dq$field$dq"* ]] || return 1
    rest=${raw#*"$dq"$field"$dq"}
    prefix=${rest%%"$dq"*}
    if [[ "$prefix" == *[!:[:space:]]* ]]; then return 1; fi
    value=${rest#*"$dq"}
    value=${value%%"$dq"*}
    value=${value//\\\\/\\}
    [ -n "$value" ] || return 1
    printf '%s' "$value"
}

_stdin_json_string_into() {
    local _sjsi_var="$1" _sjsi_field="$2" _sjsi_raw="$3" _sjsi_rest _sjsi_prefix _sjsi_value _sjsi_dq
    printf -v "$_sjsi_var" '%s' ""
    printf -v _sjsi_dq '\042'  # the double quote, as in _stdin_json_string
    [[ "$_sjsi_raw" == *"$_sjsi_dq$_sjsi_field$_sjsi_dq"* ]] || return 1
    _sjsi_rest=${_sjsi_raw#*"$_sjsi_dq"$_sjsi_field"$_sjsi_dq"}
    _sjsi_prefix=${_sjsi_rest%%"$_sjsi_dq"*}
    if [[ "$_sjsi_prefix" == *[!:[:space:]]* ]]; then return 1; fi
    _sjsi_value=${_sjsi_rest#*"$_sjsi_dq"}
    _sjsi_value=${_sjsi_value%%"$_sjsi_dq"*}
    _sjsi_value=${_sjsi_value//\\\\/\\}  # decode `\\`, as above (#829)
    [ -n "$_sjsi_value" ] || return 1
    printf -v "$_sjsi_var" '%s' "$_sjsi_value"
}

_stdin_json_string_into REMEMBER_HOOK_CWD cwd "$HOOK_STDIN" 2>/dev/null
if [[ "$REMEMBER_HOOK_CWD" == *$'\n'* ]] \
    || [[ "$REMEMBER_HOOK_CWD" == *$'\r'* ]]; then
    REMEMBER_HOOK_CWD=""
fi
export REMEMBER_HOOK_CWD

REMEMBER_PATHS_SOFT_FAIL=1 __remember_src_resolve_paths ${1+"$@"} || exit 0
_REMEMBER_LAZY_PYTHON=1
__remember_src_detect_tools ${1+"$@"}
__remember_src_bootstrap_dirs ${1+"$@"}
PLUGIN_ROOT="$PIPELINE_DIR"
PROJECT="$PROJECT_DIR"
__remember_src_log ${1+"$@"} 2>/dev/null
if ! command -v _remember_date >/dev/null 2>&1; then
    echo "session-start-hook: ERROR -- failed to source $PLUGIN_ROOT/scripts/log.sh" >&2
    exit 127
fi
TODAY=""
_remember_date_into TODAY '+%Y-%m-%d'
log "hook" "session-start: PROJECT_DIR=$PROJECT_DIR PIPELINE_DIR=$PIPELINE_DIR REMEMBER_DIR=$REMEMBER_DIR"

REMEMBER_SESSION_START_SLOW_S=""
config_into REMEMBER_SESSION_START_SLOW_S ".session_start_slow_threshold_s" "5"
if [ -z "$REMEMBER_SESSION_START_SLOW_S" ] || [ "${REMEMBER_SESSION_START_SLOW_S#*[!0-9]}" != "$REMEMBER_SESSION_START_SLOW_S" ]; then
    REMEMBER_SESSION_START_SLOW_S=5
fi

__remember_src_lib_env_cache ${1+"$@"}
_remember_env_cache_publish

__remember_src_lib_memory_context ${1+"$@"}

_stdin_session_id() {
    _stdin_json_string session_id "$1"
}

CURRENT_SESSION_ID=$(_stdin_session_id "$HOOK_STDIN" 2>/dev/null) || CURRENT_SESSION_ID=""
if [ -z "${CURRENT_SESSION_ID#.}" ] || [ -z "${CURRENT_SESSION_ID#..}" ] \
    || [[ "$CURRENT_SESSION_ID" == *[!A-Za-z0-9._-]* ]]; then
    CURRENT_SESSION_ID=""
fi

_stdin_json_string_into REMEMBER_TRANSCRIPT_PATH transcript_path "$HOOK_STDIN" 2>/dev/null
if [[ "$REMEMBER_TRANSCRIPT_PATH" == *$'\n'* ]] \
    || [[ "$REMEMBER_TRANSCRIPT_PATH" == *$'\r'* ]]; then
    REMEMBER_TRANSCRIPT_PATH=""
fi
export REMEMBER_TRANSCRIPT_PATH

_stdin_json_string_into SESSION_START_SOURCE source "$HOOK_STDIN" 2>/dev/null
if [ "$SESSION_START_SOURCE" != startup ] && [ "$SESSION_START_SOURCE" != resume ] \
    && [ "$SESSION_START_SOURCE" != clear ] \
    && [ "$SESSION_START_SOURCE" != compact ] && [ "$SESSION_START_SOURCE" != fork ]; then
    SESSION_START_SOURCE=""
fi

REMEMBER_HOOK_STDIN_MAX=32768
_hook_stdin_file=""

_session_start_listener() {
    local f
    for f in "$REMEMBER_HOOKS_DIR/before_session_start"/* \
             "$REMEMBER_HOOKS_DIR/after_session_start"/*; do
        [ -x "$f" ] && return 0
    done
    return 1
}

if [ -n "$HOOK_STDIN" ] && _session_start_listener; then
    _hook_stdin_file="$REMEMBER_DIR/tmp/session-start-stdin.$$"
    if (umask 077; printf '%s' "$HOOK_STDIN" > "$_hook_stdin_file") 2>/dev/null; then
        export REMEMBER_HOOK_STDIN_FILE="$_hook_stdin_file"
    else
        rm -f "$_hook_stdin_file" 2>/dev/null
        _hook_stdin_file=""
    fi
    if [ "${#HOOK_STDIN}" -le "$REMEMBER_HOOK_STDIN_MAX" ]; then
        export REMEMBER_HOOK_STDIN="$HOOK_STDIN"
    else
        export REMEMBER_HOOK_STDIN=""
    fi
fi

_remember_defer_dispatch=0
if [ "${REMEMBER_DEFER:-1}" != "0" ]; then
    _remember_git_restore_on=""
    config_into _remember_git_restore_on '.git_restore.enabled' false
    if [ "$_remember_git_restore_on" != "true" ]; then
        _remember_bss_was_nullglob=0
        shopt -q nullglob && _remember_bss_was_nullglob=1
        shopt -s nullglob
        _remember_bss_scripts=("$PLUGIN_ROOT/hooks.d/before_session_start/"*)
        [ "$_remember_bss_was_nullglob" = 1 ] || shopt -u nullglob
        if [ "${#_remember_bss_scripts[@]}" -eq 1 ]; then
            if [ "${_remember_bss_scripts[0]%/50-git-restore.sh}" != "${_remember_bss_scripts[0]}" ]; then
                _remember_defer_dispatch=1
            fi
        fi
        unset _remember_bss_scripts _remember_bss_was_nullglob
    fi
    unset _remember_git_restore_on
fi
if [ "$_remember_defer_dispatch" = 1 ]; then
    {
        export _REMEMBER_PHASE=deferred
        dispatch "before_session_start"
    } </dev/null >/dev/null 2>&1 3>&- & disown 2>/dev/null || true
else
    dispatch "before_session_start"
fi
unset _remember_defer_dispatch

rm -f "$REMEMBER_DIR/tmp/save-session.pid"

LAST_SAVE_FILE="$REMEMBER_DIR/tmp/last-save.json"
SAVED_QUERY='def isline: type == "number" and ((isnan or isinfinite) | not) and . == floor; if (((.sessions // {})[$id]) | isline) or (.session == $id and (.line | isline)) then "saved" else "unsaved" end'

session_was_saved() {
    [ -n "$1" ] && [ -f "$LAST_SAVE_FILE" ] || return 1
    if [ "$JQ" = "_jq_fallback" ]; then
        _remember_python || return 1
        [ "$(_remember_run_python "$_HOOK_DIR/session_saved.py" "$LAST_SAVE_FILE" "$1" 2>/dev/null)" = "saved" ]
    else
        [ "$(_remember_run_jq -r --arg id "$1" "$SAVED_QUERY" "$LAST_SAVE_FILE" 2>/dev/null)" = "saved" ]
    fi
}

PROJECT_PATH_SLUG="$(session_dir_slug "$PROJECT")"
SESSIONS_DIR="$(claude_projects_dir)/${PROJECT_PATH_SLUG}"

_remember_write_slug_record() {
    local _dir="$REMEMBER_DIR/tmp" _tmp
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0
    _tmp="$_dir/session-slug.$$"

    local _reason=""
    if [ -z "$PROJECT_PATH_SLUG" ]; then
        _reason="empty-slug"
    else
        local _paths_joined="${PROJECT}${SESSIONS_DIR}${REMEMBER_DIR}"
        if [[ "$_paths_joined" == *$'\n'* ]]; then
            _reason="unrepresentable-path"
        fi
    fi

    if [ -n "$_reason" ]; then
        printf 'format=1\nstatus=unavailable\nreason=%s\n' "$_reason" \
            > "$_tmp" 2>/dev/null || { rm -f "$_tmp" 2>/dev/null; return 0; }
    else
        {
            printf 'format=1\n'
            printf 'status=ok\n'
            printf 'project_dir=%s\n' "$PROJECT"
            printf 'slug=%s\n' "$PROJECT_PATH_SLUG"
            printf 'sessions_dir=%s\n' "$SESSIONS_DIR"
            printf 'memory_dir=%s\n' "$REMEMBER_DIR"
            if [ -n "$CURRENT_SESSION_ID" ]; then
                printf 'session_id=%s\n' "$CURRENT_SESSION_ID"
            fi
        } > "$_tmp" 2>/dev/null || { rm -f "$_tmp" 2>/dev/null; return 0; }
    fi

    mv -f "$_tmp" "$_dir/session-slug" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    return 0
}

SLUG_INDEX_LOCK_TIMEOUT=2
SLUG_INDEX_MAX_ROWS=1000
_remember_write_slug_index() {
    [ -n "${REMEMBER_STORE_ROOT:-}" ] || return 0
    [ "$REMEMBER_STORE_ROOT" != "$REMEMBER_DIR" ] || return 0
    [ -n "$PROJECT_PATH_SLUG" ] || return 0

    local _paths_joined="${PROJECT}${REMEMBER_DIR}"
    if [[ "$_paths_joined" == *$'\n'* ]] || [[ "$_paths_joined" == *$'\t'* ]]; then
        return 0
    fi

    local _dir="$REMEMBER_STORE_ROOT/tmp"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0

    local _index="$_dir/sessions" _lock="$_dir/sessions.lock" _tmp

    __remember_src_lib_lock ${1+"$@"} 2>/dev/null || return 0
    command -v lock_acquire >/dev/null 2>&1 || return 0
    lock_acquire "$_lock" "$SLUG_INDEX_LOCK_TIMEOUT" || return 0

    _tmp="$_index.$$"
    {
        printf 'format=1\n'
        if [ -f "$_index" ]; then
            awk -v self="$PROJECT" -v max="$((SLUG_INDEX_MAX_ROWS - 1))" '
                NR == 1 { if ($0 != "format=1") exit 0; next }
                $0 == "" { next }
                {
                    n = index($0, "\tproject_dir=")
                    if (n == 0) next
                    if (substr($0, n + 13) == self) next
                    rows[++c] = $0
                }
                END {
                    start = (c > max) ? c - max + 1 : 1
                    for (i = start; i <= c; i++) print rows[i]
                }
            ' "$_index" 2>/dev/null
        fi
        printf 'status=ok\tslug=%s\tmemory_dir=%s\tproject_dir=%s\n' \
            "$PROJECT_PATH_SLUG" "$REMEMBER_DIR" "$PROJECT"
    } > "$_tmp" 2>/dev/null || {
        rm -f "$_tmp" 2>/dev/null
        lock_release "$_lock" 2>/dev/null
        return 0
    }

    mv -f "$_tmp" "$_index" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    lock_release "$_lock" 2>/dev/null
    return 0
}

_remember_write_case_divergence() {
    __remember_src_lib_case_divergence ${1+"$@"} 2>/dev/null || return 0
    command -v remember_case_divergence >/dev/null 2>&1 || return 0
    remember_case_divergence

    local _dir="$REMEMBER_DIR/tmp" _tmp _old="" _body="" NL=$'\n'

    _body="format=1${NL}status=$REMEMBER_CASE_STATUS"
    if [ "$REMEMBER_CASE_STATUS" != "not-applicable" ]; then
        _body="$_body${NL}resolved=$REMEMBER_CASE_RESOLVED"
        _body="$_body${NL}store_root=$REMEMBER_CASE_ROOT"
        _body="$_body${NL}disk_state=$REMEMBER_CASE_DISK_STATE"
        if [ -n "$REMEMBER_CASE_DISK_REASON" ]; then
            _body="$_body${NL}disk_reason=$REMEMBER_CASE_DISK_REASON"
        fi
        if [ -n "$REMEMBER_CASE_DISK_NAMES" ]; then
            _body="$_body${NL}disk_names=$REMEMBER_CASE_DISK_NAMES"
        fi
        _body="$_body${NL}git_state=$REMEMBER_CASE_GIT_STATE"
        if [ -n "$REMEMBER_CASE_GIT_REASON" ]; then
            _body="$_body${NL}git_reason=$REMEMBER_CASE_GIT_REASON"
        fi
        if [ -n "$REMEMBER_CASE_GIT_NAMES" ]; then
            _body="$_body${NL}git_names=$REMEMBER_CASE_GIT_NAMES"
        fi
    fi

    if [ -f "$_dir/case-divergence" ]; then
        local _pline
        while IFS= read -r _pline; do
            _old="${_old:+$_old$NL}$_pline"
        done < "$_dir/case-divergence"
    fi

    if [ "$_old" != "$_body" ]; then
        [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0
        _tmp="$_dir/case-divergence.$$"
        printf '%s\n' "$_body" > "$_tmp" 2>/dev/null \
            || { rm -f "$_tmp" 2>/dev/null; return 0; }
        mv -f "$_tmp" "$_dir/case-divergence" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    fi

    if [ "$REMEMBER_CASE_STATUS" = diverged ]; then
        log "case-divergence" "$REMEMBER_CASE_MESSAGE"
        [ "$_old" = "$_body" ] && return 0
        printf '%s\n' "$REMEMBER_CASE_MESSAGE" \
            > "$_dir/case-divergence-notice" 2>/dev/null || true
    elif [ "$REMEMBER_CASE_STATUS" = unavailable ]; then
        [ "$_old" = "$_body" ] && return 0
        log "case-divergence" "could not check whether this store is known by a second spelling (disk=$REMEMBER_CASE_DISK_STATE${REMEMBER_CASE_DISK_REASON:+/$REMEMBER_CASE_DISK_REASON} git=$REMEMBER_CASE_GIT_STATE${REMEMBER_CASE_GIT_REASON:+/$REMEMBER_CASE_GIT_REASON}) -- this is not a report that they agree"
    fi
    return 0
}

_ENTRYPOINT_SNIFF_CAP=50
_transcript_is_pluginless_sdk() {
    local f=$1 n=0 line ep rest prefix is_dialogue dq
    printf -v dq '\042'  # the double quote, as in _stdin_json_string
    while IFS= read -r line; do
        n=$((n + 1))
        is_dialogue=0
        rest=${line#*"$dq"message"$dq"}
        if [ "$rest" != "$line" ]; then
            prefix=${rest%%"$dq"*}
            if [[ "$prefix" == *[!:[:space:]]* ]]; then
                is_dialogue=1
            fi
        fi
        if [ "$is_dialogue" -eq 0 ]; then
            if [ "${line#*"$dq"entrypoint"$dq"}" != "$line" ]; then
                ep=$(_stdin_json_string entrypoint "$line" 2>/dev/null) || return 1
                if [ "${ep#sdk-}" != "$ep" ]; then
                    return 0
                else
                    return 1
                fi
            fi
        fi
        [ "$n" -ge "$_ENTRYPOINT_SNIFF_CAP" ] && return 1
    done < "$f" 2>/dev/null
    return 1
}

_PREV_TRANSCRIPT_EXCLUDE_CAP="${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20}"
previous_transcript() {
    local dir=$1 f base newest="" tries=0 i best_idx
    local -a candidates=()
    local -a taken=()
    for f in "$dir"/*.jsonl; do
        [ -e "$f" ] || continue
        base=${f##*/}
        base=${base%.jsonl}
        [ "$base" = "$CURRENT_SESSION_ID" ] && continue
        candidates+=("$f")
    done
    while :; do
        newest="" best_idx=-1 i=0
        for f in "${candidates[@]}"; do
            if [ -z "${taken[$i]:-}" ]; then
                if [ -z "$newest" ] || [ "$f" -nt "$newest" ]; then
                    newest=$f
                    best_idx=$i
                fi
            fi
            i=$((i + 1))
        done
        [ -z "$newest" ] && break
        if _transcript_is_pluginless_sdk "$newest"; then
            taken[$best_idx]=1
            tries=$((tries + 1))
            if [ "$tries" -ge "${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20}" ]; then
                log "hook" "WARNING: previous_transcript gave up after ${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20} pluginless-SDK exclusions in $dir -- a real previous session may exist beyond the cap; recovery and the #200 capture-gap check will both treat this the same as no previous session existing"
                newest=""
                break
            fi
            continue
        fi
        break
    done
    [ -n "$newest" ] && printf '%s\n' "$newest"
    return 0
}

_second_newest_jsonl() {
    local dir=$1 f own="" newest="" tries=0 i best_idx
    local -a candidates=()
    local -a taken=()
    for f in "$dir"/*.jsonl; do
        [ -e "$f" ] || continue
        if [ -z "$own" ] || [ "$f" -nt "$own" ]; then
            own=$f
        fi
    done
    for f in "$dir"/*.jsonl; do
        [ -e "$f" ] || continue
        [ "$f" = "$own" ] && continue
        candidates+=("$f")
    done
    while :; do
        newest="" best_idx=-1 i=0
        for f in "${candidates[@]}"; do
            if [ -z "${taken[$i]:-}" ]; then
                if [ -z "$newest" ] || [ "$f" -nt "$newest" ]; then
                    newest=$f
                    best_idx=$i
                fi
            fi
            i=$((i + 1))
        done
        [ -z "$newest" ] && break
        if _transcript_is_pluginless_sdk "$newest"; then
            taken[$best_idx]=1
            tries=$((tries + 1))
            if [ "$tries" -ge "${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20}" ]; then
                log "hook" "WARNING: _second_newest_jsonl gave up after ${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20} pluginless-SDK exclusions in $dir -- a real second-newest transcript may exist beyond the cap; the #200 capture-gap check will treat this the same as no previous session existing"
                newest=""
                break
            fi
            continue
        fi
        break
    done
    _TWO_NEWEST_JSONL_SECOND=$newest
}

_remember_deferred_phase() {
local -x _REMEMBER_PHASE=deferred

_remember_write_slug_record
_remember_write_slug_index
_remember_write_case_divergence

if [ -n "$CURRENT_SESSION_ID" ]; then
    PREV_JSONL=""
else
    :
    PREV_JSONL=""
fi
PREV_ID=""
if [ -n "$PREV_JSONL" ]; then
    PREV_ID=$PREV_JSONL
    PREV_ID=${PREV_ID%.jsonl}
fi

PREV_WAS_SAVED="no"
if [ -n "$PREV_ID" ] && session_was_saved "$PREV_ID"; then
    PREV_WAS_SAVED="yes"
fi

_recovery_enabled=""
config_into _recovery_enabled '.features.recovery' true
_recovery_attempted=""
if [ "$_recovery_enabled" = "true" ]; then
if [ -d "$SESSIONS_DIR" ] && [ -f "$LAST_SAVE_FILE" ] && [ -n "$PREV_ID" ]; then
    if [ "$PREV_WAS_SAVED" = "no" ]; then
        ( unset REMEMBER_TRANSCRIPT_PATH; "$PLUGIN_ROOT/scripts/save-session.sh" "$PREV_ID" --force ) </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true
        _recovery_attempted="true"
    fi
fi
fi

CAPTURE_ALIVE="$REMEMBER_DIR/tmp/capture-alive"
CAPTURE_SEEN_DIR="$REMEMBER_DIR/tmp/capture-alive.d"
CAPTURE_REPORTED="$REMEMBER_DIR/tmp/capture-gap-reported"
CAPTURE_SEEN_KEEP=200

SEEN_ID=""
[ -f "$CAPTURE_ALIVE" ] && SEEN_ID=$(<"$CAPTURE_ALIVE")

capture_was_seen() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local _d _ok
    [ -n "$1" ] || return 1
    printf -v _d '\056'
    _ok="^[A-Za-z0-9${_d}_-]*\$"
    if [ "$1" = "$_d" ] || [ "$1" = "$_d$_d" ]; then
        :
    elif ! [[ "$1" =~ $_ok ]]; then
        :
    else
        [ -e "$CAPTURE_SEEN_DIR/$1" ] && return 0
    fi
    [ "$SEEN_ID" = "$1" ] && return 0
    [ "$1" = "$PREV_ID" ] && [ "$PREV_WAS_SAVED" = "yes" ] && return 0
    [ "$1" != "$PREV_ID" ] && session_was_saved "$1" && return 0
    return 1
}

_remember_prune_keep_newest() {
    local dir=$1 keep=$2 f n=0 oldest nl skip
    printf -v nl '\n'
    skip=$nl
    for f in "$dir"/*; do
        [ -e "$f" ] && n=$((n + 1))
    done
    while [ "$n" -gt "$keep" ]; do
        oldest=""
        for f in "$dir"/*; do
            [ -e "$f" ] || continue
            [[ "$skip" == *"$nl$f$nl"* ]] && continue
            if [ -z "$oldest" ] || ! [ "$f" -nt "$oldest" ]; then
                oldest=$f
            fi
        done
        [ -n "$oldest" ] || break
        rm -f "$oldest" 2>/dev/null
        [ -e "$oldest" ] && skip="$skip$oldest$nl"
        n=$((n - 1))
    done
    return 0
}
_remember_prune_keep_newest "$CAPTURE_SEEN_DIR" "$CAPTURE_SEEN_KEEP"

CAPTURE_SKIPPED="$REMEMBER_DIR/tmp/capture-gap-skipped"

if [ -z "$CURRENT_SESSION_ID" ]; then
    printf '%s\n' "the SessionStart payload carried no usable session_id" \
        > "$CAPTURE_SKIPPED" 2>/dev/null || true
    log "hook" "session-start: capture-gap check skipped -- no session_id on stdin"
else
    rm -f "$CAPTURE_SKIPPED" 2>/dev/null || true

    REPORTED_ID=""
    [ -f "$CAPTURE_REPORTED" ] && REPORTED_ID=$(<"$CAPTURE_REPORTED")

    if [ -n "$PREV_ID" ] && [ "$REPORTED_ID" != "$PREV_ID" ] \
       && ! capture_was_seen "$PREV_ID" \
       && grep -q '"tool_use"' "$PREV_JSONL" 2>/dev/null; then
        _capture_gap_notice="remember: your previous session was not captured. If you just installed or enabled the plugin, that is expected -- capture starts now. Otherwise its hooks were not registered for that session; run /remember:doctor."
        if [ "$_recovery_attempted" = "true" ]; then
            _capture_gap_notice="remember: your previous session was not captured live -- its hooks did not run. It is being recovered from its transcript now, so nothing should be lost; if this keeps happening for interactive sessions, run /remember:doctor."
        fi
        echo "$_capture_gap_notice" \
            > "$REMEMBER_DIR/tmp/capture-gap-notice" 2>/dev/null || true
        printf '%s' "$PREV_ID" > "$CAPTURE_REPORTED" 2>/dev/null || true
    fi
fi
}

if [ "${REMEMBER_DEFER:-1}" = "0" ]; then
    _remember_deferred_phase
else
    _remember_deferred_phase </dev/null >/dev/null 2>&1 3>&- & disown 2>/dev/null || true
fi

_remember_memory_paths

config_into HANDOFF_MODE ".handoff_mode" "single"
PER_SESSION_HANDOFF=""
HANDOFF_MODE_DEGRADED=""
if [ "$HANDOFF_MODE" = "per_session" ] && [ -n "$CURRENT_SESSION_ID" ]; then
    REMEMBER_HANDOFF="$REMEMBER_DIR/remember.${CURRENT_SESSION_ID}.md"
    PER_SESSION_HANDOFF="true"
else
    REMEMBER_HANDOFF="$REMEMBER_DIR/remember.md"
    [ "$HANDOFF_MODE" = "per_session" ] && HANDOFF_MODE_DEGRADED="true"
fi

[ -d "$REMEMBER_DIR/tmp" ] || mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null
printf '%s\n' "$REMEMBER_HANDOFF" > "$REMEMBER_DIR/tmp/handoff-path" 2>/dev/null

if [ -n "$CURRENT_SESSION_ID" ]; then
    printf '%s\n' "$REMEMBER_HANDOFF" > "$REMEMBER_DIR/tmp/handoff-path.$CURRENT_SESSION_ID" 2>/dev/null
fi


echo '{}'
