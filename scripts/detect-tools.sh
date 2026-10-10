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

_REMEMBER_PY_FLOOR_MAJOR=3
_REMEMBER_PY_FLOOR_MINOR=9

_remember_run_python() {
    if [ "$PYTHON" = python3 ]; then
        python3 "$@"
    elif [ "$PYTHON" = python ]; then
        python "$@"
    elif [ "$PYTHON" = "py -3" ]; then
        py -3 "$@"
    elif [ "$PYTHON" = py ]; then
        py "$@"
    elif [[ "$PYTHON" == python3.[0-9]* ]]; then
        command "$PYTHON" "$@"
    else
        echo "FATAL: _remember_run_python: unrecognized PYTHON value '$PYTHON'" >&2
        return 127
    fi
}

_py_ok() {
    [[ "$1" =~ ([0-9]+)\.([0-9]+) ]] || return 1
    (( ${BASH_REMATCH[1]}*100+${BASH_REMATCH[2]} >= _REMEMBER_PY_FLOOR_MAJOR*100+_REMEMBER_PY_FLOOR_MINOR ))
}

_remember_tools_cache_load() {
    [ "${REMEMBER_TOOLS_CACHE:-1}" = "1" ] || return 1
    local _f="$_REMEMBER_TOOLS_CACHE"
    [ -f "$_f" ] && [ ! -L "$_f" ] && [ -O "$_f" ] && [ -r "$_f" ] || return 1
    local _line _path="" _py="" _jq="" _pf=""
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        if [ "${_line#CACHE_PATH=}" != "$_line" ]; then
            _path="${_line#*=}"
        elif [ "${_line#PYTHON=}" != "$_line" ]; then
            _py="${_line#*=}"
        elif [ "${_line#JQ=}" != "$_line" ]; then
            _jq="${_line#*=}"
        elif [ "${_line#PYFLOOR=}" != "$_line" ]; then
            _pf="${_line#*=}"
        else
            return 1
        fi
    done < "$_f"
    [[ -n $_py && -n $_jq && -n $_pf ]] || return 1
    [ -n "$_path" ] && [ "$_path" = "$PATH" ] || return 1
    [ "$_jq" = jq ] || [ "$_jq" = _jq_fallback ] || return 1
    PYTHON="$_py"
    JQ="$_jq"
    export PYTHON JQ
    return 0
}

_remember_tools_cache_publish() {
    [ "${REMEMBER_TOOLS_CACHE:-1}" = "1" ] || return 0
    local _f="$_REMEMBER_TOOLS_CACHE" _t
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    printf '%s=%s\n' CACHE_PATH "$PATH" PYTHON "$PYTHON" JQ "$JQ" PYFLOOR 1 \
        > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t" 2>/dev/null
    return 0
}

if _remember_tools_cache_load; then
    _remember_python() { [ -n "${PYTHON:-}" ]; }
else

PYTHON=""
_remember_python() {
    [ -n "${PYTHON:-}" ] && return 0
    local _c _ps _pr="" _bl="" _v
    for _c in "python3" "python" "py -3" "py" python3.{13..9}; do
        if ! command -v "${_c%% *}" >/dev/null 2>&1; then
            _pr="$_pr
$_c: -"
            continue
        fi
        _v=$(PYTHON="$_c" _remember_run_python -V 2>&1)
        _ps=$?
        if [ "$_ps" -ne 0 ]; then
            _pr="$_pr
$_c: exit $_ps"
            continue
        fi
        if _py_ok "$_v"; then
            PYTHON="$_c"
            break
        fi
        _bl="$_bl$_c $_v"
    done
    if [ -z "$PYTHON" ]; then
        [ -n "$_bl" ] && echo "FATAL: below the floor this plugin supports:$_bl" >&2 \
            || echo "FATAL: No working Python found $PATH$_pr" >&2
        return 1
    fi
    export PYTHON
    _remember_tools_cache_publish
    return 0
}

if command -v jq >/dev/null 2>&1; then
    JQ="jq"
else
    JQ="_jq_fallback"
fi
export JQ

if [ "${_REMEMBER_LAZY_PYTHON:-0}" != "1" ]; then
    _remember_python || exit 1
fi
fi

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
