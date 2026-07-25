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
    print(val if isinstance(val, (str, int, float, bool)) else json.dumps(val))
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
# Non-ASCII: Claude Code slugs with a JS regex, which replaces one CHARACTER
# per dash. Getting sed to agree portably is the whole problem, because every
# locale answer is wrong somewhere:
#   * ambient — on Git Bash/MSYS sed matched byte-wise even under
#     LC_CTYPE=C.UTF-8, so a CJK path got three dashes per character, the slug
#     missed ~/.claude/projects/<slug>/ entirely and the pipeline silently
#     never ran (issue #144);
#   * LC_ALL=C.utf8 — absent on macOS, and a missing locale falls back to C,
#     which is byte-wise: the same bug, moved;
#   * LC_ALL=en_US.UTF-8 — present on macOS, but then [a-z] follows collation
#     and matches accented letters, so "café" keeps its é and never matches
#     the slug Claude Code wrote.
# So force byte semantics and collapse UTF-8 sequences by hand: a lead byte
# plus its continuation bytes is one character, hence one dash. Deterministic
# under any locale, and byte-identical to the JS for every path tested.
session_dir_slug() {
    local path="$1"
    if command -v cygpath >/dev/null 2>&1; then
        local winpath
        winpath=$(cygpath -w "$path" 2>/dev/null) || winpath="$path"
        # Lowercase the drive letter (first character) to match Claude Code.
        path="${winpath:0:1}"
        path="${path,,}${winpath:1}"
    fi
    local lead lead_hi cont_lo cont_hi
    lead=$(printf '\302'); lead_hi=$(printf '\364')
    cont_lo=$(printf '\200'); cont_hi=$(printf '\277')
    printf '%s\n' "$path" | LC_ALL=C sed \
        -e "s/[$lead-$lead_hi][$cont_lo-$cont_hi]*/-/g" \
        -e 's/[^a-zA-Z0-9]/-/g'
}
