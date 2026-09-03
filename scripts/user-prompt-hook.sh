#!/bin/bash
# ============================================================================
# user-prompt-hook.sh — UserPromptSubmit hook for the Remember plugin
# ============================================================================
#
# DESCRIPTION
#   Runs on every user prompt submission. Injects the current timestamp
#   so the agent knows what time it is during the conversation.
#
# USAGE
#   Called automatically by Claude Code's UserPromptSubmit hook system.
#   Not intended for manual invocation.
#
# ENVIRONMENT
#   CLAUDE_PLUGIN_ROOT   Plugin install directory (set by Claude Code)
#   CLAUDE_PROJECT_DIR   Project root (default: .)
#
# DEPENDENCIES
#   lib-clock.sh    (the timestamp, without a process where bash can do it)
#   lib-env-cache.sh (replays an already-resolved REMEMBER_DIR / REMEMBER_TZ)
#   jq (for config.json reading via log.sh — first run in a project only)
#   log.sh (for timezone, dispatch via hooks.d/ — first run in a project only)
#
# EXIT CODES
#   0   Always (hook must not block the agent)
#
# OUTPUT
#   Prints "[HH:MM TZ — username]" to stdout. The `prompt_stamp` config option
#   narrows that to "[username]" (`stable`) or to nothing (`off`) — see #301.
#
# COST (#227)
#   This runs on every prompt the user submits and the user waits for it. It
#   used to source resolve-paths.sh → bootstrap-dirs.sh → log.sh to print one
#   line: 19 processes on macOS, 27 on the Windows-ARM64-under-QEMU box
#   @MargeryOlethea measured, where per-spawn costs of 150-800ms turned it into
#   a p50 of 8718ms — blocking, on every prompt, with 6 outright timeouts in 256
#   runs. It now takes the chain only when it has no already-resolved answer to
#   replay, which is once per project per config change.
#
#   That matters more here than the numbers suggest: on UserPromptSubmit a
#   non-zero exit does not merely error, it BLOCKS THE PROMPT AND ERASES WHAT
#   THE USER TYPED. Every process this does not start is an opportunity to fail
#   that it does not take.
#
# ============================================================================

_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="."

# --- Nested summarizer: there is no project here (#204) ---
# Normally this guard lives in resolve-paths.sh, which the fast path below does
# not reach. It has to hold for every hook this plugin registers — the whole
# point of #204 was that any one of them alone scaffolds a memory directory
# under the summarizer's temp dir and injects into its context.
[ -n "${REMEMBER_NESTED_SUMMARIZER:-}" ] && exit 0

# --- REMEMBER_HOOK_CWD (#417, #444) ---
# resolve-paths.sh falls back to this variable when CLAUDE_PROJECT_DIR is
# unset (#411). Cleared here first and unconditionally, before anything below
# could set or inherit it -- the #417 leak this closes is a DIFFERENT
# session's SessionStart-exported value surviving into a hook that has not
# validated it, or (on a host that reuses one process environment across
# invocations) a stale value this same hook wrote on a PREVIOUS run. This
# hook now offers its own stdin `cwd` further down (#444, #479), but that
# read has to happen after this unset, never instead of it, or the #417 leak
# reopens. Clearing it here is cheap and unconditionally
# correct regardless of whether that reuse is possible on any supported host:
# on the common path CLAUDE_PROJECT_DIR is already set and this arm never
# runs anyway.
unset REMEMBER_HOOK_CWD

# --- REMEMBER_TRANSCRIPT_PATH (#424) ---
# pipeline/host.transcript_path() trusts this variable once it names a real
# file, and pipeline/extract.py's find_session() returns that value BEFORE
# the traversal validator (_validate_session_id) ever runs -- so a value set
# anywhere in the ambient environment reads an arbitrary file straight into
# the memory store, no `../` required. Only session-start-hook.sh and
# session-end-hook.sh have a legitimate transcript_path to offer, extracted
# fresh from their own stdin payload on every run. This hook has none and
# must not silently consult whatever the process environment already holds,
# for the same reason and under the same unestablished-reachability
# reasoning as the REMEMBER_HOOK_CWD unset just above (#417).
unset REMEMBER_TRANSCRIPT_PATH

# --- Host-conditional stdout shape (#451) ---
# Claude Code treats this hook's plain stdout as `additionalContext` -- that
# is what the prompt stamp below is for, and every existing regression test
# (#301, #280) pins it byte-for-byte. Codex's own UserPromptSubmit parser
# does something different: it sniffs the first non-whitespace byte of
# stdout, and `{` or `[` means "this claims to be my JSON contract"
# (codex-rs/hooks/src/engine/output_parser.rs::looks_like_json, read from
# openai/codex @ 2026-08-29 -- Codex ships no separate hooks.md). That
# contract is user-prompt-submit.command.output.schema.json
# (codex-rs/hooks/schema/generated/), consumed by
# events/user_prompt_submit.rs::parse_completed. The plain stamp this hook
# has always printed, "[HH:MM TZ -- user]", opens with `[` -- so Codex tries
# to read it as that JSON contract, fails, and reports the hook run
# HookRunStatus::Failed even though nothing failed. Plain text that does
# NOT open with `{`/`[` is accepted by that same parser and appended as
# additionalContext with status Completed, so the fix is not "never print
# the stamp on this host", it is "print it inside the envelope Codex's own
# schema names" -- see the tail of this file.
#
# CLAUDE_PROJECT_DIR is the signal already used for this exact distinction
# in resolve-paths.sh's own ENVIRONMENT block: Claude Code always sets it,
# Codex documents no such variable and never does, and Gemini CLI
# "documents no hook environment variables whatsoever" either. So this flag
# also covers Gemini CLI, whose stdout contract this repo has NOT observed
# -- REASONED, not observed, for any host besides Codex 0.150.1. The JSON
# envelope is what Codex's own schema documents, and is no worse than a
# bracket that collides with Codex's heuristic on every host it has not
# been checked against either.
if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
    _REMEMBER_HOST_JSON_STDOUT=0
else
    _REMEMBER_HOST_JSON_STDOUT=1
fi

source "$_HOOK_DIR/lib-clock.sh"
source "$_HOOK_DIR/lib-env-cache.sh"

# --- REMEMBER_HOOK_CWD from stdin (#444, moved ahead of the cache lookup
# below for #479) ---
# resolve-paths.sh's REMEMBER_HOOK_CWD fallback (#411) only ever gets a
# value from session-start-hook.sh and session-end-hook.sh, which is why a
# host that never sets CLAUDE_PROJECT_DIR (Codex, Gemini CLI) hit the FATAL
# in resolve-paths.sh on this hook before #411/#444: the #417 unset above
# left it correct but with no legitimate source of its own. UserPromptSubmit
# carries `cwd` on its own stdin payload on all three hosts (#407's
# comparison table), so read it here.
#
# THIS MUST RUN BEFORE _remember_env_cache_load BELOW (#479). That
# function's own key, _remember_env_cache_path, falls back to
# REMEMBER_HOOK_CWD when CLAUDE_PROJECT_DIR is unset (#469) -- and on
# Codex/Gemini CLAUDE_PROJECT_DIR is NEVER set. Before #479 this read lived
# inside the "cache missed" branch below, i.e. it only ever ran AFTER the
# cache lookup this very variable was meant to key had already failed for
# want of it: every UserPromptSubmit wrote a cache file and none ever read
# one back -- verbatim the #469 symptom, still present on the one hook #469
# exists to fix.
#
# Paying this unconditionally -- on the fast path too -- is a real change
# from the #227 reasoning further up this file, which is about FORKED
# processes (150-800ms each, per the COST section above). This read forks
# nothing: it is a bash builtin loop over stdin the host has already
# written and closed by the time this script starts, bounded in TIME only
# below. Measured 2026-09-01 on macOS/bash 3.2 (this read's own semantics):
# 50 runs of this exact read-and-parse loop against a realistic
# UserPromptSubmit payload averaged 8.5ms end-to-end, against 9.5ms for a
# no-op bash process doing nothing but consume the same stdin -- the loop's
# own marginal cost does not clear that measurement's noise floor. The
# alternative -- leaving the read on the slow path only -- keeps this hook
# with no working fast path at all on Codex/Gemini, which is the choice
# #479 exists to make explicit rather than silent.
#
# WORST CASE, not just average case: before this change, a cache-HIT
# invocation never touched stdin at all, so it could never block on it.
# After this change every invocation, hit or miss, carries the same
# `read -t 1` ceiling the slow path already had -- a host that leaves the
# pipe open without writing/closing it now costs up to 1s on the hot path
# too, not 0ms. Accepted deliberately, not overlooked: `post-tool-hook.sh`
# already reads stdin unconditionally ahead of its own cache-load check
# (see its REMEMBER_HOOK_CWD block), on the hook that fires roughly ten
# times more often than this one, with no reported incident -- this is an
# extension of that same accepted tradeoff to a second hook, not a new
# risk class.
#
# Bounded in TIME only (`read -t 1`, never a tty), the same reasoning every
# other stdin-reading hook in this repo already carries: a blocking read
# here is not a slow prompt, it is a lost one (see the COST section above).
# bash 3.2 has no sub-second -t, hence 1.
_HOOK_STDIN=""
if [ ! -t 0 ]; then
    _line=""
    while IFS= read -r -t 1 _line || [ -n "$_line" ]; do
        _HOOK_STDIN="$_HOOK_STDIN$_line"
        _line=""
    done
fi
# The same deliberately narrow extractor session-start-hook.sh uses: the
# key must be followed by nothing but whitespace and a colon before the
# value's opening quote, so a `cwd` appearing inside some other field is
# not mistaken for it.
#
# #494 -- REACHABILITY OF THE NESTED-BEFORE-TOP-LEVEL GAP (researched, not
# assumed): tests/test_stdin_extractor_top_level_wins_447.py pins that THIS
# function takes the FIRST `"cwd"` occurrence in the raw stdin text, so a
# same-named key nested inside some other field wins over the top-level one
# when it occurs earlier in the byte stream. Whether that shape is reachable
# from a real host was the open half of #447/#493, settled here by reading
# all three hosts' hook payload schemas:
#
#   Claude Code -- every hook payload (docs.claude.com/en/docs/claude-code/
#   hooks) puts `cwd` in the shared top-level object (session_id,
#   transcript_path, cwd, permission_mode, hook_event_name, ...), and
#   `tool_input`/`tool_response` -- the only nested objects a hook payload
#   ever carries -- are declared AFTER it in every documented example. No
#   built-in tool's input schema uses a `cwd` parameter. NOT source-verified
#   (Claude Code's hook serializer is not open source): docs-observed only.
#
#   Codex -- SOURCE-VERIFIED (codex-rs/core/src/hook_runtime.rs): the
#   request structs declare `cwd` before `tool_input`, and serde's default
#   struct serialization preserves declaration order, so `cwd` is
#   guaranteed to serialize first. The built-in shell tool's own working-
#   directory parameter is named `workdir`, not `cwd`.
#
#   Gemini CLI -- docs (github.com/google-gemini/gemini-cli, docs/hooks/
#   reference.md) show the same shape: `cwd` in the shared base object,
#   `tool_input` appended after it for BeforeTool/AfterTool. The built-in
#   shell tool's directory parameter is named `dir_path`, not `cwd`.
#   NOT source-verified (spread/construction order not confirmed): docs-
#   observed only.
#
# The remaining theoretical opening on all three hosts is a THIRD-PARTY MCP
# tool whose author names one of ITS OWN input parameters `cwd` -- `tool_input`
# for an MCP call is passed through as an opaque value on every host checked,
# unconstrained by any schema this plugin controls. But that still does not
# reach the gap this file's extractor has: on every host and every payload
# shape found, `tool_input` (the only place such a key could appear) is
# positioned AFTER the top-level `cwd` field, not before it -- so even an
# adversarial MCP tool parameter named `cwd` lands in the SAFE "nested-after"
# case (tests/test_stdin_extractor_top_level_wins_447.py's first test), never
# the "nested-before" one this comment is about. The gap the second test pins
# is real as a property of THIS extractor's mechanism (first-occurrence
# scanning, not top-level-aware), but no known, currently-shipped host payload
# reaches it -- and a host is free to reorder its own schema in a future
# release, which is why this stays documentation and a synthetic-input test
# rather than a load-bearing guarantee. #340/#344's standing decision not to
# acquire a JSON parser for a hook that must survive a broken install is
# unchanged by this finding.
_stdin_cwd() {
    local raw="$1" rest prefix value
    case "$raw" in *'"cwd"'*) ;; *) return 1 ;; esac
    rest=${raw#*\"cwd\"}
    prefix=${rest%%\"*}
    case "$prefix" in *[!:[:space:]]*) return 1 ;; esac
    value=${rest#*\"}
    value=${value%%\"*}
    [ -n "$value" ] || return 1
    printf '%s' "$value"
}
# _stdin_cwd_into VARNAME RAW (#511): the same scan as _stdin_cwd above,
# writing the result into VARNAME with `printf -v` instead of printing it
# for a caller to capture with `$( )`. `_stdin_cwd` stays as it is --
# tests/test_stdin_extractor_top_level_wins_447.py extracts and calls it
# verbatim by name, print-and-capture, and this file's own extractor must
# stay the thing that test actually exercises. But every real invocation of
# this hook pays for that capture too, on every single prompt, for a value
# that never needs to leave this process at all -- `$( )` forks a subshell
# for the substitution itself regardless of how cheap the function body is,
# same reasoning as lib-clock.sh's _remember_date_into. Kept in exact sync
# with _stdin_cwd by hand (both are five lines of parameter expansion, not a
# function this hook can safely delegate to a sourced library it might fail
# to load -- see _stdin_cwd's own comment above).
_stdin_cwd_into() {
    local _var="$1" raw="$2" rest prefix value
    case "$raw" in *'"cwd"'*) ;; *) return 1 ;; esac
    rest=${raw#*\"cwd\"}
    prefix=${rest%%\"*}
    case "$prefix" in *[!:[:space:]]*) return 1 ;; esac
    value=${rest#*\"}
    value=${value%%\"*}
    [ -n "$value" ] || return 1
    printf -v "$_var" '%s' "$value"
}
# Validated the same way session-start-hook.sh validates its own copy: data
# from a host payload, at the point of entry. A project directory
# legitimately contains slashes and dots, so only an embedded newline or
# carriage return is rejected -- whether the value actually names a
# directory is decided in resolve-paths.sh, which falls back to the
# existing derivation when it does not.
_stdin_cwd_into REMEMBER_HOOK_CWD "$_HOOK_STDIN" || REMEMBER_HOOK_CWD=""
case "$REMEMBER_HOOK_CWD" in
    *$'\n'*|*$'\r'*) REMEMBER_HOOK_CWD="" ;;
esac
export REMEMBER_HOOK_CWD

# --- Resolve paths ---
# Two facts are needed below: REMEMBER_DIR (for the capture-gap notice) and
# REMEMBER_TZ (for the timestamp). If a previous hook in this project already
# derived them from the same config files, replay that instead of re-deriving
# it; lib-env-cache.sh validates and declines, it never computes.
#
# hooks.d/after_user_prompt/ is checked here because a listener there is the one
# thing on this path that needs log.sh's dispatch(). The distribution ships no
# such directory, so `dispatch` was already a no-op for everyone — but a user
# who creates one gets the documented behaviour, at the old price.
_REMEMBER_FAST=0
if _remember_env_cache_load && [ ! -d "$PIPELINE_DIR/hooks.d/after_user_prompt" ]; then
    _REMEMBER_FAST=1
    # Both normally come from the chain: umask from resolve-paths.sh (#68),
    # SYS_TMPDIR from bootstrap-dirs.sh. Same values, no processes.
    umask 077
    SYS_TMPDIR="${TMPDIR:-/tmp}"
    dispatch() { :; }
fi

if [ "$_REMEMBER_FAST" = "0" ]; then
    # Opt into resolve-paths.sh's soft-failure mode — see the comment in
    # session-start-hook.sh. This hook must never block the agent, so a
    # resolution failure is a silent no-op, not a crash.
    REMEMBER_PATHS_SOFT_FAIL=1 source "$_HOOK_DIR/resolve-paths.sh" || exit 0
    source "$_HOOK_DIR/bootstrap-dirs.sh"
    source "$_HOOK_DIR/log.sh" 2>/dev/null
    _remember_env_cache_publish
fi

# log.sh returns early — before it defines dispatch() — on a store whose
# logs/ dir it cannot create. That leaves dispatch() undefined on this branch
# only (the fast path above already stubs it unconditionally), and this hook
# is documented "EXIT CODES: 0 Always": it must not shell out to a
# "command not found" for a listener nobody has to install (adjacent to
# #361, same mechanism, this file's own unguarded call).
declare -F dispatch >/dev/null 2>&1 || dispatch() { :; }

# --- Notices for the human (#200, #253) ───────────────────────────────────
# Other hooks leave a file here when they find something the HUMAN has to know
# and the model cannot act on. They are delivered from this hook rather than
# from the one that found them because `systemMessage` is the only hook output
# the HUMAN sees, and a notice only the model sees is how #200 stayed invisible
# for a day in the first place — and how #253 stayed invisible for twelve.
#
#   capture-gap-notice  session-start-hook.sh: the PREVIOUS session ran
#                       SessionStart but never PostToolUse — the signature of a
#                       plugin enabled mid-session, whose hooks Claude Code
#                       never wired in.
#   git-backup-notice   after_save/50-git-backup.sh: the backup remote has
#                       rejected the last N pushes, so memory is committed
#                       locally and going nowhere, and no retry will fix it.
#   git-restore-notice  before_session_start/50-git-restore.sh: the store has
#                       DIVERGED from its backup remote, so the memory loaded
#                       this session is missing what the other machine wrote,
#                       and nothing will merge or rebase it for you.
#   case-divergence-notice
#                       session-start-hook.sh: this store is known by a second
#                       spelling that differs only in case (#298). Harmless on
#                       the case-insensitive filesystem it is sitting on, and it
#                       splits the store in two on a case-sensitive restore.
#                       Written only when the finding CHANGES: the condition
#                       never clears itself, so a notice every session would
#                       spend this channel on wallpaper.
#
# Consumed on read: these are one-line nudges, not persistent banners. Adding
# one is deliberately cheap and deliberately rare — this channel interrupts a
# human mid-thought, and one that fires often is one that gets tuned out.
NOTICE_MSG=""
for _notice_name in capture-gap-notice git-backup-notice git-restore-notice case-divergence-notice; do
    NOTICE_FILE="$REMEMBER_DIR/tmp/$_notice_name"
    [ -f "$NOTICE_FILE" ] || continue
    _notice_body=$(cat "$NOTICE_FILE" 2>/dev/null)
    rm -f "$NOTICE_FILE" 2>/dev/null || true
    [ -n "$_notice_body" ] || continue
    if [ -n "$NOTICE_MSG" ]; then
        NOTICE_MSG="$NOTICE_MSG
$_notice_body"
    else
        NOTICE_MSG="$_notice_body"
    fi
done

# --- Timestamp + context injection ---
# Collected rather than echoed: with a notice to deliver the whole reply has to
# become one JSON object, and stdout cannot be half plain text and half JSON.
# The no-notice path below still prints exactly what it always did.
CTX=$(
# What this hook is allowed to say (#301). Resolved by log.sh and replayed from
# the env cache, so reading it costs nothing here — see lib-env-cache.sh. The
# case is a second guard rather than trust in the writer: this value arrives
# from a file in a shared temp directory.
#
# NOT a `case`, and it must not become one: this whole block runs inside a
# `$( )` command substitution, where bash's parser reads the `)` closing a case
# pattern as the end of the substitution and fails at parse time — the hook then
# exits 2, and on UserPromptSubmit exit 2 erases what the user typed. Two
# string comparisons, no fork, no parser edge.
_REMEMBER_STAMP="${REMEMBER_PROMPT_STAMP:-full}"
if [ "$_REMEMBER_STAMP" != "stable" ] && [ "$_REMEMBER_STAMP" != "off" ]; then
  _REMEMBER_STAMP="full"
fi

if [ "$_REMEMBER_STAMP" != "off" ]; then
CTX_PCT=""
CTX_PCT_FILE="${SYS_TMPDIR:-/tmp}/claude-ctx-pct"
if [ -f "$CTX_PCT_FILE" ]; then
  # `read`, not `cat`: this file exists on every prompt for anyone running the
  # status line, so the process was not the rare path it looks like (#227).
  # Status untested on purpose: `read` returns 1 on a file with no trailing
  # newline, having set the variable anyway — and a status line writing "45"
  # without one is the likely case, not the exotic one. CTX_PCT is already ""
  # if nothing was read.
  read -r CTX_PCT < "$CTX_PCT_FILE" 2>/dev/null
fi
# whoami was a process for a value the shell already has. They differ only where
# the effective user is not the login user (`su` without `-`), which is not a
# shape a Claude Code hook runs in — and whoami is still the fallback when the
# environment carries neither name.
_REMEMBER_WHO="${USER:-${USERNAME:-}}"
[ -n "$_REMEMBER_WHO" ] || _REMEMBER_WHO=$(whoami 2>/dev/null)
if [ "$_REMEMBER_STAMP" = "stable" ]; then
  # The username is the one field here that does not change between turns, and
  # it is the field the reporter asked to keep. No clock is read at all, which
  # on bash 3.2 (stock macOS, no printf '%(...)T') also removes this path's last
  # process.
  echo "[$_REMEMBER_WHO]"
elif [ -n "$CTX_PCT" ]; then
  # _remember_date_into (#511), not `$(_remember_date ...)`: the latter forks
  # a subshell for the substitution itself even when _remember_date's own
  # builtin path forks nothing -- see lib-clock.sh for the full reasoning.
  _remember_date_into _REMEMBER_NOW '+%H:%M %Z'
  echo "[$_REMEMBER_NOW -- $_REMEMBER_WHO -- ${CTX_PCT}%]"
else
  _remember_date_into _REMEMBER_NOW '+%H:%M %Z'
  echo "[$_REMEMBER_NOW -- $_REMEMBER_WHO]"
fi
# Kept under `stable`, deliberately: it is gated on a threshold, so it changes
# bytes only when it changes behaviour — and it is the only line here anybody
# acts on. `off` is the mode that suppresses it, because a user who asked for
# silence and got a surprise line at 95% is a user back to filtering our stdout.
if [ -n "$CTX_PCT" ] && [ "$CTX_PCT" -ge 95 ] 2>/dev/null; then
  echo "WARNING: Context at ${CTX_PCT}%. Run /remember to save session state before context death."
fi
fi

# ── Dispatch: after_user_prompt ─────────────────────────────────────────
dispatch "after_user_prompt"
)

# detect-tools.sh is deliberately NOT sourced here — it hard-exits when python
# is missing, and this hook must never block a prompt. jq is resolved directly.
JQ_BIN="${JQ:-jq}"
if [ "$_REMEMBER_HOST_JSON_STDOUT" = "1" ]; then
    # --- Non-Claude-Code host (#451) ---
    # See the comment at the top of this file. Whether or not there is a
    # notice, this host's stdout must never open with the bare stamp -- so
    # both are folded into the one JSON envelope Codex's own schema names,
    # never printed raw the way the Claude Code branch below does.
    if [ -z "$CTX" ] && [ -z "$NOTICE_MSG" ]; then
        : # nothing to say -- printing nothing is Completed on every host
    else
        _JSON=""
        if command -v "$JQ_BIN" >/dev/null 2>&1; then
            _JSON=$("$JQ_BIN" -n --arg ctx "$CTX" --arg msg "$NOTICE_MSG" \
                '(if $ctx != "" then {hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}} else {} end)
                 + (if $msg != "" then {systemMessage:$msg} else {} end)' 2>/dev/null) || _JSON=""
        fi
        if [ -n "$_JSON" ]; then
            printf '%s\n' "$_JSON"
        else
            # jq missing, or present but failed: stay silent on stdout
            # rather than print the bracketed stamp raw -- that raw print
            # is the exact defect this branch exists to avoid, and a bare
            # `[`/`{` on this host reads as Failed regardless of WHY it is
            # there. Silent on stdout is not silent everywhere, though:
            # `jq` is already a hard dependency of this hook's slow path
            # (bootstrap-dirs.sh, log.sh's own config reads), so its
            # absence here is a symptom worth a diagnostic line -- logged
            # only when `log()` is actually defined (the fast path never
            # sources log.sh, so it never gets one; that path also never
            # dispatches `after_user_prompt` for the same #227 cost reason,
            # and losing this one diagnostic line there is the same trade).
            declare -F log >/dev/null 2>&1 && log "hook" \
                "user-prompt-hook: jq unavailable/failed on a non-Claude-Code host -- dropped this turn's Codex-safe stdout envelope (stamp/notice lost, not printed raw to avoid the #451 collision)"
        fi
    fi
elif [ -z "$NOTICE_MSG" ]; then
    printf '%s\n' "$CTX"
else
    # jq's status must not become this hook's status. Left as the last command
    # of the script, a jq usage error (exit 2) is read by Claude Code as
    # "block this prompt" — for UserPromptSubmit, exit 2 BLOCKS AND ERASES
    # what the user typed. A cosmetic notice must never be able to do that, so
    # the JSON is built first and only printed if it was actually produced.
    _JSON=""
    if command -v "$JQ_BIN" >/dev/null 2>&1; then
        _JSON=$(printf '%s\n' "$CTX" | "$JQ_BIN" -Rs --arg msg "$NOTICE_MSG" \
            '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:.},systemMessage:$msg}' 2>/dev/null) \
            || _JSON=""
    fi
    if [ -n "$_JSON" ]; then
        printf '%s\n' "$_JSON"
    else
        # No jq, or jq failed: a plain-text reply cannot carry systemMessage,
        # so the notice goes into context instead. The model sees it and can
        # relay it — worse than the terminal line, better than swallowing the
        # one thing worth saying, and far better than eating the prompt.
        printf '%s\n' "$CTX"
        printf 'remember: %s\n' "$NOTICE_MSG"
    fi
fi

# Never inherit a failure status from anything above: this hook is documented
# to always exit 0, and on UserPromptSubmit a non-zero exit is not cosmetic.
exit 0
