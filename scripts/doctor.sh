#!/bin/bash
# ============================================================================
# doctor.sh — User-facing diagnostics command for the Remember plugin
# ============================================================================
#
# DESCRIPTION
#   Manual, human-run health check. Reports plugin version, resolved paths,
#   detected tools, storage mode, and capture health (whether PostToolUse has
#   ever actually fired and produced a save) in a single plain-text report.
#
#   Closes suggestion 3 of issue #200: the plugin can go silently no-op when
#   it is enabled mid-session (PostToolUse is never registered because Claude
#   Code reads hook definitions once, at session start — see issue #144 for
#   the sibling failure, a slug mismatch that no-ops the same way). This
#   script exists so a user can ask "is capture actually working?" and get a
#   direct answer instead of hours of silence.
#
# USAGE
#   bash scripts/doctor.sh
#   Invoked by the /remember:doctor slash command (commands/doctor.md), which runs
#   this via ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh and relays the output
#   verbatim. Safe to also run directly from a shell for the same report.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT   Plugin install directory (set by Claude Code)
#   CLAUDE_PROJECT_DIR   Project root. Claude Code exports this to *hooks*,
#                        not to the Bash tool that runs this script, so on a
#                        marketplace install it normally arrives unset here
#                        (#207). When unset, doctor.sh — and only doctor.sh,
#                        never resolve-paths.sh's other callers — defaults it
#                        to the current directory, and says so in the report
#                        instead of presenting the guess as given.
#
# DEPENDENCIES
#   resolve-paths.sh  (sourced with REMEMBER_PATHS_SOFT_FAIL=1 — unlike the
#                      hooks, a resolution failure is reported as a FAIL
#                      finding here, not swallowed, since this command exists
#                      specifically to surface problems)
#   detect-tools.sh   (sourced, but only after a subshell probe: it calls
#                      `exit 1` when no python is found, which would kill a
#                      hook silently — the exact failure mode this report
#                      needs to SHOW, not die from. See lib-slug.sh's own
#                      comment on the same hazard.)
#   lib-memory-dir.sh (sourced directly, not via bootstrap-dirs.sh: this is a
#                      read-only report and must not trigger bootstrap-dirs'
#                      legacy-to-external migration or its stderr redirect —
#                      a diagnostic tool that hides its own errors defeats
#                      the point.)
#
# EXIT CODES
#   0   Always — a diagnostic tool must print its findings, not fail to run.
#
# OUTPUT
#   Plain text, one finding per line, prefixed OK / WARN / FAIL so the report
#   greps cleanly. No colour (matches the rest of the plugin's scripts — the
#   reporting environment includes Windows Git Bash/MSYS terminals that don't
#   reliably render ANSI). Ends with a single VERDICT line.
#
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Remember Doctor"
echo "==============="
echo ""

# ── 1. Plugin version ───────────────────────────────────────────────────────
# Best-effort plugin root from the script's own location, independent of
# whether path resolution below succeeds — this line should print even when
# everything else fails.
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

# ── 2. Resolved paths ────────────────────────────────────────────────────────
echo "-- Paths --"

# Claude Code exports CLAUDE_PROJECT_DIR to its *hooks*, not to a plain shell —
# and this script runs through the Bash tool, where it is unset on any install
# that isn't a local .claude/remember/ layout (marketplace and symlinked
# installs both hit this). resolve-paths.sh then refuses to guess and fatals,
# which is correct for the hooks (a wrong guess there writes memory into the
# wrong project) but turns this read-only report into a false FAIL on a
# healthy install (#207).
#
# Scoped to doctor.sh only: default to the current directory here, never in
# resolve-paths.sh itself, so every other caller keeps the strict refusal.
# The guess is reported as a guess below (see _PROJECT_DIR_ASSUMED) — a
# diagnostic that silently assumes a project and reports on it as fact would
# just be a quieter version of the same false signal.
_PROJECT_DIR_ASSUMED=0
if [ -z "${CLAUDE_PROJECT_DIR:-}" ]; then
    CLAUDE_PROJECT_DIR="$PWD"
    _PROJECT_DIR_ASSUMED=1
    export CLAUDE_PROJECT_DIR
fi

_RESOLVE_ERR_FILE="${TMPDIR:-/tmp}/remember-doctor-resolve-$$"
REMEMBER_PATHS_SOFT_FAIL=1 source "$SCRIPT_DIR/resolve-paths.sh" 2>"$_RESOLVE_ERR_FILE"
_RESOLVE_STATUS=$?
_RESOLVE_ERR=$(cat "$_RESOLVE_ERR_FILE" 2>/dev/null)
rm -f "$_RESOLVE_ERR_FILE"

if [ "$_RESOLVE_STATUS" -ne 0 ]; then
    echo "FAIL Path resolution failed: ${_RESOLVE_ERR:-unknown error}"
    echo ""
    echo "VERDICT: problem — paths did not resolve, capture cannot run (see above)"
    exit 0
fi

if [ "$_PROJECT_DIR_ASSUMED" -eq 1 ]; then
    echo "WARN CLAUDE_PROJECT_DIR was not set — assumed the current directory:"
    echo "     $PROJECT_DIR"
    echo "     Everything below describes that directory, not one Claude Code told"
    echo "     us about. Rerun with CLAUDE_PROJECT_DIR set to check a different project."
else
    echo "OK   CLAUDE_PROJECT_DIR = $PROJECT_DIR"
fi
echo "OK   PIPELINE_DIR       = $PIPELINE_DIR"

# lib-memory-dir.sh directly (not bootstrap-dirs.sh — see header). It sources
# lib-slug.sh itself, so session_dir_slug/claude_projects_dir are available
# without going through detect-tools.sh's exit-prone python check.
source "$SCRIPT_DIR/lib-memory-dir.sh"
echo "OK   REMEMBER_DIR       = $REMEMBER_DIR"

# lib-memory-dir.sh returns early when already loaded, and the slug helpers
# live in lib-slug.sh, so they are not guaranteed to be defined by the source
# above. Take them from their definer, and if they are still missing SAY so —
# an empty slug printed as OK, with the existence check quietly skipped, is
# precisely the no-answer-mistaken-for-a-clean-answer this tool exists to end.
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
    echo "     therefore unverified — this is not the same as 'no problem')"
fi
echo ""

# ── 3. Detected tools ────────────────────────────────────────────────────────
echo "-- Tools --"

# Probe detect-tools.sh in a subshell first: it `exit 1`s when no usable
# python is found, and sourcing it directly here would take doctor.sh down
# with it — the one failure mode this section most needs to report cleanly.
_DT_LOG=$(source "$SCRIPT_DIR/detect-tools.sh" 2>&1 1>/dev/null)
_DT_STATUS=$?

_PYTHON_OK=1
if [ "$_DT_STATUS" -ne 0 ]; then
    _PYTHON_OK=0
    echo "FAIL Python: ${_DT_LOG:-no usable python found (tried python3, python, py -3, py)}"
    echo "WARN jq: skipped (python detection failed first)"
else
    # Safe to source for real now — the subshell probe already proved this
    # succeeds, so the exit path inside it will not trigger.
    source "$SCRIPT_DIR/detect-tools.sh" >/dev/null 2>&1
    _PY_FIRST="${PYTHON%% *}"
    _PY_PATH=$(command -v "$_PY_FIRST" 2>/dev/null)
    _PY_VERSION=$($PYTHON -V 2>&1)
    echo "OK   python: $PYTHON -> ${_PY_PATH:-$_PY_FIRST} ($_PY_VERSION)"

    if command -v jq >/dev/null 2>&1; then
        _JQ_PATH=$(command -v jq)
        _JQ_VERSION=$(jq --version 2>&1)
        echo "OK   jq: $_JQ_PATH ($_JQ_VERSION)"
    else
        echo "WARN jq: not found — using the python fallback (slower, single-key reads only)"
    fi
fi
echo ""

# ── 4. Storage mode ──────────────────────────────────────────────────────────
echo "-- Storage --"

if [ "$REMEMBER_DIR" = "${PROJECT_DIR}/.remember" ] || [[ "$REMEMBER_DIR" == "$PROJECT_DIR"/* ]]; then
    echo "OK   Storage mode: legacy (in-project: $REMEMBER_DIR)"
else
    echo "OK   Storage mode: external ($REMEMBER_DIR)"
fi

if [ -f "$REMEMBER_CONFIG" ] && [ -s "$REMEMBER_CONFIG" ]; then
    echo "OK   config.json: parsed (merged from bundled/user-global/per-project layers)"
else
    echo "WARN config.json: none found — running on bundled defaults"
fi
echo ""

# ── 5. Capture health ────────────────────────────────────────────────────────
echo "-- Capture health --"

# _file_age_seconds <path> — mtime age in seconds, or empty if unreadable.
# GNU stat (-c) tried first, then BSD stat (-f): the reverse order silently
# succeeds on Linux because BSD `stat -f` there reports something else
# entirely, and the OR fallback never fires (same hazard as lib-lock.sh's
# _lock_dir_age, which this mirrors). Each probe's stdout must stay isolated
# from the other so a failing GNU probe never contaminates the BSD one.
_file_age_seconds() {
    local _path="$1" _mtime _now
    _mtime=$(stat -c %Y "$_path" 2>/dev/null) || _mtime=$(stat -f %m "$_path" 2>/dev/null) || true
    case "$_mtime" in
        ''|*[!0-9]*) return 1 ;;
    esac
    _now=$(date +%s)
    echo $(( _now - _mtime ))
    return 0
}

# "Has the hook run at all" is a different question from "which session did it
# service", and conflating them is what made this report tell a slug-mismatch
# victim to restart Claude Code. post-tool-ran is written before every early
# exit in the hook; capture-alive only after a transcript is found.
_RAN_MARKER="$REMEMBER_DIR/tmp/post-tool-ran"
_ALIVE_MARKER="$REMEMBER_DIR/tmp/capture-alive"
_POST_TOOL_FIRED=0
if [ -f "$_RAN_MARKER" ] && [ ! -f "$_ALIVE_MARKER" ]; then
    echo "WARN PostToolUse is wired and running, but has not serviced a session"
    echo "     — it is exiting early. The cause is above: most often the session"
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
    # The marker arrived with the #200 fix. An install that predates it has
    # never written one and never will until its next tool call — but a
    # completed save proves PostToolUse HAS run here. Calling that "never
    # fired" would send a working user off to restart for nothing, and a
    # diagnostic that cries wolf is worse than none.
    echo "WARN PostToolUse marker absent, but a save has completed — capture has"
    echo "     worked here. The marker is new; it appears on the next tool call."
    _POST_TOOL_FIRED=1
else
    echo "FAIL PostToolUse has never fired for this project (no $_ALIVE_MARKER)"
fi

_LAST_SAVE_FILE="$REMEMBER_DIR/tmp/last-save.json"
_LAST_SAVE_TIME=""
if [ -f "$_LAST_SAVE_FILE" ]; then
    _LS_AGE=$(_file_age_seconds "$_LAST_SAVE_FILE")
    _LS_SESSION=""
    _LS_LINE=""
    if command -v jq >/dev/null 2>&1; then
        _LS_SESSION=$(jq -r '.session // empty' "$_LAST_SAVE_FILE" 2>/dev/null)
        _LS_LINE=$(jq -r '.line // empty' "$_LAST_SAVE_FILE" 2>/dev/null)
    fi
    # `date -r <file>` prints the file's mtime, formatted — true on both GNU
    # date (documented: --reference=FILE) and BSD/macOS date (undocumented in
    # the man page, which only shows the epoch-seconds form, but verified to
    # accept a file path the same way). No `date -d` (GNU-only) needed.
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
    for _pattern in "today-"'*.md' "now.md" "recent.md" "archive"'*.md'; do
        for _mf in "$REMEMBER_DIR"/$_pattern; do
            [ -f "$_mf" ] || continue
            _MEMORY_FILE_COUNT=$((_MEMORY_FILE_COUNT + 1))
            _mf_bytes=$(wc -c < "$_mf" 2>/dev/null | tr -d ' ')
            case "$_mf_bytes" in ''|*[!0-9]*) _mf_bytes=0 ;; esac
            _MEMORY_BYTES=$((_MEMORY_BYTES + _mf_bytes))
        done
    done
fi
echo "OK   Memory files: $_MEMORY_FILE_COUNT file(s), $_MEMORY_BYTES bytes total"

if [ "$_POST_TOOL_FIRED" -eq 0 ]; then
    echo ""
    echo "REMEDIATION: enabling the plugin mid-session does not register its"
    echo "hooks for that session — Claude Code reads hook definitions only at"
    echo "session start. Restart Claude Code to activate PostToolUse capture."
fi
echo ""

# ── 6. Recent errors ─────────────────────────────────────────────────────────
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

# Log rotation (#252). A rotation that cannot run is invisible by construction:
# it happens inside a consolidation the user never watches, it writes one line
# into the very directory it failed to tidy, and it never escalates on its own.
# The reporter's install failed every day for five weeks — hook-errors.log was
# empty throughout, so this report would have said "OK" the entire time. This
# is the pull-based half of the fix: rotate_logs leaves a breadcrumb, and the
# command whose whole job is answering "is something silently broken?" reads it.
#
# WARN, not FAIL, and the VERDICT is deliberately left alone: rotation failing
# does not stop capture, and overstating it would devalue the verdict line that
# commands/doctor.md tells the operator to trust without scrolling up.
#
# The filename is log.sh's `_ROTATE_STATE_NAME`, spelled again here because this
# script must not source log.sh (read-only report; see the header). Rename both
# or neither.
_ROTATE_STATE="$REMEMBER_DIR/logs/.rotate-failed"
if [ -f "$_ROTATE_STATE" ]; then
    _RT_COUNT=$(sed -n 1p "$_ROTATE_STATE" 2>/dev/null)
    _RT_WHEN=$(sed -n 2p "$_ROTATE_STATE" 2>/dev/null)
    _RT_ERR=$(sed -n 3p "$_ROTATE_STATE" 2>/dev/null)
    _RT_PENDING=0
    for _rt_f in "$REMEMBER_DIR"/logs/memory-*.log; do
        [ -f "$_rt_f" ] && _RT_PENDING=$((_RT_PENDING + 1))
    done
    echo "WARN Log rotation has failed ${_RT_COUNT:-?} time(s) in a row (last ${_RT_WHEN:-unknown})"
    echo "     Reason: ${_RT_ERR:-not recorded}"
    # The count is every log file present, not only the aged ones — the live
    # log is in there too. Worded so it does not claim they are all overdue.
    echo "     $_RT_PENDING log file(s) currently in $REMEMBER_DIR/logs; the aged"
    echo "     ones will not be archived until this is fixed. Capture is unaffected."
else
    echo "OK   Log rotation: no failure recorded"
fi
echo ""

# ── Verdict ──────────────────────────────────────────────────────────────────
# Order matters more than it looks. Every one of these ends with "capture is
# not running", but they need different actions, and the generic
# "restart Claude Code" was previously reached FIRST — so a missing Python and
# a mismatched slug were both answered with a restart that fixes neither, on a
# line commands/doctor.md tells the operator not to second-guess. Specific
# causes are named before the general one.
#
# _ASSUMED_NOTE repeats the CLAUDE_PROJECT_DIR-was-guessed disclosure from the
# Paths section on the one line commands/doctor.md tells the operator to trust
# without scrolling up — every verdict below describes $PROJECT_DIR whether or
# not that's the project the operator meant (#207).
_ASSUMED_NOTE=""
if [ "$_PROJECT_DIR_ASSUMED" -eq 1 ]; then
    _ASSUMED_NOTE=" (CLAUDE_PROJECT_DIR was not set; this describes $PROJECT_DIR, assumed from the current directory)"
fi
if [ "$_POST_TOOL_FIRED" -eq 1 ] && [ -n "$_LAST_SAVE_TIME" ]; then
    echo "VERDICT: capture is working — last save $_LAST_SAVE_TIME$_ASSUMED_NOTE"
elif [ "$_PYTHON_OK" -eq 0 ]; then
    echo "VERDICT: problem — no usable Python; the pipeline cannot run at all (see Tools above)$_ASSUMED_NOTE"
elif [ -n "$_SESSION_DIR" ] && [ ! -d "$_SESSION_DIR" ]; then
    echo "VERDICT: problem — session dir slug does not match Claude Code's transcript directory (#144); restarting will not help$_ASSUMED_NOTE"
elif [ "$_POST_TOOL_FIRED" -eq 0 ]; then
    echo "VERDICT: problem — PostToolUse has never fired; restart Claude Code (see REMEDIATION above)$_ASSUMED_NOTE"
else
    echo "VERDICT: problem — PostToolUse has fired but no save has completed yet; check hook-errors.log above$_ASSUMED_NOTE"
fi
