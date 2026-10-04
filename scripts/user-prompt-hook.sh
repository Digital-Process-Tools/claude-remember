#!/bin/bash

_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="$(pwd)"

[ -n "${REMEMBER_NESTED_SUMMARIZER:-}" ] && exit 0

unset REMEMBER_HOOK_CWD

unset REMEMBER_TRANSCRIPT_PATH

if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
    _REMEMBER_HOST_JSON_STDOUT=0
else
    _REMEMBER_HOST_JSON_STDOUT=1
fi


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
    case "$1" in
        *%-*|*%_*|*%0*|*%^*|*%#*) return 1 ;;
    esac
    return 0
}

_remember_date() {
    if [ -n "${REMEMBER_TZ:-}" ]; then
        TZ="$REMEMBER_TZ" date "$@"
        return
    fi
    if [ "$_REMEMBER_PRINTF_T" = "1" ] && [ "$#" -eq 1 ] \
        && _remember_date_builtin_ok "$1"; then
        printf "%(${1#+})T\\n" -1 && return
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


[ -n "${_REMEMBER_LIB_ENV_CACHE_LOADED:-}" ] && return 0
_REMEMBER_LIB_ENV_CACHE_LOADED=1

_remember_env_cache_normalize_into() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
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
                _drive=$(printf '%s' "$_drive" | LC_ALL=C tr '[:lower:]' '[:upper:]')
                _rest="${_rest//\//\\}"
                printf -v "$_var" '%s:\\%s' "$_drive" "$_rest"
                return 0
            fi
            ;;
    esac
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

_remember_env_cache_load() {
    [ "${REMEMBER_ENV_CACHE:-1}" = "1" ] || return 1
    _remember_env_cache_path || return 1
    local _f="$_REMEMBER_ENV_CACHE_FILE"
    [ -f "$_f" ] || return 1
    [ -L "$_f" ] && return 1
    [ -O "$_f" ] || return 1
    [ -r "$_f" ] || return 1

    local _line _dir="" _tz="" _mem="" _proj="" _pipe="" _stamp=""
    local _cooldown="" _delta="" _cfg_exists_raw="" _cfg_raw=""
    local _env_proj="" _env_pipe="" _env_home=""
    local _cfgs=()
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        if [ "${_line#REMEMBER_DIR=}" != "$_line" ]; then
            _dir="${_line#*=}"
        elif [ "${_line#REMEMBER_TZ=}" != "$_line" ]; then
            _tz="${_line#*=}"
        elif [ "${_line#REMEMBER_PROMPT_STAMP=}" != "$_line" ]; then
            _stamp="${_line#*=}"
        elif [ "${_line#REMEMBER_SAVE_COOLDOWN=}" != "$_line" ]; then
            _cooldown="${_line#*=}"
        elif [ "${_line#REMEMBER_DELTA_THRESHOLD=}" != "$_line" ]; then
            _delta="${_line#*=}"
        elif [ "${_line#MEMORY_PROJECT_DIR=}" != "$_line" ]; then
            _mem="${_line#*=}"
        elif [ "${_line#PROJECT_DIR=}" != "$_line" ]; then
            _proj="${_line#*=}"
        elif [ "${_line#PIPELINE_DIR=}" != "$_line" ]; then
            _pipe="${_line#*=}"
        elif [ "${_line#CACHE_ENV_PROJECT_DIR=}" != "$_line" ]; then
            _env_proj="${_line#*=}"
        elif [ "${_line#CACHE_ENV_PLUGIN_ROOT=}" != "$_line" ]; then
            _env_pipe="${_line#*=}"
        elif [ "${_line#CACHE_ENV_HOME=}" != "$_line" ]; then
            _env_home="${_line#*=}"
        elif [ "${_line#CACHE_CONFIG=}" != "$_line" ]; then
            _cfg_raw="${_line#*=}"
            if [ "${_cfg_raw#1:}" != "$_cfg_raw" ]; then
                _cfgs[${#_cfgs[@]}]="${_cfg_raw#1:}"
                _cfg_exists_raw="${_cfg_exists_raw}1"
            elif [ "${_cfg_raw#0:}" != "$_cfg_raw" ]; then
                _cfgs[${#_cfgs[@]}]="${_cfg_raw#0:}"
                _cfg_exists_raw="${_cfg_exists_raw}0"
            else
                return 1
            fi
        else
            return 1
        fi
    done < "$_f"

    [ -n "$_dir" ] && [ -n "$_proj" ] && [ -n "$_pipe" ] || return 1
    [ -n "$_stamp" ] || return 1
    case "$_cooldown" in '' | *[!0-9]*) return 1 ;; esac
    case "$_delta" in '' | *[!0-9]*) return 1 ;; esac
    [ "$_env_proj" = "${_REMEMBER_ENV_CACHE_PROJECT_DIR:-}" ] || return 1
    [ "$_env_pipe" = "${CLAUDE_PLUGIN_ROOT:-}" ] || return 1
    [ "$_env_home" = "${HOME:-}" ] || return 1
    [ -d "$_pipe" ] || return 1

    local _cfg _cfg_exists_now=""
    for _cfg in ${_cfgs[@]+"${_cfgs[@]}"}; do
        [ -n "$_cfg" ] || continue
        if [ -e "$_cfg" ]; then
            _cfg_exists_now="${_cfg_exists_now}1"
            [ "$_f" -nt "$_cfg" ] || return 1
        else
            _cfg_exists_now="${_cfg_exists_now}0"
        fi
    done
    [ "$_cfg_exists_raw" = "$_cfg_exists_now" ] || return 1

    PROJECT_DIR="$_proj"
    PIPELINE_DIR="$_pipe"
    REMEMBER_DIR="$_dir"
    REMEMBER_TZ="$_tz"
    REMEMBER_PROMPT_STAMP="$_stamp"
    REMEMBER_SAVE_COOLDOWN="$_cooldown"
    REMEMBER_DELTA_THRESHOLD="$_delta"
    MEMORY_PROJECT_DIR="$_mem"
    [ -n "$MEMORY_PROJECT_DIR" ] || MEMORY_PROJECT_DIR="$_proj"
    export PROJECT_DIR PIPELINE_DIR REMEMBER_DIR REMEMBER_TZ MEMORY_PROJECT_DIR
    export REMEMBER_PROMPT_STAMP REMEMBER_SAVE_COOLDOWN REMEMBER_DELTA_THRESHOLD
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


_HOOK_STDIN=""
if [ ! -t 0 ]; then
    _line=""
    while IFS= read -r -t 1 _line || [ -n "$_line" ]; do
        _HOOK_STDIN="$_HOOK_STDIN$_line"
        _line=""
    done
fi
_stdin_cwd() {
    local raw="$1" rest prefix value dq
    printf -v dq '\042'  # the double quote, held in a variable (#898 round 8)
    rest=${raw#*"$dq"cwd"$dq"}
    [ "$rest" != "$raw" ] || return 1
    prefix=${rest%%"$dq"*}
    case "$prefix" in *[!:[:space:]]*) return 1 ;; esac
    value=${rest#*"$dq"}
    value=${value%%"$dq"*}
    value=${value//\\\\/\\}
    [ -n "$value" ] || return 1
    printf '%s' "$value"
}
_stdin_cwd_into() {
    local _var="$1" raw="$2" rest prefix value dq
    printf -v dq '\042'  # the double quote, held in a variable (#898 round 8)
    rest=${raw#*"$dq"cwd"$dq"}
    [ "$rest" != "$raw" ] || return 1
    prefix=${rest%%"$dq"*}
    case "$prefix" in *[!:[:space:]]*) return 1 ;; esac
    value=${rest#*"$dq"}
    value=${value%%"$dq"*}
    value=${value//\\\\/\\}  # decode `\\`, as in _stdin_cwd (#829)
    [ -n "$value" ] || return 1
    printf -v "$_var" '%s' "$value"
}
_stdin_cwd_into REMEMBER_HOOK_CWD "$_HOOK_STDIN" || REMEMBER_HOOK_CWD=""
case "$REMEMBER_HOOK_CWD" in
    *$'\n'*|*$'\r'*) REMEMBER_HOOK_CWD="" ;;
esac
export REMEMBER_HOOK_CWD

_REMEMBER_FAST=0
if _remember_env_cache_load && [ ! -d "$PIPELINE_DIR/hooks.d/after_user_prompt" ]; then
    _REMEMBER_FAST=1
    umask 077
    SYS_TMPDIR="${TMPDIR:-/tmp}"
    dispatch() { :; }
fi

if [ "$_REMEMBER_FAST" = "0" ]; then
REMEMBER_PATHS_SOFT_FAIL=1

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
    PIPELINE_DIR="$CLAUDE_PLUGIN_ROOT"
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
                _drive=$(printf '%s' "$_drive" | LC_ALL=C tr '[:lower:]' '[:upper:]')
                _rest="${_rest//\//\\}"
                printf '%s' "${_drive}:\\${_rest}"
                return 0
            fi
            ;;
    esac
    printf '%s' "$_in"
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


_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"

[ -n "${_LIB_MEMORY_DIR_LOADED:-}" ] && return 0
_LIB_MEMORY_DIR_LOADED=1

_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"

[ -n "${_REMEMBER_LIB_SLUG_LOADED:-}" ] && return 0
_REMEMBER_LIB_SLUG_LOADED=1

_remember_slug_run_python() {
    case "${PYTHON:-python3}" in
        python3) python3 "$@" ;;
        python) python "$@" ;;
        py\ -3) py -3 "$@" ;;
        py) py "$@" ;;
        *) return 127 ;;
    esac
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


_remember_should_check_utf8() {
    [ "${REMEMBER_UTF8_STRICT:-0}" = "1" ] && return 0
    case "${OSTYPE:-}" in
        linux*) return 0 ;;
    esac
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
    case "$path" in
        ?:*)
            _drive_at="${_REMEMBER_DRIVE_UPPER%%"${path:0:1}"*}"
            if [ "$_drive_at" != "$_REMEMBER_DRIVE_UPPER" ]; then
                path="${_REMEMBER_DRIVE_LOWER:${#_drive_at}:1}${path:1}"
            fi
            ;;
    esac
    local _orig="$path"

    if _remember_should_check_utf8; then
    local _high_byte=0 _lc_was_set="${LC_ALL+set}" _lc_prev="${LC_ALL:-}"
    LC_ALL=C
    case "$path" in
        *[!$'\001'-$'\177']*) _high_byte=1 ;;
    esac
    if [ -n "$_lc_was_set" ]; then LC_ALL="$_lc_prev"; else unset LC_ALL; fi

    case "$_high_byte" in
        1)
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
            ;;
    esac
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

    case "$_hash" in
        *[!0-9a-z]*) _hash="" ;;
    esac

    if [ -z "$_hash" ]; then
        printf '%s\n' "$_slug"
        return 0
    fi
    printf '%s-%s\n' "${_slug:0:200}" "$_hash"
}

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

    case "$data_dir" in
        /*|~*|[A-Za-z]:/*|[A-Za-z]:\\*)
            local slug
            slug=$(session_dir_slug "$proj")
            local expanded="${data_dir/#\~/$HOME}"
            echo "${expanded//\{slug\}/$slug}"
            ;;
        *)
            echo "${proj}/${data_dir}"
            ;;
    esac
}

_set_store_root() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    local data_dir="$1" prefix
    REMEMBER_STORE_ROOT=""

    case "$data_dir" in
        /*|~*|[A-Za-z]:/*|[A-Za-z]:\\*) ;;
        *) return 0 ;;
    esac
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

    case "$prefix" in
        ''|/|[A-Za-z]:|[A-Za-z]:/|[A-Za-z]:\\) return 0 ;;
    esac

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
    case "$_data_dir_raw" in
        /*|~*|[A-Za-z]:/*|[A-Za-z]:\\*) _project_cfg_haiku_untrusted=0 ;;
        *) _project_cfg_haiku_untrusted=1 ;;
    esac
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
    case "$(_remember_config_tracked_status "${_project_cfg%/*}" "${_project_cfg##*/}")" in
        untracked) _project_cfg_model_reject_untrusted=0 ;;
        *) _project_cfg_model_reject_untrusted=1 ;;  # tracked or could-not-tell -> fail CLOSED
    esac
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
        jq -s 'reduce .[] as $x ({}; . * $x) | with_entries(select(.key | startswith("_") | not))' "${_jq_merge_sources[@]}" > "$_merged_cfg" 2>/dev/null \
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
        case "${PYTHON:-python3}" in
            python3) python3 "$@" ;;
            python) python "$@" ;;
            py\ -3) py -3 "$@" ;;
            py) py "$@" ;;
            *) return 127 ;;
        esac
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
            case "$(_remember_config_tracked_status "$_mem_proj" ".remember/config.json")" in
                untracked) : ;;
                *)
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
        || { echo 'This file marks when remember was first bootstrapped here. Read only by scripts/doctor.sh (#401); do not delete it.' \
            > "$REMEMBER_DIR/.install-marker"; } 2>/dev/null
fi

if [ -d "$REMEMBER_DIR" ]; then
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
        printf 'remember: %s, so stderr is NOT being redirected to %s -- point BASH_XTRACEFD at its own fd to get both (#690)\n' \
            "$_remember_bd_keep_fd2" "$REMEMBER_DIR/logs/hook-errors.log" >&2
    else
        exec 2>> "$REMEMBER_DIR/logs/hook-errors.log"
    fi
    unset _remember_bd_keep_fd2
fi
unset _remember_bd_has_xtracefd


if [ -z "${PIPELINE_DIR:-}" ]; then
    if [ -n "${PROJECT_DIR:-}" ]; then
        PIPELINE_DIR="${PROJECT_DIR}/.claude/remember"
    else
        PIPELINE_DIR="./.claude/remember"
    fi
fi

_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
:
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

_config_is_private_key() {
    case "$1" in
        .haiku|.haiku.*) return 0 ;;
    esac
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
    case "${REMEMBER_CONFIG:-}" in
        */remember-config-*) return 0 ;;
        *) return 1 ;;
    esac
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
    local _rcfgqe_v="$2"
    _rcfgqe_v="${_rcfgqe_v//\\/\\\\}"
    _rcfgqe_v="${_rcfgqe_v//$'\n'/\\n}"
    _rcfgqe_v="${_rcfgqe_v//$'\r'/\\r}"
    _rcfgqe_v="${_rcfgqe_v//$'\t'/\\t}"
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
                case "$_exists_raw" in
                    *[!01]*|'')
                        rm -f "$_f" 2>/dev/null
                        return 1
                        ;;
                esac
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
    case "${PYTHON:-python3}" in
        python3) python3 "$@" ;;
        python) python "$@" ;;
        py\ -3) py -3 "$@" ;;
        py) py "$@" ;;
        *) return 127 ;;
    esac
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

    if [ "$_REMEMBER_CFG_STATE" = "ok" ] && ! _config_is_private_key "$_cfg_into_name"; then
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
case "$REMEMBER_PROMPT_STAMP" in
    stable|off) ;;
    *) REMEMBER_PROMPT_STAMP="full" ;;
esac
export REMEMBER_PROMPT_STAMP

config_into REMEMBER_SAVE_COOLDOWN ".cooldowns.save_seconds" 120
case "$REMEMBER_SAVE_COOLDOWN" in ''|*[!0-9]*) REMEMBER_SAVE_COOLDOWN=120 ;; esac
export REMEMBER_SAVE_COOLDOWN

config_into REMEMBER_DELTA_THRESHOLD ".thresholds.delta_lines_trigger" 50
case "$REMEMBER_DELTA_THRESHOLD" in ''|*[!0-9]*) REMEMBER_DELTA_THRESHOLD=50 ;; esac
export REMEMBER_DELTA_THRESHOLD

[ -n "${REMEMBER_MODEL:-}" ] || config_into REMEMBER_MODEL ".model" "haiku"
export REMEMBER_MODEL
[ -n "${REMEMBER_REJECT_PATTERN:-}" ] || config_into REMEMBER_REJECT_PATTERN ".reject_pattern" ""
export REMEMBER_REJECT_PATTERN

_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
:
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
        case "$_DISPATCH_RC" in
            143|137) _DISPATCH_TIMEDOUT=1 ;;
        esac
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
            case "$_budget" in
                ''|*[!0-9]*) _budget=$_DISPATCH_BUDGET_FALLBACK ;;
            esac
            _grace=$(config '.hooks.dispatch_kill_grace_seconds' "$_DISPATCH_KILL_GRACE_DEFAULT")
            case "$_grace" in
                ''|*[!0-9]*) _grace=$_DISPATCH_KILL_GRACE_DEFAULT ;;
            esac
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



    _remember_env_cache_publish
fi

declare -F dispatch >/dev/null 2>&1 || dispatch() { :; }

NOTICE_MSG=""
for _notice_name in capture-gap-notice git-backup-notice git-restore-notice case-divergence-notice; do
    NOTICE_FILE="$REMEMBER_DIR/tmp/$_notice_name"
    [ -f "$NOTICE_FILE" ] || continue
    _notice_body=$(cat "$NOTICE_FILE" 2>/dev/null)
    rm -f "$NOTICE_FILE" 2>/dev/null || true
    [ -n "$_notice_body" ] || continue
    if [ -n "$NOTICE_MSG" ]; then
        NOTICE_MSG="$NOTICE_MSG
$_notice_body"
    else
        NOTICE_MSG="$_notice_body"
    fi
done

CTX=$(
_REMEMBER_STAMP="${REMEMBER_PROMPT_STAMP:-full}"
if [ "$_REMEMBER_STAMP" != "stable" ] && [ "$_REMEMBER_STAMP" != "off" ]; then
  _REMEMBER_STAMP="full"
fi

if [ "$_REMEMBER_STAMP" != "off" ]; then
CTX_PCT=""
CTX_PCT_FILE="${SYS_TMPDIR:-/tmp}/claude-ctx-pct"
if [ -f "$CTX_PCT_FILE" ]; then
  read -r CTX_PCT < "$CTX_PCT_FILE" 2>/dev/null
fi
if [ -n "${USER:-}" ]; then
    _REMEMBER_WHO="$USER"
elif [ -n "${USERNAME:-}" ]; then
    _REMEMBER_WHO="$USERNAME"
else
    _REMEMBER_WHO=""
fi
[ -n "$_REMEMBER_WHO" ] || _REMEMBER_WHO=$(whoami 2>/dev/null)
if [ "$_REMEMBER_STAMP" = "stable" ]; then
  echo "[$_REMEMBER_WHO]"
elif [ -n "$CTX_PCT" ]; then
  _remember_date_into _REMEMBER_NOW '+%H:%M %Z'
  echo "[$_REMEMBER_NOW -- $_REMEMBER_WHO -- ${CTX_PCT}%]"
else
  _remember_date_into _REMEMBER_NOW '+%H:%M %Z'
  echo "[$_REMEMBER_NOW -- $_REMEMBER_WHO]"
fi
if [ -n "$CTX_PCT" ] && [ "$CTX_PCT" -ge 95 ] 2>/dev/null; then
  echo "WARNING: Context at ${CTX_PCT}%. Run /remember to save session state before context death."
fi
fi

dispatch "after_user_prompt"
)

JQ_BIN="${JQ:-jq}"
_remember_run_jq_bin() {
    case "$JQ_BIN" in
        jq) jq "$@" ;;
        _jq_fallback) _jq_fallback "$@" ;;
        *)
            echo "FATAL: _remember_run_jq_bin: unrecognized JQ_BIN value '$JQ_BIN'" >&2
            return 127
            ;;
    esac
}
if [ "$_REMEMBER_HOST_JSON_STDOUT" = "1" ]; then
    if [ -z "$CTX" ] && [ -z "$NOTICE_MSG" ]; then
        : # nothing to say -- printing nothing is Completed on every host
    else
        _JSON=""
        if command -v "$JQ_BIN" >/dev/null 2>&1; then
            _JSON=$(_remember_run_jq_bin -n --arg ctx "$CTX" --arg msg "$NOTICE_MSG" \
                '(if $ctx != "" then {hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}} else {} end)
                 + (if $msg != "" then {systemMessage:$msg} else {} end)' 2>/dev/null) || _JSON=""
        fi
        if [ -n "$_JSON" ]; then
            printf '%s\n' "$_JSON"
        else
            declare -F log >/dev/null 2>&1 && log "hook" \
                "user-prompt-hook: jq unavailable/failed on a non-Claude-Code host -- dropped this turn's Codex-safe stdout envelope (stamp/notice lost, not printed raw to avoid the #451 collision)"
        fi
    fi
elif [ -z "$NOTICE_MSG" ]; then
    printf '%s\n' "$CTX"
else
    _JSON=""
    if command -v "$JQ_BIN" >/dev/null 2>&1; then
        _JSON=$(printf '%s\n' "$CTX" | _remember_run_jq_bin -Rs --arg msg "$NOTICE_MSG" \
            '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:.},systemMessage:$msg}' 2>/dev/null) \
            || _JSON=""
    fi
    if [ -n "$_JSON" ]; then
        printf '%s\n' "$_JSON"
    else
        printf '%s\n' "$CTX"
        printf 'remember: %s\n' "$NOTICE_MSG"
    fi
fi

exit 0
