#!/usr/bin/env bash

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
    local _os="${OSTYPE:-}"
    [ "${REMEMBER_UTF8_STRICT:-0}" = "1" ] || [ "${_os#linux}" != "$_os" ]
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
