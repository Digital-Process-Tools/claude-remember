#!/bin/bash

_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="$(pwd)"

[ -n "${REMEMBER_NESTED_SUMMARIZER:-}" ] && exit 0

unset REMEMBER_HOOK_STDIN REMEMBER_HOOK_STDIN_FILE

if [ -n "${REMEMBER_SESSION_END_DETACHED:-}" ]; then
    HOOK_STDIN="${REMEMBER_SESSION_END_PAYLOAD:-}"
    unset REMEMBER_SESSION_END_DETACHED REMEMBER_SESSION_END_PAYLOAD
else
    HOOK_STDIN=""
    if [ ! -t 0 ]; then
        _line=""
        while IFS= read -r -t 1 _line || [ -n "$_line" ]; do
            HOOK_STDIN="$HOOK_STDIN$_line"
            _line=""
        done
    fi
    if [ -z "${REMEMBER_SESSION_END_FOREGROUND:-}" ]; then
        REMEMBER_SESSION_END_DETACHED=1 REMEMBER_SESSION_END_PAYLOAD="$HOOK_STDIN" \
            nohup bash "${BASH_SOURCE[0]}" </dev/null >/dev/null 2>&1 &
        disown 2>/dev/null || true
        exit 0
    fi
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


_stdin_json_string() {
    local key="$1" raw="$2" rest prefix value
    case "$raw" in *"\"$key\""*) ;; *) return 1 ;; esac
    rest=${raw#*\"$key\"}
    prefix=${rest%%\"*}
    case "$prefix" in *[!:[:space:]]*) return 1 ;; esac
    value=${rest#*\"}
    value=${value%%\"*}
    value=${value//\\\\/\\}
    [ -n "$value" ] || return 1
    printf '%s' "$value"
}

STDIN_SESSION_ID=$(_stdin_json_string session_id "$HOOK_STDIN" 2>/dev/null) || STDIN_SESSION_ID=""
case "$STDIN_SESSION_ID" in
    ''|.|..|-*|*[!A-Za-z0-9._-]*) STDIN_SESSION_ID="" ;;
esac

SESSION_END_REASON=$(_stdin_json_string reason "$HOOK_STDIN" 2>/dev/null) || SESSION_END_REASON=""
case "$SESSION_END_REASON" in
    ''|*[!A-Za-z0-9_]*) SESSION_END_REASON="unknown" ;;
esac

REMEMBER_TRANSCRIPT_PATH=$(_stdin_json_string transcript_path "$HOOK_STDIN" 2>/dev/null) || REMEMBER_TRANSCRIPT_PATH=""
case "$REMEMBER_TRANSCRIPT_PATH" in
    *$'\n'*|*$'\r'*) REMEMBER_TRANSCRIPT_PATH="" ;;
esac
export REMEMBER_TRANSCRIPT_PATH

REMEMBER_HOOK_CWD=$(_stdin_json_string cwd "$HOOK_STDIN" 2>/dev/null) || REMEMBER_HOOK_CWD=""
case "$REMEMBER_HOOK_CWD" in
    *$'\n'*|*$'\r'*) REMEMBER_HOOK_CWD="" ;;
esac
export REMEMBER_HOOK_CWD

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
if [ -n "$_REMEMBER_PLUGIN_ROOT" ] && [ -f "$_REMEMBER_PLUGIN_ROOT/pipeline/haiku.py" ]; then
    PIPELINE_DIR="$_REMEMBER_PLUGIN_ROOT"
elif [ -n "${PLUGIN_ROOT:-}" ] && [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] \
        && [ "$_REMEMBER_PLUGIN_ROOT" != "$CLAUDE_PLUGIN_ROOT" ] \
        && [ -f "${CLAUDE_PLUGIN_ROOT}/pipeline/haiku.py" ]; then
    PIPELINE_DIR="$CLAUDE_PLUGIN_ROOT"
elif [ -f "$_PLUGIN_ROOT_CANDIDATE/pipeline/haiku.py" ]; then
    PIPELINE_DIR="$_PLUGIN_ROOT_CANDIDATE"
else
    _msg="FATAL: Cannot resolve plugin root. PLUGIN_ROOT/CLAUDE_PLUGIN_ROOT do not point at a valid plugin install (missing pipeline/haiku.py) and $_PLUGIN_ROOT_CANDIDATE/pipeline/haiku.py does not exist."
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

_REMEMBER_TOOLS_CACHE="${TMPDIR:-/tmp}/remember-detect-tools-cache"

_jq_fallback() {
    local _jq_flags=""
    while [[ "$1" == -* ]]; do _jq_flags="$_jq_flags $1"; shift; done
    local _jq_query="$1"
    local _jq_file="$2"
    if declare -f _remember_python >/dev/null 2>&1; then
        _remember_python || return 1
    fi
    $PYTHON - "$_jq_file" "$_jq_query" <<< 'import json, sys
try:
    data = json.load(open(sys.argv[1]))
    keys = sys.argv[2].strip('"'"'.'"'"').split('"'"'.'"'"')
    val = data
    for k in keys:
        if k and isinstance(val, dict):
            val = val.get(k)
        if val is None:
            break
    if val is None:
        sys.exit(0)
    # jq -r prints strings raw and everything else in jq'"'"'s JSON textual
    # form — crucially "true"/"false" for booleans, not Python'"'"'s capitalized
    # str(True)/str(False). Getting this wrong silently breaks every caller
    # that does `[ "$x" = "true" ]` against a boolean config key (e.g.
    # git_backup.gpg_sign, allow_remote_change) whenever jq is absent: the
    # comparison never matches, so the key always reads as false.
    print(val if isinstance(val, str) else json.dumps(val))
except Exception:
    sys.exit(0)
' 2>/dev/null
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
        case "$_line" in
            CACHE_PATH=*) _path="${_line#*=}" ;;
            PYTHON=*)     _py="${_line#*=}" ;;
            JQ=*)         _jq="${_line#*=}" ;;
            *) return 1 ;;
        esac
    done < "$_f"
    [ -n "$_py" ] || return 1
    [ -n "$_jq" ] || return 1
    [ -n "$_path" ] || return 1
    [ "$_path" = "$PATH" ] || return 1
    case "$_jq" in
        jq|_jq_fallback) ;;
        *) return 1 ;;
    esac
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


_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"

[ -n "${_REMEMBER_LIB_SLUG_LOADED:-}" ] && return 0
_REMEMBER_LIB_SLUG_LOADED=1

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
        case "$winpath" in
            '\\?\UNC\'*) winpath='\\'"${winpath#'\\?\UNC\'}" ;;
            '\\?\'*)     winpath="${winpath#'\\?\'}" ;;
        esac
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
                    _decoded=$("${PYTHON:-python3}" "$_py_slug" "$path" 2>/dev/null) \
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
        _hash=$("${PYTHON:-python3}" "$_slug_py" --hash "$_orig" 2>/dev/null) || _hash=""
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


_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"

[ -n "${_LIB_MEMORY_DIR_LOADED:-}" ] && return 0
_LIB_MEMORY_DIR_LOADED=1

_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
:
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
    case "$data_dir" in
        *'{slug}'*) ;;
        *) return 0 ;;
    esac

    prefix="${data_dir%%\{slug\}*}"
    prefix="${prefix/#\~/$HOME}"

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
        case "$_out" in
            *"not a git repository"*) echo "untracked" ;;
            *) echo "could-not-tell" ;;
        esac
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
    "${PYTHON:-python3}" - "$_merged_cfg" "$_untrusted_haiku_source" "$_strip_model_reject" "$_project_drop_marker" "${_cfg_sources[@]}" > /dev/null 2>&1 <<< 'import json
import sys


def deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = deep_merge(out[k], v) if k in out else v
        return out
    return b


out_path = sys.argv[1]
# Empty string when the project layer'"'"'s `haiku` block is trusted (external
# storage mode, or no project cfg at all) -- never equal to a real path then,
# so nothing is stripped (#726, see the case switch this mirrors above).
untrusted_haiku_path = sys.argv[2]
# #757: "1" only when the SAME untrusted source is also git-tracked --
# model/reject_pattern are stripped alongside haiku only then, never for
# an untracked (the operator'"'"'s own) in-project config.
strip_model_reject = sys.argv[3] == "1"
# #748: empty when mktemp itself failed above -- tolerated the same way
# every other mktemp-failure path in this file is (fall through, don'"'"'t
# crash the merge over the logging side-channel itself).
drop_marker_path = sys.argv[4]


def load_documents(path):
    """Parse every whitespace-concatenated JSON document in `path` (#740):
    the untrusted project layer may ship more than one, and a plain
    json.load() raises `JSONDecodeError` on any file with more than one --
    which used to take the WHOLE merge down with it (the `|| cp
    "$_bundled_cfg" ...` fallback below), dropping the trusted user-global
    layer too rather than just stripping `haiku` from this file'"'"'s own
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
_dropped_project_layer = False
_dropped_trusted_layer = False
for path in sys.argv[5:]:
    if untrusted_haiku_path and path == untrusted_haiku_path:
        # #744: fail CLOSED -- if the untrusted file can'"'"'t even be loaded
        # (unreadable, a permissions error, malformed JSON, anything
        # load_documents() itself doesn'"'"'t already tolerate), drop just this
        # layer rather than let the exception propagate and crash the whole
        # merge down to the bundled-only fallback below, taking the trusted
        # user-global layer'"'"'s own overrides with it for no reason connected
        # to them. `json.JSONDecodeError` (raised by decoder.raw_decode() on
        # invalid JSON) and `UnicodeDecodeError` (raised by f.read() on a
        # file that isn'"'"'t valid text in the expected encoding) are both
        # ValueError subclasses -- catching only OSError let either one
        # through uncaught.
        try:
            docs = load_documents(path)
        except (OSError, ValueError):
            # #748: drop a marker for the shell to notice and report --
            # this process'"'"'s own stdout/stderr are discarded by the caller.
            # #804: also record the drop in a flag that becomes THIS
            # process'"'"'s own exit code below -- a second, independent
            # signal that does not depend on drop_marker_path'"'"'s own
            # mktemp (the shell side, above) having succeeded at all.
            _dropped_project_layer = True
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
    # #815: this is a TRUSTED source (bundled config, user-global config, or
    # the project config when it is NOT the untrusted-haiku source handled
    # above) -- but "trusted" only means the operator wrote it, not that it
    # parses. A malformed file here used to raise uncaught, exiting neither
    # 0 nor 3, so the shell'"'"'s bundled-only fallback fired below with NO
    # disclosure at all -- the #804 gate only checks the drop-marker file
    # (which this path never touches) or rc == 3 (reserved for the
    # untrusted-layer drop above). Skip just this layer instead, the same
    # fail-CLOSED shape the untrusted branch already uses, and signal it on
    # the interpreter'"'"'s own exit path rather than a second marker file.
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        _dropped_trusted_layer = True
        continue
    merged = deep_merge(merged, data)
# Strip `_`-prefixed doc keys, top-level only — same convention as the jq path.
merged = {k: v for k, v in merged.items() if not str(k).startswith("_")}
with open(out_path, "w") as f:
    json.dump(merged, f)
# #804: exit 3 means "merge above completed and $out_path was written, but
# the untrusted project layer was dropped" -- distinct from 0 (clean) and
# from any other non-zero exit (a genuine merge failure, still handled by
# the shell'"'"'s own bundled-only fallback below).
# #815: exit 4 means the same, but for a malformed TRUSTED layer (bundled
# config, user-global config, or project config outside the untrusted-haiku
# case); exit 5 means both a trusted AND the untrusted layer were dropped.
# Distinct codes so the shell can choose which warning(s) to print without a
# second marker file.
if _dropped_project_layer and _dropped_trusted_layer:
    sys.exit(5)
elif _dropped_project_layer:
    sys.exit(3)
elif _dropped_trusted_layer:
    sys.exit(4)
' || _py_merge_rc=$?
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
                case "$_legacy_repo_check" in
                    *"not a git repository"*) : ;;
                    (*) _legacy_other_tracked="could-not-tell" ;;
                esac
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

_config_is_private_key() {
    case "$1" in
        .haiku|.haiku.*) return 0 ;;
    esac
    return 1
}

_REMEMBER_CFG_FLATTEN_JQ='. as $doc | [paths(type != "object" and type != "array") | select(all(.[]; type == "string")) | select(.[0] != "haiku")] as $ks | if (($ks | flatten) | any(test("^[A-Za-z0-9_]+$") | not)) then "#refuse a config key is outside [A-Za-z0-9_]" elif (($ks | map(join("_")) | unique | length) != ($ks | length)) then "#refuse two config keys flatten to the same name" elif ([$ks[] as $p | $doc | getpath($p) | select(type == "string" and test("[\t\n]"))] | length) > 0 then "#refuse a config value contains a tab or a newline" else $ks[] as $p | ($doc | getpath($p)) as $v | select($v != null) | ($p | join(".")) + "\t" + ($v | tostring) end'

_REMEMBER_CFG_FLATTEN_PY='
import json, re, sys

def walk(node, prefix, out):
    if isinstance(node, dict):
        for k, v in node.items():
            walk(v, prefix + [k], out)
    elif isinstance(node, list):
        return
    else:
        out.append((prefix, node))

try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)

rows = []
walk(doc, [], rows)
rows = [(p, v) for p, v in rows if p and p[0] != "haiku" and v is not None]

ok = re.compile(r"^[A-Za-z0-9_]+$")
for p, v in rows:
    if not all(ok.match(part) for part in p):
        print("#refuse a config key is outside [A-Za-z0-9_]")
        sys.exit(0)
    if isinstance(v, str) and ("\t" in v or "\n" in v):
        print("#refuse a config value contains a tab or a newline")
        sys.exit(0)
slots = ["_".join(p) for p, _ in rows]
if len(set(slots)) != len(slots):
    print("#refuse two config keys flatten to the same name")
    sys.exit(0)

out = []
for p, v in rows:
    out.append(".".join(p) + "\t" + (v if isinstance(v, str) else json.dumps(v)))
sys.stdout.write("\n".join(out))
'

_remember_cfg_flatten_cache_path() {
    local LC_ALL=C  # bracket ranges below are byte-wise, not collated (#695)
    [ -n "${REMEMBER_DIR:-}" ] || return 1
    local _key="${REMEMBER_DIR//[!a-zA-Z0-9]/-}"
    [ "${#_key}" -gt 120 ] && _key="${_key: -120}"
    printf '%s' "${TMPDIR:-/tmp}/remember-config-cache-v2-${_key}"
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
            case "$_line" in
                '#REMEMBER_DIR='*)
                    _identity_raw="${_line#'#REMEMBER_DIR='}"
                    _remember_cfg_flatten_cache_valid_value "$_identity_raw" || {
                        rm -f "$_f" 2>/dev/null
                        return 1
                    }
                    continue
                    ;;
                *)
                    rm -f "$_f" 2>/dev/null
                    return 1
                    ;;
            esac
        fi
        if [ "$_stage" = "1" ]; then
            _stage=2
            case "$_line" in
                '#RCFG_EXISTS='*)
                    _exists_raw="${_line#'#RCFG_EXISTS='}"
                    case "$_exists_raw" in
                        *[!01]*|'')
                            rm -f "$_f" 2>/dev/null
                            return 1
                            ;;
                    esac
                    continue
                    ;;
                *)
                    rm -f "$_f" 2>/dev/null
                    return 1
                    ;;
            esac
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

    local _assign _assign_name _assign_value
    for _assign in ${_lines[@]+"${_lines[@]}"}; do
        _assign_name="${_assign%%$'\t'*}"
        _assign_value="${_assign#*$'\t'}"
        _remember_cfg_flatten_q_decode "$_assign_name" "$_assign_value" || {
            rm -f "$_f" 2>/dev/null
            return 1
        }
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
        _dump=$(jq -r "$_REMEMBER_CFG_FLATTEN_JQ" "$REMEMBER_CONFIG" 2>/dev/null) || _rc=1
    else
        declare -f _remember_python >/dev/null 2>&1 && _remember_python
        _dump=$("${PYTHON:-python3}" -c "$_REMEMBER_CFG_FLATTEN_PY" "$REMEMBER_CONFIG" 2>/dev/null) || _rc=1
    fi

    if [ "$_rc" -ne 0 ]; then
        echo "remember: could not read ${REMEMBER_CONFIG} -- is it valid JSON? falling back to per-key reads" >&2
        _REMEMBER_CFG_STATE="fallback"
        return 0
    fi

    case "$_dump" in
        '#refuse'*)
            [ "${REMEMBER_DEBUG:-}" = "1" ] && \
                echo "remember: ${_dump#'#refuse' } -- reading config one key at a time" >&2
            _REMEMBER_CFG_STATE="fallback"
            return 0
            ;;
    esac

    local _k _v
    while IFS=$'\t' read -r _k _v; do
        [ -n "$_k" ] || continue
        printf -v "_RCFG_${_k//./_}" '%s' "$_v"
    done <<< "$_dump"
    _remember_cfg_flatten_cache_publish "$_dump"
    _REMEMBER_CFG_STATE="ok"
}

config() {
    local _cfg_result
    config_into _cfg_result "$1" "$2"
    printf '%s\n' "$_cfg_result"
}

config_into() {
    local _cfg_into_var="$1"
    local _cfg_into_key="$2"
    local _cfg_into_default="$3"

    if ! ( LC_ALL=C; [[ "$_cfg_into_key" =~ ^\.[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$ ]] ); then
        [ "${REMEMBER_DEBUG:-}" = "1" ] && \
            echo "remember: config() key '$_cfg_into_key' is not a plain dotted path -- returning the default rather than looking it up" >&2
        printf -v "$_cfg_into_var" '%s' "$_cfg_into_default"
        return
    fi

    if [ -z "$_REMEMBER_CFG_STATE" ] || \
       [ "$_REMEMBER_CFG_LOADED_FROM" != "${REMEMBER_CONFIG:-}" ]; then
        _config_load
    fi

    if [ "$_REMEMBER_CFG_STATE" = "ok" ] && ! _config_is_private_key "$_cfg_into_key"; then
        local _cfg_into_slot="_RCFG_${_cfg_into_key#.}"
        _cfg_into_slot="${_cfg_into_slot//./_}"
        local _cfg_into_hit="${!_cfg_into_slot:-}"
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
        _cfg_into_val=$(jq -r "if $_cfg_into_key == null then \"\" else ($_cfg_into_key | tostring) end" \
            "$REMEMBER_CONFIG" 2>/dev/null)
    elif type _jq_fallback >/dev/null 2>&1; then
        _cfg_into_val=$(_jq_fallback -r "$_cfg_into_key" "$REMEMBER_CONFIG" 2>/dev/null)
    else
        _cfg_into_val=$("${PYTHON:-python3}" -c '
import json, sys
try:
    data = json.load(open(sys.argv[2]))
    keys = sys.argv[1].strip(".").split(".")
    v = data
    for k in keys:
        if k and isinstance(v, dict):
            v = v.get(k)
        if v is None:
            break
    if v is not None:
        # jq -r semantics: raw strings, JSON textual form otherwise
        # (crucially "true"/"false", not Python str(True)/str(False)).
        print(v if isinstance(v, str) else json.dumps(v))
except Exception:
    pass
' "$_cfg_into_key" "$REMEMBER_CONFIG" 2>/dev/null)
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




_ROTATE_ESCALATE_AFTER=3

_ROTATE_STATE_NAME=".rotate-failed"

_ROTATE_MAX_PARTS=100



declare -F log >/dev/null 2>&1 || log() {
    printf '%s [%s] %s\n' "$(_remember_date +%H:%M:%S)" "$1" "$2" >&2
}
declare -F report_error >/dev/null 2>&1 || report_error() { log "$1" "$2"; }
log "hook" "session-end: reason=$SESSION_END_REASON session=${STDIN_SESSION_ID:-unresolved}"

if ! mkdir -p "$REMEMBER_DIR/logs/autonomous" 2>/dev/null; then
    report_error "session-end" "WARNING: could not create $REMEMBER_DIR/logs/autonomous -- this session's flush will not be recorded, and /remember:doctor may misreport SessionEnd as never having fired."
fi
_END_LOG="$REMEMBER_DIR/logs/autonomous/session-end-$(_remember_date +%H%M%S)-$$.log"
if ! printf '%s [session-end] flush started\n' "$(_remember_date +%H:%M:%S)" >> "$_END_LOG" 2>/dev/null; then
    report_error "session-end" "WARNING: could not seed $_END_LOG -- if this file stays absent or empty, an ordinary housekeeping sweep will reclaim it, and /remember:doctor may misreport this session as one where SessionEnd never fired."
fi

if [ ! -d "$REMEMBER_DIR" ]; then
    report_error "session-end" "WARNING: $REMEMBER_DIR does not exist and could not be created -- nothing was flushed at session end."
    exit 0
fi

SAVE_SCRIPT="$PIPELINE_DIR/scripts/save-session.sh"
if [ ! -f "$SAVE_SCRIPT" ]; then
    report_error "session-end" "WARNING: $SAVE_SCRIPT is missing -- nothing was flushed at session end. Reinstall the plugin."
    exit 0
fi

if [ -n "${REMEMBER_TEST_COMPLETION_MARKER:-}" ]; then
    printf '%s session-end: about to launch subshell\n' "$(_remember_date +%H:%M:%S)" \
        >> "$REMEMBER_TEST_COMPLETION_MARKER" 2>&1
fi
(
    if [ -n "$STDIN_SESSION_ID" ]; then
        bash "$SAVE_SCRIPT" "$STDIN_SESSION_ID" --force
    else
        bash "$SAVE_SCRIPT" --force
    fi
    _flush_status=$?
    if [ -n "${REMEMBER_TEST_COMPLETION_MARKER:-}" ]; then
        printf '%s session-end: save-session.sh exited status=%s\n' \
            "$(_remember_date +%H:%M:%S)" "$_flush_status" >> "$REMEMBER_TEST_COMPLETION_MARKER" 2>&1
    fi
    if [ "$_flush_status" -ne 0 ]; then
        report_error "session-end" "WARNING: save-session.sh --force exited $_flush_status at session end -- this session's unsaved tail may be lost. See $_END_LOG for what save-session.sh itself logged."
    fi
) < /dev/null >> "$_END_LOG" 2>&1 &
echo $! > "$REMEMBER_DIR/tmp/save-session.pid" 2>/dev/null
disown 2>/dev/null || true

exit 0
