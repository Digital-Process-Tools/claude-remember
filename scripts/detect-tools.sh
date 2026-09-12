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
# --- Tool verdict cache (#668) ---
# PYTHON/JQ detection is a subprocess probe (python3 -V, and up to 4
# candidates on a cold PATH; a jq presence check) paid on EVERY hook
# invocation that sources this file -- the post-tool hot path included. The
# verdict cannot change unless $PATH changes, so it is cached keyed on the
# exact PATH string: there is no "source file" whose mtime could track a
# PATH change the way the other #668 caches key on file mtimes, so identity
# is the whole PATH value itself, byte for byte (the issue's own guidance:
# "bash ${PATH} compared as a string is enough").
#
# Lives under the system temp dir (like lib-env-cache.sh's own cache file),
# NOT under REMEMBER_DIR: this file runs before bootstrap-dirs.sh, so
# REMEMBER_DIR is not known yet. It is machine/PATH-global rather than
# per-project, which is correct -- which python and whether jq is on PATH do
# not depend on which project a hook is running for.
#
# SECURITY: same convention as lib-env-cache.sh -- read only when it is a
# regular file (never a symlink) owned by the current user; a pre-planted
# file from another user on a shared tmp dir simply fails that check and
# falls through to a real (re-)detection, exactly as if no cache existed.
_REMEMBER_TOOLS_CACHE="${TMPDIR:-/tmp}/remember-detect-tools-cache"

# Defined unconditionally (cheap -- a function definition, never invoked
# unless JQ actually points to it) so a cache hit reporting JQ=_jq_fallback
# has something to call: before this refactor the function only existed
# inside the "no jq on PATH" branch, which a cache hit would skip entirely.
_jq_fallback() {
    local _jq_flags=""
    while [[ "$1" == -* ]]; do _jq_flags="$_jq_flags $1"; shift; done
    local _jq_query="$1"
    local _jq_file="$2"
    # Resolves PYTHON on first use (#662) -- a no-op everywhere except lazy
    # mode's cold path, where this is the first of the four call sites that
    # could actually need an interpreter. `declare -f` guard: a caller that
    # sources log.sh/lib-memory-dir.sh WITHOUT this file never defines
    # _remember_python at all, and _jq_fallback is only ever reachable
    # through $JQ, which this same file is the only thing that sets.
    declare -f _remember_python >/dev/null 2>&1 && _remember_python
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

_remember_tools_cache_load() {
    [ "${REMEMBER_TOOLS_CACHE:-1}" = "1" ] || return 1
    local _f="$_REMEMBER_TOOLS_CACHE"
    [ -f "$_f" ] || return 1
    [ -L "$_f" ] && return 1
    [ -O "$_f" ] || return 1
    [ -r "$_f" ] || return 1
    local _line _path="" _py="" _jq=""
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        case "$_line" in
            CACHE_PATH=*) _path="${_line#*=}" ;;
            PYTHON=*)     _py="${_line#*=}" ;;
            JQ=*)         _jq="${_line#*=}" ;;
            # Unknown line: not our file, or not our version of it -- distrust
            # the whole thing rather than partially validate it.
            *) return 1 ;;
        esac
    done < "$_f"
    [ -n "$_py" ] || return 1
    [ -n "$_jq" ] || return 1
    # An EMPTY PATH compares equal to itself just as readily as a real one --
    # never let a process that genuinely has no PATH short-circuit real
    # detection on that coincidence.
    [ -n "$_path" ] || return 1
    [ "$_path" = "$PATH" ] || return 1
    case "$_jq" in
        jq|_jq_fallback) ;;
        *) return 1 ;;
    esac
    PYTHON="$_py"
    JQ="$_jq"
    export PYTHON JQ
    return 0
}

_remember_tools_cache_publish() {
    [ "${REMEMBER_TOOLS_CACHE:-1}" = "1" ] || return 0
    local _f="$_REMEMBER_TOOLS_CACHE" _t
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    {
        printf 'CACHE_PATH=%s\n' "$PATH"
        printf 'PYTHON=%s\n' "$PYTHON"
        printf 'JQ=%s\n' "$JQ"
    } > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
    mv -f "$_t" "$_f" 2>/dev/null || rm -f "$_t" 2>/dev/null
    return 0
}

if _remember_tools_cache_load; then
    # Cache hit: PYTHON (and JQ) are already set above, from a plain file
    # read -- no fork paid either way, so _remember_python has nothing left
    # to probe. Defined as a no-op anyway so every call site below can call
    # it unconditionally without first checking whether it exists.
    _remember_python() { [ -n "${PYTHON:-}" ]; }
else

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
#
# --- Lazy mode (#662) ---
# Sourcing this file used to ALWAYS run the probe below, even though PYTHON
# is only read by four call sites (lib-memory-dir.sh's jq-less config merge,
# log.sh's jq-less per-key read, lib-slug.sh's two Python-hash fallbacks, and
# _jq_fallback itself below) -- none of which fire on the common jq-present
# foreground path. A caller that sets _REMEMBER_LAZY_PYTHON=1 BEFORE sourcing
# this file (session-start-hook.sh does; nothing else does, because every
# other sourcer -- post-tool-hook.sh, save-session.sh, run-consolidation.sh,
# doctor.sh -- genuinely invokes $PYTHON -m pipeline.shell unconditionally
# right after sourcing, so eager detection there is real work, not waste)
# gets the probe deferred into `_remember_python`, called at first actual
# need instead of here. Every other sourcer keeps today's exact behavior:
# `_remember_python` is defined as a no-op (PYTHON already set, below) so a
# call site that unconditionally calls it (to cover BOTH modes) costs
# nothing extra when lazy mode is off.
if [ "${_REMEMBER_LAZY_PYTHON:-0}" = "1" ]; then
    PYTHON=""
    _remember_python() {
        [ -n "${PYTHON:-}" ] && return 0
        local _candidate _first _probe_status _probe_report=""
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
                _probe_status=$?
            fi
            _probe_report="$_probe_report
  $_candidate: on PATH ($(command -v "$_first" 2>/dev/null)), '-V' exit $_probe_status"
        done
        unset _probe_status
        if [ -z "$PYTHON" ]; then
            # Not `exit 1`: this runs lazily, on demand, deep inside whatever
            # jq-less call site needed an interpreter -- exiting the whole
            # hook from there is strictly worse than today's eager FATAL,
            # which at least ran before any real work started. Every call
            # site that reaches here already has a graceful fallback for "no
            # interpreter available" (lib-memory-dir.sh copies the bundled
            # config; log.sh's config() returns its default); this message
            # is the same diagnostic, on the same stderr, just returned
            # instead of exited so those fallbacks still run.
            echo "FATAL: No working Python found. Tried: python3, python, py -3, py. Windows users: install Python from python.org (not Microsoft Store) and ensure 'python' or 'py' works from the shell Claude Code launches hooks in." >&2
            echo "  PATH searched: $PATH" >&2
            echo "  per-candidate (exit 49 = Microsoft Store placeholder, not a real interpreter):$_probe_report" >&2
            unset _probe_report
            return 1
        fi
        unset _probe_report
        export PYTHON
        _remember_tools_cache_publish
        return 0
    }
else
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
    # No-op resolver for callers that call it unconditionally to cover both
    # modes (see the comment above the lazy branch): PYTHON is already
    # resolved above, so there is nothing left to probe.
    _remember_python() { [ -n "${PYTHON:-}" ]; }
fi

# --- Detect jq ---
# jq is optional — provide a Python-based fallback for simple JSON reads
# Builtin (`command -v`), never a fork -- cheap enough to stay eager in both
# modes; the probe laziness above is specifically about the Python
# candidates, which FORK a `-V` each, not about this check.
if command -v jq >/dev/null 2>&1; then
    JQ="jq"
else
    JQ="_jq_fallback"
fi
export JQ

# In lazy mode PYTHON is still unresolved here -- publishing now would cache
# an empty PYTHON field, which _remember_tools_cache_load already refuses to
# load (it requires a non-empty PYTHON), so it would just be a wasted write.
# `_remember_python` publishes for real once it has resolved something.
if [ "${_REMEMBER_LAZY_PYTHON:-0}" != "1" ]; then
    _remember_tools_cache_publish
fi
fi

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
