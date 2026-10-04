#!/usr/bin/env bash

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
            case "$_owner" in
                *[!0-9]*) continue ;;
            esac
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
    case "$_pid" in
        *[!0-9]*) return 1 ;;
    esac
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
    case "$_r" in
        *[.,]*) _s="${_r%%[.,]*}"; _f="${_r#*[.,]}" ;;
        *)      _s="$_r"; _f="000000" ;;
    esac
    if [ -z "$_s" ] || [ "${_s#*[!0-9]}" != "$_s" ]; then
        _LOCK_TIMING_NOW=0; return 0
    fi
    case "$_f" in
        *[!0-9]*) _f="000000" ;;
    esac
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
    case "$_LOCK_TIMING_PRECISION" in
        us)
            _lock_timing_us_to_ms "$EPOCHREALTIME"
            ;;
        ms)
            _n=$(date +%s%N 2>/dev/null) || _n=""
            _lock_timing_ns_to_ms "$_n"
            ;;
        *)
            _n=$(date +%s 2>/dev/null) || _n=""
            _lock_timing_s_to_ms "$_n"
            ;;
    esac
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
