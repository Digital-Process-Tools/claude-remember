#!/bin/bash
_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="$(pwd)"

if [ -n "${PROJECT_DIR:-}" ] && [ -d "${PROJECT_DIR:-}" ] \
    && [ -n "${PIPELINE_DIR:-}" ] && [ -d "${PIPELINE_DIR:-}" ] \
    && [ -n "${REMEMBER_DIR:-}" ]; then
    PLUGIN_ROOT="$PIPELINE_DIR"
    PROJECT="$PROJECT_DIR"
    umask 077
    SYS_TMPDIR="${TMPDIR:-/tmp}"
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
    _REMEMBER_LAZY_PYTHON=1
    source "$_HOOK_DIR/detect-tools.sh"

    _REMEMBER_FAST=0
    if [ -n "${REMEMBER_RECOVERY_ENABLED:-}" ]; then
        _REMEMBER_FAST=1
    else
        source "$_HOOK_DIR/lib-env-cache.sh" 2>/dev/null
        if command -v _remember_env_cache_load >/dev/null 2>&1 \
            && _remember_env_cache_load && [ -n "${REMEMBER_RECOVERY_ENABLED:-}" ]; then
            _REMEMBER_FAST=1
        fi
    fi
    if [ "$_REMEMBER_FAST" = "1" ]; then
        log() {
            unset -f log
            source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
            declare -F log >/dev/null 2>&1 || log() { :; }
            log "$@"
        }
    fi
    if [ "$_REMEMBER_FAST" != "1" ]; then
        source "$_HOOK_DIR/log.sh" 2>/dev/null
        _remember_gap_log_sourced=1
        if ! command -v _remember_date >/dev/null 2>&1; then
            echo "capture-gap-check: ERROR -- failed to source $_HOOK_DIR/log.sh" >&2
            exit 127
        fi
    fi
else
    REMEMBER_PATHS_SOFT_FAIL=1 source "$_HOOK_DIR/resolve-paths.sh" || exit 0

    _REMEMBER_LAZY_PYTHON=1
    source "$_HOOK_DIR/detect-tools.sh"
    source "$_HOOK_DIR/bootstrap-dirs.sh"
    PLUGIN_ROOT="$PIPELINE_DIR"
    PROJECT="$PROJECT_DIR"
    source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
    _remember_gap_log_sourced=1
    if ! command -v _remember_date >/dev/null 2>&1; then
        echo "capture-gap-check: ERROR -- failed to source $PLUGIN_ROOT/scripts/log.sh" >&2
        exit 127
    fi
fi

CURRENT_SESSION_ID="${1:-}"

if [ -n "$CURRENT_SESSION_ID" ]; then
    _remember_deferred_pid_file="$REMEMBER_DIR/tmp/deferred-${CURRENT_SESSION_ID}.pid"
    _remember_cleanup_deferred_pid_file() {
        [ -f "$_remember_deferred_pid_file" ] || return 0
        local _remember_dpf_owner
        read -r _remember_dpf_owner < "$_remember_deferred_pid_file" 2>/dev/null
        [ "$_remember_dpf_owner" = "$$" ] \
            && rm -f "$_remember_deferred_pid_file" 2>/dev/null
        return 0
    }
    if [ -n "${_remember_gap_log_sourced:-}" ]; then
        _remember_gap_trap_raw=$(trap -p EXIT 2>/dev/null)
        _remember_gap_existing_trap="${_remember_gap_trap_raw#trap -- \'}"
        _remember_gap_existing_trap="${_remember_gap_existing_trap%\' EXIT}"
        while [[ "$_remember_gap_existing_trap" == *"'\''"* ]]; do
            _remember_gap_existing_trap="${_remember_gap_existing_trap%%"'\''"*}'${_remember_gap_existing_trap#*"'\''"}"
        done
        unset _remember_gap_trap_raw
        if [ -n "$_remember_gap_existing_trap" ]; then
            trap "${_remember_gap_existing_trap}; _remember_cleanup_deferred_pid_file" EXIT
        else
            trap _remember_cleanup_deferred_pid_file EXIT
        fi
        unset _remember_gap_existing_trap
    else
        trap _remember_cleanup_deferred_pid_file EXIT
    fi
fi

if [ -n "${REMEMBER_TEST_DEFER_DELAY:-}" ]; then
    sleep "$REMEMBER_TEST_DEFER_DELAY" 2>/dev/null || true
fi

PROJECT_PATH_SLUG="$(session_dir_slug "$PROJECT")"
SESSIONS_DIR="$(claude_projects_dir)/${PROJECT_PATH_SLUG}"
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
_remember_write_slug_record() {
    local _dir="$REMEMBER_DIR/tmp" _tmp
    [ "${REMEMBER_DIR_UNSAFE:-0}" = 1 ] && return 0
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
            printf '%s=%s\n' format 1 status ok project_dir "$PROJECT" slug "$PROJECT_PATH_SLUG" \
                sessions_dir "$SESSIONS_DIR" memory_dir "$REMEMBER_DIR"
            [ -z "$CURRENT_SESSION_ID" ] || printf 'session_id=%s\n' "$CURRENT_SESSION_ID"
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

    [ "${REMEMBER_DIR_UNSAFE:-0}" = 1 ] && return 0
    local _dir="$REMEMBER_STORE_ROOT/tmp"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0

    local _index="$_dir/sessions" _lock="$_dir/sessions.lock" _tmp

    source "$_HOOK_DIR/lib-lock.sh" 2>/dev/null || return 0
    command -v _lock_acquire_impl >/dev/null 2>&1 || return 0
    _lock_acquire_impl "$_lock" "$SLUG_INDEX_LOCK_TIMEOUT" || return 0

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
        _lock_release_impl "$_lock" 2>/dev/null
        return 0
    }

    mv -f "$_tmp" "$_index" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    _lock_release_impl "$_lock" 2>/dev/null
    return 0
}

_remember_write_case_divergence() {
    source "$_HOOK_DIR/lib-case-divergence.sh" 2>/dev/null || return 0
    command -v remember_case_divergence >/dev/null 2>&1 || return 0
    remember_case_divergence

    local _dir="$REMEMBER_DIR/tmp" _tmp _old="" _body="" NL=$'\n'

    _body="format=1${NL}status=$REMEMBER_CASE_STATUS"
    if [ "$REMEMBER_CASE_STATUS" != "not-applicable" ]; then
        _body="$_body${NL}resolved=$REMEMBER_CASE_RESOLVED${NL}store_root=$REMEMBER_CASE_ROOT"
        _body="$_body${NL}disk_state=$REMEMBER_CASE_DISK_STATE"
        _body="$_body${REMEMBER_CASE_DISK_REASON:+${NL}disk_reason=$REMEMBER_CASE_DISK_REASON}"
        _body="$_body${REMEMBER_CASE_DISK_NAMES:+${NL}disk_names=$REMEMBER_CASE_DISK_NAMES}"
        _body="$_body${NL}git_state=$REMEMBER_CASE_GIT_STATE"
        _body="$_body${REMEMBER_CASE_GIT_REASON:+${NL}git_reason=$REMEMBER_CASE_GIT_REASON}"
        _body="$_body${REMEMBER_CASE_GIT_NAMES:+${NL}git_names=$REMEMBER_CASE_GIT_NAMES}"
    fi

    if [ -f "$_dir/case-divergence" ]; then
        local _pline
        while IFS= read -r _pline; do
            _old="${_old:+$_old$NL}$_pline"
        done < "$_dir/case-divergence"
    fi

    if [ "$_old" != "$_body" ]; then
        [ "${REMEMBER_DIR_UNSAFE:-0}" = 1 ] && return 0
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
        log "case-divergence" "could not check for a second spelling of this store (disk=$REMEMBER_CASE_DISK_STATE${REMEMBER_CASE_DISK_REASON:+/$REMEMBER_CASE_DISK_REASON} git=$REMEMBER_CASE_GIT_STATE${REMEMBER_CASE_GIT_REASON:+/$REMEMBER_CASE_GIT_REASON}) -- not a report that they agree"
    fi
    return 0
}

_stdin_json_string_into() {
    local LC_ALL=C
    local _sjsi_var="$1" _sjsi_field="$2" _sjsi_raw="$3" _sjsi_rest _sjsi_prefix _sjsi_value _sjsi_dq
    printf -v "$_sjsi_var" '%s' ""
    [ "${#_sjsi_raw}" -gt 4096 ] && _sjsi_raw=${_sjsi_raw:0:4096}
    printf -v _sjsi_dq '\042'
    [[ "$_sjsi_raw" == *"$_sjsi_dq$_sjsi_field$_sjsi_dq"* ]] || return 1
    _sjsi_rest=${_sjsi_raw#*"$_sjsi_dq"$_sjsi_field"$_sjsi_dq"}
    _sjsi_prefix=${_sjsi_rest%%"$_sjsi_dq"*}
    if [[ "$_sjsi_prefix" == *[!:[:space:]]* ]]; then return 1; fi
    _sjsi_value=${_sjsi_rest#*"$_sjsi_dq"}
    [[ "$_sjsi_value" == *"$_sjsi_dq"* ]] || return 1
    _sjsi_value=${_sjsi_value%%"$_sjsi_dq"*}
    _sjsi_value=${_sjsi_value//\\\\/\\}
    [ -n "$_sjsi_value" ] || return 1
    printf -v "$_sjsi_var" '%s' "$_sjsi_value"
}

_ENTRYPOINT_SNIFF_CAP=50
_transcript_is_pluginless_sdk() {
    local f=$1 n=0 line ep rest prefix is_dialogue dq
    printf -v dq '\042'
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
                _stdin_json_string_into ep entrypoint "$line" 2>/dev/null || return 1
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

_TRANSCRIPT_SUFFIX=.jsonl

_find_previous_transcript() {
    local dir=$1 own_id=$2 floor="" before_floor tries=0 f id
    local assume_newest_is_ours=""
    local -a files
    [ -z "$own_id" ] && assume_newest_is_ours=1
    files=("$dir"/*"$_TRANSCRIPT_SUFFIX")
    while :; do
        PREV_JSONL="" PREV_ID="" before_floor=1
        for f in "${files[@]}"; do
            [ -e "$f" ] || continue
            if [ -n "$floor" ]; then
                if [ "$f" = "$floor" ]; then
                    before_floor=""
                    continue
                fi
                [ "$f" -nt "$floor" ] && continue
                [ -n "$before_floor" ] && ! [ "$floor" -nt "$f" ] && continue
            fi
            id=${f#"$dir"/}
            id=${id%"$_TRANSCRIPT_SUFFIX"}
            [ -n "$own_id" ] && [ "$id" = "$own_id" ] && continue
            if [ -z "$PREV_JSONL" ] || [ "$f" -nt "$PREV_JSONL" ]; then
                PREV_JSONL=$f
                PREV_ID=$id
            fi
        done
        [ -z "$PREV_JSONL" ] && return 0
        if [ -n "$assume_newest_is_ours" ]; then
            assume_newest_is_ours=""
        elif _transcript_is_pluginless_sdk "$PREV_JSONL"; then
            tries=$((tries + 1))
            if [ "$tries" -ge "${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20}" ]; then
                log "hook" "WARNING: _find_previous_transcript gave up after ${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20} pluginless-SDK exclusions in $dir -- recovery and the #200 capture-gap check treat this as no previous session"
                PREV_JSONL="" PREV_ID=""
                return 0
            fi
        else
            return 0
        fi
        floor=$PREV_JSONL
    done
}

export _REMEMBER_PHASE=deferred

_remember_write_slug_record
_remember_write_slug_index
_remember_write_case_divergence

_find_previous_transcript "$SESSIONS_DIR" "$CURRENT_SESSION_ID"

PREV_WAS_SAVED="no"
if [ -n "$PREV_ID" ] && session_was_saved "$PREV_ID"; then
    PREV_WAS_SAVED="yes"
fi

_remember_recovery_prev_id_vouched() {
    local LC_ALL=C
    local _d _ok
    [ -n "$1" ] || return 1
    printf -v _d '\056'
    _ok="^[A-Za-z0-9${_d}_-]*\$"
    if [ "$1" = "$_d" ] || [ "$1" = "$_d$_d" ]; then
        return 1
    elif ! [[ "$1" =~ $_ok ]]; then
        return 1
    fi
    [ -e "$REMEMBER_DIR/tmp/capture-alive.d/$1" ] && return 0
    [ -e "$REMEMBER_DIR/tmp/session-started.d/$1" ] && return 0
    [ -f "$REMEMBER_DIR/tmp/capture-alive" ] \
        && [ "$(<"$REMEMBER_DIR/tmp/capture-alive")" = "$1" ] && return 0
    return 1
}

_ROSTER_LIVE_GRACE_MIN=5
_REMEMBER_ROSTER_GRACE_BLOCK_REASON=""
_remember_roster_grace_other_store_marker() {
    local _store="$1" _pid="$2" _marker
    [ -d "$_store" ] || return 1
    [ "${_store%/}" != "${REMEMBER_DIR%/}" ] || return 1
    for _marker in "$_store/tmp/capture-alive.d/$_pid" "$_store/tmp/session-started.d/$_pid"; do
        [ -e "$_marker" ] && return 0
    done
    return 1
}
_remember_roster_grace_scan_root() {
    local _root="$1" _pid="$2" _d1 _d2
    [ -n "$_root" ] && [ -d "$_root" ] || return 1
    for _d1 in "$_root"/*/; do
        [ -d "$_d1" ] || continue
        _remember_roster_grace_other_store_marker "${_d1%/}" "$_pid" && return 0
        for _d2 in "$_d1"*/; do
            [ -d "$_d2" ] || continue
            _remember_roster_grace_other_store_marker "${_d2%/}" "$_pid" && return 0
        done
    done
    return 1
}
_remember_roster_grace_blocked() {
    local _pid="$1" _jsonl="$2" _now="" _mtime="" _LC_ALL_SAVE

    _REMEMBER_ROSTER_GRACE_BLOCK_REASON=""

    if [ -n "$_jsonl" ] && [ -f "$_jsonl" ]; then
        _mtime=$(stat -c %Y "$_jsonl" 2>/dev/null) \
            || _mtime=$(stat -f %m "$_jsonl" 2>/dev/null) \
            || _mtime=""
        if [ -z "$_mtime" ] || [[ "$_mtime" == *[!0-9]* ]]; then
            _REMEMBER_ROSTER_GRACE_BLOCK_REASON="recency"
            return 0
        fi
        if ! command -v _remember_date_into >/dev/null 2>&1; then
            source "$_HOOK_DIR/lib-clock.sh" 2>/dev/null
        fi
        if command -v _remember_date_into >/dev/null 2>&1; then
            _remember_date_into _now +%s
        fi
        if [ -z "$_now" ] || [[ "$_now" == *[!0-9]* ]]; then
            _REMEMBER_ROSTER_GRACE_BLOCK_REASON="recency"
            return 0
        fi
        if [ $((10#$_now - 10#$_mtime)) -lt $((_ROSTER_LIVE_GRACE_MIN * 60)) ]; then
            _REMEMBER_ROSTER_GRACE_BLOCK_REASON="recency"
            return 0
        fi
    else
        _REMEMBER_ROSTER_GRACE_BLOCK_REASON="recency"
        return 0
    fi

    [ -n "$_pid" ] || return 1
    _LC_ALL_SAVE="${LC_ALL:-}"
    local LC_ALL=C
    local _dot
    printf -v _dot '\056'
    if [ "$_pid" = "$_dot" ] || [ "$_pid" = "$_dot$_dot" ] \
        || ! [[ "$_pid" =~ ^[A-Za-z0-9._-]*$ ]]; then
        LC_ALL="$_LC_ALL_SAVE"
        return 1
    fi
    LC_ALL="$_LC_ALL_SAVE"

    if [ -n "${PROJECT:-}" ] \
        && _remember_roster_grace_other_store_marker "$PROJECT/.remember" "$_pid"; then
        _REMEMBER_ROSTER_GRACE_BLOCK_REASON="other-store"
        return 0
    fi
    if [ -n "${HOME:-}" ] \
        && _remember_roster_grace_scan_root "$HOME/.remember" "$_pid"; then
        _REMEMBER_ROSTER_GRACE_BLOCK_REASON="other-store"
        return 0
    fi
    if [ -n "${REMEMBER_STORE_ROOT:-}" ] \
        && [ "${REMEMBER_STORE_ROOT%/}" != "${HOME:-}/.remember" ] \
        && _remember_roster_grace_scan_root "$REMEMBER_STORE_ROOT" "$_pid"; then
        _REMEMBER_ROSTER_GRACE_BLOCK_REASON="other-store"
        return 0
    fi
    return 1
}

_recovery_enabled=""
if [ -n "${REMEMBER_RECOVERY_ENABLED:-}" ]; then
    _recovery_enabled="$REMEMBER_RECOVERY_ENABLED"
else
    config_into _recovery_enabled '.features.recovery' true
fi
_recovery_attempted=""
_prev_id_possibly_foreign_store=""
_remember_roster_epoch_grace=""
_remember_roster_epoch_eligible=""
ROSTER_EPOCH="$REMEMBER_DIR/tmp/roster-epoch"
if [ -n "$REMEMBER_DIR" ]; then
    if [ ! -f "$ROSTER_EPOCH" ] || { [ -n "$PREV_JSONL" ] && [ -f "$PREV_JSONL" ] \
        && ! [ "$PREV_JSONL" -nt "$ROSTER_EPOCH" ]; }; then
        _remember_roster_epoch_eligible="true"
        if ! _remember_roster_grace_blocked "$PREV_ID" "$PREV_JSONL"; then
            _remember_roster_epoch_grace="true"
        fi
    fi
fi
if [ "$_recovery_enabled" = "true" ]; then
if [ -d "$SESSIONS_DIR" ] && [ -f "$LAST_SAVE_FILE" ] && [ -n "$PREV_ID" ]; then
    if [ "$PREV_WAS_SAVED" = "no" ]; then
        if [ -n "$REMEMBER_DIR" ] && [ "$_remember_roster_epoch_grace" != "true" ] \
            && ! _remember_recovery_prev_id_vouched "$PREV_ID"; then
            log "hook" "recovery: skipping force-save of $PREV_ID -- no capture-alive.d marker or legacy capture-alive slot under this REMEMBER_DIR (#1046: may belong to a different store sharing this project's transcript directory)"
            _prev_id_possibly_foreign_store="true"
        else
        ( unset REMEMBER_TRANSCRIPT_PATH; "$PLUGIN_ROOT/scripts/save-session.sh" "$PREV_ID" --force ) </dev/null >/dev/null 2>&1
        _recovery_attempted="true"
        fi
    fi
fi
fi

if [ -n "$REMEMBER_DIR" ] && [ ! -f "$ROSTER_EPOCH" ] \
    && { [ "$_remember_roster_epoch_eligible" != "true" ] \
        || [ "$_remember_roster_epoch_grace" = "true" ] \
        || [ "$_REMEMBER_ROSTER_GRACE_BLOCK_REASON" != "recency" ]; }; then
    : > "$ROSTER_EPOCH" 2>/dev/null || true
fi

CAPTURE_ALIVE="$REMEMBER_DIR/tmp/capture-alive"
CAPTURE_SEEN_DIR="$REMEMBER_DIR/tmp/capture-alive.d"
SESSION_STARTED_DIR="$REMEMBER_DIR/tmp/session-started.d"
CAPTURE_REPORTED="$REMEMBER_DIR/tmp/capture-gap-reported"
CAPTURE_SEEN_KEEP=200

SEEN_ID=""
[ -f "$CAPTURE_ALIVE" ] && SEEN_ID=$(<"$CAPTURE_ALIVE")

capture_was_seen() {
    local LC_ALL=C
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
_remember_prune_keep_newest "$SESSION_STARTED_DIR" "$CAPTURE_SEEN_KEEP"

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
        elif [ "$_prev_id_possibly_foreign_store" = "true" ]; then
            _capture_gap_notice="remember: no record of your previous session under this store. If several Claude Code windows share this project with different pinned stores, that is expected. Otherwise its hooks were not registered for that session; run /remember:doctor."
        fi
        echo "$_capture_gap_notice" \
            > "$REMEMBER_DIR/tmp/capture-gap-notice" 2>/dev/null || true
        printf '%s' "$PREV_ID" > "$CAPTURE_REPORTED" 2>/dev/null || true
    fi
fi
