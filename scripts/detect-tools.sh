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
# The sed program for the slug, assembled ONCE at source time. It used to be
# built inside session_dir_slug, where every $(printf) forked a subshell — ~20
# of them on a function the post-tool hook calls on every single tool call.
_remember_build_slug_sed() {
    local c cont
    cont="$(printf '\200')-$(printf '\277')"
    _REMEMBER_SLUG_SED=(
        -e "s/$(printf '\360')[$(printf '\220')-$(printf '\277')][$cont][$cont]/--/g"
        -e "s/[$(printf '\361')-$(printf '\363')][$cont][$cont][$cont]/--/g"
        -e "s/$(printf '\364')[$(printf '\200')-$(printf '\217')][$cont][$cont]/--/g"
        -e "s/$(printf '\340')[$(printf '\240')-$(printf '\277')][$cont]/-/g"
        -e "s/[$(printf '\341')-$(printf '\354')][$cont][$cont]/-/g"
        -e "s/$(printf '\355')[$(printf '\200')-$(printf '\237')][$cont]/-/g"
        -e "s/[$(printf '\356')-$(printf '\357')][$cont][$cont]/-/g"
        -e "s/[$(printf '\302')-$(printf '\337')][$cont]/-/g"
        -e 's/[^a-zA-Z0-9]/-/g'
    )
}
_remember_build_slug_sed

# Non-ASCII: Claude Code slugs with `s.replace(/[^a-zA-Z0-9]/g, '-')` — checked
# in the installed CLI bundle, and note there is no /u flag. So it replaces one
# UTF-16 CODE UNIT per dash, not one character: BMP characters (é, 日, プ) give
# one dash, but anything astral (emoji, and the Extension-B kanji that turn up
# in Japanese name registries) is a surrogate PAIR and gives two.
# Getting sed to agree portably is the whole problem, because every locale
# answer is wrong somewhere:
#   * ambient — on Git Bash/MSYS sed matched byte-wise even under
#     LC_CTYPE=C.UTF-8, so a CJK path got three dashes per character, the slug
#     missed ~/.claude/projects/<slug>/ entirely and the pipeline silently
#     never ran (issue #144);
#   * LC_ALL=C.utf8 — absent on macOS, and a missing locale falls back to C,
#     which is byte-wise: the same bug, moved;
#   * LC_ALL=en_US.UTF-8 — present on macOS, but then [a-z] follows collation
#     and matches accented letters, so "café" keeps its é and never matches
#     the slug Claude Code wrote.
# So force byte semantics and collapse UTF-8 sequences by hand: a 4-byte
# sequence is one surrogate pair, hence two dashes; a 2- or 3-byte sequence is
# one BMP character, hence one. Deterministic under any locale, and verified
# byte-identical to the real regex run under node.
session_dir_slug() {
    local path="$1"
    if command -v cygpath >/dev/null 2>&1; then
        local winpath
        winpath=$(cygpath -w "$path" 2>/dev/null) || winpath="$path"
        # Lowercase the drive letter (first character) to match Claude Code.
        path="${winpath:0:1}"
        path="${path,,}${winpath:1}"
    fi
    # The UTF-8 well-formedness table, one expression per row. Ranges matter:
    # a lead byte does not accept every continuation. \355 (U+D800-DFFF, the
    # surrogate block) and the overlong \340/\360 forms are not valid UTF-8,
    # and the decoder emits one replacement character per byte for them — so
    # they must fall through to the catch-all below rather than collapse here.
    # Each lead also takes EXACTLY its own continuations: an unbounded run
    # would swallow the lone continuation bytes after a valid sequence, which
    # again are one replacement character each.
    #
    # Verified against the real regex under node over several hundred generated
    # paths: identical for ALL well-formed UTF-8. The one shape that still
    # differs is a TRUNCATED sequence (a valid lead and some of its
    # continuations, then the string ends) — the decoder folds that into a
    # single replacement character by the maximal-subpart rule, while this
    # gives one dash per byte. Expressing that needs the decoder's state
    # machine, and it cannot arise from a path a filesystem would hand us:
    # macOS enforces well-formed UTF-8, Windows paths come from UTF-16. If it
    # ever does, the slug misses and the hook now says so rather than sitting
    # silent, which is the whole point of the warning below.
    # A raw newline is a legal byte in a POSIX filename (only / and NUL are
    # not) and is sed's own record separator, so it splits the input before any
    # s/// rule sees it and would survive into the slug untouched — missing the
    # directory exactly the way the locale bug did. Convert it here, where bash
    # still has the string whole.
    path=${path//$'\n'/-}
    printf '%s\n' "$path" | LC_ALL=C sed "${_REMEMBER_SLUG_SED[@]}"
}
