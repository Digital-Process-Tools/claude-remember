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
        $PYTHON -c "
import json, sys
try:
    data = json.load(open('$_jq_file'))
    keys = '$_jq_query'.strip('.').split('.')
    val = data
    for k in keys:
        if k and isinstance(val, dict):
            val = val.get(k)
        if val is None:
            break
    if val is None:
        sys.exit(0)
    print(val if isinstance(val, (str, int, float, bool)) else json.dumps(val))
except Exception:
    sys.exit(0)
" 2>/dev/null
    }
    JQ="_jq_fallback"
fi
export JQ

# --- CRLF-safe safe_eval ---
# Override safe_eval to strip \r from lines before eval.
# On Windows (Git Bash), Python outputs \r\n. read -r keeps the \r,
# which corrupts variable values (e.g., POSITION="42\r" breaks arithmetic).
safe_eval() {
    while IFS= read -r line; do
        line="${line%$'\r'}"
        if [[ "$line" =~ ^[A-Z_][A-Z0-9_]*= ]]; then
            eval "$line"
        fi
    done
}

# --- CRLF-safe session dir slug ---
# Replaces all non-alphanumeric chars with dashes. Must match Claude Code's
# own slug pattern for its ~/.claude/projects/<slug>/ session directories.
#
# Unix: Claude Code slugs the native path directly (e.g., /home/u/p → -home-u-p).
#
# Windows: Claude Code slugs the native Windows path with the drive letter
# lowercased (e.g., D:\Users\p → d--Users-p). Hook scripts on Git Bash / MSYS
# receive the path in Unix form (/d/Users/p), which would slug differently
# (-d-Users-p). Convert back to the Windows form via cygpath before slugging
# so we match the actual directory Claude Code created.
session_dir_slug() {
    local path="$1"
    if command -v cygpath >/dev/null 2>&1; then
        local winpath
        winpath=$(cygpath -w "$path" 2>/dev/null) || winpath="$path"
        # Lowercase the drive letter (first character) to match Claude Code.
        path="${winpath:0:1}"
        path="${path,,}${winpath:1}"
    fi
    echo "$path" | sed 's/[^a-zA-Z0-9]/-/g'
}
