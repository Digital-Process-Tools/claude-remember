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
dispatch() {
    local event="$1"
    local event_dir="$REMEMBER_HOOKS_DIR/$event"
    [ -d "$event_dir" ] || return 0
    local current_uid=""
    for hook in "$event_dir"/*; do
        [ -x "$hook" ] || continue
        # Resolved on first use, not on entry (#230). The distribution ships
        # every hooks.d/<event>/ directory containing nothing but a .gitkeep, so
        # the `-d` test above passes and this loop finds nothing executable —
        # and `id` was forked on every tool call to compare against nobody.
        [ -n "$current_uid" ] || current_uid=$(id -u)
        # Ownership check: skip hooks not owned by the current user.
        local hook_uid
        # Try GNU stat (-c) first, then BSD (-f). The reverse order silently
        # succeeds on Linux because `stat -f %u` there returns filesystem free
        # blocks, not file owner UID — and the OR fallback never fires.
        hook_uid=$(stat -c %u "$hook" 2>/dev/null || stat -f %u "$hook" 2>/dev/null || echo "")
        if [ -z "$hook_uid" ] || [ "$hook_uid" != "$current_uid" ]; then
            log "dispatch" "WARNING: skipping hook not owned by current user: $event/$(basename "$hook")"
            continue
        fi
        # World-writable check: skip hooks writable by others.
        if [ -n "$(find "$hook" -maxdepth 0 -perm -002 2>/dev/null)" ]; then
            log "dispatch" "WARNING: skipping world-writable hook: $event/$(basename "$hook")"
            continue
        fi
        REMEMBER_PROJECT="${PROJECT_DIR:-.}" "$hook" 2>/dev/null \
            || log "dispatch" "hook failed: $event/$(basename "$hook")"
    done
}

# Archive log files older than 7 days into monthly tar.gz bundles.
#
# Finds memory-*.log files with mtime > 7 days, compresses them into
# logs-YYYY-MM.tar.gz, and removes the originals on success.
# No-op if no old logs exist.
#
# Args:
#   (none — operates on REMEMBER_LOG_DIR)
#
# Side effects:
#   Creates logs-YYYY-MM.tar.gz in the log directory.
#   Deletes archived .log files.
rotate_logs() {
    local old_logs
    old_logs=$(find "$REMEMBER_LOG_DIR" -name "memory-*.log" -mtime +7 2>/dev/null)
    [ -z "$old_logs" ] && return 0

    local archive_month
    archive_month=$(date -v-7d +%Y-%m 2>/dev/null || date -d '7 days ago' +%Y-%m)
    local archive="${REMEMBER_LOG_DIR}/logs-${archive_month}.tar.gz"
    local count
    count=$(echo "$old_logs" | wc -l | tr -d ' ')

    local basenames=()
    while IFS= read -r f; do
        basenames+=("$(basename "$f")")
    done <<< "$old_logs"

    if tar -czf "$archive" -C "$REMEMBER_LOG_DIR" "${basenames[@]}" 2>/dev/null; then
        while IFS= read -r f; do rm -f "$f"; done <<< "$old_logs"
        log "rotate" "archived ${count} logs → $(basename "$archive")"
    else
        log "rotate" "ERROR: tar failed for ${count} logs"
    fi
}
