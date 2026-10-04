#!/bin/bash
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
source "$_REMEMBER_SRC_DIR/lib-slug.sh"
unset _REMEMBER_SRC_DIR
