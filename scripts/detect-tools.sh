#!/bin/bash
# ============================================================================
# detect-tools.sh — Detect python and jq with cross-platform fallbacks
# ============================================================================
#
# DESCRIPTION
#   Finds the correct python and jq commands, handling platform differences:
#     - python3 vs python (Windows only has python by default)
#     - jq presence check with shell fallback for simple JSON reads
#     - CRLF-safe variable capture from Python output (Windows Git Bash)
#
# USAGE
#   source "$(dirname "$0")/detect-tools.sh"
#   # Now PYTHON and JQ are set
#   $PYTHON -m pipeline.shell extract ...
#   val=$($JQ -r '.key' file.json)
#
# ENVIRONMENT (outputs)
#   PYTHON       Path/command for python (python3 or python, validated)
#   JQ           Path/command for jq (jq or _jq_fallback function)
#
# EXIT CODES
#   1   No usable python found
#
# ============================================================================

# --- Detect Python ---
# Try python3 first (macOS/Linux default), fall back to python, then the
# Windows `py` launcher. On Windows, `python3` and `python` may resolve to
# the Microsoft Store placeholder (a stub that only opens the Store when
# Python is not installed via Store). A `command -v` check alone is not
# enough — validate with `-V` to confirm the binary actually runs.
#
# Each candidate's verdict is kept as it is probed (#650) -- `not on PATH`,
# or the exit status of its `-V` -- and printed ONLY on the fatal path. A
# Windows reporter logged 1,650 consecutive hook failures over a month,
# every one this FATAL, while `python -V` and `py -3 -V` worked in the shell
# the hooks launch from; a second plugin's independent probe failed
# identically in the same window, then both self-resolved with nothing
# changed. Nobody can say why, because the old message named the
# candidates and nothing about what was seen: not the PATH searched, not
# which names resolved, not what the resolved ones exited with. With those
# in hook-errors.log the next such report is answerable from the log alone.
# Nothing is printed on success: this file is sourced on the post-tool hot
# path, where stderr is hook-errors.log, on every tool call.
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
        # Captured in the else arm, where `$?` is still the probe's own
        # status: after `fi` it is the compound's, which is 0 here, and
        # after the `command -v` substitution below it would be that one's.
        _probe_status=$?
    fi
    _probe_report="$_probe_report
  $_candidate: on PATH ($(command -v "$_first" 2>/dev/null)), '-V' exit $_probe_status"
done
unset _probe_status
if [ -z "$PYTHON" ]; then
    echo "FATAL: No working Python found. Tried: python3, python, py -3, py. Windows users: install Python from python.org (not Microsoft Store) and ensure 'python' or 'py' works from the shell Claude Code launches hooks in." >&2
    echo "  PATH searched: $PATH" >&2
    echo "  per-candidate (exit 49 = Microsoft Store placeholder, not a real interpreter):$_probe_report" >&2
    unset _probe_report
    exit 1
fi
unset _probe_report
export PYTHON

# --- Detect jq ---
# jq is optional — provide a Python-based fallback for simple JSON reads
if command -v jq >/dev/null 2>&1; then
    JQ="jq"
else
    # Fallback: use Python for JSON queries
    # Supports: jq -r '.key' file.json  (single-level key extraction)
    _jq_fallback() {
        local _jq_flags=""
        while [[ "$1" == -* ]]; do _jq_flags="$_jq_flags $1"; shift; done
        local _jq_query="$1"
        local _jq_file="$2"
        $PYTHON - "$_jq_file" "$_jq_query" << 'PYEOF' 2>/dev/null
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    keys = sys.argv[2].strip('.').split('.')
    val = data
    for k in keys:
        if k and isinstance(val, dict):
            val = val.get(k)
        if val is None:
            break
    if val is None:
        sys.exit(0)
    # jq -r prints strings raw and everything else in jq's JSON textual
    # form — crucially "true"/"false" for booleans, not Python's capitalized
    # str(True)/str(False). Getting this wrong silently breaks every caller
    # that does `[ "$x" = "true" ]` against a boolean config key (e.g.
    # git_backup.gpg_sign, allow_remote_change) whenever jq is absent: the
    # comparison never matches, so the key always reads as false.
    print(val if isinstance(val, str) else json.dumps(val))
except Exception:
    sys.exit(0)
PYEOF
    }
    JQ="_jq_fallback"
fi
export JQ

# Note: safe_eval lives in log.sh (single source of truth). It strips CR
# from CRLF input — needed because Python on Windows emits \r\n (issue #84).
# Earlier versions overrode safe_eval here as a Windows-CRLF patch — removed
# now that log.sh carries the fix and is sourced after this file.

# --- Session dir slug ---
# Moved to lib-slug.sh so lib-memory-dir.sh can reach it without sourcing this
# file (which exits 1 when it finds no Python) and without keeping the naive
# inline copy that drifted from it (#158).
# Parameter expansion, not `dirname` (#230) — matching log.sh and
# lib-memory-dir.sh, which already resolve their own directory this way. A path
# with no slash in it (`source detect-tools.sh` from the scripts dir) leaves the
# filename behind, not a directory; `dirname` answered "." and this must too.
_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="."
source "$_REMEMBER_SRC_DIR/lib-slug.sh"
unset _REMEMBER_SRC_DIR
