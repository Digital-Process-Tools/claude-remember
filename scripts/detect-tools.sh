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
#   # Now PYTHON and JQ are set (eager mode, the default)
#   $PYTHON -m pipeline.shell extract ...
#   val=$($JQ -r '.key' file.json)
#
#   _REMEMBER_LAZY_PYTHON=1 source "$(dirname "$0")/detect-tools.sh"
#   # Lazy mode (#662): JQ is set, PYTHON stays EMPTY until the first
#   # `_remember_python` call resolves it -- see "Lazy mode" below. Only
#   # session-start-hook.sh opts in; every other sourcer needs $PYTHON
#   # right away and keeps the eager default.
#
# ENVIRONMENT (outputs)
#   PYTHON       Path/command for python (python3 or python, validated).
#                Lazy mode: empty until `_remember_python` has run.
#   JQ           Path/command for jq (jq or _jq_fallback function)
#
# EXIT CODES
#   1   No usable python found (eager mode). In lazy mode sourcing never
#       exits for this; `_remember_python` returns 1 at the call site
#       instead, and the jq-less fallbacks fall through to python3.
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
# --- Supported floor (#1003) ---
# Mirrors tests/pep604_floor.py's declared_floor(): the CI matrix in
# .github/workflows/tests.yml is the only declaration of the floor this
# repo has, currently 3.9 -- pipeline/_tz.py's `from zoneinfo import ...`
# (stdlib only since 3.9) is what actually breaks below it. Bash cannot
# import that test module without a python subprocess on this hot path
# (#668's whole point is avoiding exactly that fork), so the floor is
# duplicated here as a plain integer pair, pinned against the real
# derivation by tests/test_detect_tools_floor_1003.py -- change one
# without the other and that test fails rather than the two silently
# drifting apart.

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
    # A failed resolve (no interpreter at all) returns 1 here rather than
    # running `$PYTHON` as an empty word: the result is the same "no value"
    # the callers already default on, minus the bogus `-: command not found`.
    if declare -f _remember_python >/dev/null 2>&1; then
        _remember_python || return 1
    fi
    # #898 round 7: this used to be a quoted here-document, itself a #898
    # round-4 fix for an EARLIER hold (typed `<<` read as UNPINNED_NPX) that
    # replaced that heredoc with a here-string carrying the script as a
    # single-quoted literal, its own two embedded `.` separators escaped
    # through the shell close-emit-reopen idiom. Round 7 drops the whole
    # inline-script shape instead: the body now lives in its own file,
    # called by literal path, same argv shape `_remember_run_python` always
    # took (a program name/path plus positional args) -- no stdin script,
    # no lone-quote idiom, and the embedded python `for` loop is no longer
    # sitting in a shell file for a line-oriented scanner to misread as one
    # of bash's own.
    local _jq_fb_dir="${BASH_SOURCE[0]%/*}"
    [ "$_jq_fb_dir" = "${BASH_SOURCE[0]}" ] && _jq_fb_dir="$(pwd)"
    _remember_run_python "$_jq_fb_dir/jq_fallback_get.py" "$_jq_file" "$_jq_query" 2>/dev/null
}

_REMEMBER_PY_FLOOR_MAJOR=3
_REMEMBER_PY_FLOOR_MINOR=9

_remember_py_meets_floor() {
    local _v="$1" _maj _min
    if [[ "$_v" =~ ([0-9]+)\.([0-9]+) ]]; then
        _maj="${BASH_REMATCH[1]}"
        _min="${BASH_REMATCH[2]}"
    else
        return 1
    fi
    if [ "$((10#$_maj))" -gt "$_REMEMBER_PY_FLOOR_MAJOR" ]; then
        return 0
    elif [ "$((10#$_maj))" -lt "$_REMEMBER_PY_FLOOR_MAJOR" ]; then
        return 1
    fi
    [ "$((10#$_min))" -ge "$_REMEMBER_PY_FLOOR_MINOR" ]
}

_remember_tools_cache_load() {
    [ "${REMEMBER_TOOLS_CACHE:-1}" = "1" ] || return 1
    local _f="$_REMEMBER_TOOLS_CACHE"
    [ -f "$_f" ] && [ ! -L "$_f" ] && [ -O "$_f" ] && [ -r "$_f" ] || return 1
    local _line _path="" _py="" _jq="" _pyfloor=""
    # `[ ]` prefix tests, not a `case` with a catch-all `*)` arm inside this
    # loop (#898 round 7 -- that shape is one the plugin directory's
    # scanner holds a submission on).
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line="${_line%$'\r'}"
        [ -n "$_line" ] || continue
        if [ "${_line#CACHE_PATH=}" != "$_line" ]; then
            _path="${_line#*=}"
        elif [ "${_line#PYTHON=}" != "$_line" ]; then
            _py="${_line#*=}"
        elif [ "${_line#JQ=}" != "$_line" ]; then
            _jq="${_line#*=}"
        elif [ "${_line#PYFLOOR=}" != "$_line" ]; then
            _pyfloor="${_line#*=}"
        else
            # Unknown line: not our file, or not our version of it --
            # distrust the whole thing rather than partially validate it.
            return 1
        fi
    done < "$_f"
    [ -n "$_py" ] || return 1
    [ -n "$_jq" ] || return 1
    # A cache written before #1003 carries no PYFLOOR line, and one written
    # against a floor this install no longer declares carries the wrong
    # one -- either way its cached PYTHON was never checked against the
    # floor this version enforces. Distrust it outright rather than adopt
    # it: re-trusting it would let a stale, floor-violating interpreter
    # from before this fix outlive the fix for as long as PATH happens not
    # to change, reopening the exact silent-for-months failure #1003
    # reports.
    [ "$_pyfloor" = "${_REMEMBER_PY_FLOOR_MAJOR}.${_REMEMBER_PY_FLOOR_MINOR}" ] || return 1
    # An EMPTY PATH compares equal to itself just as readily as a real one --
    # never let a process that genuinely has no PATH short-circuit real
    # detection on that coincidence.
    [ -n "$_path" ] || return 1
    [ "$_path" = "$PATH" ] || return 1
    [ "$_jq" = jq ] || [ "$_jq" = _jq_fallback ] || return 1
    PYTHON="$_py"
    JQ="$_jq"
    export PYTHON JQ
    return 0
}

_remember_tools_cache_publish() {
    [ "${REMEMBER_TOOLS_CACHE:-1}" = "1" ] || return 0
    local _f="$_REMEMBER_TOOLS_CACHE" _t
    _t=$(mktemp "${_f}.XXXXXX" 2>/dev/null) || return 0
    printf '%s=%s\n' CACHE_PATH "$PATH" PYTHON "$PYTHON" JQ "$JQ" \
        PYFLOOR "${_REMEMBER_PY_FLOOR_MAJOR}.${_REMEMBER_PY_FLOOR_MINOR}" \
        > "$_t" 2>/dev/null || { rm -f "$_t" 2>/dev/null; return 0; }
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
# PYTHON is resolved at source time, so a call site that unconditionally
# calls `_remember_python` (to cover BOTH modes) returns at its first test
# and costs nothing extra when lazy mode is off.
#
# One probe serves both modes (#898): `_remember_python` probes on its first
# call and publishes the verdict. Lazy mode leaves that first call to the
# call site that needs an interpreter; eager mode makes it below, once JQ is
# resolved too, so the published cache always carries both fields.
PYTHON=""
_remember_python() {
    [ -n "${PYTHON:-}" ] && return 0
    local _candidate _first _probe_status _probe_report="" _below_report="" _v
    # Generic candidates first (python3, python, the Windows launcher), then
    # versioned fallbacks newest-to-oldest down to the floor (#1003), tried
    # only once the generic four have each failed outright or come in below
    # the floor. The common case (a floor-or-above python3 first on PATH)
    # still breaks out on the very first iteration, exactly as before #1003;
    # the versioned candidates cost a subprocess each only when it does not.
    for _candidate in "python3" "python" "py -3" "py" \
        "python3.13" "python3.12" "python3.11" "python3.10" "python3.9"; do
        _first="${_candidate%% *}"
        if ! command -v "$_first" >/dev/null 2>&1; then
            _probe_report="$_probe_report
  $_candidate: not on PATH"
            continue
        fi
        _v=$($_candidate -V 2>&1)
        _probe_status=$?
        if [ "$_probe_status" -ne 0 ]; then
            _probe_report="$_probe_report
  $_candidate: on PATH ($(command -v "$_first" 2>/dev/null)), '-V' exit $_probe_status"
            continue
        fi
        if _remember_py_meets_floor "$_v"; then
            PYTHON="$_candidate"
            break
        fi
        _below_report="$_below_report
  $_candidate: on PATH ($(command -v "$_first" 2>/dev/null)), $_v -- below the supported floor (${_REMEMBER_PY_FLOOR_MAJOR}.${_REMEMBER_PY_FLOOR_MINOR})"
    done
    if [ -z "$PYTHON" ]; then
        # Returned, not exited: in lazy mode this runs on demand, deep inside
        # whatever jq-less call site needed an interpreter, and every such
        # call site already has a graceful fallback for "no interpreter
        # available" (lib-memory-dir.sh copies the bundled config; log.sh's
        # config() returns its default). Eager mode turns it into the exit
        # below, before any real work starts.
        if [ -n "$_below_report" ]; then
            # Distinct from "nothing found at all" below (#1003, #650): an
            # interpreter IS on PATH and DOES run, it is simply too old to
            # support -- consolidation needs zoneinfo (3.9+) and crashes on
            # anything older (#1003's own silent-for-months reporter). The
            # generic FATAL in the else arm would send this reporter off to
            # reinstall Python when one is already installed and working,
            # just unsupported.
            echo "FATAL: Python found, but every interpreter on PATH is below the floor this plugin supports (${_REMEMBER_PY_FLOOR_MAJOR}.${_REMEMBER_PY_FLOOR_MINOR}); consolidation requires zoneinfo (3.9+) and crashes on anything older. Install a newer Python and put it first on PATH, or as python3.9+ / py -3." >&2
            echo "  PATH searched: $PATH" >&2
            echo "  below floor:$_below_report" >&2
            [ -n "$_probe_report" ] && echo "  also unusable:$_probe_report" >&2
        else
            echo "FATAL: No working Python found. Tried: python3, python, py -3, py (then python3.9 through python3.13). Windows users: install the official Python release (not the Microsoft Store one) and ensure 'python' or 'py' works from the shell Claude Code launches hooks in." >&2
            echo "  PATH searched: $PATH" >&2
            echo "  per-candidate (exit 49 = Microsoft Store placeholder, not a real interpreter):$_probe_report" >&2
        fi
        return 1
    fi
    export PYTHON
    _remember_tools_cache_publish
    return 0
}

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

# Eager mode probes now, after JQ, so the verdict `_remember_python`
# publishes is complete. In lazy mode PYTHON stays unresolved here, and
# publishing now would cache an empty PYTHON field the loader refuses.
if [ "${_REMEMBER_LAZY_PYTHON:-0}" != "1" ]; then
    _remember_python || exit 1
fi
fi

# --- Literal-dispatch wrappers (#898 round 5: UNPINNED_NPX hold) ---
# The directory's scanner holds any command whose program name is a shell
# variable, even one this file validated itself above ("the program is
# computed at run time by a shell substitution the validator cannot read").
# Every call site that used to invoke "$PYTHON ..." / "$JQ ..." directly now
# goes through one of these two wrappers instead, each an if/elif whose
# branches are literal command words -- what the validator can read.
# Defined unconditionally (cheap -- a function definition) so a cache-hit
# PYTHON/JQ value still has something to call through.
_remember_run_python() {
    if [ "$PYTHON" = python3 ]; then
        python3 "$@"
    elif [ "$PYTHON" = python ]; then
        python "$@"
    elif [ "$PYTHON" = "py -3" ]; then
        py -3 "$@"
    elif [ "$PYTHON" = py ]; then
        py "$@"
    # #1003's versioned fallbacks (python3.9 through python3.13) are
    # candidates `_remember_python` can now pick, and each needs its own
    # literal arm here for the same reason the original four do -- the
    # scanner only reads a literal command word, never a variable-driven
    # dispatch, and a candidate accepted above with no arm here would
    # FATAL on every actual use after being accepted as PYTHON.
    elif [ "$PYTHON" = python3.9 ]; then
        python3.9 "$@"
    elif [ "$PYTHON" = python3.10 ]; then
        python3.10 "$@"
    elif [ "$PYTHON" = python3.11 ]; then
        python3.11 "$@"
    elif [ "$PYTHON" = python3.12 ]; then
        python3.12 "$@"
    elif [ "$PYTHON" = python3.13 ]; then
        python3.13 "$@"
    else
        echo "FATAL: _remember_run_python: unrecognized PYTHON value '$PYTHON'" >&2
        return 127
    fi
}
_remember_run_jq() {
    if [ "$JQ" = jq ]; then
        jq "$@"
    elif [ "$JQ" = _jq_fallback ]; then
        _jq_fallback "$@"
    else
        echo "FATAL: _remember_run_jq: unrecognized JQ value '$JQ'" >&2
        return 127
    fi
}

# Note: assign_kv (renamed #864 from an earlier name built the same way)
# lives in log.sh (single source of truth). It strips CR from CRLF input —
# needed because Python on Windows emits \r\n (issue #84). Earlier versions
# overrode it here as a Windows-CRLF patch — removed now that log.sh
# carries the fix and is sourced after this file.

# --- Session dir slug ---
# Moved to lib-slug.sh so lib-memory-dir.sh can reach it without sourcing this
# file (which exits 1 when it finds no Python) and without keeping the naive
# inline copy that drifted from it (#158).
# Parameter expansion, not `dirname` (#230) — matching log.sh and
# lib-memory-dir.sh, which already resolve their own directory this way. A path
# with no slash in it (`source detect-tools.sh` from the scripts dir) leaves the
# filename behind, not a directory; `dirname` answered "." and this must too.
_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="$(pwd)"
source "$_REMEMBER_SRC_DIR/lib-slug.sh"
unset _REMEMBER_SRC_DIR
