#!/bin/bash
# ============================================================================
# lib-clock.sh — _remember_date, without a process where bash can do it (#227)
# ============================================================================
#
# DESCRIPTION
#   Resolves "now" using REMEMBER_TZ when set, else system local time. This
#   lived in log.sh; it moved here so the one hook that needs nothing else from
#   log.sh — user-prompt-hook.sh — can have the time without the whole chain.
#
#   Two implementations, one output:
#
#     * bash's `printf '%(FMT)T'` builtin — no process at all. bash >= 4.2.
#     * `date "+FMT"` — one process, and the ONLY path on stock macOS, which
#       ships bash 3.2.57. Not a legacy fallback: it is what the plugin's own
#       author and the #204 reporter run.
#
#   Both hand the same format to the same libc strftime, so they agree byte for
#   byte — pinned by tests/test_prompt_hook_spawns.py, which runs both and
#   compares, rather than trusting the format string. Getting fast on a slow
#   platform must not cost the timestamp on a fast one: a builtin-only rewrite
#   prints a literal `%(%H:%M %Z)T` on every stock Mac.
#
# USAGE
#   source "${BASH_SOURCE[0]%/*}/lib-clock.sh"
#   NOW=$(_remember_date '+%H:%M %Z')
#
# ENVIRONMENT
#   REMEMBER_TZ            Timezone override (set by log.sh from config.json).
#                          An empty value must NOT become `TZ="" date` — that is
#                          UTC on macOS/BSD, which is how a log file once got
#                          tomorrow's name after 20:00 local.
#   REMEMBER_NO_PRINTF_T=1 Force the `date` path. Exists so CI on bash 5 can
#                          still exercise the bash 3.2 implementation; a
#                          fallback only ever run on one platform is a fallback
#                          nobody tests.
#
# THE SEAM THIS REMOVED
#   A builtin is not on PATH, so putting a fake `date` in front of PATH — the
#   obvious way to test anything time-dependent in a shell pipeline, and how
#   tests/test_ndc_day_boundary.py fakes midnight — no longer intercepts
#   anything on bash >= 4.2. The fake is not refused, it is ignored, and the test
#   quietly asserts against the real clock: green on macOS bash 3.2, red
#   everywhere else. REMEMBER_NO_PRINTF_T=1 restores the seam, and the shared
#   test env builder (tests/test_save_session_gates.py::_make_env) sets it for
#   every shell test. A lint in tests/test_prompt_hook_spawns.py fails if a test
#   shims `date` without naming that variable.
#
# ============================================================================

[ -n "${_REMEMBER_LIB_CLOCK_LOADED:-}" ] && return 0
_REMEMBER_LIB_CLOCK_LOADED=1

# BASH_VERSINFO is a shell variable, not a fork — the version test costs
# nothing, which is the whole point of the exercise.
if [ "${BASH_VERSINFO[0]:-0}" -gt 4 ] 2>/dev/null; then
    _REMEMBER_PRINTF_T=1
elif [ "${BASH_VERSINFO[0]:-0}" -eq 4 ] 2>/dev/null && [ "${BASH_VERSINFO[1]:-0}" -ge 2 ] 2>/dev/null; then
    _REMEMBER_PRINTF_T=1
else
    _REMEMBER_PRINTF_T=0
fi
[ "${REMEMBER_NO_PRINTF_T:-0}" = "1" ] && _REMEMBER_PRINTF_T=0

# Formats the builtin is NOT trusted with, checked as a glob — no fork.
#
# `%-I` and friends are GNU *date* padding flags. GNU coreutils implements them
# in its own strftime copy, so `date '+%-I'` works on a box whose libc strftime
# would print the flag verbatim — and save-session.sh uses exactly that for the
# 12-hour clock. `%s` is likewise a strftime extension, and post-tool-hook.sh
# feeds its result straight into $(( )), where a literal "%s" is not a wrong
# number but an arithmetic error.
#
# Neither is on a hot path. Send them to `date`, where they already worked.
_remember_date_builtin_ok() {
    case "$1" in
        *%-*|*%_*|*%0*|*%^*|*%#*|*%s*) return 1 ;;
    esac
    return 0
}

# _remember_date [+FORMAT ...]
# Print the current time. Argument form is `date`'s, because every call site
# already used `date`.
_remember_date() {
    if [ -n "${REMEMBER_TZ:-}" ]; then
        # An explicit timezone goes to `date`. bash's builtin reads TZ from the
        # process environment, and changing that for one builtin invocation is
        # exactly the kind of "probably fine" this file exists to avoid — while
        # a configured timezone is, by definition, not the common case that
        # needs to be free.
        TZ="$REMEMBER_TZ" date "$@"
        return
    fi
    if [ "$_REMEMBER_PRINTF_T" = "1" ] && [ "$#" -eq 1 ] \
        && _remember_date_builtin_ok "$1"; then
        printf "%(${1#+})T\\n" -1 && return
    fi
    date "$@"
}
