#!/bin/bash
# ============================================================================
# log.sh — Shared logging and utility functions for memory pipeline scripts
# ============================================================================
#
# DESCRIPTION
#   Provides timestamped logging, token usage tracking, safe shell evaluation,
#   config reading, and log rotation. Sourced by every other script in the
#   memory pipeline — never executed directly.
#
# USAGE
#   source "$(dirname "$0")/log.sh"
#   log "save" "5 exchanges extracted"
#   log_tokens "save" 1247 342
#   config ".cooldowns.save_seconds" 120
#
# ENVIRONMENT
#   PROJECT_DIR   Project root — must be set before sourcing
#   PIPELINE_DIR  Plugin root  — must be set before sourcing
#   REMEMBER_DIR  Memory data dir — set by lib-memory-dir.sh (sourced here)
#
# OUTPUT
#   $REMEMBER_DIR/logs/memory-YYYY-MM-DD.log
#   Format: HH:MM:SS [component] message
#
# DEPENDENCIES
#   jq (optional, for config reading)
#   date, find, tar (for log rotation)
#
# FUNCTIONS
#   log             Log a timestamped message
#   log_tokens      Log token usage with optional cost
#   safe_eval       Evaluate only valid shell variable assignments from stdin
#   config          Read a value from config.json with jq, with fallback default
#   rotate_logs     Archive log files older than 7 days into monthly tarballs
#
# ============================================================================

# Ensure PIPELINE_DIR is set. Should be set by resolve-paths.sh before
# sourcing this file. Falls back to local-install convention if unset.
PIPELINE_DIR="${PIPELINE_DIR:-${PROJECT_DIR:-.}/.claude/remember}"

# Resolve REMEMBER_DIR and the merged REMEMBER_CONFIG (lib-memory-dir.sh is a
# no-op if already loaded via the _LIB_MEMORY_DIR_LOADED guard).
_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
# A path with no slash in it (`source log.sh` from the scripts dir) leaves the
# filename behind, not a directory — `dirname` answered "." and this must too.
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="."
source "$_REMEMBER_SRC_DIR/lib-memory-dir.sh"
unset _REMEMBER_SRC_DIR

# ── Logging setup ─────────────────────────────────────────────────────────────

REMEMBER_LOG_DIR="${REMEMBER_DIR}/logs"
# `[ -d ]` first (#230): bootstrap-dirs.sh has almost always just created this,
# and re-asking `mkdir` costs a process per hook invocation to learn nothing. The
# mkdir — and its FATAL — is still exactly what runs when the directory is not
# there, which is the only case it was ever about.
if [ ! -d "$REMEMBER_LOG_DIR" ] && ! mkdir -p "$REMEMBER_LOG_DIR" 2>/dev/null; then
    echo "FATAL: cannot create $REMEMBER_LOG_DIR" >&2
    return 1 2>/dev/null || true
fi

# ── One-pass config reading (#232) ────────────────────────────────────────────
#
# config() spent one `jq` process per key, against a merged config file that
# does not change for the life of the process. #230 measured post-tool-hook.sh
# at 20 external spawns per tool call and named these reads as the largest
# remaining block: three while log.sh is being sourced (.timezone, .model,
# .reject_pattern), two more in the hook (.cooldowns.save_seconds,
# .thresholds.delta_lines_trigger), and a dozen in save-session.sh.
#
# So the merged config is flattened ONCE into ordinary shell variables —
# `_RCFG_cooldowns_save_seconds=120` — and every later config() call is a
# parameter expansion. Nothing is written to disk. That is the whole reason
# this was preferred over caching the merged file at a stable path: that file
# can carry `haiku.oauth_token`, a live OAuth credential, which is why
# lib-memory-dir.sh creates it 0600 per PID under an EXIT trap (#68). Collapsing
# reads must not re-introduce that trade by the back door.
#
# The load happens ONCE, at source time, from log.sh's own body — not lazily
# from inside config(). It has to: every caller writes `X=$(config ...)`, and a
# command substitution is a subshell, so a table built inside config() dies with
# the call that built it and the next key pays for it all over again. Source
# time is not a change of moment either way: log.sh already reads .timezone,
# .model and .reject_pattern while being sourced, so a broken config.json is
# discovered exactly where it was before.
#
# THREE states, and the third is the point:
#
#   ""         not loaded yet.
#   ok         the table is authoritative. A key absent from it is genuinely
#              absent from the config, and the caller's default is the answer.
#   fallback   the one-pass read DID NOT HAPPEN. Never answer from the table in
#              this state — fall through to the per-key reads below, which are
#              the pre-#232 code path verbatim. "the file does not mention this
#              key" and "the file was never read" produce the same value and
#              must not become the same event; the second one is reported.
_REMEMBER_CFG_STATE=""
_REMEMBER_CFG_LOADED_FROM=""

# `.haiku.*` is deliberately NOT flattened. Reading every key up front means
# reading the OAuth token up front, and it would then sit in a shell variable
# in every process that sources log.sh — including one that runs other people's
# scripts via dispatch(). Nothing needs it there: pipeline/haiku.py reads the
# token from the merged file in Python, and no config() caller asks for it.
# This rule and the `select(.[0] != "haiku")` in the flattener are one decision
# in two places — change both or neither.
_config_is_private_key() {
    case "$1" in
        .haiku|.haiku.*) return 0 ;;
    esac
    return 1
}

# Flatten every scalar to `dotted.key<TAB>value`, or decline to.
#
# It REFUSES rather than guesses, in three cases, because each one is a way for
# a flattened table to answer a question wrongly and silently:
#   - a key containing anything but [A-Za-z0-9_], which could not survive the
#     mapping to a shell variable name;
#   - two distinct keys that collapse to the same variable name (`a.b` and
#     `a_b`), where the table would hand one key's value to the other;
#   - a value containing a tab or a newline, which the line protocol below
#     would truncate.
# A refusal prints a `#refuse <reason>` sentinel and exits 0, which lands in the
# `fallback` state — per-key reads, exactly as before. Slower and correct beats
# faster and wrong.
#
# The sentinel rather than `error()` because jq exits 5 for BOTH `error()` and a
# file that does not parse, so the exit code cannot tell "this config is fine
# and I am declining to flatten it" from "this config is broken". Those are not
# the same event and only the second one is worth waking anybody up for. No
# emitted line can begin with `#`: keys are [A-Za-z0-9_.] by the time they are
# printed.
#
# Paths through arrays are skipped, not refused: config() only accepts dotted
# keys, so it could never name one.
#
# NOT `paths(scalars)`. jq's `paths(f)` keeps a path when f's OUTPUT is truthy,
# so `paths(scalars)` silently drops every `false` in the file — the #159 bug
# exactly, arriving inside its own fix. Ask for the type instead.
#
# Kept on one line: the PATH-shim spawn counters in tests/ log a command with
# its arguments, one line per execution, and a multi-line jq program turns one
# spawn into eighty lines of "spawns".
_REMEMBER_CFG_FLATTEN_JQ='. as $doc | [paths(type != "object" and type != "array") | select(all(.[]; type == "string")) | select(.[0] != "haiku")] as $ks | if (($ks | flatten) | any(test("^[A-Za-z0-9_]+$") | not)) then "#refuse a config key is outside [A-Za-z0-9_]" elif (($ks | map(join("_")) | unique | length) != ($ks | length)) then "#refuse two config keys flatten to the same name" elif ([$ks[] as $p | $doc | getpath($p) | select(type == "string" and test("[\t\n]"))] | length) > 0 then "#refuse a config value contains a tab or a newline" else $ks[] as $p | ($doc | getpath($p)) as $v | select($v != null) | ($p | join(".")) + "\t" + ($v | tostring) end'

# The same contract without jq, for the machines test_jq_free_config.py exists
# for. Same refusals, same skips, and jq's textual form for non-strings —
# "true"/"false", never Python's "True"/"False" (the #159 near miss).
_REMEMBER_CFG_FLATTEN_PY='
import json, re, sys

def walk(node, prefix, out):
    if isinstance(node, dict):
        for k, v in node.items():
            walk(v, prefix + [k], out)
    elif isinstance(node, list):
        return
    else:
        out.append((prefix, node))

try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)

rows = []
walk(doc, [], rows)
rows = [(p, v) for p, v in rows if p and p[0] != "haiku" and v is not None]

ok = re.compile(r"^[A-Za-z0-9_]+$")
for p, v in rows:
    if not all(ok.match(part) for part in p):
        print("#refuse a config key is outside [A-Za-z0-9_]")
        sys.exit(0)
    if isinstance(v, str) and ("\t" in v or "\n" in v):
        print("#refuse a config value contains a tab or a newline")
        sys.exit(0)
slots = ["_".join(p) for p, _ in rows]
if len(set(slots)) != len(slots):
    print("#refuse two config keys flatten to the same name")
    sys.exit(0)

out = []
for p, v in rows:
    out.append(".".join(p) + "\t" + (v if isinstance(v, str) else json.dumps(v)))
sys.stdout.write("\n".join(out))
'

_config_load() {
    _REMEMBER_CFG_LOADED_FROM="${REMEMBER_CONFIG:-}"
    if [ ! -f "${REMEMBER_CONFIG:-}" ]; then
        # No merged config is not a failed read: every key is legitimately
        # absent and every caller's default is the right answer. Silent, and
        # correctly so — this is the ordinary state of a fresh install.
        _REMEMBER_CFG_STATE="ok"
        return 0
    fi

    local _dump="" _rc=0
    if command -v jq >/dev/null 2>&1; then
        _dump=$(jq -r "$_REMEMBER_CFG_FLATTEN_JQ" "$REMEMBER_CONFIG" 2>/dev/null) || _rc=1
    else
        _dump=$("${PYTHON:-python3}" -c "$_REMEMBER_CFG_FLATTEN_PY" "$REMEMBER_CONFIG" 2>/dev/null) || _rc=1
    fi

    if [ "$_rc" -ne 0 ]; then
        # The file could not be read at all. Say so, once. Before this, a
        # config.json that did not parse made every config() call return its
        # built-in default with no indication anywhere — a silent degraded read
        # in the hook that decides whether memory gets captured at all. The
        # VALUE is unchanged (the default is still the right answer); being
        # quiet about it was the defect.
        echo "remember: could not read ${REMEMBER_CONFIG} — is it valid JSON? falling back to per-key reads" >&2
        _REMEMBER_CFG_STATE="fallback"
        return 0
    fi

    case "$_dump" in
        '#refuse'*)
            # Not a problem, and deliberately not reported as one: the config
            # is fine, its shape is simply one the flattener declines rather
            # than risk answering wrongly. Per-key reads give the right answers
            # for it. A warning that fires on a valid config is a warning
            # nobody reads, so this one only shows up when debugging.
            [ "${REMEMBER_DEBUG:-}" = "1" ] && \
                echo "remember: ${_dump#'#refuse' } — reading config one key at a time" >&2
            _REMEMBER_CFG_STATE="fallback"
            return 0
            ;;
    esac

    local _k _v
    while IFS=$'\t' read -r _k _v; do
        [ -n "$_k" ] || continue
        printf -v "_RCFG_${_k//./_}" '%s' "$_v"
    done <<EOF
$_dump
EOF
    _REMEMBER_CFG_STATE="ok"
}

# Read .timezone from config BEFORE computing MEMORY_LOG_DATE — otherwise
# TZ="" falls back to UTC on macOS/BSD and produces next-day filenames after
# ~20:00 local in zones west of UTC.
config() {
    local key="$1"
    local default="$2"

    # Lazily, and again if a caller repointed REMEMBER_CONFIG at another file.
    if [ -z "$_REMEMBER_CFG_STATE" ] || \
       [ "$_REMEMBER_CFG_LOADED_FROM" != "${REMEMBER_CONFIG:-}" ]; then
        _config_load
    fi

    if [ "$_REMEMBER_CFG_STATE" = "ok" ] && ! _config_is_private_key "$key"; then
        case "$key" in
            ""|.|.*[!A-Za-z0-9_.]*)
                # Not a plain dotted key — the table cannot name it. Fall
                # through to the per-key reader, which speaks jq.
                ;;
            .?*)
                local _slot="_RCFG_${key#.}"
                _slot="${_slot//./_}"
                local _hit="${!_slot:-}"
                [ -n "$_hit" ] && echo "$_hit" || echo "$default"
                return
                ;;
        esac
    fi

    if [ ! -f "${REMEMBER_CONFIG:-}" ]; then
        echo "$default"
        return
    fi
    local val=""
    if command -v jq >/dev/null 2>&1; then
        # NOT `$key // empty`: jq's // treats false the same as null, so every
        # boolean option set to false read back as its default and could never
        # be switched off (#159). features.ndc_compression and features.recovery
        # are both documented, both default true, and neither could be disabled.
        # Ask for the value and treat only null — a genuinely absent key — as
        # missing.
        # Asking jq to map a genuinely absent key to "" and everything else to
        # its string form. Two near misses this needs to avoid:
        #   `$key // empty` treats FALSE like null, so no boolean set to false
        #   could ever be read (#159);
        #   testing the printed value against "null" cannot tell JSON null from
        #   the string "null" — `jq -r` prints both as the same bare word.
        val=$(jq -r "if $key == null then \"\" else ($key | tostring) end" \
            "$REMEMBER_CONFIG" 2>/dev/null)
    elif type _jq_fallback >/dev/null 2>&1; then
        # No jq — detect-tools.sh already defined a Python-based fallback for
        # exactly this (bare-key `jq -r '.key' file` reads). config() branched
        # on `command -v jq` and fell straight to `echo "$default"` without
        # ever trying it, so every user config override was silently ignored
        # on any jq-less machine. Wire it in here, matching #159's null-vs-
        # false semantics: an absent/null key falls through to `default`
        # below; a present `false` prints as the string "false" (see
        # detect-tools.sh's isinstance(val, str) fix for why that's not
        # Python's "False").
        val=$(_jq_fallback -r "$key" "$REMEMBER_CONFIG" 2>/dev/null)
    else
        # log.sh can be sourced directly without detect-tools.sh (some
        # callers/tests do), so _jq_fallback may not exist. Same read,
        # inlined, so config() never regresses to bundled-default-only just
        # because of sourcing order. Same null-vs-false semantics as above:
        # a genuinely absent/null key leaves $val empty (falls to $default
        # below); a present `false` renders as jq's "false", not Python's
        # str(False).
        val=$("${PYTHON:-python3}" -c '
import json, sys
try:
    data = json.load(open(sys.argv[2]))
    keys = sys.argv[1].strip(".").split(".")
    v = data
    for k in keys:
        if k and isinstance(v, dict):
            v = v.get(k)
        if v is None:
            break
    if v is not None:
        # jq -r semantics: raw strings, JSON textual form otherwise
        # (crucially "true"/"false", not Python str(True)/str(False)).
        print(v if isinstance(v, str) else json.dumps(v))
except Exception:
    pass
' "$key" "$REMEMBER_CONFIG" 2>/dev/null)
    fi
    [ -n "$val" ] && echo "$val" || echo "$default"
}

# Build the table now, in THIS shell, so every `$(config ...)` subshell
# inherits it. See the note above for why this cannot live inside config().
_config_load

# Is verbose logging on? `debug` was documented in the README and shipped in
# config.example.json but passed to config() NOWHERE, so setting it did nothing
# (#176) — the same class as #159, where documented booleans could not be
# switched off. The real switch was the REMEMBER_DEBUG env var, which a user
# configuring the plugin through config.json has no obvious way to set.
#
# Precedence: the env var wins, then `debug` in config, then the caller's own
# default. That last part matters: save-session.sh was verbose unless told
# otherwise and 50-git-backup.sh was quiet unless told otherwise, and the README
# documented only the first. Wiring one shared default would have silently
# changed one of them for every existing install, so each keeps its own and the
# option now overrides both — which is what setting it was supposed to do.
#
# Usage: debug_enabled <default 0|1> && log ...
debug_enabled() {
    local _default="${1:-0}"
    if [ -n "${REMEMBER_DEBUG:-}" ]; then
        [ "$REMEMBER_DEBUG" = "1" ]
        return
    fi
    case "$(config '.debug' '')" in
        true) return 0 ;;
        false) return 1 ;;
    esac
    [ "$_default" = "1" ]
}

REMEMBER_TZ=$(config ".timezone" "")
export REMEMBER_TZ

# What user-prompt-hook.sh is allowed to inject (#301). Read here rather than in
# the hook because the hook's whole design is that it does NOT resolve config —
# it replays an answer someone else already paid for (see lib-env-cache.sh).
# `config` is table-backed by the time this line runs, so this is a lookup, not
# a process: the same read REMEMBER_TZ above gets.
#   full   — the line that has always shipped: [HH:MM TZ — user — 45%]
#   stable — [user] only, plus the >=95 warning; no per-turn-volatile bytes
#   off    — nothing at all, warning included
# An unrecognised value is `full`: a typo must not silently delete the clock.
REMEMBER_PROMPT_STAMP=$(config ".prompt_stamp" "full")
case "$REMEMBER_PROMPT_STAMP" in
    stable|off) ;;
    *) REMEMBER_PROMPT_STAMP="full" ;;
esac
export REMEMBER_PROMPT_STAMP

# The two numbers post-tool-hook.sh needs on every tool call (#350). Read here,
# for the same reason REMEMBER_PROMPT_STAMP is: that hook now replays a
# resolution rather than performing one, and `config()` is the one thing it
# could not replay — which is exactly why #227 skipped it.
#
# What is cached is these two SCALARS, never the merged config file. That file
# can carry `haiku.oauth_token`, which is why lib-memory-dir.sh creates it 0600
# per PID under an EXIT trap (#68/#232); publishing it at a stable path to save
# processes is a trade this repo has already declined once and is not making by
# the back door. A save cooldown and a line threshold are neither secret nor
# expensive to be wrong about for one prompt.
#
# Both are table-backed by the time these lines run, so they are parameter
# expansions and not two more processes.
#
# Validated HERE rather than at each reader. Both end up inside `$(( ))` and
# `[ -lt ]`, and this repo has taken the same lesson twice: garbage in
# arithmetic under `set -u` does not misbehave, it kills the shell (#258), and
# a leading zero that clears a digits-only guard is read as octal (#322/#332).
# One validation at the source beats one per consumer, which is how the
# pre-#158 duplicate readers drifted.
REMEMBER_SAVE_COOLDOWN=$(config ".cooldowns.save_seconds" 120)
case "$REMEMBER_SAVE_COOLDOWN" in ''|*[!0-9]*) REMEMBER_SAVE_COOLDOWN=120 ;; esac
export REMEMBER_SAVE_COOLDOWN

REMEMBER_DELTA_THRESHOLD=$(config ".thresholds.delta_lines_trigger" 50)
case "$REMEMBER_DELTA_THRESHOLD" in ''|*[!0-9]*) REMEMBER_DELTA_THRESHOLD=50 ;; esac
export REMEMBER_DELTA_THRESHOLD

# Model + reject-gate knobs. config.json is the source of truth; an explicit
# shell env var still wins (override) via ${VAR:=...}, then config, then the
# built-in default. Exported here (log.sh is sourced by every script) so both
# the summarize and consolidate model calls in pipeline/haiku.py see them.
: "${REMEMBER_MODEL:=$(config ".model" "haiku")}"
export REMEMBER_MODEL
: "${REMEMBER_REJECT_PATTERN:=$(config ".reject_pattern" "")}"
export REMEMBER_REJECT_PATTERN

# Resolve "today" / "now" using REMEMBER_TZ when set, else system local.
# Crucially, an empty REMEMBER_TZ must NOT produce `TZ="" date` — that's UTC.
#
# _remember_date lives in lib-clock.sh so that user-prompt-hook.sh — which needs
# the time and nothing else log.sh provides — can have it without this chain
# (#227). Sourced AFTER REMEMBER_TZ is read above, and from here rather than the
# top of the file, so a log.sh that bailed early still leaves _remember_date
# undefined and session-start-hook.sh's `command -v` guard still fires.
_REMEMBER_SRC_DIR="${BASH_SOURCE[0]%/*}"
# A path with no slash in it (`source log.sh` from the scripts dir) leaves the
# filename behind, not a directory — `dirname` answered "." and this must too.
[ "$_REMEMBER_SRC_DIR" = "${BASH_SOURCE[0]}" ] && _REMEMBER_SRC_DIR="."
source "$_REMEMBER_SRC_DIR/lib-clock.sh"
unset _REMEMBER_SRC_DIR

MEMORY_LOG_DATE=$(_remember_date +%Y-%m-%d)
MEMORY_LOG_FILE="${REMEMBER_LOG_DIR}/memory-${MEMORY_LOG_DATE}.log"

# Log a timestamped message to the daily pipeline log file.
#
# Args:
#   $1 — component name (e.g., "save", "consolidate", "team")
#   $2 — message text
#
# Output:
#   Appends "HH:MM:SS [component] message" to daily log file.
#   Falls back to stderr if log file is unwritable.
log() {
    local component="$1"
    local message="$2"
    local timestamp
    timestamp=$(_remember_date +%H:%M:%S)
    echo "${timestamp} [${component}] ${message}" >> "$MEMORY_LOG_FILE" 2>/dev/null \
        || echo "${timestamp} [${component}] ${message}" >&2
}

# Log token usage for a Haiku API call.
#
# Args:
#   $1 — component name (e.g., "save", "ndc", "team")
#   $2 — input token count (default: 0)
#   $3 — output token count (default: 0)
#   $4 — cache read token count (optional, default: 0)
#   $5 — cost in USD (optional, appended if provided)
#
# Output:
#   Logs "tokens: {in}+{cache}cache->{out}out ($cost)" via log()
log_tokens() {
    local component="$1"
    local input="${2:-0}"
    local output="${3:-0}"
    local cache="${4:-0}"
    local cost="${5:-}"
    local msg="tokens: ${input}+${cache}cache→${output}out"
    [ -n "$cost" ] && msg="${msg} (\$${cost})"
    log "$component" "$msg"
}

# Safely evaluate shell variable assignments from stdin.
#
# Reads lines from stdin and only eval's lines matching the pattern
# UPPER_CASE_VAR=... — rejects everything else (Python warnings,
# tracebacks, debug prints, or injected commands).
#
# Args:
#   (none — reads from stdin)
#
# Usage:
#   safe_eval <<< "$(python3 -m pipeline.shell extract ...)"
safe_eval() {
    while IFS= read -r line; do
        # Strip trailing CR — Python on Windows emits \r\n, which corrupts
        # numeric values and trips integer tests downstream (issue #84).
        line="${line%$'\r'}"
        if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]]; then
            local _key="${BASH_REMATCH[1]}"
            local _val="${BASH_REMATCH[2]}"
            printf -v "$_key" '%s' "$_val"
        fi
    done
}

# Dispatch a lifecycle event to all registered hooks.
#
# Runs every executable in hooks.d/<event>/, passing the project path
# as REMEMBER_PROJECT. Hooks run sequentially, failures are logged
# but don't stop the pipeline.
#
# Args:
#   $1 — event name (e.g., "after_save", "before_consolidate")
#
# Usage:
#   dispatch "after_save"
REMEMBER_HOOKS_DIR="$PIPELINE_DIR/hooks.d"

# How much of a failing hook's stderr reaches the report (#277).
#
# The bound exists because this loop runs on every tool call and a chatty hook
# would otherwise write its whole output into the log each time. It is a bound
# on the REPORT, not on the hook: the capture is a plain file, the hook writes
# to it freely and is never sent a SIGPIPE by a reader that stopped listening —
# a `head` in the pipeline would have changed the exit status of the very thing
# being diagnosed.
#
# Five lines is where a shell diagnostic lives. `unbound variable`,
# `command not found`, `Argument list too long` and a `set -x` trace's last
# frames are all in the first few; nothing past them changed the diagnosis in
# the #258 and #266 transcripts.
#
# WHAT IS DROPPED IS COUNTED AND SAID. A cap that shortens silently is another
# instance of the defect this fixes, one layer down — the reader would have no
# way to tell a hook that said three things from a hook that said three hundred.
_DISPATCH_STDERR_LINES=5
_DISPATCH_STDERR_LINE_CHARS=400

# How a hook's STDOUT reaches the MODEL (#280).
#
# dispatch()'s stdout is the caller's stdout, and two callers hand that straight
# to Claude Code as context: `user-prompt-hook.sh` wraps the whole dispatch in
# `CTX=$( … )` and delivers it as `additionalContext`, and session-start-hook.sh
# prints it into the session's opening context (that is what the documented
# `=== TEAM ===` listener is for). So a hook's stdout is not diagnostics — it is
# text the model reads, in the same stream and the same position as the
# plugin's own. Unlabelled, the two are the same thing to the reader.
#
# THE CONTRACT, and it is the whole fix: an UNPREFIXED line in dispatched
# output is the PLUGIN speaking, and a hook cannot produce one. Every line a
# hook writes is prefixed with $_DISPATCH_STDOUT_PREFIX, so there is no
# "outside the fence" to escape into — a hook that prints a closing frame, or
# the plugin's own context warning, gets that prefixed too.
#
# The alternative of discarding hook stdout was rejected: `after_user_prompt`
# and `after_session_start` exist precisely so a hook can contribute context,
# and a fix that silently deletes what a hook says is this codebase's own
# defect wearing the fix's clothes.
#
# 200 lines is far above any honest contributor (the shipped git-restore and
# git-backup hooks print nothing at all) and far below a `set -x` trace or a
# runaway loop. WHAT IS DROPPED IS COUNTED AND SAID — same reason as the
# stderr bound above: a cap that shortens silently leaves the reader unable to
# tell a hook that said three things from one that said three hundred.
_DISPATCH_STDOUT_LINES=200
_DISPATCH_STDOUT_LINE_CHARS=2000
_DISPATCH_STDOUT_PREFIX="[hook] "
_DISPATCH_FRAME="=== hooks.d: "

# How long a dispatched hook may take before it is stopped (#286).
#
# Until this existed, nothing bounded a listener at all. dispatch() runs them
# sequentially, in the foreground, so one that blocks — a network call with no
# timeout, a `read` on a pipe nobody writes to, a lock wait — stalled the agent
# for as long as it blocked and LOGGED NOTHING, because nothing had failed.
# That is the house defect in its most literal form: a hook that never returns
# never reaches the point where it could say anything, so the absence of a log
# line reads as "nothing happened" when what happened is that everything
# stopped.
#
# TWO numbers, because two kinds of caller wait on this and only one of them is
# a person:
#
#   after_post_tool, after_user_prompt, before/after_session_start
#       dispatched from inside a Claude Code hook process. A human has pressed
#       enter, or the agent's own loop is blocked on the tool call. Claude Code
#       itself kills a hook at 60s by default, so anything at or above that is
#       not a budget — it is the host killing the whole process with no report
#       from us at all. 15s sits well under it, so the stop is OURS and comes
#       with a line naming the hook.
#
#   before/after_save, before/after_consolidate
#       dispatched from save-session.sh and run-consolidation.sh, which
#       post-tool-hook.sh starts with `nohup … &`. Nobody is waiting. A backup
#       listener doing real work there is doing it on its own time, and killing
#       it at 15s would be inventing a deadline no one is keeping.
#
# MEASURED, not guessed — the argument claude-supertool#702/#658 settled twice.
# The shipped listeners' FOREGROUND time, which is the only time dispatch()
# waits on, on a 2019 Intel macOS box:
#
#   after_save/50-git-backup.sh        0.46 – 0.58 s
#   before_session_start/50-git-restore.sh   0.17 – 0.22 s
#   a bare hook that only sources this file  0.12 – 0.17 s
#
# The 0.58s is with the push remote pointed at a blackholed address, i.e. with
# the network hung: both hooks put every byte of git I/O inside a disowned
# subshell, so a slow remote costs the foreground nothing. Headroom is therefore
# ~25x on the interactive budget and ~200x on the detached one, and the number
# that would actually be tight — "a real `git push` over a slow network takes
# tens of seconds" — never touches this path at all.
#
# 0 disables the timeout, the same escape hatch every other bound in this
# codebase offers, and costs the healthy case nothing when it is on.
_DISPATCH_TIMEOUT_DEFAULT=15
_DISPATCH_TIMEOUT_DETACHED_DEFAULT=120

# Events dispatched from a process the agent is NOT waiting on. Anything not
# named here gets the interactive budget, which is the safe direction: a new
# event added without touching this list is bounded tightly rather than loosely.
_DISPATCH_DETACHED_EVENTS=" before_save after_save before_consolidate after_consolidate "

# Between TERM and KILL. The point is not politeness — both shipped hooks take a
# lock and, on the platforms without flock(1), release it from an EXIT trap.
# bash RUNS an EXIT trap when it dies of an untrapped SIGTERM and does NOT when
# it dies of SIGKILL, so this window is the difference between a lock released
# and a lock file left behind holding a dead PID. git is the same story from the
# other side: it removes its own index.lock on SIGTERM and cannot on SIGKILL.
#
# It is a courtesy and not a veto: a listener that traps TERM and keeps going is
# killed anyway, or it would reinstate the whole defect behind an opt-in.
_DISPATCH_KILL_GRACE_DEFAULT=5

# Render a captured stderr file as ONE log line, or say why it cannot.
#
# One line because `log()` is a line protocol and doctor.sh tails five lines of
# hook-errors.log: a hook's three-line death turned into three entries would
# push two other failures off the only report anybody reads.
#
# THREE outcomes, and the third is the point (#277):
#   text        what the hook actually said, bounded and disclosed.
#   no stderr   it exited without a word. A real, reportable fact.
#   (caller)    the capture never happened — reported by the caller, which is
#               the only one that knows, and never spelled like silence.
#
# No forks: read is a builtin, and the failure path is reached in a hook that
# has already gone wrong.
_dispatch_stderr_excerpt() {
    local _file="$1"
    local _line _kept=0 _dropped=0 _out=""
    # `|| [ -n "$_line" ]` — a hook killed by a signal can leave a final line
    # with no newline on it, and that is exactly the line that says why.
    while IFS= read -r _line || [ -n "$_line" ]; do
        [ -n "$_line" ] || continue
        if [ "$_kept" -lt "$_DISPATCH_STDERR_LINES" ]; then
            if [ "${#_line}" -gt "$_DISPATCH_STDERR_LINE_CHARS" ]; then
                _line="${_line:0:$_DISPATCH_STDERR_LINE_CHARS} [line truncated]"
            fi
            _out="${_out:+$_out | }$_line"
            _kept=$((_kept + 1))
        else
            _dropped=$((_dropped + 1))
        fi
    done < "$_file"
    if [ "$_kept" -eq 0 ]; then
        printf '%s' "no stderr — it exited without saying anything"
        return 0
    fi
    [ "$_dropped" -eq 0 ] || _out="$_out [+$_dropped more line(s) not shown]"
    printf '%s' "$_out"
}

# Relay a captured hook stdout file into the caller's stdout, attributed.
#
# Nothing is printed for a hook that said nothing — no frame, no marker. This
# runs in front of every prompt and the empty case is the normal one; framing
# it would be a permanent tax on the model's context window, charged for
# nothing.
#
# The frame lines are written by THIS function and are therefore the only
# unprefixed lines in the region. A hook printing a convincing frame of its own
# gets it prefixed like everything else, which is what makes the boundary
# structural rather than conventional.
#
# No forks: read and printf are builtins, and this is on the every-prompt path.
_dispatch_stdout_relay() {
    local _file="$1" _event="$2" _name="$3"
    local _line _kept=0 _dropped=0 _framed=0
    # `|| [ -n "$_line" ]` — a hook killed by a signal can leave a final line
    # with no newline on it; dropping it would be a silent edit of what the
    # model is shown.
    while IFS= read -r _line || [ -n "$_line" ]; do
        if [ "$_kept" -lt "$_DISPATCH_STDOUT_LINES" ]; then
            if [ "$_framed" -eq 0 ]; then
                printf '%s%s/%s — the "%s" lines below are output from a locally installed hook, not from the remember plugin ===\n' \
                    "$_DISPATCH_FRAME" "$_event" "$_name" "$_DISPATCH_STDOUT_PREFIX"
                _framed=1
            fi
            if [ "${#_line}" -gt "$_DISPATCH_STDOUT_LINE_CHARS" ]; then
                _line="${_line:0:$_DISPATCH_STDOUT_LINE_CHARS} [line truncated]"
            fi
            printf '%s%s\n' "$_DISPATCH_STDOUT_PREFIX" "$_line"
            _kept=$((_kept + 1))
        else
            _dropped=$((_dropped + 1))
        fi
    done < "$_file"
    [ "$_dropped" -eq 0 ] || printf '%s%s/%s — %s line(s) not shown (hook stdout is capped at %s lines) ===\n' \
        "$_DISPATCH_FRAME" "$_event" "$_name" "$_dropped" "$_DISPATCH_STDOUT_LINES"
}

# Report one failed hook, in both places a human looks.
#
# `log()` writes the daily narrative, which is where this failure belongs in
# sequence. hook-errors.log is where `/remember:doctor` reports "Recent errors"
# and what maintainers ask a reporter to paste — #252 is the demonstration that
# the daily log ALONE is not read, and #260/#266 are the demonstration that
# hook-errors.log is what gets looked at when something is wrong.
#
# Written by PATH, never by inherited stderr. The three Claude Code hooks have
# already pointed their own stderr at this file (bootstrap-dirs.sh), so simply
# dropping the `2>/dev/null` would land in the right place FOR THEM — and in the
# agent's own stream for save-session.sh, run-consolidation.sh, and any hook
# process whose bootstrap redirect was skipped on a read-only store. A hook must
# never gain the ability to write into the session, so the destination is named
# rather than inherited.
_dispatch_report_failure() {
    local _event="$1" _name="$2" _rc="$3" _why="$4"
    local _msg="ERROR: hook failed: $_event/$_name (exit $_rc): $_why"
    log "dispatch" "$_msg"
    [ -d "$REMEMBER_DIR/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [dispatch] $_msg" \
        >> "$REMEMBER_DIR/logs/hook-errors.log" 2>/dev/null || true
    return 0
}

# Report one hook that was REFUSED, in both places a human looks (#280).
#
# The ownership and world-writable guards below are the reason this is a blast
# radius rather than a vulnerability — but a refused hook is a hook that never
# ran and will never run until someone changes its mode or its owner, and until
# now that fact went only to the daily log. `/remember:doctor` reports "Recent
# errors" out of hook-errors.log, so it said OK for a store whose backup hook
# had been silently skipped since it was installed. That is #252's finding
# reached from another direction: the tool was not quiet, it was reassuring.
_dispatch_report_skip() {
    local _event="$1" _name="$2" _why="$3"
    local _msg="WARNING: hook SKIPPED and did not run: $_event/$_name ($_why) — it will not run on any later dispatch until this is fixed"
    log "dispatch" "$_msg"
    [ -d "$REMEMBER_DIR/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [dispatch] $_msg" \
        >> "$REMEMBER_DIR/logs/hook-errors.log" 2>/dev/null || true
    return 0
}

# Report one thing that went wrong, in both places a human looks (#326).
#
# The generalisation of the two functions above, for callers that are not
# dispatch. `log()` alone is not enough and #252 is the demonstration: the daily
# narrative is not read, and `/remember:doctor` reports "Recent errors" out of
# hook-errors.log, which is the file a reporter is asked to paste.
#
# Written by PATH rather than by inherited stderr, for the reason
# _dispatch_report_failure gives: save-session.sh's stderr is the agent's own
# stream, and a hook must never gain the ability to write into the session.
report_error() {
    log "$1" "$2"
    [ -d "$REMEMBER_DIR/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [$1] $2" \
        >> "$REMEMBER_DIR/logs/hook-errors.log" 2>/dev/null || true
    return 0
}

# Report one hook that was STOPPED because it never came back (#286).
#
# NOT _dispatch_report_failure, and the distinction is the three-state contract
# (0.12.0 CHANGELOG; `_push_and_report` in hooks.d/after_save/50-git-backup.sh):
#
#   a hook that exits non-zero has ANSWERED — it ran, it failed, and its exit
#   status and its own first lines are the answer.
#
#   a hook that was killed has answered NOTHING. Whether it did its work, did
#   half of it, or never started is not knowable from here.
#
# Reporting the second in the shape of the first — "hook failed (exit 143)" —
# invents a verdict the hook never gave, and 143 is not even the hook's exit
# status, it is the signal WE sent. `/remember:doctor` tails this file under
# "Recent errors", so the difference decides whether it shows a fault or an
# absence of information. It is loud either way: a listener that hangs is a real
# problem with a real installation even when its own verdict is unknown.
#
# The budget is stated in the line on purpose. Without it a reader cannot tell a
# hung hook from a budget set too tight for an honest one, and those want
# opposite fixes.
_dispatch_report_timeout() {
    local _event="$1" _name="$2" _budget="$3" _how="$4" _said="$5"
    local _msg="WARNING: hook TIMED OUT: $_event/$_name did not return within ${_budget}s and was stopped ($_how). This is NOT a failure report from the hook — it never answered, so whether it did its work is UNKNOWN, and anything it left half-done is its own to unwind. Raise hooks.dispatch_timeout_seconds if this listener is honestly slow, or 0 to disable the bound. It said: $_said"
    log "dispatch" "$_msg"
    [ -d "$REMEMBER_DIR/logs" ] || return 0
    printf '%s\n' "$(_remember_date +%H:%M:%S) [dispatch] $_msg" \
        >> "$REMEMBER_DIR/logs/hook-errors.log" 2>/dev/null || true
    return 0
}

# Run one already-started hook under a watchdog, and say what happened to it.
#
# Sets two globals rather than returning, because it has two answers: the hook's
# exit status, and whether that status is the hook's own or ours.
#   _DISPATCH_RC        the status `wait` reported
#   _DISPATCH_TIMEDOUT  1 if we stopped it, 0 if it stopped by itself
#
# THE HOOK'S OWN PID, NEVER ITS PROCESS GROUP. This is the decision #286 turns
# on. Both shipped listeners put every git write inside a disowned subshell that
# redirects its own stdio — 50-git-backup.sh does its whole add/commit/push
# there, 50-git-restore.sh its fetch — precisely so the network never blocks a
# save. A group kill would kill exactly that: a SIGTERM between `git add` and
# `git commit`, or mid-push, leaving a .git/index.lock in the very store this
# plugin exists to protect, on which the NEXT session's backup then fails with
# its owner long gone. That is trading a loud failure for a quiet one, which is
# the trade this codebase refuses everywhere else. So the kill lands on the
# process that is actually stalling dispatch and on nothing else.
#
# The cost of that choice, stated rather than hidden: a listener blocked in a
# FOREGROUND child (a `curl` with no timeout, say) leaves that child running
# when its parent dies. The stall is over — dispatch returns, the agent moves —
# but the process leaks until it finishes or the machine does. A leaked process
# is recoverable; a half-written git index in someone's memory store is not.
#
# NOT a poll of the hook from the caller. A `kill -0` loop on a one-second tick
# charges EVERY hook a second it did not spend, and after_post_tool dispatches
# on every single tool call. The caller `wait`s on the real pid, so a hook that
# returns in 40ms costs 40ms; the watchdog is a sibling that is cancelled the
# moment the hook is done. Its own loop ticks at one second, but only ever while
# a hook is genuinely still running.
#
# NOT timeout(1), which macOS does not ship — the same finding
# hooks.d/before_session_start/50-git-restore.sh made for its detached fetch,
# reached again here. One code path is one code path that gets tested.
_dispatch_supervise() {
    local _pid="$1" _budget="$2" _grace="$3" _sentinel="$4"
    local _wpid=""

    if [ "$_budget" -gt 0 ]; then
        (
            _w=0
            while [ "$_w" -lt "$_budget" ]; do
                kill -0 "$_pid" 2>/dev/null || exit 0
                sleep 1
                _w=$((_w + 1))
            done
            kill -0 "$_pid" 2>/dev/null || exit 0
            # Written BEFORE the signal. The caller distinguishes "we stopped
            # it" from "it exited 143 on its own" by this file, and a marker
            # written after the kill could lose the race with `wait`.
            [ -z "$_sentinel" ] || : > "$_sentinel" 2>/dev/null || true
            kill -TERM "$_pid" 2>/dev/null || true
            _g=0
            while [ "$_g" -lt "$_grace" ]; do
                kill -0 "$_pid" 2>/dev/null || exit 0
                sleep 1
                _g=$((_g + 1))
            done
            kill -KILL "$_pid" 2>/dev/null || true
        ) </dev/null >/dev/null 2>&1 &
        _wpid=$!
    fi

    # `if`, not a bare `wait`: every caller of dispatch runs under `set -e`, and
    # a hook that exits non-zero — or that we just signalled — would otherwise
    # abort the save. Same reason the invocation below is written this way.
    if wait "$_pid"; then _DISPATCH_RC=0; else _DISPATCH_RC=$?; fi

    if [ -n "$_wpid" ]; then
        # Cancelled the instant the hook is done. Its stdio is already pointed
        # at /dev/null, so it can neither hold open the command substitution
        # `user-prompt-hook.sh` wraps this dispatch in nor write into it.
        kill "$_wpid" 2>/dev/null || true
        wait "$_wpid" 2>/dev/null || true
    fi

    _DISPATCH_TIMEDOUT=0
    if [ -n "$_sentinel" ]; then
        if [ -f "$_sentinel" ]; then
            _DISPATCH_TIMEDOUT=1
            rm -f "$_sentinel" 2>/dev/null || true
        fi
    elif [ "$_budget" -gt 0 ]; then
        # No writable tmp, so the watchdog had nowhere to leave a marker and the
        # signal is all there is to go on. A hook may legitimately exit 143, so
        # this is an INFERENCE and the report says so rather than asserting it.
        case "$_DISPATCH_RC" in
            143|137) _DISPATCH_TIMEDOUT=1 ;;
        esac
    fi
    return 0
}

dispatch() {
    local event="$1"
    local event_dir="$REMEMBER_HOOKS_DIR/$event"
    [ -d "$event_dir" ] || return 0
    local current_uid=""
    # Both resolved on first use, like current_uid and for the same reason
    # (#230): the shipped distribution's hooks.d/<event>/ holds a .gitkeep and
    # nothing else, so the common case is a loop that finds nothing executable.
    # Nothing below may cost that case a process, a directory or a file.
    local _err_file="" _out_file="" _err_unavailable=""
    # The budget (#286), also on first use, and for the same reason. "" means
    # not yet read — 0 is a legal value meaning the bound is off.
    local _budget="" _grace="" _to_file=""
    for hook in "$event_dir"/*; do
        [ -x "$hook" ] || continue
        # Resolved on first use, not on entry (#230). The distribution ships
        # every hooks.d/<event>/ directory containing nothing but a .gitkeep, so
        # the `-d` test above passes and this loop finds nothing executable —
        # and `id` was forked on every tool call to compare against nobody.
        [ -n "$current_uid" ] || current_uid=$(id -u)
        if [ -z "$_budget" ]; then
            case "$_DISPATCH_DETACHED_EVENTS" in
                *" $event "*)
                    _budget=$(config '.hooks.dispatch_timeout_detached_seconds' "$_DISPATCH_TIMEOUT_DETACHED_DEFAULT")
                    _DISPATCH_BUDGET_FALLBACK=$_DISPATCH_TIMEOUT_DETACHED_DEFAULT ;;
                *)
                    _budget=$(config '.hooks.dispatch_timeout_seconds' "$_DISPATCH_TIMEOUT_DEFAULT")
                    _DISPATCH_BUDGET_FALLBACK=$_DISPATCH_TIMEOUT_DEFAULT ;;
            esac
            # Arithmetic on garbage must not decide whether a hook is killed —
            # and under `set -u` an unvalidated value inside $(( )) does not
            # merely misbehave, it kills the shell (the #258 lesson). Falling
            # back to the shipped default is the safe direction in both senses.
            case "$_budget" in
                ''|*[!0-9]*) _budget=$_DISPATCH_BUDGET_FALLBACK ;;
            esac
            _grace=$(config '.hooks.dispatch_kill_grace_seconds' "$_DISPATCH_KILL_GRACE_DEFAULT")
            case "$_grace" in
                ''|*[!0-9]*) _grace=$_DISPATCH_KILL_GRACE_DEFAULT ;;
            esac
        fi
        # Ownership check: skip hooks not owned by the current user.
        local hook_uid
        # Try GNU stat (-c) first, then BSD (-f). The reverse order silently
        # succeeds on Linux because `stat -f %u` there returns filesystem free
        # blocks, not file owner UID — and the OR fallback never fires.
        hook_uid=$(stat -c %u "$hook" 2>/dev/null || stat -f %u "$hook" 2>/dev/null || echo "")
        if [ -z "$hook_uid" ] || [ "$hook_uid" != "$current_uid" ]; then
            _dispatch_report_skip "$event" "${hook##*/}" "not owned by the current user"
            continue
        fi
        # World-writable check: skip hooks writable by others.
        if [ -n "$(find "$hook" -maxdepth 0 -perm -002 2>/dev/null)" ]; then
            _dispatch_report_skip "$event" "${hook##*/}" "world-writable"
            continue
        fi
        # The capture file, prepared once and only once a hook is about to run.
        # Overwritten per hook (`2>` truncates), removed when the loop ends.
        if [ -z "$_err_file" ] && [ -z "$_err_unavailable" ]; then
            _err_file="$REMEMBER_DIR/tmp/dispatch-stderr.$$"
            _out_file="$REMEMBER_DIR/tmp/dispatch-stdout.$$"
            # `|| true`, and it is load-bearing: `A || B` where BOTH fail is a
            # failed compound command, and every caller of dispatch runs under
            # `set -e`. Without it, a store whose tmp/ cannot be created aborts
            # the save outright — the loud failure traded for the quiet one.
            [ -d "$REMEMBER_DIR/tmp" ] || mkdir -p "$REMEMBER_DIR/tmp" 2>/dev/null || true
            # Opened HERE rather than discovered at the redirect. The shell
            # opens a redirection target before running the command and reports
            # its own failure OUTSIDE the scope of that redirect — the #204
            # lesson — so an unopenable `2>"$_err_file"` would both print to the
            # caller's stderr and count as the hook failing when it never ran.
            if ! : > "$_err_file" 2>/dev/null || ! : > "$_out_file" 2>/dev/null; then
                _err_file=""
                _out_file=""
                _err_unavailable="yes"
            else
                # Where the watchdog says it fired (#286). Not opened here — its
                # EXISTENCE is the signal, so it must be absent until then.
                _to_file="$REMEMBER_DIR/tmp/dispatch-timeout.$$"
                rm -f "$_to_file" 2>/dev/null || true
            fi
        fi

        # `if`, not a bare command: save-session.sh and run-consolidation.sh
        # both `set -e` around their dispatch calls, and the old `|| log` kept a
        # failing hook non-fatal by accident of syntax. A bare invocation whose
        # status is read afterwards would hand every third-party hook the power
        # to abort a save.
        # Backgrounded so it can be supervised (#286), never so it can outrun
        # the loop: _dispatch_supervise `wait`s on it before this iteration
        # ends, so hooks still run strictly one at a time and in name order.
        # The redirections belong to the background job, so a hook's output is
        # captured exactly as it was when this was a foreground call.
        local _rc=0 _hpid=""
        _DISPATCH_RC=0
        _DISPATCH_TIMEDOUT=0
        if [ -n "$_err_file" ]; then
            REMEMBER_PROJECT="${PROJECT_DIR:-.}" "$hook" >"$_out_file" 2>"$_err_file" &
            _hpid=$!
            _dispatch_supervise "$_hpid" "$_budget" "$_grace" "$_to_file"
            _rc=$_DISPATCH_RC
            # Relayed whether the hook succeeded, failed, or was stopped: a hook
            # that says something useful and then dies has still said it, and
            # #277 is the standing argument against discarding its words. A hook
            # that was killed mid-sentence is the case where they matter most —
            # they are the only evidence of what it was doing when it stopped.
            _dispatch_stdout_relay "$_out_file" "$event" "${hook##*/}"
        else
            # No writable tmp, so stdout cannot be captured — and uncaptured
            # stdout is inherited stdout, which is exactly the unattributed
            # injection this fixes. It is DISCARDED and SAID, never quietly
            # passed through and never quietly dropped: "could not check" is a
            # third state here as it is everywhere else in this codebase.
            REMEMBER_PROJECT="${PROJECT_DIR:-.}" "$hook" >/dev/null 2>/dev/null &
            _hpid=$!
            _dispatch_supervise "$_hpid" "$_budget" "$_grace" ""
            _rc=$_DISPATCH_RC
            printf '%s%s/%s — output NOT SHOWN: stdout could not be captured (no writable %s/tmp), so it was discarded rather than delivered unattributed ===\n' \
                "$_DISPATCH_FRAME" "$event" "${hook##*/}" "$REMEMBER_DIR"
        fi

        # A stop is reported BEFORE the failure branch and instead of it. The
        # status in $_rc is the signal WE sent, not an answer the hook gave.
        if [ "$_DISPATCH_TIMEDOUT" -eq 1 ]; then
            local _how="SIGTERM, then SIGKILL after ${_grace}s if it was still there"
            [ -n "$_to_file" ] || _how="$_how; inferred from the exit status because $REMEMBER_DIR/tmp is not writable, so a hook that genuinely exited on this signal would look the same"
            local _said
            if [ -n "$_err_file" ]; then
                _said=$(_dispatch_stderr_excerpt "$_err_file")
            else
                _said="nothing captured — no writable $REMEMBER_DIR/tmp"
            fi
            _dispatch_report_timeout "$event" "${hook##*/}" "$_budget" "$_how" "$_said"
            continue
        fi

        [ "$_rc" -eq 0 ] && continue

        # Only a FAILING hook is reported. A hook that chatters and exits 0 is
        # not an event, and this fires on every tool call — that noise is the
        # one thing `2>/dev/null` was genuinely buying, and it is kept.
        local _why
        if [ -n "$_err_file" ]; then
            _why=$(_dispatch_stderr_excerpt "$_err_file")
        else
            _why="stderr not captured — no writable $REMEMBER_DIR/tmp, so the reason is MISSING, not absent; rerun the hook by hand to see what it says"
        fi
        _dispatch_report_failure "$event" "${hook##*/}" "$_rc" "$_why"
    done
    [ -z "$_err_file" ] || rm -f "$_err_file" "$_out_file" 2>/dev/null
    [ -z "$_to_file" ] || rm -f "$_to_file" 2>/dev/null
    return 0
}

# Archive log files older than 7 days into monthly tar.gz bundles.
#
# Finds memory-*.log files with mtime > 7 days, compresses them into
# logs-YYYY-MM.tar.gz, and removes the originals once the archive is verified
# to contain them. No-op if no old logs exist.
#
# Args:
#   (none — operates on REMEMBER_LOG_DIR)
#
# Returns:
#   0  archived, or nothing had aged out
#   1  could not archive — the reason is in the log line and in .rotate-failed.
#      Callers running under `set -e` must guard the call (`rotate_logs || true`):
#      a log directory that cannot be tidied is not a reason to abort the work
#      that was about to happen.
#
# Side effects:
#   Creates logs-YYYY-MM.tar.gz — or logs-YYYY-MM-partN.tar.gz — in the log
#   directory. NEVER writes over an archive that is already there.
#   Deletes archived .log files, and only those the new archive lists back.
#   Writes/removes .rotate-failed (consecutive-failure breadcrumb for doctor.sh).
# Consecutive failures before the log line stops repeating itself and starts
# naming the consequence. #252's reporter watched ONE identical line a day for
# five weeks and only investigated when the directory grew to 2.3 MB — so
# "logs a line" is demonstrably not enough on its own, while a louder line on
# every invocation would just be a faster way to become wallpaper. Three
# consecutive failures is past transient (a full disk, a held lock) and into
# stuck.
_ROTATE_ESCALATE_AFTER=3

# Where a stuck rotation leaves its breadcrumb. Deliberately not a
# `memory-*.log`, so rotation never selects its own state file as something to
# archive. doctor.sh spells this path a second time rather than sourcing log.sh
# (it is a read-only report and must not run log.sh's source-time side effects)
# — the two are one decision in two places, so rename both or neither.
_ROTATE_STATE_NAME=".rotate-failed"

# THREE states, and the third is what #252 was really about:
#
#   0, silent   nothing aged out. Not an event, not worth a line.
#   0, logged   archived N logs.
#   1, logged   COULD NOT RUN. Distinct return value, tar's own diagnostic in
#               the line, and a breadcrumb doctor.sh reports. This used to
#               return 0 like the other two and discard the reason, so
#               "nothing to do" and "permanently broken" were the same event
#               to every caller and to whoever read the log.
#
# THE ARCHIVE NAME IS RELATIVE, DELIBERATELY. GNU tar parses an `-f` argument
# whose colon precedes the first slash as `host:path`, so the old absolute
# `${REMEMBER_LOG_DIR}/logs-YYYY-MM.tar.gz` became a request to connect to a
# machine called `C` on Windows ("Cannot connect to C: resolve failed", exit 2,
# GNU tar 1.35). Only `-f` is parsed that way — `-C` never was, which is why
# passing the directory separately did not save it.
#
# `--force-local` is the usual GNU answer and is NOT used here: bsdtar — which
# is `/usr/bin/tar` on macOS, the platform this is developed on — rejects it
# outright with exit 1 and writes no archive. Hardcoding it would have fixed
# Windows by disabling macOS, inside a branch whose stderr was discarded.
# Detecting the implementation was the other option and was rejected too: the
# axis is the tar binary, not the OS (Git Bash ships GNU tar, but Windows also
# has a bsdtar in System32 that can win the PATH), so a detector would be a
# second bug waiting to happen. So: `cd` into the directory and name the
# archive with no directory prefix. A name with no slash has nowhere to put a
# colon, no tar can read it as remote, and no flag is needed on any of them.
# Verified against GNU tar 1.35 and bsdtar 3.5.3 — identical members.
#
# THE ARCHIVE IS NEVER A NAME THAT ALREADY EXISTS, AND THE ORIGINALS ARE NEVER
# DELETED ON THE STRENGTH OF AN EXIT STATUS (#255). Both halves are one bug:
# `tar -czf` opens with O_TRUNC, this function deletes what it archived, and the
# name carried only a month — so the second rotation of a month replaced the
# first one's archive with its own contents, and the logs that had been inside
# it were deleted from disk when it was written. Nothing survived anywhere.
#
# The month can never be made exact enough to fix that, and it is worth being
# precise about why, because "just name it correctly" is the obvious answer.
# `-mtime +7` selects every log that has aged out since the last successful
# rotation — an unbounded window, so one archive legitimately spans months, and
# the label is anyway derived from `date -v-7d` (the month a week ago) rather
# than from the logs. But even a per-month grouping, which the filenames do
# support, collides: logs from the same June age out on different days, so a
# rotation in July and another a week later both want `logs-2026-06.tar.gz`.
# Month granularity is revisited by construction. The name has to be *claimed*,
# not computed.
#
# So: claim the first unused name, and on the second and later archive of a
# month add `-partN`. This scatters a month across several tarballs, which is
# the cost, and it is paid deliberately. The alternative that keeps one archive
# per month is extract-merge-recreate, and its worst moment is unacceptable
# here — a crash or a full disk midway through recreating leaves a truncated
# archive whose contents were deleted from disk weeks earlier. This design's
# worst moment is a crash between a verified archive and the deletion of its
# originals: the next rotation archives them a second time under a new name.
# Duplicated, never lost — the same trade the failure branch below already
# makes, where accumulation is preferred to deletion.
#
# The claim uses `set -C` in a subshell so it is atomic: the redirection fails
# if the file appeared between the test and the create, so two rotations racing
# cannot select the same name. The empty file it leaves behind is the one tar
# then overwrites — its own, by construction, which is why the failure paths
# below may remove it without asking what was in it.
_ROTATE_MAX_PARTS=100

# The names the new archive does not list back, as a readable string; empty
# means it lists all of them.
#
# tar exiting 0 is not evidence that a file is inside the archive — it is
# evidence that tar had no complaint, which is a different claim, and #255's
# deletion was gated on the second while meaning the first. Asking the archive
# what it contains is the only question whose answer justifies `rm`.
_rotate_missing_members() {
    local dir="$1" archive="$2"
    shift 2
    local listing member missing=""
    if ! listing=$( { cd "$dir" && tar -tzf "$archive"; } 2>/dev/null ); then
        printf '%s' "the archive cannot be read back"
        return 0
    fi
    for member in "$@"; do
        printf '%s\n' "$listing" | grep -Fqx -- "$member" \
            || missing="${missing}${missing:+, }${member}"
    done
    printf '%s' "$missing"
}

rotate_logs() {
    local state="${REMEMBER_LOG_DIR}/${_ROTATE_STATE_NAME}"

    local old_logs
    old_logs=$(find "$REMEMBER_LOG_DIR" -name "memory-*.log" -mtime +7 2>/dev/null)
    if [ -z "$old_logs" ]; then
        # Nothing aged out — including the case where a stuck rotation's
        # backlog was cleared by hand. The problem is over, so the breadcrumb
        # goes with it; a warning that outlives its cause is a false alarm, and
        # doctor.sh would otherwise report it forever.
        rm -f "$state" 2>/dev/null || true
        return 0
    fi

    local archive_month
    archive_month=$(date -v-7d +%Y-%m 2>/dev/null || date -d '7 days ago' +%Y-%m)
    local count
    count=$(echo "$old_logs" | wc -l | tr -d ' ')

    local basenames=()
    while IFS= read -r f; do
        basenames+=("$(basename "$f")")
    done <<< "$old_logs"

    # Claim a name nothing else holds. The claim is by creation, not by a test:
    # `[ -e ]` first only saves a fork on the common repeat, and the `set -C`
    # redirection is what actually decides.
    local archive_name="" candidate part=1
    while [ "$part" -le "$_ROTATE_MAX_PARTS" ]; do
        if [ "$part" -eq 1 ]; then
            candidate="logs-${archive_month}.tar.gz"
        else
            candidate="logs-${archive_month}-part${part}.tar.gz"
        fi
        if [ ! -e "${REMEMBER_LOG_DIR}/${candidate}" ] \
           && ( set -C; : > "${REMEMBER_LOG_DIR}/${candidate}" ) 2>/dev/null; then
            archive_name="$candidate"
            break
        fi
        part=$((part + 1))
    done

    # The whole group is captured, not just tar: a failing `cd` writes its own
    # diagnostic, and that is exactly the kind of reason this used to lose.
    local err=""
    if [ -z "$archive_name" ]; then
        err="no unused archive name for ${archive_month} after ${_ROTATE_MAX_PARTS} tries — the names are taken, or ${REMEMBER_LOG_DIR} is not writable. Refusing to overwrite an existing archive"
    elif err=$( { cd "$REMEMBER_LOG_DIR" && tar -czf "$archive_name" "${basenames[@]}"; } 2>&1 ); then
        local missing
        missing=$(_rotate_missing_members "$REMEMBER_LOG_DIR" "$archive_name" "${basenames[@]}")
        if [ -z "$missing" ]; then
            while IFS= read -r f; do rm -f "$f"; done <<< "$old_logs"
            rm -f "$state" 2>/dev/null || true
            log "rotate" "archived ${count} logs → ${archive_name}"
            return 0
        fi
        # The archive was claimed by this call and held nothing before it, so
        # removing it loses nothing — and leaving an incomplete archive next to
        # the originals it does not contain is how a later rotation, or a
        # person, comes to trust it.
        rm -f "${REMEMBER_LOG_DIR}/${archive_name}" 2>/dev/null || true
        err="tar exited 0 but ${archive_name} does not list back: ${missing}"
    else
        rm -f "${REMEMBER_LOG_DIR}/${archive_name}" 2>/dev/null || true
    fi

    # --- Could not run. ------------------------------------------------------
    # The originals are deliberately NOT deleted, and rotation deliberately does
    # NOT degrade to deleting or truncating them after N failures. These logs
    # are the only record of what went wrong, on a machine that has just proved
    # it cannot run the archive path; trading a slowly growing directory for
    # irreversible loss of the evidence is the wrong way round. Accumulation is
    # visible and recoverable; deletion is neither. Bound it by making it loud,
    # not by making it destructive.
    local first_line
    first_line=$(printf '%s\n' "$err" | head -1)
    [ -z "$first_line" ] && first_line="tar exited non-zero without a diagnostic"

    # `|| prev=0` is not decoration: `read` is the command following the final
    # `&&`, so it is the one position in this list that errexit does NOT exempt.
    # An empty or unreadable state file would abort a caller running under
    # `set -e` — losing the consolidation to the failure of a counter.
    local prev=0
    if [ -f "$state" ]; then read -r prev < "$state" 2>/dev/null || prev=0; fi
    case "$prev" in ''|*[!0-9]*) prev=0 ;; esac
    # 10# after the case (#332) — an "08" here abandons the rest of this
    # function, which is where the escalation ERROR is logged.
    local streak=$((10#$prev + 1))
    printf '%s\n%s\n%s\n' "$streak" "$(date '+%Y-%m-%d %H:%M:%S')" "$first_line" \
        > "$state" 2>/dev/null || true

    if [ "$streak" -ge "$_ROTATE_ESCALATE_AFTER" ]; then
        log "rotate" "ERROR: log rotation has now failed ${streak} times in a row — ${count} aged log files are accumulating unarchived in ${REMEMBER_LOG_DIR} and nothing will clear them until this is fixed. Run /remember:doctor. Last error: ${first_line}"
    else
        log "rotate" "ERROR: tar failed for ${count} logs: ${first_line}"
    fi
    return 1
}
