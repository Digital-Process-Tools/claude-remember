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
fi
}
fi
echo '{}'
