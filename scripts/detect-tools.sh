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
PYTHON=""
for _candidate in "python3" "python" "py -3" "py"; do
    _first="${_candidate%% *}"
    if command -v "$_first" >/dev/null 2>&1 && $_candidate -V >/dev/null 2>&1; then
        PYTHON="$_candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "FATAL: No working Python found. Tried: python3, python, py -3, py. Windows users: install Python from python.org (not Microsoft Store) and ensure 'python' or 'py' works from the shell Claude Code launches hooks in." >&2
    exit 1
fi
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
source "$(dirname "${BASH_SOURCE[0]}")/lib-slug.sh"
