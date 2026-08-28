#!/bin/bash
# ============================================================================
# doctor.sh — User-facing diagnostics command for the Remember plugin
# ============================================================================
#
# DESCRIPTION
#   Manual, human-run health check. Reports plugin version, resolved paths,
#   detected tools, storage mode, and capture health (whether PostToolUse has
#   ever actually fired and produced a save, and whether SessionEnd — the
#   last-chance flush, #370 — has ever fired) in a single plain-text report.
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

# Is this store known by a second spelling? (#298)
#
# A user who suspects their memory went missing had no way to check this, which
# is why it is here and not only in a log. Re-run live rather than read from
# tmp/case-divergence: the record is written at session start, and someone
# running the doctor has usually just changed something.
#
# WARN, never FAIL, and the VERDICT is deliberately left alone. On a
# case-insensitive filesystem this condition is harmless — the reporter of #298
# measured 88 files reachable through either spelling on the same directory
# object — and capture is entirely unaffected. It is a heads-up about a restore.
if source "$SCRIPT_DIR/lib-case-divergence.sh" 2>/dev/null && \
   command -v remember_case_divergence >/dev/null 2>&1; then
    remember_case_divergence
    case "$REMEMBER_CASE_STATUS" in
        diverged)
            echo "WARN Store spelling: this store is known by more than one spelling, differing only in case (resolved: $REMEMBER_CASE_RESOLVED)"
            echo "     $REMEMBER_CASE_MESSAGE"
            ;;
        ok)
            echo "OK   Store spelling: $REMEMBER_CASE_RESOLVED, on disk and in the store's git repository alike"
            ;;
        unavailable)
            # Both halves named, because the commonest case by far is a disk
            # that answered `ok` beside a git that has nothing to say — an
            # external store with no backup repository. Printing only "could
            # not check" would hide the half that did answer, and printing
            # "OK" would claim the half that did not.
            echo "WARN Store spelling: could not check in full — on disk: $REMEMBER_CASE_DISK_STATE${REMEMBER_CASE_DISK_REASON:+ ($REMEMBER_CASE_DISK_REASON)}; in git: $REMEMBER_CASE_GIT_STATE${REMEMBER_CASE_GIT_REASON:+ ($REMEMBER_CASE_GIT_REASON)}. This is not a report that they agree."
            ;;
    esac
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
    # 10# after the case, never instead of it (#332).
    echo $(( _now - 10#$_mtime ))
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

# ── SessionEnd liveness (#370) ──────────────────────────────────────────────
#
# PostToolUse's freshness-window reading above (a marker refreshed on every
# one of its many calls inside a live session) does not transfer here:
# SessionEnd fires at most once per session, so "how old is the marker" is a
# different question from "did the hook run last time it had the chance".
#
# No new marker is written for this. session-end-hook.sh already leaves
# usable evidence of its own accord, as a side effect of its background
# flush: a logs/autonomous/session-end-<HHMMSS>.log file, created
# unconditionally once that hook gets past its own SAVE_SCRIPT-missing check
# (see session-end-hook.sh's own comments around its `_END_LOG` redirect).
# Presence of even one such file is proof the hook has run; absence needs a
# second signal before it can be called a problem, since a hook that never
# had the chance to fire yet is not the same as one that had the chance and
# stayed silent — the third state the issue calls out by name.
#
# $_SESSION_DIR (Paths section, above) is Claude Code's own transcript
# directory for this project — one *.jsonl file per session it has ever
# started. A COUNT of files there is not evidence a session ended: two or
# more concurrently open Claude Code windows on the same project each keep
# their own transcript, live, at the same time, and neither has ended just
# because the other exists (#370 review). What distinguishes "a session
# existed and stopped being active" from "another window is open right now"
# is whether a transcript OTHER than the one currently growing has gone
# quiet — Claude Code appends to the active transcript on every turn, so a
# file nobody has touched in a while is read as no longer live. 900s (15
# minutes) is generous on purpose: false NEGATIVES here (a truly-ended
# session not yet counted) cost nothing but a delayed FAIL, while false
# POSITIVES are the failure mode #370's own review caught — a hard "problem"
# verdict for an ordinary two-windows-open workflow. It is not proof
# SessionEnd itself was invoked (its firing conditions on a crash or a
# killed terminal are undocumented; see session-end-hook.sh's own header) —
# only that the opportunity existed and the window for it has passed.
_SESSION_END_LOG_DIR="$REMEMBER_DIR/logs/autonomous"
_SESSION_END_FIRED=0
for _sel in "$_SESSION_END_LOG_DIR"/session-end-*.log; do
    [ -f "$_sel" ] && _SESSION_END_FIRED=1 && break
done

# Quietness alone is not proof of a genuine SessionEnd failure (#392): a
# transcript can go quiet because a session that ran here predates this
# project ever having a remember store, in which case no SessionEnd hook was
# ever registered to fire for it. REMEMBER_DIR's own mtime looked like the
# earliest "remember became active here" signal doctor.sh could read without
# new marker infrastructure, but it is NOT stable: save-session.sh writes
# now.md via mktemp-in-REMEMBER_DIR + mv (see save-session.sh's own Step 6
# comments), and both the mktemp and the mv update REMEMBER_DIR's own mtime,
# not just now.md's — so on any project with ongoing captures it reads as
# "time since the last save", not "time since install", reopening the exact
# false-negative window this fix exists to close (measured: a genuinely
# 2-day-old store with one ordinary save 5 minutes ago reads as installed 5
# minutes ago). $REMEMBER_DIR/.gitignore is what this reads instead:
# bootstrap-dirs.sh writes it exactly once, gated on
# `[ -f "$REMEMBER_DIR/.gitignore" ] || …`, and unlike every other path under
# REMEMBER_DIR it is never rewritten by ordinary hook activity.
#
# It is NOT permanently stable, though (caught in review): a legacy
# (in-project) store that is later migrated to external mode and backed up
# with git has this exact file deleted by
# hooks.d/after_save/50-git-backup.sh's own cleanup of the migration
# artifact ("removed per-slug .gitignore (legacy bootstrap artifact)") the
# first time a backed-up save runs, and bootstrap-dirs.sh's write is gated on
# the store being inside the project (`case "$REMEMBER_DIR" in
# "$_mem_proj"/*)`), which is false once external, so it is never recreated.
# From that point this baseline is permanently unavailable for that store —
# the same practical limit EXTERNAL storage mode already has from the start
# (bootstrap-dirs.sh never writes this file there either — "no gitignore to
# write" — so the baseline never exists to begin with), reached here via
# migration instead. Either way the arm below can only ever reach WARN, never
# FAIL, for a store in that state, until a marker survives that cleanup too
# (filed as a follow-up rather than fixed here: the fix touches
# hooks.d/after_save/50-git-backup.sh, outside this arm's own file). That is
# a known, documented gap, not a silently accepted one — the same "no
# reliable precondition, so WARN is the honest answer" outcome the issue
# itself sanctions. Absent or unreadable for any reason, no transcript can be
# attributed to "after install", which is the safe default: fall through to
# the third state below rather than guess.
_STORE_INSTALL_AGE=$(_file_age_seconds "$REMEMBER_DIR/.gitignore")

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
        case "$_tf_age" in
            ''|*[!0-9]*)
                # Found, but its age could not be read — the same third
                # state this file already names for the PostToolUse marker
                # above, not folded into either "counted" or "silently
                # dropped" (#392, defect 2).
                _SE_UNREADABLE_COUNT=$((_SE_UNREADABLE_COUNT + 1))
                continue
                ;;
        esac
        if [ -z "$_STORE_INSTALL_AGE" ] || [ "$_tf_age" -gt "$_STORE_INSTALL_AGE" ]; then
            # Quiet since before remember's own store existed for this
            # project (or no baseline could be read at all) — this
            # transcript's silence proves nothing about SessionEnd (#392).
            _SE_PREDATES_STORE_COUNT=$((_SE_PREDATES_STORE_COUNT + 1))
            continue
        fi
        _SE_TRANSCRIPT_COUNT=$((_SE_TRANSCRIPT_COUNT + 1))
        [ "$_tf_age" -gt 900 ] && _SE_STALE_TRANSCRIPT_COUNT=$((_SE_STALE_TRANSCRIPT_COUNT + 1))
    done
    if [ "$_SE_STALE_TRANSCRIPT_COUNT" -ge 1 ]; then
        echo "FAIL SessionEnd has never fired for this project (no $_SESSION_END_LOG_DIR/session-end-*.log),"
        echo "     though $_SE_STALE_TRANSCRIPT_COUNT prior session transcript(s) in $_SESSION_DIR"
        echo "     have gone quiet for over 15 minutes since remember became active here —"
        echo "     the last-chance flush is not running. See session-end-hook.sh's own"
        echo "     header for the endings Claude Code does not document firing on."
        _SESSION_END_STATE="not-fired"
    else
        echo "WARN SessionEnd has not fired yet, and no prior session has demonstrably"
        echo "     ended in this project since remember became active here"
        echo "     ($_SE_TRANSCRIPT_COUNT transcript(s) in $_SESSION_DIR attributable to"
        echo "     that window, none quiet long enough to call finished) — nothing has"
        echo "     had the chance to prove or disprove this yet."
        if [ "$_SE_PREDATES_STORE_COUNT" -gt 0 ]; then
            echo "     ($_SE_PREDATES_STORE_COUNT more transcript(s) predate this project's"
            echo "     remember store — or no store baseline could be read — and cannot"
            echo "     testify either way.)"
        fi
        if [ "$_SE_UNREADABLE_COUNT" -gt 0 ]; then
            echo "     ($_SE_UNREADABLE_COUNT more transcript(s) whose age could not be read"
            echo "     were excluded rather than counted.)"
        fi
    fi
else
    echo "WARN SessionEnd has not fired yet, and the session transcript directory is"
    echo "     unavailable (see Session dir slug above) — cannot tell whether a prior"
    echo "     session has had the chance to fire it."
fi
echo ""

# The capture-gap check can decline to answer (#270): without a session_id on
# the SessionStart payload it cannot tell the current session's transcript from
# the previous one, and it stays silent rather than accuse a healthy install.
# Silence is a claim of its own, so the skip is reported here — otherwise "no
# capture-gap warning" would mean both "nothing was missed" and "nobody looked".
# WARN, and the VERDICT is deliberately left alone: capture itself is unaffected.
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
            _MEMORY_BYTES=$((_MEMORY_BYTES + 10#$_mf_bytes))
        done
    done
fi
echo "OK   Memory files: $_MEMORY_FILE_COUNT file(s), $_MEMORY_BYTES bytes total"

# ── Is the store too large to consolidate? (#348) ────────────────────────────
#
# The session-start notice added in #347 tells the user to run this command
# when a memory file is too large to inject — and until now this command had
# nothing whatever to say about that condition. A remedy that points at a
# diagnostic which is silent about the thing it was pointed at is worse than no
# pointer: the user follows it, reads a clean report, and concludes the notice
# was noise.
#
# The number being checked is the one the pipeline actually enforces:
# pipeline/shell.py sizes staging + recent.md + archive.md before it reads any
# of them and skips the round when that sum is over thresholds.
# consolidate_max_bytes. So this measures the same three parts against the same
# cap, rather than warning on one file's size and hoping it correlates.
#
# Read without config() on purpose. That helper lives in log.sh, and this
# script must not source log.sh (read-only report; see the header). One grep
# against the merged config, which is flat JSON produced by lib-memory-dir.sh's
# merger and carries this key exactly once.
# Initialised before any branch can set it, so the VERDICT ladder below reads a
# defined value even when this whole section is skipped for an absent store.
_STORE_NEEDS_A_HUMAN=0
_CONSOLIDATE_MAX_BYTES=600000
_CONSOLIDATE_CAP_DISABLED=0
if [ -f "$REMEMBER_CONFIG" ] && [ -s "$REMEMBER_CONFIG" ]; then
    _cmb=$(grep -o '"consolidate_max_bytes"[[:space:]]*:[[:space:]]*[0-9]*' "$REMEMBER_CONFIG" 2>/dev/null \
        | sed 's/.*:[[:space:]]*//' | head -1)
    case "$_cmb" in (''|*[!0-9]*) : ;; (*) _CONSOLIDATE_MAX_BYTES=$((10#$_cmb)) ;; esac
fi
# pipeline/shell.py:452 documents and :542 implements 0 as the cap being
# DISABLED, not a 0-byte limit -- consolidation never skips a round on size
# when it is set. "0" is all-digits, so the case guard above happily parses
# it, and without this the block below would compare every non-empty store
# against a floor nothing can clear (#360). Read once, here, so the
# comparison below can render the disabled state instead of a permanent
# false alarm.
[ "$_CONSOLIDATE_MAX_BYTES" -eq 0 ] && _CONSOLIDATE_CAP_DISABLED=1

# Three states, not two. An absent file contributes 0 to the prompt and that is
# a measurement; a file that EXISTS and cannot be read contributes an unknown
# number, and folding it into the same 0 makes "I looked and found nothing"
# and "I could not look" arrive as the same sentence — from the one command
# whose whole job is telling a human whether to worry. So the unreadable ones
# are named, and the total they are missing from is not signed off as healthy.
#
# Sets _SIZE_BYTES rather than echoing it: a caller writing
# `x=$(_size_of f)` runs the function in a subshell, and the _STORE_UNREADABLE
# append below would die with it — the third state detected and then discarded,
# which is the same defect one level down.
_STORE_UNREADABLE=""
_SIZE_BYTES=0
_size_of() {
    _SIZE_BYTES=0
    [ -f "$1" ] || return 0
    _size_raw=$(wc -c < "$1" 2>/dev/null | tr -d ' ')
    case "$_size_raw" in
        (''|*[!0-9]*)
            _STORE_UNREADABLE="${_STORE_UNREADABLE}${1}
"
            ;;
        (*) _SIZE_BYTES=$((10#$_size_raw)) ;;
    esac
}

if [ ! -d "$REMEMBER_DIR" ]; then
    # The third state, and it is not "healthy". A check that cannot look has to
    # say it could not look; reporting OK here would be a clean bill of health
    # for a store nothing measured.
    echo "WARN Consolidation size check: skipped — $REMEMBER_DIR does not exist"
else
    _size_of "$REMEMBER_DIR/recent.md";  _RECENT_BYTES=$_SIZE_BYTES
    _size_of "$REMEMBER_DIR/archive.md"; _ARCHIVE_BYTES=$_SIZE_BYTES
    # Staging as consolidation counts it: past days only, and never a file
    # already retired to .done.md. The pipeline reaches "today" through
    # config.timezone -> REMEMBER_TZ (scripts/log.sh:366) ->
    # pipeline/_tz.py's today_str(), which is what _eligible_staging
    # (pipeline/shell.py:393) excludes today by -- so TODAY here has to be
    # read the same way, or a configured timezone ahead of the machine's own
    # can make this diagnostic exclude the file the pipeline counts and count
    # the file the pipeline excludes, at once. That is a divergence in BOTH
    # directions, not the safe under-counting one this comment used to claim
    # (#357): when the excluded/counted file is the larger of the two,
    # _STAGING_BYTES can cross the cap on a store the pipeline is about to
    # rotate happily.
    #
    # Read without config() on purpose, same grep-then-use shape
    # _CONSOLIDATE_MAX_BYTES already uses above: this script must not source
    # log.sh (read-only report; see the header). An empty/absent timezone
    # must NOT become `TZ="" date` -- that's UTC on macOS/BSD, the same trap
    # lib-clock.sh's own comment names.
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
    _STAGING_BYTES=0
    for _sf in "$REMEMBER_DIR"/today-*.md; do
        [ -f "$_sf" ] || continue
        case "${_sf##*/}" in
            (*.done.md) continue ;;
            (*"$_DOCTOR_TODAY"*) continue ;;
        esac
        _size_of "$_sf"
        _STAGING_BYTES=$((_STAGING_BYTES + _SIZE_BYTES))
    done
    _STORE_BYTES=$((_STAGING_BYTES + _RECENT_BYTES + _ARCHIVE_BYTES))

    if [ -n "$_STORE_UNREADABLE" ]; then
        echo "WARN Consolidation size check is incomplete — these memory files exist"
        echo "     but could not be read, so nothing below counts their bytes:"
        printf '%s' "$_STORE_UNREADABLE" | while IFS= read -r _uf; do
            [ -n "$_uf" ] && echo "     $_uf"
        done
        echo "     The total is therefore a floor, not the store's size."
    fi

    if [ "$_CONSOLIDATE_CAP_DISABLED" -eq 1 ]; then
        # Three states, not "OK" doing double duty for "measured and fine"
        # and "never measured against anything" -- that would be this file's
        # own defect class one line up (#360). thresholds.consolidate_max_bytes
        # of 0 is not a 0-byte cap; pipeline/shell.py:542 never skips a round
        # on size when it reads 0, so nothing below is compared against it.
        echo "OK   Consolidation size check: disabled (thresholds.consolidate_max_bytes: 0) — size never blocks a round"
    elif [ "$_STORE_BYTES" -gt "$_CONSOLIDATE_MAX_BYTES" ]; then
        if [ "$_STAGING_BYTES" -gt "$_CONSOLIDATE_MAX_BYTES" ]; then
            # The one shape rotation cannot fix. FAIL, and it takes a VERDICT
            # arm below, because nothing in the pipeline will clear it and the
            # user reading this was sent here by a notice that promised an
            # answer. Rotating recent.md here would split an unconsolidated
            # span for nothing and the very next round would skip identically.
            _STORE_NEEDS_A_HUMAN=1
            echo "FAIL Store is too large to consolidate and cannot heal itself:"
            echo "     $_STORE_BYTES bytes against a thresholds.consolidate_max_bytes cap"
            echo "     of $_CONSOLIDATE_MAX_BYTES — recent.md $_RECENT_BYTES + archive.md $_ARCHIVE_BYTES"
            echo "     + past-day staging $_STAGING_BYTES."
            echo "     Past-day staging ALONE is over the cap, so rotating recent.md or"
            echo "     archive.md would not help and the pipeline will not do it: the next"
            echo "     round would skip on the same sum. Every round skips while this holds."
            echo "     The oversized today-*.md files under $REMEMBER_DIR are the thing to look at."
        else
            # WARN, not FAIL, and the VERDICT is deliberately left alone — the
            # same trade the log-rotation and case-divergence checks make above.
            # Capture is entirely unaffected (saves still land in today-*.md),
            # and since #348 the remedy is "do nothing": the next consolidation
            # rotates the oversized file and resumes on its own.
            echo "WARN Store is too large to consolidate right now: $_STORE_BYTES bytes"
            echo "     against a thresholds.consolidate_max_bytes cap of $_CONSOLIDATE_MAX_BYTES"
            echo "     — recent.md $_RECENT_BYTES + archive.md $_ARCHIVE_BYTES + past-day staging $_STAGING_BYTES."
            echo "     Capture is unaffected; consolidation is what skips, and staging piles"
            echo "     up until it runs."
            echo "     REMEDIATION: none by hand. The next consolidation rotates the"
            echo "     oversized file to a dated sibling (recent-YYYY-MM-DD.md /"
            echo "     archive-YYYY-MM-DD.md), starts a fresh one, and resumes. Nothing is"
            echo "     deleted — the bytes stay on disk, stay greppable, and session start"
            echo "     names the rotated slices."
        fi
    elif [ -n "$_STORE_UNREADABLE" ]; then
        # A floor under the cap proves nothing. Saying OK here would be the
        # absence this section exists to remove, one level up: an unmeasured
        # store signed off as a measured one.
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
# One new arm (#348), and only for the store shape nothing in the pipeline will
# clear on its own. The self-healing shape stays a WARN with the verdict left
# alone: capture is unaffected there and the next round repairs it, so claiming
# a problem would devalue the line commands/doctor.md tells the operator to
# trust without scrolling. This arm sits ABOVE "capture is working" because
# capture usually IS working in this state — saves land in today-*.md and pile
# up unconsolidated, which is exactly why a healthy-looking verdict here would
# send away the user the session-start notice sent in.
#
# It sits BELOW the no-usable-Python arm (#359): staging over the cap on its
# own is the EFFECT of consolidation not running, and no usable Python is one
# CAUSE of that. This ladder's own rule above is specific causes before the
# general one, and "no usable Python" is the more specific of the two — a
# broken interpreter on an already-large store used to read as "the staging
# files are over the prompt cap on their own," sending the operator to look
# at oversized files instead of the Tools section that actually explains it.
# One more arm (#370), and it sits ABOVE "capture is working" rather than
# below it — a break with how every other secondary WARN-only check in this
# file behaves (log rotation, case divergence, the self-healing oversized-
# store shape all leave the VERDICT alone on purpose). Those all describe
# conditions where capture ITSELF is unaffected; this one does not. A
# SessionEnd that never fires is capture's own last-chance flush silently
# not running, which is #370's whole complaint: a user whose PostToolUse
# capture looks perfectly healthy sees the exact same "capture is working"
# line as one whose SessionEnd hook is unregistered or dying before it
# forks, right up until the session that ends in conversation rather than
# tool calls loses its tail with no warning at all. The ladder's own rule
# above is "specific causes before the general one" for DIFFERENT
# explanations of the SAME symptom; here it is a genuinely separate hook
# with its own failure mode, and reaching the general "capture is working"
# line first would be exactly the invisibility this issue reports, just
# moved one arm down the same ladder. It sits BELOW the no-usable-Python and
# oversized-store arms because those mean literally nothing in the pipeline
# runs at all, which outranks a narrower, single-hook failure every time.
if [ "$_PYTHON_OK" -eq 0 ]; then
    echo "VERDICT: problem — no usable Python; the pipeline cannot run at all (see Tools above)$_ASSUMED_NOTE"
elif [ "${_STORE_NEEDS_A_HUMAN:-0}" -eq 1 ]; then
    echo "VERDICT: problem — memory is being captured but never consolidated; the staging files are over the prompt cap on their own (see above)$_ASSUMED_NOTE"
elif [ "$_SESSION_END_STATE" = "not-fired" ]; then
    echo "VERDICT: problem — SessionEnd has never fired despite prior sessions ending in this project; the last-chance flush is not running (see above)$_ASSUMED_NOTE"
elif [ "$_POST_TOOL_FIRED" -eq 1 ] && [ -n "$_LAST_SAVE_TIME" ]; then
    echo "VERDICT: capture is working — last save $_LAST_SAVE_TIME$_ASSUMED_NOTE"
elif [ -n "$_SESSION_DIR" ] && [ ! -d "$_SESSION_DIR" ]; then
    echo "VERDICT: problem — session dir slug does not match Claude Code's transcript directory (#144); restarting will not help$_ASSUMED_NOTE"
elif [ "$_POST_TOOL_FIRED" -eq 0 ]; then
    echo "VERDICT: problem — PostToolUse has never fired; restart Claude Code (see REMEDIATION above)$_ASSUMED_NOTE"
else
    echo "VERDICT: problem — PostToolUse has fired but no save has completed yet; check hook-errors.log above$_ASSUMED_NOTE"
fi
