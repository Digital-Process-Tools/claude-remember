#!/bin/bash

if [ -z "${PIPELINE_DIR:-}" ]; then
    if [ -n "${PROJECT_DIR:-}" ]; then
        PIPELINE_DIR="${PROJECT_DIR}/.claude/remember"
    else
        PIPELINE_DIR="./.claude/remember"
    fi
fi

_remember_log_src_dir="${BASH_SOURCE[0]%/*}"
[ "$_remember_log_src_dir" = "${BASH_SOURCE[0]}" ] && _remember_log_src_dir="$(pwd)"
source "$_remember_log_src_dir/lib-memory-dir.sh"


REMEMBER_LOG_DIR="${REMEMBER_DIR}/logs"
_remember_log_dir_unsafe() {
    local LC_ALL=C
    { [[ "$REMEMBER_DIR" != /* ]] && [[ "$REMEMBER_DIR" != [A-Za-z]:[/\\]* ]]; } \
        || [[ "$REMEMBER_DIR" == *$'\n'* || "$REMEMBER_DIR" == *$'\r'* ]]
}
_REMEMBER_LOG_SINK=""
if _remember_log_dir_unsafe; then
    echo "FATAL: unsafe REMEMBER_DIR ($REMEMBER_DIR) -- refusing to mkdir" >&2
    _REMEMBER_LOG_SINK=/dev/null
elif [ ! -d "$REMEMBER_LOG_DIR" ] && ! mkdir -p "$REMEMBER_LOG_DIR" 2>/dev/null; then
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
    [ "$1" = .haiku ] || [ "${1#.haiku.}" != "$1" ]
}



_remember_cfg_flatten_cache_path() {
    local LC_ALL=C
    [ -n "${REMEMBER_DIR:-}" ] || return 1
    local _slug="${REMEMBER_DIR//[!a-zA-Z0-9]/-}"
    [ "${#_slug}" -gt 120 ] && _slug="${_slug: -120}"
    printf '%s' "${TMPDIR:-/tmp}/remember-config-cache-v2-${_slug}"
}

_remember_cfg_flatten_cache_path_v1() {
    local _v2
    _v2=$(_remember_cfg_flatten_cache_path) || return 1
    printf '%s' "${_v2/-v2-/-}"
}

_remember_cfg_flatten_cache_sources() {
    printf '%s\n' "${PIPELINE_DIR:-}/config.json"
    printf '%s\n' "${HOME:-}/.remember/config.json"
    printf '%s\n' "${REMEMBER_DIR:-}/config.json"
}

_remember_cfg_flatten_cache_is_standard_merge() {
    [[ "${REMEMBER_CONFIG:-}" == */remember-config-* ]]
}

_remember_cfg_flatten_cache_valid_value() {
    local _value="$1"
    local LC_ALL=C
    [[ "$_value" =~ ^([^\\]|\\[\\nrt])*$ ]]
}

_remember_cfg_flatten_cache_valid_line() {
    local LC_ALL=C
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

_remember_cfg_flatten_cache_exists_into() {
    local _src _sources _m=""
    _sources=$(_remember_cfg_flatten_cache_sources)
    while IFS= read -r _src; do
        [ -n "$_src" ] || continue
        if [ -e "$_src" ]; then
            _m="${_m}1"
            [ -z "${2:-}" ] || [ "$2" -nt "$_src" ] || return 1
        else
            _m="${_m}0"
        fi
    done <<< "$_sources"
    printf -v "$1" '%s' "$_m"
}

_remember_cfg_flatten_cache_load() {
    [ "${REMEMBER_CONFIG_CACHE:-1}" = "1" ] || return 1
    _remember_cfg_flatten_cache_is_standard_merge || return 1
    local _f
    _f=$(_remember_cfg_flatten_cache_path) || return 1
    [ -f "$_f" ] && [ ! -L "$_f" ] && [ -O "$_f" ] && [ -r "$_f" ] || return 1
    local _exists_now=""
    _remember_cfg_flatten_cache_exists_into _exists_now "$_f" || return 1

    local _line _lines=() _stage=0 _identity_raw="" _exists_raw="" _bad=""
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        if [ "$_stage" = "0" ]; then
            _stage=1
            _identity_raw="${_line#'#REMEMBER_DIR='}"
            [ "$_identity_raw" != "$_line" ] \
                && _remember_cfg_flatten_cache_valid_value "$_identity_raw" || { _bad=1; break; }
        elif [ "$_stage" = "1" ]; then
            _stage=2
            _exists_raw="${_line#'#RCFG_EXISTS='}"
            [ "$_exists_raw" != "$_line" ] || { _bad=1; break; }
            if [[ "$_exists_raw" == *[!01]* ]] || [ -z "$_exists_raw" ]; then
                _bad=1
                break
            fi
        elif _remember_cfg_flatten_cache_valid_line "$_line"; then
            _lines[${#_lines[@]}]="$_line"
        else
            _bad=1
            break
        fi
    done < "$_f"
    if [ -n "$_bad" ] || [ "$_stage" != "2" ]; then
        rm -f "$_f" 2>/dev/null
        return 1
    fi

    [ "$_exists_raw" = "$_exists_now" ] || return 1

    local _identity
    if ! _remember_cfg_flatten_q_decode _identity "$_identity_raw" \
        || [ "$_identity" != "${REMEMBER_DIR:-}" ]; then
        rm -f "$_f" 2>/dev/null
        return 1
    fi

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
    local _k _v _exists_now=""
    _remember_cfg_flatten_cache_exists_into _exists_now
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
    local _f_v1="${_f/-v2-/-}"
    [ -f "$_f_v1" ] && rm -f "$_f_v1" 2>/dev/null
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

    local _dump="" _rc=0 _cfg_flatten_dir="${BASH_SOURCE[0]%/*}"
    [ "$_cfg_flatten_dir" = "${BASH_SOURCE[0]}" ] && _cfg_flatten_dir="$(pwd)"
    if command -v jq >/dev/null 2>&1; then
        _dump=$(jq -r -f "$_cfg_flatten_dir/cfg_flatten.jq" "$REMEMBER_CONFIG" 2>/dev/null) || _rc=1
    else
        declare -f _remember_python >/dev/null 2>&1 && _remember_python
        _dump=$(_remember_slug_run_python "$_cfg_flatten_dir/cfg_flatten.py" "$REMEMBER_CONFIG" 2>/dev/null) || _rc=1
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


config_into() {
    local _cfg_into_var="$1"
    local _cfg_into_name="$2"
    local _cfg_into_default="$3"

    if ! ( LC_ALL=C; [[ "$_cfg_into_name" =~ ^\.[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$ ]] ); then
        [ "${REMEMBER_DEBUG:-}" = "1" ] && \
            echo "remember: config() key '$_cfg_into_name' is not a plain dotted path -- returning the default" >&2
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
        _cfg_into_val=$(_remember_slug_run_python "$_cfg_py_dir/jq_fallback_get.py" "$REMEMBER_CONFIG" "$_cfg_into_name")
    fi
    [ -n "$_cfg_into_val" ] || _cfg_into_val="$_cfg_into_default"
    printf -v "$_cfg_into_var" '%s' "$_cfg_into_val"
}

_config_load

debug_enabled() {
    local _default="${1:-0}"
    if [ -n "${REMEMBER_DEBUG:-}" ]; then
        [ "$REMEMBER_DEBUG" = "1" ]
        return
    fi
    local _debug_cfg
    config_into _debug_cfg '.debug' ''
    if [ "$_debug_cfg" = true ]; then
        return 0
    elif [ "$_debug_cfg" = false ]; then
        return 1
    fi
    [ "$_default" = "1" ]
}

config_into REMEMBER_TZ ".timezone" ""
export REMEMBER_TZ

config_into REMEMBER_PROMPT_STAMP ".prompt_stamp" "full"
if [ "$REMEMBER_PROMPT_STAMP" != stable ] && [ "$REMEMBER_PROMPT_STAMP" != off ]; then
    REMEMBER_PROMPT_STAMP="full"
fi
export REMEMBER_PROMPT_STAMP

_remember_is_uint() { [[ -n "$1" && "$1" != *[!0-9]* ]]; }
config_into REMEMBER_SAVE_COOLDOWN ".cooldowns.save_seconds" 120
_remember_is_uint "$REMEMBER_SAVE_COOLDOWN" || REMEMBER_SAVE_COOLDOWN=120
export REMEMBER_SAVE_COOLDOWN

config_into REMEMBER_DELTA_THRESHOLD ".thresholds.delta_lines_trigger" 50
_remember_is_uint "$REMEMBER_DELTA_THRESHOLD" || REMEMBER_DELTA_THRESHOLD=50
export REMEMBER_DELTA_THRESHOLD

[ -n "${REMEMBER_MODEL:-}" ] || config_into REMEMBER_MODEL ".model" "haiku"
export REMEMBER_MODEL
[ -n "${REMEMBER_REJECT_PATTERN:-}" ] || config_into REMEMBER_REJECT_PATTERN ".reject_pattern" ""
export REMEMBER_REJECT_PATTERN

source "$_remember_log_src_dir/lib-clock.sh"
unset _remember_log_src_dir

MEMORY_LOG_DATE=""
_remember_date_into MEMORY_LOG_DATE +%Y-%m-%d
if [ -n "$_REMEMBER_LOG_SINK" ]; then
    MEMORY_LOG_FILE="$_REMEMBER_LOG_SINK"
else
    MEMORY_LOG_FILE="${REMEMBER_LOG_DIR}/memory-${MEMORY_LOG_DATE}.log"
fi

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
        if [ -n "$_REMEMBER_LOG_SINK" ]; then
            MEMORY_LOG_FILE="$_REMEMBER_LOG_SINK"
        else
            MEMORY_LOG_FILE="${REMEMBER_LOG_DIR}/memory-${MEMORY_LOG_DATE}.log"
        fi
    fi
    _REMEMBER_LOG_LAST_TIME="$timestamp"
    [ -n "$_REMEMBER_LOG_LAST_EPOCH" ] && _REMEMBER_LOG_LAST_EPOCH="$EPOCHSECONDS"
    message="${timestamp} [${component}] $(printf '%s' "$message" | LC_ALL=C tr '[:cntrl:]' ' ')"
    echo "$message" >> "$MEMORY_LOG_FILE" 2>/dev/null || echo "$message" >&2
}

log_usage() {
    local component="$1"
    local input="${2:-0}"
    local output="${3:-0}"
    local cache="${4:-0}"
    local cost="${5:-}"
    local msg="tokens: ${input}+${cache}cache->${output}out"
    [ -n "$cost" ] && msg="${msg} (\$${cost})"
    log "$component" "$msg"
}

assign_kv() {
    local LC_ALL=C
    while IFS= read -r line; do
        line="${line%$'\r'}"
        if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]]; then
            local _var_name="${BASH_REMATCH[1]}"
            local _val="${BASH_REMATCH[2]}"
            printf -v "$_var_name" '%s' "$_val"
        fi
    done
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
    local _msg="WARNING: hook TIMED OUT: $_event/$_name did not return within ${_budget}s and was stopped ($_how). Whether it did its work is UNKNOWN; this is not a failure report. Raise hooks.dispatch_timeout_seconds if it is honestly slow, or 0 to disable the bound. It said: $_said"
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
            if [ -z "$_budget" ] || [ "${_budget#*[!0-9]}" != "$_budget" ]; then _budget=$_DISPATCH_BUDGET_FALLBACK; fi
            _grace=$(config '.hooks.dispatch_kill_grace_seconds' "$_DISPATCH_KILL_GRACE_DEFAULT")
            if [ -z "$_grace" ] || [ "${_grace#*[!0-9]}" != "$_grace" ]; then _grace=$_DISPATCH_KILL_GRACE_DEFAULT; fi
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
            [ -n "$_REMEMBER_LOG_SINK" ] || [ -d "$REMEMBER_DIR/tmp" ] \
                || mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            if ! : > "$_err_file" 2>/dev/null || ! : > "$_out_file" 2>/dev/null; then
                _err_file=""
                _out_file=""
                _err_unavailable="yes"
            else
                _to_file="$REMEMBER_DIR/tmp/dispatch-timeout.$$"
                rm -f "$_to_file" 2>/dev/null || true
            fi
        fi

        local _rc _said
        if [ -n "$_err_file" ]; then
            REMEMBER_PROJECT="${PROJECT_DIR:-.}" "$hook" >"$_out_file" 2>"$_err_file" &
            _dispatch_supervise "$!" "$_budget" "$_grace" "$_to_file"
            _rc=$_DISPATCH_RC
            _dispatch_stdout_relay "$_out_file" "$event" "${hook##*/}"
        else
            REMEMBER_PROJECT="${PROJECT_DIR:-.}" "$hook" >/dev/null 2>/dev/null &
            _dispatch_supervise "$!" "$_budget" "$_grace" ""
            _rc=$_DISPATCH_RC
            printf '%s%s/%s -- output NOT SHOWN: no writable %s/tmp to capture it, so it was discarded ===\n' \
                "$_DISPATCH_FRAME" "$event" "${hook##*/}" "$REMEMBER_DIR"
        fi

        [ "$_DISPATCH_TIMEDOUT" -eq 1 ] || [ "$_rc" -ne 0 ] || continue
        if [ -n "$_err_file" ]; then
            _said=$(_dispatch_stderr_excerpt "$_err_file")
        else
            _said="stderr not captured (no writable $REMEMBER_DIR/tmp); rerun the hook by hand to see it"
        fi
        if [ "$_DISPATCH_TIMEDOUT" -eq 1 ]; then
            local _how="SIGTERM, then SIGKILL after ${_grace}s"
            [ -n "$_to_file" ] || _how="$_how; inferred from the exit status (no writable tmp)"
            _dispatch_report_timeout "$event" "${hook##*/}" "$_budget" "$_how" "$_said"
        else
            _dispatch_report_failure "$event" "${hook##*/}" "$_rc" "$_said"
        fi
    done
    [ -z "$_err_file" ] || rm -f "$_err_file" "$_out_file" 2>/dev/null
    [ -z "$_to_file" ] || rm -f "$_to_file" 2>/dev/null
    return 0
}

_ROTATE_ESCALATE_AFTER=3

_ROTATE_STATE_NAME=".rotate-failed"

_ROTATE_MAX_PARTS=100

_rotate_missing_members() {
    local dir="$1" archive="$2"
    shift 2
    local listing member missing=""
    if ! listing=$( { cd "$dir" && tar -tzf "$archive"; } 2>/dev/null ); then
        printf '%s' "the archive cannot be read back"
        return 0
    fi
    for member in "$@"; do
        printf '%s\n' "$listing" | grep -Fqx -- "$member" \
            || missing="${missing}${missing:+, }${member}"
    done
    printf '%s' "$missing"
}

rotate_logs() {
    local state="${REMEMBER_LOG_DIR}/${_ROTATE_STATE_NAME}"

    local old_logs
    old_logs=$(find "$REMEMBER_LOG_DIR" -name "memory-*.log" -mtime +7 2>/dev/null)
    if [ -z "$old_logs" ]; then
        rm -f "$state" 2>/dev/null || true
        return 0
    fi

    local archive_month
    archive_month=$(date -v-7d +%Y-%m 2>/dev/null || date -d '7 days ago' +%Y-%m)
    local count
    count=$(echo "$old_logs" | wc -l | tr -d ' ')

    local basenames=()
    while IFS= read -r f; do
        basenames+=("$(basename "$f")")
    done <<< "$old_logs"

    local archive_name="" candidate part=1
    while [ "$part" -le "$_ROTATE_MAX_PARTS" ]; do
        if [ "$part" -eq 1 ]; then
            candidate="logs-${archive_month}.tar.gz"
        else
            candidate="logs-${archive_month}-part${part}.tar.gz"
        fi
        if [ ! -e "${REMEMBER_LOG_DIR}/${candidate}" ] \
           && ( set -C; : > "${REMEMBER_LOG_DIR}/${candidate}" ) 2>/dev/null; then
            archive_name="$candidate"
            break
        fi
        part=$((part + 1))
    done

    local err=""
    if [ -z "$archive_name" ]; then
        err="no unused archive name for ${archive_month} after ${_ROTATE_MAX_PARTS} tries -- the names are taken, or ${REMEMBER_LOG_DIR} is not writable. Refusing to overwrite an existing archive"
    elif err=$( { cd "$REMEMBER_LOG_DIR" && tar -czf "$archive_name" "${basenames[@]}"; } 2>&1 ); then
        local missing
        missing=$(_rotate_missing_members "$REMEMBER_LOG_DIR" "$archive_name" "${basenames[@]}")
        if [ -z "$missing" ]; then
            while IFS= read -r f; do rm -f "$f"; done <<< "$old_logs"
            rm -f "$state" 2>/dev/null || true
            log "rotate" "archived ${count} logs -> ${archive_name}"
            return 0
        fi
        rm -f "${REMEMBER_LOG_DIR}/${archive_name}" 2>/dev/null || true
        err="tar exited 0 but ${archive_name} does not list back: ${missing}"
    else
        rm -f "${REMEMBER_LOG_DIR}/${archive_name}" 2>/dev/null || true
    fi

    local first_line
    first_line=$(printf '%s\n' "$err" | head -1)
    [ -z "$first_line" ] && first_line="tar exited non-zero without a diagnostic"

    local prev=0
    if [ -f "$state" ]; then read -r prev < "$state" 2>/dev/null || prev=0; fi
    if [ -z "$prev" ] || [ "${prev#*[!0-9]}" != "$prev" ]; then prev=0; fi
    local streak=$((10#$prev + 1))
    printf '%s\n%s\n%s\n' "$streak" "$(date '+%Y-%m-%d %H:%M:%S')" "$first_line" \
        > "$state" 2>/dev/null || true

    if [ "$streak" -ge "$_ROTATE_ESCALATE_AFTER" ]; then
        log "rotate" "ERROR: log rotation has now failed ${streak} times in a row -- ${count} aged log files are accumulating unarchived in ${REMEMBER_LOG_DIR} and nothing will clear them until this is fixed. Run /remember:doctor. Last error: ${first_line}"
    else
        log "rotate" "ERROR: tar failed for ${count} logs: ${first_line}"
    fi
    return 1
}
