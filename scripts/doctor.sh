#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_DOCTOR_SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"
if [ -z "${_DOCTOR_SESSION_ID#.}" ] || [ -z "${_DOCTOR_SESSION_ID#..}" ] \
    || [[ "$_DOCTOR_SESSION_ID" == *[!A-Za-z0-9._-]* ]]; then
    _DOCTOR_SESSION_ID=""
fi

_doctor_trial_remember_dir() {
    (
        CLAUDE_PROJECT_DIR="$1"
        export CLAUDE_PROJECT_DIR
        REMEMBER_PATHS_SOFT_FAIL=1 source "$SCRIPT_DIR/resolve-paths.sh" >/dev/null 2>&1 || exit 1
        source "$SCRIPT_DIR/lib-memory-dir.sh" >/dev/null 2>&1 || exit 1
        [ -n "${REMEMBER_DIR:-}" ] || exit 1
        printf '%s\n' "$REMEMBER_DIR"
    )
}

_doctor_resolve_project_dir_candidate() {
    _doctor_git_root="$1"
    if [ -n "$_doctor_git_root" ] && [ "$_doctor_git_root" != "$(pwd)" ] && [ -n "$_DOCTOR_SESSION_ID" ]; then
        _doctor_rd_cwd=$(_doctor_trial_remember_dir "$(pwd)") || _doctor_rd_cwd=""
        _doctor_rd_gitroot=$(_doctor_trial_remember_dir "$_doctor_git_root") || _doctor_rd_gitroot=""
        _doctor_cwd_has_hint=0
        _doctor_gitroot_has_hint=0
        [ -n "$_doctor_rd_cwd" ] && [ -f "$_doctor_rd_cwd/tmp/handoff-path.$_DOCTOR_SESSION_ID" ] && _doctor_cwd_has_hint=1
        [ -n "$_doctor_rd_gitroot" ] && [ -f "$_doctor_rd_gitroot/tmp/handoff-path.$_DOCTOR_SESSION_ID" ] && _doctor_gitroot_has_hint=1
        if [ "$_doctor_cwd_has_hint" = 1 ] && [ "$_doctor_gitroot_has_hint" = 0 ]; then
            _doctor_git_root=""
        fi
        unset _doctor_rd_cwd _doctor_rd_gitroot _doctor_cwd_has_hint _doctor_gitroot_has_hint
    fi
    printf '%s' "$_doctor_git_root"
    unset _doctor_git_root
}

if [ "${1:-}" = "--json" ]; then
    _JSON_PROJECT_DIR_ASSUMED=0
    if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
        _DOCTOR_JSON_GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || _DOCTOR_JSON_GIT_ROOT=""
        _DOCTOR_JSON_GIT_ROOT=$(_doctor_resolve_project_dir_candidate "$_DOCTOR_JSON_GIT_ROOT")
        if [ -n "$_DOCTOR_JSON_GIT_ROOT" ]; then
            CLAUDE_PROJECT_DIR="$_DOCTOR_JSON_GIT_ROOT"
        else
            CLAUDE_PROJECT_DIR="$(pwd)"
        fi
        unset _DOCTOR_JSON_GIT_ROOT
        _JSON_PROJECT_DIR_ASSUMED=1
        export CLAUDE_PROJECT_DIR
    fi

    _JSON_RESOLVE_ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/remember-doctor-json-resolve-XXXXXX")
    REMEMBER_PATHS_SOFT_FAIL=1 source "$SCRIPT_DIR/resolve-paths.sh" 2>"$_JSON_RESOLVE_ERR_FILE"
    _JSON_RESOLVE_STATUS=$?
    _JSON_RESOLVE_ERR=$(cat "$_JSON_RESOLVE_ERR_FILE" 2>/dev/null)
    rm -f "$_JSON_RESOLVE_ERR_FILE"

    _json_escape() {
        local _je_bs='\'
        local _je_dq
        printf -v _je_dq '\042'
        local _je_s="$1"
        _je_s="${_je_s//"$_je_bs"/${_je_bs}${_je_bs}${_je_bs}${_je_bs}}"
        _je_s="${_je_s//"$_je_dq"/${_je_bs}${_je_dq}}"
        printf '%s' "$_je_s" | tr '[:cntrl:]' ' '
    }

    if [ "$_JSON_RESOLVE_STATUS" -ne 0 ]; then
        printf '{"schema_version":1,"state":"could_not_resolve","reason":"%s"}\n' \
            "$(_json_escape "${_JSON_RESOLVE_ERR:-unknown error}")"
        exit 0
    fi

    source "$SCRIPT_DIR/lib-memory-dir.sh"

    if [ "$REMEMBER_DIR" = "${PROJECT_DIR}/.remember" ] \
        || [[ "$(_remember_forward_slash "$REMEMBER_DIR")" == "$(_remember_forward_slash "$PROJECT_DIR")"/* ]]; then
        _JSON_STORAGE_MODE="legacy"
    else
        _JSON_STORAGE_MODE="external"
    fi

    if [ "$_JSON_PROJECT_DIR_ASSUMED" -eq 1 ]; then
        _JSON_STATE="resolved_assumed_project_dir"
    else
        _JSON_STATE="resolved"
    fi

    printf '{"schema_version":1,"state":"%s","remember_dir":"%s","storage_mode":"%s","project_dir":"%s"}\n' \
        "$_JSON_STATE" "$(_json_escape "$REMEMBER_DIR")" "$_JSON_STORAGE_MODE" "$(_json_escape "$PROJECT_DIR")"
    exit 0
fi

echo "Remember Doctor"
echo "==============="
echo ""

_FALLBACK_PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
_PLUGIN_JSON="$_FALLBACK_PLUGIN_ROOT/.claude-plugin/plugin.json"
if [ -f "$_PLUGIN_JSON" ]; then
    _VERSION=$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' "$_PLUGIN_JSON" \
        | sed 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/' | head -1)
    [ -n "$_VERSION" ] && echo "OK   Plugin version $_VERSION (root: $_FALLBACK_PLUGIN_ROOT)" \
        || echo "WARN Plugin version: could not parse $_PLUGIN_JSON"
else
    echo "WARN Plugin version: $_PLUGIN_JSON not found"
fi
echo ""

if [ -n "${REMEMBER_TRANSCRIPT_PATH:-}" ]; then
    _DT_TRANSCRIPT_PATH_SAFE=$(printf '%s' "$REMEMBER_TRANSCRIPT_PATH" | tr -d '[:cntrl:]')
    echo "WARN REMEMBER_TRANSCRIPT_PATH is set in this shell's environment:"
    echo "     $_DT_TRANSCRIPT_PATH_SAFE"
    echo "     This is trusted input for a manual run (#431), the same as any"
    echo "     other variable your shell inherits -- pipeline.extract will read"
    echo "     it verbatim if you invoke it by hand, with no containment check."
    echo "     session-start-hook.sh and session-end-hook.sh export it freshly on"
    echo "     every hook run; post-tool-hook.sh and user-prompt-hook.sh clear it"
    echo "     before doing anything else. If you did not set this yourself,"
    echo "     unset it before running anything by hand."
    echo ""
fi

echo "-- Paths --"

_PROJECT_DIR_ASSUMED=0
if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
    _DOCTOR_GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || _DOCTOR_GIT_ROOT=""
    _DOCTOR_GIT_ROOT=$(_doctor_resolve_project_dir_candidate "$_DOCTOR_GIT_ROOT")
    if [ -n "$_DOCTOR_GIT_ROOT" ]; then
        CLAUDE_PROJECT_DIR="$_DOCTOR_GIT_ROOT"
    else
        CLAUDE_PROJECT_DIR="$(pwd)"
    fi
    unset _DOCTOR_GIT_ROOT
    _PROJECT_DIR_ASSUMED=1
    export CLAUDE_PROJECT_DIR
fi

_RESOLVE_ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/remember-doctor-resolve-XXXXXX")
REMEMBER_PATHS_SOFT_FAIL=1 source "$SCRIPT_DIR/resolve-paths.sh" 2>"$_RESOLVE_ERR_FILE"
_RESOLVE_STATUS=$?
_RESOLVE_ERR=$(cat "$_RESOLVE_ERR_FILE" 2>/dev/null)
rm -f "$_RESOLVE_ERR_FILE"

if [ "$_RESOLVE_STATUS" -ne 0 ]; then
    echo "FAIL Path resolution failed: ${_RESOLVE_ERR:-unknown error}"
    echo ""
    echo "VERDICT: problem -- paths did not resolve, capture cannot run (see above)"
    exit 0
fi

if [ "$_PROJECT_DIR_ASSUMED" -eq 1 ]; then
    echo "WARN CLAUDE_PROJECT_DIR was not set -- assumed:"
    echo "     $PROJECT_DIR"
    echo "     Everything below describes that directory, not one Claude Code told"
    echo "     us about. Rerun with CLAUDE_PROJECT_DIR set to check a different project."
else
    echo "OK   CLAUDE_PROJECT_DIR = $PROJECT_DIR"
fi
echo "OK   PIPELINE_DIR       = $PIPELINE_DIR"

source "$SCRIPT_DIR/lib-memory-dir.sh"
echo "OK   REMEMBER_DIR       = $REMEMBER_DIR"

if ! command -v session_dir_slug >/dev/null 2>&1; then
    source "$SCRIPT_DIR/lib-slug.sh" 2>/dev/null || true
fi
if command -v session_dir_slug >/dev/null 2>&1 && command -v claude_projects_dir >/dev/null 2>&1; then
    _PROJECTS_DIR="$(claude_projects_dir)"
    _SLUG="$(session_dir_slug "$PROJECT_DIR")"
    _SESSION_DIR="$_PROJECTS_DIR/$_SLUG"
    echo "OK   Claude projects dir = $_PROJECTS_DIR"
    echo "OK   Session dir slug    = $_SLUG"
    if [ -d "$_SESSION_DIR" ]; then
        echo "OK   Session dir exists  = $_SESSION_DIR"
    else
        echo "FAIL Session dir MISSING = $_SESSION_DIR"
        echo "     A slug that does not match the directory Claude Code actually"
        echo "     created means capture no-ops for the life of this project (#144)."
    fi
else
    _SESSION_DIR=""
    echo "WARN Session dir: slug helpers unavailable, cannot check (#144 is"
    echo "     therefore unverified -- this is not the same as 'no problem')"
fi
echo ""

echo "-- Tools --"

_DT_LOG=$(source "$SCRIPT_DIR/detect-tools.sh" 2>&1 1>/dev/null)
_DT_STATUS=$?

_PYTHON_OK=1
if [ "$_DT_STATUS" -ne 0 ]; then
    _PYTHON_OK=0
    echo "FAIL Python: ${_DT_LOG:-no usable python found (tried python3, python, py -3, py)}"
    echo "WARN jq: skipped (python detection failed first)"
else
    source "$SCRIPT_DIR/detect-tools.sh" >/dev/null 2>&1
    _PY_FIRST="${PYTHON%% *}"
    _PY_PATH=$(command -v "$_PY_FIRST" 2>/dev/null)
    _PY_VERSION=$(_remember_run_python -V 2>&1)
    _PY_DISPLAY="$_PY_PATH"
    [ -n "$_PY_DISPLAY" ] || _PY_DISPLAY="$_PY_FIRST"
    echo "OK   python: $PYTHON -> $_PY_DISPLAY ($_PY_VERSION)"

    if command -v jq >/dev/null 2>&1; then
        _JQ_PATH=$(command -v jq)
        _JQ_VERSION=$(jq --version 2>&1)
        echo "OK   jq: $_JQ_PATH ($_JQ_VERSION)"
    else
        echo "WARN jq: not found -- using the python fallback (slower, single-key reads only)"
    fi
fi
echo ""

echo "-- Storage --"

if [ "$REMEMBER_DIR" = "${PROJECT_DIR}/.remember" ] \
    || [[ "$(_remember_forward_slash "$REMEMBER_DIR")" == "$(_remember_forward_slash "$PROJECT_DIR")"/* ]]; then
    echo "OK   Storage mode: legacy (in-project: $REMEMBER_DIR)"
else
    echo "OK   Storage mode: external ($REMEMBER_DIR)"
fi

_legacy_store="${MEMORY_PROJECT_DIR:-}"
[ -n "$_legacy_store" ] || _legacy_store="$PROJECT_DIR"
_remember_forward_slash_into _legacy_store "$_legacy_store/.remember"
_remember_forward_slash_into _legacy_rd "$REMEMBER_DIR"
if [ "$_legacy_rd" != "$_legacy_store" ] && [ "${_legacy_rd#"$_legacy_store"/}" = "$_legacy_rd" ]; then
    for _legacy_f in now.md recent.md archive.md core-memories.md remember.md; do
        if [ -f "$_legacy_store/$_legacy_f" ]; then
            echo "WARN Legacy store: $_legacy_store still holds memory data ($_legacy_f); data_dir now points to $REMEMBER_DIR"
            echo "     Move the memory files there by hand, then remove $_legacy_store (nothing moves them automatically since #898)."
            break
        fi
    done
fi
unset _legacy_store _legacy_rd _legacy_f

if [ -f "$REMEMBER_CONFIG" ] && [ -s "$REMEMBER_CONFIG" ]; then
    echo "OK   config.json: parsed (merged from bundled/user-global/per-project layers)"
else
    echo "WARN config.json: none found -- running on bundled defaults"
fi

if source "$SCRIPT_DIR/lib-case-divergence.sh" 2>/dev/null && \
   command -v remember_case_divergence >/dev/null 2>&1; then
    remember_case_divergence
    if [ "$REMEMBER_CASE_STATUS" = diverged ]; then
        echo "WARN Store spelling: this store is known by more than one spelling, differing only in case (resolved: $REMEMBER_CASE_RESOLVED)"
        echo "     $REMEMBER_CASE_MESSAGE"
    elif [ "$REMEMBER_CASE_STATUS" = ok ]; then
        echo "OK   Store spelling: $REMEMBER_CASE_RESOLVED, on disk and in the store's git repository alike"
    elif [ "$REMEMBER_CASE_STATUS" = unavailable ]; then
        echo "WARN Store spelling: could not check in full -- on disk: $REMEMBER_CASE_DISK_STATE${REMEMBER_CASE_DISK_REASON:+ ($REMEMBER_CASE_DISK_REASON)}; in git: $REMEMBER_CASE_GIT_STATE${REMEMBER_CASE_GIT_REASON:+ ($REMEMBER_CASE_GIT_REASON)}. This is not a report that they agree."
    fi
fi
echo ""

echo "-- Capture health --"

_file_age_seconds() {
    local _path="$1" _mtime _now
    _mtime=$(stat -c %Y "$_path" 2>/dev/null) || _mtime=$(stat -f %m "$_path" 2>/dev/null) || true
    if [ -z "$_mtime" ] || [ "${_mtime#*[!0-9]}" != "$_mtime" ]; then
        return 1
    fi
    _now=$(date +%s)
    echo $(( _now - 10#$_mtime ))
    return 0
}

_RAN_MARKER="$REMEMBER_DIR/tmp/post-tool-ran"
_ALIVE_MARKER="$REMEMBER_DIR/tmp/capture-alive"
_POST_TOOL_FIRED=0
if [ -f "$_RAN_MARKER" ] && [ ! -f "$_ALIVE_MARKER" ]; then
    echo "WARN PostToolUse is wired and running, but has not serviced a session"
    echo "     -- it is exiting early. The cause is above: most often the session"
    echo "     dir slug (#144), or a Python it cannot find. Restarting will not help."
    _POST_TOOL_FIRED=1
elif [ -f "$_ALIVE_MARKER" ]; then
    _ALIVE_AGE=$(_file_age_seconds "$_ALIVE_MARKER")
    if [ -n "$_ALIVE_AGE" ]; then
        echo "OK   PostToolUse marker present (${_ALIVE_AGE}s old): $_ALIVE_MARKER"
        _POST_TOOL_FIRED=1
    else
        echo "WARN PostToolUse marker present but its age could not be read: $_ALIVE_MARKER"
        _POST_TOOL_FIRED=1
    fi
elif [ -f "$REMEMBER_DIR/tmp/last-save.json" ]; then
    echo "WARN PostToolUse marker absent, but a save has completed -- capture has"
    echo "     worked here. The marker is new; it appears on the next tool call."
    _POST_TOOL_FIRED=1
else
    echo "FAIL PostToolUse has never fired for this project (no $_ALIVE_MARKER)"
fi

_SESSION_END_LOG_DIR="$REMEMBER_DIR/logs/autonomous"
_SESSION_END_FIRED=0
_remember_session_end_glob_dir=$(_remember_forward_slash "$_SESSION_END_LOG_DIR")
for _sel in "$_remember_session_end_glob_dir"/session-end-*.log; do
    [ -f "$_sel" ] && _SESSION_END_FIRED=1 && break
done

_STORE_INSTALL_AGE=$(_file_age_seconds "$REMEMBER_DIR/.install-marker")

_SESSION_END_STATE="unknown"
if [ "$_SESSION_END_FIRED" -eq 1 ]; then
    echo "OK   SessionEnd has fired at least once for this project ($_SESSION_END_LOG_DIR/session-end-*.log)"
    _SESSION_END_STATE="fired"
elif [ -n "$_SESSION_DIR" ] && [ -d "$_SESSION_DIR" ]; then
    _SE_TRANSCRIPT_COUNT=0
    _SE_STALE_TRANSCRIPT_COUNT=0
    _SE_UNREADABLE_COUNT=0
    _SE_PREDATES_STORE_COUNT=0
    for _tf in "$_SESSION_DIR"/*.jsonl; do
        [ -f "$_tf" ] || continue
        _tf_age=$(_file_age_seconds "$_tf")
        if [ -z "$_tf_age" ] || [[ "$_tf_age" == *[!0-9]* ]]; then
            _SE_UNREADABLE_COUNT=$((_SE_UNREADABLE_COUNT + 1))
            continue
        fi
        if [ -z "$_STORE_INSTALL_AGE" ] || [ "$_tf_age" -gt "$_STORE_INSTALL_AGE" ]; then
            _SE_PREDATES_STORE_COUNT=$((_SE_PREDATES_STORE_COUNT + 1))
            continue
        fi
        _SE_TRANSCRIPT_COUNT=$((_SE_TRANSCRIPT_COUNT + 1))
        [ "$_tf_age" -gt 900 ] && _SE_STALE_TRANSCRIPT_COUNT=$((_SE_STALE_TRANSCRIPT_COUNT + 1))
    done
    if [ "$_SE_STALE_TRANSCRIPT_COUNT" -ge 1 ]; then
        echo "FAIL SessionEnd has never fired for this project (no $_SESSION_END_LOG_DIR/session-end-*.log),"
        echo "     though $_SE_STALE_TRANSCRIPT_COUNT prior session transcript(s) in $_SESSION_DIR"
        echo "     have gone quiet for over 15 minutes since remember became active here --"
        echo "     the last-chance flush is not running. See session-end-hook.sh's own"
        echo "     header for the endings Claude Code does not document firing on."
        _SESSION_END_STATE="not-fired"
    else
        echo "WARN SessionEnd has not fired yet, and no prior session has demonstrably"
        echo "     ended in this project since remember became active here"
        echo "     ($_SE_TRANSCRIPT_COUNT transcript(s) in $_SESSION_DIR attributable to"
        echo "     that window, none quiet long enough to call finished) -- nothing has"
        echo "     had the chance to prove or disprove this yet."
        if [ "$_SE_PREDATES_STORE_COUNT" -gt 0 ]; then
            echo "     ($_SE_PREDATES_STORE_COUNT more transcript(s) predate this project's"
            echo "     remember store -- or no store baseline could be read -- and cannot"
            echo "     testify either way.)"
        fi
        if [ "$_SE_UNREADABLE_COUNT" -gt 0 ]; then
            echo "     ($_SE_UNREADABLE_COUNT more transcript(s) whose age could not be read"
            echo "     were excluded rather than counted.)"
        fi
    fi
else
    echo "WARN SessionEnd has not fired yet, and the session transcript directory is"
    echo "     unavailable (see Session dir slug above) -- cannot tell whether a prior"
    echo "     session has had the chance to fire it."
fi
echo ""

_GAP_SKIPPED="$REMEMBER_DIR/tmp/capture-gap-skipped"
if [ -f "$_GAP_SKIPPED" ]; then
    _GAP_WHY=$(cat "$_GAP_SKIPPED" 2>/dev/null)
    echo "WARN capture-gap check did not run at the last session start"
    echo "     (${_GAP_WHY:-reason unrecorded}). Capture is unaffected; this report"
    echo "     is the check that still answers."
fi

_LAST_SAVE_FILE="$REMEMBER_DIR/tmp/last-save.json"
_LAST_SAVE_TIME=""
if [ -f "$_LAST_SAVE_FILE" ]; then
    _LS_AGE=$(_file_age_seconds "$_LAST_SAVE_FILE")
    _LS_SESSION=""
    _LS_LINE=""
    if command -v jq >/dev/null 2>&1; then
        _LS_SESSION=$(jq -r '.session // empty' "$_LAST_SAVE_FILE" 2>/dev/null | tr -d '[:cntrl:]')
        _LS_LINE=$(jq -r '.line // empty' "$_LAST_SAVE_FILE" 2>/dev/null | tr -d '[:cntrl:]')
    fi
    _LAST_SAVE_TIME=$(date -r "$_LAST_SAVE_FILE" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
    if [ -n "$_LAST_SAVE_TIME" ]; then
        echo "OK   Last successful save: $_LAST_SAVE_TIME (session ${_LS_SESSION:-unknown}, line ${_LS_LINE:-unknown})"
    else
        echo "OK   Last successful save: recorded (session ${_LS_SESSION:-unknown}, line ${_LS_LINE:-unknown}), timestamp unreadable"
    fi
else
    echo "FAIL No save has ever completed for this project (no $_LAST_SAVE_FILE)"
fi

_MEMORY_FILE_COUNT=0
_MEMORY_BYTES=0
if [ -d "$REMEMBER_DIR" ]; then
    _remember_memory_glob_dir=$(_remember_forward_slash "$REMEMBER_DIR")
    for _pattern in "today-"'*.md' "now.md" "recent.md" "archive"'*.md'; do
        for _mf in "$_remember_memory_glob_dir"/$_pattern; do
            [ -f "$_mf" ] || continue
            _MEMORY_FILE_COUNT=$((_MEMORY_FILE_COUNT + 1))
            _mf_bytes=$(wc -c < "$_mf" 2>/dev/null | tr -d ' ')
            if [ -z "$_mf_bytes" ] || [ "${_mf_bytes#*[!0-9]}" != "$_mf_bytes" ]; then _mf_bytes=0; fi
            _MEMORY_BYTES=$((_MEMORY_BYTES + 10#$_mf_bytes))
        done
    done
fi
echo "OK   Memory files: $_MEMORY_FILE_COUNT file(s), $_MEMORY_BYTES bytes total"

_STORE_NEEDS_A_HUMAN=0
_CONSOLIDATE_MAX_BYTES=600000
_CONSOLIDATE_CAP_DISABLED=0
if [ -f "$REMEMBER_CONFIG" ] && [ -s "$REMEMBER_CONFIG" ]; then
    _cmb=$(grep -o '"consolidate_max_bytes"[[:space:]]*:[[:space:]]*[0-9]*' "$REMEMBER_CONFIG" 2>/dev/null \
        | sed 's/.*:[[:space:]]*//' | head -1)
    if [ -n "$_cmb" ] && [ "${_cmb#*[!0-9]}" = "$_cmb" ]; then _CONSOLIDATE_MAX_BYTES=$((10#$_cmb)); fi
fi
[ "$_CONSOLIDATE_MAX_BYTES" -eq 0 ] && _CONSOLIDATE_CAP_DISABLED=1

_STORE_UNREADABLE=""
_SIZE_BYTES=0
_size_of() {
    _SIZE_BYTES=0
    [ -f "$1" ] || return 0
    _size_raw=$(wc -c < "$1" 2>/dev/null | tr -d ' ')
    if [ -z "$_size_raw" ] || [[ "$_size_raw" == *[!0-9]* ]]; then
        _STORE_UNREADABLE="${_STORE_UNREADABLE}${1}
"
    else
        _SIZE_BYTES=$((10#$_size_raw))
    fi
}

if [ ! -d "$REMEMBER_DIR" ]; then
    echo "WARN Consolidation size check: skipped -- $REMEMBER_DIR does not exist"
else
    _size_of "$REMEMBER_DIR/recent.md";  _RECENT_BYTES=$_SIZE_BYTES
    _size_of "$REMEMBER_DIR/archive.md"; _ARCHIVE_BYTES=$_SIZE_BYTES
    _doctor_tz=""
    if [ -f "$REMEMBER_CONFIG" ] && [ -s "$REMEMBER_CONFIG" ]; then
        _doctor_tz=$(grep -o '"timezone"[[:space:]]*:[[:space:]]*"[^"]*"' "$REMEMBER_CONFIG" 2>/dev/null \
            | sed 's/.*:[[:space:]]*"//; s/"$//' | head -1)
    fi
    if [ -n "$_doctor_tz" ]; then
        _DOCTOR_TODAY=$(TZ="$_doctor_tz" date '+%Y-%m-%d')
    else
        _DOCTOR_TODAY=$(date '+%Y-%m-%d')
    fi
    _remember_staging_bytes_glob_dir=$(_remember_forward_slash "$REMEMBER_DIR")
    _STAGING_BYTES=0
    for _sf in "$_remember_staging_bytes_glob_dir"/today-*.md; do
        [ -f "$_sf" ] || continue
        _sf_name="${_sf##*/}"
        if [ "${_sf_name%.done.md}" != "$_sf_name" ]; then
            continue
        fi
        if [ -z "$_DOCTOR_TODAY" ] || [[ "$_sf_name" == *"$_DOCTOR_TODAY"* ]]; then
            continue
        fi
        _size_of "$_sf"
        _STAGING_BYTES=$((_STAGING_BYTES + _SIZE_BYTES))
    done
    _STORE_BYTES=$((_STAGING_BYTES + _RECENT_BYTES + _ARCHIVE_BYTES))

    if [ -n "$_STORE_UNREADABLE" ]; then
        echo "WARN Consolidation size check is incomplete -- these memory files exist"
        echo "     but could not be read, so nothing below counts their bytes:"
        printf '%s' "$_STORE_UNREADABLE" | while IFS= read -r _uf; do
            [ -n "$_uf" ] && echo "     $_uf"
        done
        echo "     The total is therefore a floor, not the store's size."
    fi

    if [ "$_CONSOLIDATE_CAP_DISABLED" -eq 1 ]; then
        echo "OK   Consolidation size check: disabled (thresholds.consolidate_max_bytes: 0) -- size never blocks a round"
    elif [ "$_STORE_BYTES" -gt "$_CONSOLIDATE_MAX_BYTES" ]; then
        if [ "$_STAGING_BYTES" -gt "$_CONSOLIDATE_MAX_BYTES" ]; then
            _STORE_NEEDS_A_HUMAN=1
            echo "FAIL Store is too large to consolidate and cannot heal itself:"
            echo "     $_STORE_BYTES bytes against a thresholds.consolidate_max_bytes cap"
            echo "     of $_CONSOLIDATE_MAX_BYTES -- recent.md $_RECENT_BYTES + archive.md $_ARCHIVE_BYTES"
            echo "     + past-day staging $_STAGING_BYTES."
            echo "     Past-day staging ALONE is over the cap, so rotating recent.md or"
            echo "     archive.md would not help and the pipeline will not do it: the next"
            echo "     round would skip on the same sum. Every round skips while this holds."
            echo "     The oversized today-*.md files under $REMEMBER_DIR are the thing to look at."
        else
            echo "WARN Store is too large to consolidate right now: $_STORE_BYTES bytes"
            echo "     against a thresholds.consolidate_max_bytes cap of $_CONSOLIDATE_MAX_BYTES"
            echo "     -- recent.md $_RECENT_BYTES + archive.md $_ARCHIVE_BYTES + past-day staging $_STAGING_BYTES."
            echo "     Capture is unaffected; consolidation is what skips, and staging piles"
            echo "     up until it runs."
            echo "     REMEDIATION: none by hand. The next consolidation rotates the"
            echo "     oversized file to a dated sibling (recent-YYYY-MM-DD.md /"
            echo "     archive-YYYY-MM-DD.md), starts a fresh one, and resumes. Nothing is"
            echo "     deleted -- the bytes stay on disk, stay greppable, and session start"
            echo "     names the rotated slices."
        fi
    elif [ -n "$_STORE_UNREADABLE" ]; then
        echo "WARN Whether the store fits the consolidation cap could not be determined:"
        echo "     the $_STORE_BYTES bytes that could be read are under the"
        echo "     $_CONSOLIDATE_MAX_BYTES cap, but the files named above went uncounted."
    else
        echo "OK   Store fits the consolidation cap: $_STORE_BYTES of $_CONSOLIDATE_MAX_BYTES bytes"
    fi
fi

if [ "$_POST_TOOL_FIRED" -eq 0 ]; then
    echo ""
    echo "REMEDIATION: enabling the plugin mid-session does not register its"
    echo "hooks for that session -- Claude Code reads hook definitions only at"
    echo "session start. Restart Claude Code to activate PostToolUse capture."
fi
echo ""

echo "-- Recent errors --"
_ERR_LOG="$REMEMBER_DIR/logs/hook-errors.log"
if [ -s "$_ERR_LOG" ]; then
    echo "WARN $_ERR_LOG is non-empty, last 5 lines:"
    tail -n 5 "$_ERR_LOG" | while IFS= read -r _line; do
        echo "     $_line"
    done
else
    echo "OK   No hook errors logged ($_ERR_LOG empty or absent)"
fi
echo ""

echo "-- Summarizer failures (#870) --"
_SUMMARY_FAILURE_MARKER="$REMEMBER_DIR/tmp/last-summary-failure"
_SUMMARIZER_FAILING=0
if [ -s "$_SUMMARY_FAILURE_MARKER" ]; then
    _SUMMARIZER_FAILING=1
    _remember_sf_glob_dir=$(_remember_forward_slash "$REMEMBER_DIR")
    _SF_DETAIL=""
    _SF_FILES=()
    for _sf_f in "$_remember_sf_glob_dir"/logs/memory-*.log; do
        [ -f "$_sf_f" ] || continue
        _SF_FILES+=("$_sf_f")
    done
    if [ "${#_SF_FILES[@]}" -gt 0 ]; then
        _SF_OLD_IFS="$IFS"
        IFS=$'\n'
        _SF_SORTED=($(printf '%s\n' "${_SF_FILES[@]}" | LC_ALL=C sort))
        IFS="$_SF_OLD_IFS"
        unset _SF_OLD_IFS
        for _sf_f in "${_SF_SORTED[@]}"; do
            _sf_match=$(grep -F "call-haiku error:" "$_sf_f" 2>/dev/null | tail -n 1)
            [ -n "$_sf_match" ] && _SF_DETAIL="$_sf_match"
        done
        unset _SF_SORTED
    fi
    unset _SF_FILES
    echo "FAIL summarizer: last attempt failed${_SF_DETAIL:+: $_SF_DETAIL}"
    _SF_DETAIL_LOWER=$(printf '%s' "$_SF_DETAIL" | tr '[:upper:]' '[:lower:]')
    _sf_login=0
    for _sf_m in 'not logged in' 'please run /login' 'invalid api key' \
                 'invalid bearer token' 'authentication_error' 'failed to authenticate'; do
        [ "${_SF_DETAIL_LOWER#*"$_sf_m"}" != "$_SF_DETAIL_LOWER" ] && _sf_login=1
    done
    if [ "$_sf_login" = 1 ]; then
        echo "     this looks like an expired login -- log in again with"
        echo "     your coding agent's own CLI. This plugin reads no"
        echo "     credential of its own (#129/#131/#860)."
    fi
    unset _remember_sf_glob_dir _SF_LATEST_LOG _SF_DETAIL _SF_DETAIL_LOWER _sf_f _sf_m _sf_login
else
    echo "OK   No summarizer failure recorded ($_SUMMARY_FAILURE_MARKER empty or absent)"
fi
echo ""

echo "-- SessionStart duration (#706) --"
_remember_ss_glob_dir=$(_remember_forward_slash "$REMEMBER_DIR")
_SS_LATEST_LOG=""
for _ss_f in "$_remember_ss_glob_dir"/logs/memory-*.log; do
    [ -f "$_ss_f" ] || continue
    if [ -z "$_SS_LATEST_LOG" ] || [ "$_ss_f" -nt "$_SS_LATEST_LOG" ]; then
        _SS_LATEST_LOG="$_ss_f"
    fi
done
if [ -z "$_SS_LATEST_LOG" ]; then
    echo "--   No daily log found yet -- SessionStart has not run, or logging is unwritable"
else
    _SS_LAST_LINE=$(grep -F "] session-start took" "$_SS_LATEST_LOG" 2>/dev/null | tail -n 1)
    if [ -n "$_SS_LAST_LINE" ]; then
        echo "OK   Last recorded: $_SS_LAST_LINE"
        echo "     ($_SS_LATEST_LOG)"
    else
        echo "--   $_SS_LATEST_LOG has no session-start duration line -- either #706 predates"
        echo "     this install's last SessionStart, or no line has been logged in it yet"
    fi
fi
unset _remember_ss_glob_dir _SS_LATEST_LOG _SS_LAST_LINE _ss_f


_ROTATE_STATE="$REMEMBER_DIR/logs/.rotate-failed"
if [ -f "$_ROTATE_STATE" ]; then
    _RT_COUNT=$(sed -n 1p "$_ROTATE_STATE" 2>/dev/null)
    _RT_WHEN=$(sed -n 2p "$_ROTATE_STATE" 2>/dev/null)
    _RT_ERR=$(sed -n 3p "$_ROTATE_STATE" 2>/dev/null)
    _remember_rt_pending_glob_dir=$(_remember_forward_slash "$REMEMBER_DIR")
    _RT_PENDING=0
    for _rt_f in "$_remember_rt_pending_glob_dir"/logs/memory-*.log; do
        [ -f "$_rt_f" ] && _RT_PENDING=$((_RT_PENDING + 1))
    done
    echo "WARN Log rotation has failed ${_RT_COUNT:-?} time(s) in a row (last ${_RT_WHEN:-unknown})"
    echo "     Reason: ${_RT_ERR:-not recorded}"
    echo "     $_RT_PENDING log file(s) currently in $REMEMBER_DIR/logs; the aged"
    echo "     ones will not be archived until this is fixed. Capture is unaffected."
else
    echo "OK   Log rotation: no failure recorded"
fi
echo ""

_ASSUMED_NOTE=""
if [ "$_PROJECT_DIR_ASSUMED" -eq 1 ]; then
    _ASSUMED_NOTE=" (CLAUDE_PROJECT_DIR was not set; this describes $PROJECT_DIR, assumed from the current directory)"
fi
if [ "$_PYTHON_OK" -eq 0 ]; then
    echo "VERDICT: problem -- no usable Python; the pipeline cannot run at all (see Tools above)$_ASSUMED_NOTE"
elif [ "${_STORE_NEEDS_A_HUMAN:-0}" -eq 1 ]; then
    echo "VERDICT: problem -- memory is being captured but never consolidated; the staging files are over the prompt cap on their own (see above)$_ASSUMED_NOTE"
elif [ "$_POST_TOOL_FIRED" -eq 1 ] && [ -z "$_LAST_SAVE_TIME" ] \
    && { [ -z "$_SESSION_DIR" ] || [ -d "$_SESSION_DIR" ]; }; then
    echo "VERDICT: problem -- PostToolUse has fired but no save has completed yet; check hook-errors.log above$_ASSUMED_NOTE"
elif [ "$_SESSION_END_STATE" = "not-fired" ]; then
    echo "VERDICT: problem -- SessionEnd has never fired despite prior sessions ending in this project; the last-chance flush is not running (see above)$_ASSUMED_NOTE"
elif [ "$_POST_TOOL_FIRED" -eq 1 ] && [ -n "$_LAST_SAVE_TIME" ] && [ "${_SUMMARIZER_FAILING:-0}" -eq 1 ] \
    && { [ -z "$_SESSION_DIR" ] || [ -d "$_SESSION_DIR" ]; }; then
    echo "VERDICT: problem -- the summarizer's last attempt failed and no save has completed since (see Summarizer failures above)$_ASSUMED_NOTE"
elif [ "$_POST_TOOL_FIRED" -eq 1 ] && [ -n "$_LAST_SAVE_TIME" ] \
    && { [ -z "$_SESSION_DIR" ] || [ -d "$_SESSION_DIR" ]; }; then
    echo "VERDICT: capture is working -- last save $_LAST_SAVE_TIME$_ASSUMED_NOTE"
elif [ -n "$_SESSION_DIR" ] && [ ! -d "$_SESSION_DIR" ]; then
    echo "VERDICT: problem -- session dir slug does not match Claude Code's transcript directory (#144); restarting will not help$_ASSUMED_NOTE"
elif [ "$_POST_TOOL_FIRED" -eq 0 ]; then
    echo "VERDICT: problem -- PostToolUse has never fired; restart Claude Code (see REMEDIATION above)$_ASSUMED_NOTE"
else
    echo "VERDICT: problem -- PostToolUse has fired but no save has completed yet; check hook-errors.log above$_ASSUMED_NOTE"
fi
