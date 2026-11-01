#!/bin/bash
# capture-gap-check.sh -- deferred post-SessionStart bookkeeping, missed-
# session recovery and capture-gap detection, extracted from
# session-start-hook.sh (#1067).
#
# WHY A SEPARATE FILE, NOT A SOURCE INSIDE session-start-hook.sh: every
# function below (session_was_saved, _remember_write_slug_record,
# _remember_write_slug_index, _remember_write_case_divergence,
# _find_previous_transcript) and the capture-gap detection block that follows
# them are reachable ONLY from the deferred phase this file now IS -- nothing
# on session-start-hook.sh's own hot synchronous path calls any of them.
# Left inline, compile_hooks.py's tree-shaking keeps every one of them anyway
# (they ARE reachable from _remember_deferred_phase, wherever that function
# lives), so the whole block counted against session-start-hook.sh's own
# 120 KiB release budget (check_release_tree.py's HOOK_SCRIPT_MAX_BYTES) with
# no margin left to absorb #1064/#1056. Moved here, this file's own source
# chain is never inlined into any hook's compiled output, because it is not
# itself one of compile_hooks.py's four HOOK_SCRIPT_NAMES.
#
# session-start-hook.sh invokes this EITHER synchronously (REMEMBER_DEFER=0,
# kept for the handful of tests that need the deferred phase's side effects
# on disk the instant the hook process exits) OR backgrounded (a trailing
# `&disown`, the default) -- this file does not decide which; the caller's
# own `if [ "${REMEMBER_DEFER:-1}" = "0" ]` still lives in
# session-start-hook.sh, exactly where it was.
#
# Args: $1 -- CURRENT_SESSION_ID (may be empty; see session-start-hook.sh's
#             own STDIN comment for why this is never re-derived here).
_HOOK_DIR="${BASH_SOURCE[0]%/*}"
[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="$(pwd)"

# #1069 review (fork-cost finding on #1067): our own parent,
# session-start-hook.sh, already ran the full resolve-paths.sh ->
# detect-tools.sh -> bootstrap-dirs.sh chain moments ago, in the SAME
# process this one was forked/exec'd from -- and exported every answer that
# chain produces (PROJECT_DIR/PIPELINE_DIR: resolve-paths.sh; REMEMBER_DIR/
# REMEMBER_STORE_ROOT: lib-memory-dir.sh, sourced by bootstrap-dirs.sh).
# Re-running that chain here would redo work this process already has for
# free via ordinary environment inheritance. Trust those values when
# present rather than re-deriving them: a direct/standalone invocation
# (every test fixture that execs this file without first execing
# session-start-hook.sh) will not have them set, and falls through to the
# full chain below exactly as before.
#
# Measured (tests/test_session_start_spawn_reduction_679.py's own
# #1069-tagged tests): this fast path's own trace never sources
# resolve-paths.sh at all, confirmed directly rather than by fork-counting.
#
# #1079 CORRECTION: an earlier version of this comment claimed log.sh's own
# config merge was "needed regardless" here and could not be removed
# "without further work" -- false; it was never removed, only unconditionally
# re-run on every SessionStart (2-6 extra spawns, measured on macOS) plus a
# #362-style mktemp leak into shared OS tmp, because this process is
# forked/exec'd fresh and none of session-start-hook.sh's own in-process
# config table survives into it. The "further work" is the env-cache load a
# few lines below: it replays the one scalar (REMEMBER_RECOVERY_ENABLED)
# this file's own common path actually needs, so the merge itself now runs
# only on a cache miss -- not on every single session start. This is still
# NOT the full fix #1056 ("fewer forks under concurrent Windows session
# starts") tracks; it removes the redundant resolve-paths.sh/bootstrap-dirs.sh
# re-run (as before) AND the redundant log.sh config merge (#1079), not every
# fork #1056's own scope covers.
if [ -n "${PROJECT_DIR:-}" ] && [ -d "${PROJECT_DIR:-}" ] \
    && [ -n "${PIPELINE_DIR:-}" ] && [ -d "${PIPELINE_DIR:-}" ] \
    && [ -n "${REMEMBER_DIR:-}" ]; then
    PLUGIN_ROOT="$PIPELINE_DIR"
    PROJECT="$PROJECT_DIR"
    # resolve-paths.sh's own umask (#68) and bootstrap-dirs.sh's own
    # SYS_TMPDIR (#1040) -- neither is exported by either file (a plain
    # assignment, not `export`), so inheritance cannot carry them; both are
    # cheap builtins to redo by hand, the same way post-tool-hook.sh's own
    # fast path already does for the identical reason.
    umask 077
    SYS_TMPDIR="${TMPDIR:-/tmp}"
    # _remember_forward_slash[_into]: resolve-paths.sh's own exported
    # FUNCTIONS, not just its variables -- function definitions are never
    # inherited across exec() the way exported variables are, so skipping
    # resolve-paths.sh here drops them unless redefined. Both are small,
    # pure (no forks: "[ ]" and a parameter expansion only, per the
    # original's own comment) and identical to resolve-paths.sh's copy;
    # lib-case-divergence.sh and several other files this chain still
    # sources call one or the other. #1069 review round: the FIRST version
    # of this fast path omitted these and _remember_write_case_divergence
    # silently returned "not-applicable" for every external store --
    # command-not-found on stderr, empty stdout from the failed command
    # substitution, caught only by tests/test_case_divergence_298.py.
    _remember_forward_slash() {
        if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
            printf '%s' "${1//\\//}"
        else
            printf '%s' "$1"
        fi
    }
    _remember_forward_slash_into() {
        if [ "$OSTYPE" = msys ] || [ "$OSTYPE" = cygwin ]; then
            printf -v "$1" '%s' "${2//\\//}"
        else
            printf -v "$1" '%s' "$2"
        fi
    }
    _REMEMBER_LAZY_PYTHON=1
    source "$_HOOK_DIR/detect-tools.sh"

    # #1079 review on #1069's own review comment above (now corrected): this
    # process is forked/exec'd fresh, so none of session-start-hook.sh's own
    # in-process _RCFG_* table survives into it -- sourcing log.sh here
    # unconditionally redid lib-memory-dir.sh's whole three-layer jq/mktemp
    # merge on EVERY SessionStart (2-6 extra spawns, measured on macOS) and
    # left that merge's own mktemp'd file in shared OS tmp, since this fast
    # path never sources bootstrap-dirs.sh (the #362 fix) either. Load the
    # env cache session-start-hook.sh already published moments ago instead
    # -- it carries the ONE config value this common path actually needs
    # (REMEMBER_RECOVERY_ENABLED, used by the recovery check below), the
    # same way post-tool-hook.sh's own fast path replays REMEMBER_SAVE_COOLDOWN/
    # REMEMBER_DELTA_THRESHOLD instead of calling config_into. Falls through
    # to the full chain whenever the cache is missing, stale, or predates
    # this key -- the "needed regardless" case this comment used to claim
    # could not be removed without "further work": this is that work.
    _REMEMBER_FAST=0
    source "$_HOOK_DIR/lib-env-cache.sh" 2>/dev/null
    if command -v _remember_env_cache_load >/dev/null 2>&1 \
        && _remember_env_cache_load && [ -n "${REMEMBER_RECOVERY_ENABLED:-}" ]; then
        _REMEMBER_FAST=1
        # Lazy log(): a handful of rare call sites further down (a
        # case-divergence mismatch, a pluginless-SDK exclusion cap, the
        # no-session_id skip message) still need log.sh's real definitions.
        # The lazy stub works the same regardless of which one fires first
        # -- same shape, same rationale, as post-tool-hook.sh:343-359.
        log() {
            unset -f log
            source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
            declare -F log >/dev/null 2>&1 || log() { :; }
            log "$@"
        }
    fi
    if [ "$_REMEMBER_FAST" != "1" ]; then
        source "$_HOOK_DIR/log.sh" 2>/dev/null
        if ! command -v _remember_date >/dev/null 2>&1; then
            echo "capture-gap-check: ERROR -- failed to source $_HOOK_DIR/log.sh" >&2
            exit 127
        fi
    fi
else
    # resolve-paths.sh sets PROJECT_DIR/PIPELINE_DIR, which bootstrap-dirs.sh
    # (via lib-memory-dir.sh) requires to resolve REMEMBER_DIR/
    # REMEMBER_STORE_ROOT correctly -- session-start-hook.sh sources this
    # exact file, the exact same way, before its own detect-tools.sh/
    # bootstrap-dirs.sh (see its own comment on this call site).
    # REMEMBER_HOOK_CWD is already exported by the parent hook process, so
    # the SAME fallback it resolved from is what this process inherits too.
    REMEMBER_PATHS_SOFT_FAIL=1 source "$_HOOK_DIR/resolve-paths.sh" || exit 0

    _REMEMBER_LAZY_PYTHON=1
    source "$_HOOK_DIR/detect-tools.sh"
    source "$_HOOK_DIR/bootstrap-dirs.sh"
    PLUGIN_ROOT="$PIPELINE_DIR"
    PROJECT="$PROJECT_DIR"
    source "$PLUGIN_ROOT/scripts/log.sh" 2>/dev/null
    # log.sh is sourced with stderr suppressed; a silent failure (e.g.
    # read-only mount where log.sh `return 1`s) would leave _remember_date /
    # log / dispatch undefined, crashing later with a cryptic "command not
    # found". Same guard session-start-hook.sh itself carries right after
    # its own identical source.
    if ! command -v _remember_date >/dev/null 2>&1; then
        echo "capture-gap-check: ERROR -- failed to source $PLUGIN_ROOT/scripts/log.sh" >&2
        exit 127
    fi
fi

CURRENT_SESSION_ID="${1:-}"

# #1060 review: session-start-hook.sh's own per-session PID marker
# (REMEMBER_DIR/tmp/deferred-<id>.pid) names this process once it is
# backgrounded, and nothing ever removed it on a normal finish -- an
# unbounded, never-cleaned file per session id, the same class of leak
# this issue's own #362-style tmp fix elsewhere in this diff is about.
# Removed here, under an EXIT trap so every return/exit path is covered,
# and only when the file STILL names OUR OWN pid: a session that resumed
# again and relaunched (this file's own stale-PID recovery) owns a NEWER
# file by the time this trap fires for the OLDER, now-finishing process,
# and that newer file must survive this cleanup untouched.
if [ -n "$CURRENT_SESSION_ID" ]; then
    _remember_deferred_pid_file="$REMEMBER_DIR/tmp/deferred-${CURRENT_SESSION_ID}.pid"
    _remember_cleanup_deferred_pid_file() {
        [ -f "$_remember_deferred_pid_file" ] || return 0
        [ "$(<"$_remember_deferred_pid_file")" = "$$" ] 2>/dev/null \
            && rm -f "$_remember_deferred_pid_file" 2>/dev/null
        return 0
    }
    trap _remember_cleanup_deferred_pid_file EXIT
fi

# PROJECT_PATH_SLUG/SESSIONS_DIR: a pure function of PROJECT_DIR
# (session-start-hook.sh's own comment on this, above its matching
# assignment, explains why -- "the slug is a pure function of PROJECT_DIR and
# PROJECT_DIR does not change mid-session"), so recomputing it here, in this
# file's own process, is exactly as safe as save-session.sh independently
# recomputing REMEMBER_DIR when forked from the recovery site below.
PROJECT_PATH_SLUG="$(session_dir_slug "$PROJECT")"
SESSIONS_DIR="$(claude_projects_dir)/${PROJECT_PATH_SLUG}"
# ── "Was session X saved?" ────────────────────────────────────────────────
# Hoisted out of the recovery block below because the capture-gap check
# further down asks the same question of the same file, and the two answers
# must not be allowed to drift: a detector that tells you a session "was not
# captured" while the save record says it was captured 72/72 is reporting on
# its own bookkeeping, not on anything you lost (#206).
#
# Ask whether this session was EVER saved, not whether it owns the one
# slot: positions are keyed by session now, and the old equality test
# force-saved an already-saved session whenever another had saved since
# (issue #140). Legacy single-slot files still answer correctly.
# Type-check the value, not just the key: the python readers require an
# int, so `has($id)` alone would call a corrupt {"id": null} entry saved
# while they resume it from 0 — re-summarizing the whole span, which is
# what #140 exists to prevent.
# isinfinite guard: JSON has no infinity literal, but 1e400 overflows to
# one, and floor(infinite) == infinite — so it would read as a line
# number here while python's is_integer() rejects it. jq would then call
# the session saved and the recovery force-save below would never fire,
# losing that session's tail entirely.
LAST_SAVE_FILE="$REMEMBER_DIR/tmp/last-save.json"
SAVED_QUERY='def isline: type == "number" and ((isnan or isinfinite) | not) and . == floor; if (((.sessions // {})[$id]) | isline) or (.session == $id and (.line | isline)) then "saved" else "unsaved" end'

# Args: $1 — session id. Exit 0 if last-save.json records it as saved.
#
# _jq_fallback's own shim (scripts/detect-tools.sh) only ever evaluates
# dotted-path lookups and drops every `--arg`/`--argjson` pair entirely
# (#667): it eats each leading `-*` token into a flags variable it never
# reads, then treats the first non-flag token as the query and the one
# after it as the file -- fed `--arg id "$1" "$SAVED_QUERY"
# "$LAST_SAVE_FILE"`, that lands on query="id", file="$1" (the session id,
# not a real file), so the shim always prints nothing and this always read
# as "unsaved", spawning `save-session.sh --force` on every jq-less
# startup regardless of whether the previous session was actually saved.
#
# Every OTHER $JQ/$JQ_BIN call site under scripts/ that passes --arg already
# guards itself with `command -v jq` first (session-start-hook.sh's own
# promo-JSON call below, the UserPromptSubmit hook's two notice-JSON calls), so
# this is the ONLY call site that ever reaches the fallback with --arg --
# it gets its own jq-free branch instead of teaching the generic shim a
# --arg parser it would be the sole caller of.
session_was_saved() {
    [ -n "$1" ] && [ -f "$LAST_SAVE_FILE" ] || return 1
    if [ "$JQ" = "_jq_fallback" ]; then
        _remember_python || return 1
        # #898 round 20: the program lives in session_saved.py beside this
        # hook (as jq_fallback_get.py and cfg_merge.py do), called by path.
        # It used to be a multi-line here-string fed to `python -`, and the
        # directory's scanner could not follow the script past it.
        [ "$(_remember_run_python "$_HOOK_DIR/session_saved.py" "$LAST_SAVE_FILE" "$1" 2>/dev/null)" = "saved" ]
    else
        [ "$(_remember_run_jq -r --arg id "$1" "$SAVED_QUERY" "$LAST_SAVE_FILE" 2>/dev/null)" = "saved" ]
    fi
}
# ── The slug, written down once, for callers that are not bash (#294) ─────
# The slug is a pure function of PROJECT_DIR and PROJECT_DIR does not change
# mid-session, so a caller in another language had no reason to recompute it —
# and no way to ask for it except by sourcing lib-slug.sh in a subshell, once
# per tool call. The reporter of #294 drives this plugin from PowerShell and
# answered that by maintaining a port of session_dir_slug, which is how the
# long-path divergence was found: a second implementation of the one function
# whose disagreements are silent.
#
# So it is written here, where PROJECT_PATH_SLUG already exists two lines
# above. This costs one `mv`; the per-tool-call path is not touched at all,
# which is deliberate and is asserted by
# tests/test_session_slug_record_294.py::test_the_per_tool_call_path_is_not_touched.
#
# In tmp/, with the locks, the cooldown markers and the delivery record: it
# names one machine's session and one machine's absolute paths, and #285 is
# what happens when that kind of state is committed like memory. The git
# backup already excludes the whole directory.
#
# THREE STATES, NOT TWO. An empty slug is not an absence — it resolves to
# ~/.claude/projects/ ITSELF, a directory that exists and holds every
# project's transcripts, so a reader that cannot tell "nothing was written"
# from "the slug is empty" reads someone else's session and never knows. The
# record therefore always carries a status, and never carries a `slug=` key it
# cannot fill: absent means this never ran, status=unavailable means it ran
# and could not answer, status=ok means the value is usable.
#
# NO TIMESTAMP, deliberately. The slug is a pure function of the path, so a
# record written by a long-dead session is still correct; only a record
# written by a DIFFERENT project is wrong, and worktrees make that reachable —
# they share a REMEMBER_DIR with the main checkout (#56) while keeping their
# own PROJECT_DIR, so the last session to start owns this file. `project_dir`
# is the one field that decides whether the record applies to a reader. A
# timestamp beside it would only offer a staleness test that answers the wrong
# question.
_remember_write_slug_record() {
    local _dir="$REMEMBER_DIR/tmp" _tmp
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0
    _tmp="$_dir/session-slug.$$"

    # A newline is a legal byte in a POSIX filename and this record is
    # line-based, so a project path containing one would put `slug=…` inside
    # the value of `project_dir=` — a file that parses cleanly and says
    # something false. Refusing is the only honest answer, and saying which
    # state we are in is the point of the status field.
    local _reason=""
    if [ -z "$PROJECT_PATH_SLUG" ]; then
        _reason="empty-slug"
    else
        local _paths_joined="${PROJECT}${SESSIONS_DIR}${REMEMBER_DIR}"
        if [[ "$_paths_joined" == *$'\n'* ]]; then
            _reason="unrepresentable-path"
        fi
    fi

    # Written whole to a private temp name and moved into place, because two
    # worktrees of one repo start sessions against the same tmp/ and a reader
    # must never see half a record. CURRENT_SESSION_ID is already constrained
    # to [A-Za-z0-9._-] where it is read, so it cannot break a line here.
    if [ -n "$_reason" ]; then
        printf 'format=1\nstatus=unavailable\nreason=%s\n' "$_reason" \
            > "$_tmp" 2>/dev/null || { rm -f "$_tmp" 2>/dev/null; return 0; }
    else
        {
            printf '%s=%s\n' format 1 status ok project_dir "$PROJECT" slug "$PROJECT_PATH_SLUG" \
                sessions_dir "$SESSIONS_DIR" memory_dir "$REMEMBER_DIR"
            [ -z "$CURRENT_SESSION_ID" ] || printf 'session_id=%s\n' "$CURRENT_SESSION_ID"
        } > "$_tmp" 2>/dev/null || { rm -f "$_tmp" 2>/dev/null; return 0; }
    fi

    mv -f "$_tmp" "$_dir/session-slug" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    return 0
}
# Called in the deferred block below (#660), not here: this writes a record
# nothing in this hook reads back, and the foreground path's only obligation
# is the injected context.

# ── And somewhere a caller can NAME, in the layout we ship (#297) ─────────
# The record above answers "what is the slug", and in the layout
# config.user.example.json ships — "data_dir": "~/.remember/{slug}", under a
# _purpose that says to copy it — it answers from inside a directory the slug
# names. A caller holding project_dir and the template could not open it
# without already knowing what it says. That was written down as one documented
# hole when #296 shipped; it is the recommended configuration, and those are
# the installs most likely to have a non-bash caller in the first place.
#
# NOTHING IS DERIVED FROM project_dir. The alternative shape — a second copy at
# <store_root>/tmp/session-slug-<key from project_dir> — needs a key both sides
# compute the same way, which is a second algorithm over the project path, and
# deleting exactly that is what #294 and #296 were for. A language-neutral
# encoding does not escape it either: base64 of an N-byte path is 4*ceil(N/3),
# so the 260-character paths #294 was about come out at 348 bytes and the
# 300-character vector in tests/slug_vectors.py at 400, past the 255-byte
# filename limit everywhere — and truncating to fit means appending a hash, a
# slug algorithm under a different name. An index matched on project_dir
# verbatim asks the caller to compute nothing at all, and keeps the property
# the reporter's own directory scan has: it cannot answer wrongly, only fail
# to answer.
#
# WRITTEN ONLY WHEN THE SLUG NAMES THE STORE. REMEMBER_STORE_ROOT is empty in
# the legacy layout and in a single-directory external store, where it would
# equal REMEMBER_DIR and duplicate a record that is already reachable — two
# files saying one thing, with a second chance to disagree. The common layout
# pays nothing for the external one, which is also why this is not in
# bootstrap-dirs.sh.
#
# ONE FILE, MANY WRITERS — the difference from the record, which each project
# owns outright. Several projects, and several worktrees of one project, start
# sessions against one store root, and a read-modify-write across them loses
# rows silently. So the rewrite is taken under the plugin's one lock primitive
# and published with mv; a session that cannot take the lock writes no row and
# says nothing, because the per-project record remains authoritative and this
# is a way to find it, not a second source of truth.
#
# NO TIMESTAMPS, for the reason _remember_write_slug_record gives above. A row
# for a directory since deleted can never be MATCHED by a caller holding a live
# project_dir, and if that path is ever recreated the row is still correct — so
# rows are never expired by age, and never pruned by testing whether the
# directory still exists, which would drop correct rows for anything on an
# unmounted share. The only bound is the row cap, and the ordering it drops by
# is position, maintained by this rewrite. That is not a staleness test and no
# reader may use it as one.
#
# AND NO ROW AT ALL for a project_dir this file cannot hold. A newline is legal
# on POSIX and a tab is legal too, and both are structure here. The record can
# take status=unavailable for that case; a row cannot, because it would have to
# be keyed by the very value it is refusing. A caller with such a path finds no
# row and falls back — the same outcome as this never having run, which is the
# honest one.
SLUG_INDEX_LOCK_TIMEOUT=2
SLUG_INDEX_MAX_ROWS=1000
_remember_write_slug_index() {
    [ -n "${REMEMBER_STORE_ROOT:-}" ] || return 0
    [ "$REMEMBER_STORE_ROOT" != "$REMEMBER_DIR" ] || return 0
    [ -n "$PROJECT_PATH_SLUG" ] || return 0

    local _paths_joined="${PROJECT}${REMEMBER_DIR}"
    if [[ "$_paths_joined" == *$'\n'* ]] || [[ "$_paths_joined" == *$'\t'* ]]; then
        return 0
    fi

    local _dir="$REMEMBER_STORE_ROOT/tmp"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0

    local _index="$_dir/sessions" _lock="$_dir/sessions.lock" _tmp

    # Sourced here rather than at the top of the file: this is the only caller,
    # and lib-lock.sh probes for fractional sleep at source time — a fork the
    # legacy layout has no reason to pay at every session start.
    #
    # The primitive, not the lock_acquire/lock_release wrappers (#898): the
    # wrappers only add the opt-in hold-duration recorder (#226), which sizes
    # the save and staging locks, and calling them here would compile that
    # whole recorder into this hook for a lock it was never about.
    source "$_HOOK_DIR/lib-lock.sh" 2>/dev/null || return 0
    command -v _lock_acquire_impl >/dev/null 2>&1 || return 0
    _lock_acquire_impl "$_lock" "$SLUG_INDEX_LOCK_TIMEOUT" || return 0

    _tmp="$_index.$$"
    {
        printf 'format=1\n'
        if [ -f "$_index" ]; then
            # Our own row dropped — it is re-appended below, so a project that
            # starts twice moves rather than doubles — and the oldest dropped at
            # the cap. A first line that is not ours means a format this version
            # cannot read: exit, print nothing, start the file over, rather than
            # carry rows forward under rules we do not know.
            awk -v self="$PROJECT" -v max="$((SLUG_INDEX_MAX_ROWS - 1))" '
                NR == 1 { if ($0 != "format=1") exit 0; next }
                $0 == "" { next }
                {
                    n = index($0, "\tproject_dir=")
                    if (n == 0) next
                    if (substr($0, n + 13) == self) next
                    rows[++c] = $0
                }
                END {
                    start = (c > max) ? c - max + 1 : 1
                    for (i = start; i <= c; i++) print rows[i]
                }
            ' "$_index" 2>/dev/null
        fi
        # project_dir LAST, and the only field whose value may contain a tab —
        # so a reader splits on the first three and keeps the remainder whole.
        # slug is ASCII by construction, and memory_dir was refused above if it
        # carried one.
        printf 'status=ok\tslug=%s\tmemory_dir=%s\tproject_dir=%s\n' \
            "$PROJECT_PATH_SLUG" "$REMEMBER_DIR" "$PROJECT"
    } > "$_tmp" 2>/dev/null || {
        rm -f "$_tmp" 2>/dev/null
        _lock_release_impl "$_lock" 2>/dev/null
        return 0
    }

    mv -f "$_tmp" "$_index" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    _lock_release_impl "$_lock" 2>/dev/null
    return 0
}
# Called in the deferred block below (#660), not here: this writes a record
# nothing in this hook reads back, and the foreground path's only obligation
# is the injected context.

# ── Is this store known by a second spelling? (#298) ──────────────────────
# Git's index is case-sensitive where NTFS is not, so a store can be spelled
# one way on disk and another in the repository that backs it up. That costs
# nothing while it stays on a case-insensitive filesystem — measured on the
# reporter's own Windows box, where both spellings resolve to the same
# directory object — and it splits the store in two on a case-sensitive
# restore, which is the machine least likely to be looking.
#
# Session start, not the per-tool-call path: `session_dir_slug` runs on every
# tool call and #299 pinned that path byte-identical. The whole check costs one
# `git ls-tree` and only for a store that has its own repository; the disk half
# costs no fork at all, and a store in the legacy layout pays nothing because
# the library returns not-applicable before either probe runs.
#
# Disclosure only, and everything downstream keys off ONE fact: whether the
# finding has changed since last session. The record at tmp/case-divergence
# always holds the current answer in all four states, and is rewritten only
# when that answer is different — which is also what makes the steady state
# fork-free. The human-facing notice and the "could not check" log line fire
# on that same change. The condition never clears itself and is harmless
# today, so repeating it every session start would spend the one channel a
# human actually reads (#200) on wallpaper — the argument `_push_and_report`
# makes for its threshold, with the threshold replaced by "say it again only
# when it says something different". `/remember:doctor` re-runs the check live
# and reports every time, which is where someone who suspects a problem looks.
_remember_write_case_divergence() {
    source "$_HOOK_DIR/lib-case-divergence.sh" 2>/dev/null || return 0
    command -v remember_case_divergence >/dev/null 2>&1 || return 0
    remember_case_divergence

    local _dir="$REMEMBER_DIR/tmp" _tmp _old="" _body="" NL=$'\n'

    # The record built as a string first, so it can be compared with what is
    # already on disk. Every field is appended unconditionally or inside an
    # `if` — never `[ -n … ] && …`, which as a group's last command makes a
    # correct write look like a failed one, and cost this file a record it had
    # already produced until the trace said so.
    _body="format=1${NL}status=$REMEMBER_CASE_STATUS"
    # Optional fields only when set, through `${VAR:+...}` (#898).
    if [ "$REMEMBER_CASE_STATUS" != "not-applicable" ]; then
        _body="$_body${NL}resolved=$REMEMBER_CASE_RESOLVED${NL}store_root=$REMEMBER_CASE_ROOT"
        _body="$_body${NL}disk_state=$REMEMBER_CASE_DISK_STATE"
        _body="$_body${REMEMBER_CASE_DISK_REASON:+${NL}disk_reason=$REMEMBER_CASE_DISK_REASON}"
        _body="$_body${REMEMBER_CASE_DISK_NAMES:+${NL}disk_names=$REMEMBER_CASE_DISK_NAMES}"
        _body="$_body${NL}git_state=$REMEMBER_CASE_GIT_STATE"
        _body="$_body${REMEMBER_CASE_GIT_REASON:+${NL}git_reason=$REMEMBER_CASE_GIT_REASON}"
        _body="$_body${REMEMBER_CASE_GIT_NAMES:+${NL}git_names=$REMEMBER_CASE_GIT_NAMES}"
    fi

    # Read what is already there BEFORE deciding anything: it answers both
    # "has the finding changed" (which is what the human-facing notice fires
    # on) and "is there anything to write at all". Read with the shell — a
    # `grep | tr` here was two forks on a path whose whole budget is one
    # `git ls-tree`, and in the legacy layout, where this check is
    # not-applicable and its record never changes, it was two forks for a file
    # that already said the right thing.
    if [ -f "$_dir/case-divergence" ]; then
        local _pline
        while IFS= read -r _pline; do
            _old="${_old:+$_old$NL}$_pline"
        done < "$_dir/case-divergence"
    fi

    if [ "$_old" != "$_body" ]; then
        [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 0
        _tmp="$_dir/case-divergence.$$"
        printf '%s\n' "$_body" > "$_tmp" 2>/dev/null \
            || { rm -f "$_tmp" 2>/dev/null; return 0; }
        mv -f "$_tmp" "$_dir/case-divergence" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
    fi

    if [ "$REMEMBER_CASE_STATUS" = diverged ]; then
        log "case-divergence" "$REMEMBER_CASE_MESSAGE"
        # An unchanged record is an unchanged finding, and the human has
        # already been told. Saying it again every session start would
        # spend the one channel they actually read on a condition that is
        # harmless today and never clears itself.
        [ "$_old" = "$_body" ] && return 0
        printf '%s\n' "$REMEMBER_CASE_MESSAGE" \
            > "$_dir/case-divergence-notice" 2>/dev/null || true
    elif [ "$REMEMBER_CASE_STATUS" = unavailable ]; then
        # Logged on change only. The commonest reason by far is
        # `not-a-repository` — an external store nobody has pointed a git
        # backup at — and that is a standing condition, not an event: one
        # identical line per session for the life of the install is the
        # wallpaper #252's five weeks of identical daily lines proved
        # nobody reads. It is still never rendered as agreement anywhere
        # that reports it; `/remember:doctor` says it every time.
        [ "$_old" = "$_body" ] && return 0
        log "case-divergence" "could not check for a second spelling of this store (disk=$REMEMBER_CASE_DISK_STATE${REMEMBER_CASE_DISK_REASON:+/$REMEMBER_CASE_DISK_REASON} git=$REMEMBER_CASE_GIT_STATE${REMEMBER_CASE_GIT_REASON:+/$REMEMBER_CASE_GIT_REASON}) -- not a report that they agree"
    fi
    return 0
}
# Called in the deferred block below (#660), not here: this writes a record
# nothing in this hook reads back, and the foreground path's only obligation
# is the injected context.

# Args: $1 -- path to a transcript file. Reports (via exit status) whether
# its own first bookkeeping records name it as an Agent SDK session that
# never loaded this plugin at all (#745).
#
# A reporter's transcripts opened with `"type":"queue-operation"` and had no
# `capture-alive.d/<id>` marker -- the capture-gap notice fired even though
# `/remember:doctor` reported the session as saved, and separately, recovery
# force-saved another plugin's own review into the user's memory. A second
# report on the same issue (kenspc, 16 real transcripts) found the real
# discriminator: every marker-less transcript in that sample was
# `entrypoint: sdk-py`, written by another plugin's own `agentic_review()`
# call (security-guidance's commit/push review) starting a Claude Agent SDK
# session with `setting_sources=[]` -- no settings, no plugins, so
# remember's hooks were never REGISTERED for it, not merely unwired. There
# is no marker to find because PostToolUse was never there to write one, and
# there never will be.
#
# Claude Code writes `entrypoint` on a transcript's own bookkeeping records
# (`cli`, `claude-vscode`, `sdk-py`, ...); an SDK client's own transcript
# carries it before the plugin can be blamed. This is deliberately narrow:
# only a value opening with `sdk-` is treated as pluginless.
#
# NOT SOUND, on two counts logged to trap.d/745 (curator to decide) rather
# than fixed here:
#   1. An SDK integration that DOES pass `setting_sources` and loads this
#      plugin would write its own marker exactly like any other host, but
#      this check has no way to tell that apart from a pluginless one -- it
#      looks only at the entrypoint prefix, never at whether evidence of
#      capture already exists for this same transcript. No real-world
#      instance of a plugin-loaded SDK entrypoint has been observed; #745's
#      own dataset (16 transcripts) had none.
#   2. The match below is a substring scan over up to 50 RAW transcript
#      lines, not a check that the matching line is a bookkeeping record --
#      ordinary conversation content (a pasted JSON snippet, a tool result
#      quoting this very file) could in principle carry the literal text
#      `"entrypoint":"sdk-...` and be misread as a bookkeeping line. A line
#      whose "message" field is the nested {role, content} OBJECT real
#      dialogue always uses is skipped outright below (#803: a bookkeeping
#      record's own "message" field, when present, is instead a plain
#      string, discriminated from the object shape rather than assumed
#      absent) -- which closes the concrete case self-review raised (a
#      session whose own transcript discusses this issue's test fixtures)
#      without a real JSON parse -- a residual false match confined to
#      bookkeeping-shaped, non-dialogue content is not eliminated.
# The field also carries no documented stability contract; absence, or a
# shape this cannot parse, falls through to "not sdk" -- the safe direction,
# since keeping a transcript in contention risks at most the false notice
# this exists to fix, never a real gap going unreported.
#
# Bounded to the same scan depth pipeline/extract.py's own envelope sniff
# uses for the same shape of problem (#543/#556's _ENVELOPE_SNIFF_SCAN_CAP) --
# a transcript that never says who wrote it in its first 50 lines is read as
# not-sdk rather than scanned to the end.
# _stdin_json_string_into VARNAME field raw -- duplicated here, verbatim,
# from session-start-hook.sh's own copy (#1067 extraction). Not shared via a
# sourced lib: it is this hook's own stdin-payload parser, used there for
# several OTHER fields (cwd, session_id, transcript_path, source) on the hot
# synchronous path, and ends up needed here too only because
# _transcript_is_pluginless_sdk (below) calls it on one JSONL line read from
# disk, not on the hook's own stdin. Keep both copies identical if either
# ever changes -- session-start-hook.sh's own copy is the one with the full
# rationale comment.
_stdin_json_string_into() {
    local _sjsi_var="$1" _sjsi_field="$2" _sjsi_raw="$3" _sjsi_rest _sjsi_prefix _sjsi_value _sjsi_dq
    printf -v "$_sjsi_var" '%s' ""
    # The double quote is held in a variable (octal 042) rather than written
    # backslash-escaped: the plugin directory's scanner mis-tracks an
    # escaped quote (#898 round 8). Same patterns, same quoting of the field.
    printf -v _sjsi_dq '\042'
    [[ "$_sjsi_raw" == *"$_sjsi_dq$_sjsi_field$_sjsi_dq"* ]] || return 1
    _sjsi_rest=${_sjsi_raw#*"$_sjsi_dq"$_sjsi_field"$_sjsi_dq"}
    _sjsi_prefix=${_sjsi_rest%%"$_sjsi_dq"*}
    if [[ "$_sjsi_prefix" == *[!:[:space:]]* ]]; then return 1; fi
    _sjsi_value=${_sjsi_rest#*"$_sjsi_dq"}
    _sjsi_value=${_sjsi_value%%"$_sjsi_dq"*}
    # A JSON encoder writes each backslash as `\\` -- a Windows `cwd` from
    # Codex arrives as `C:\\work\\proj` otherwise (#829).
    _sjsi_value=${_sjsi_value//\\\\/\\}
    [ -n "$_sjsi_value" ] || return 1
    printf -v "$_sjsi_var" '%s' "$_sjsi_value"
}

_ENTRYPOINT_SNIFF_CAP=50
_transcript_is_pluginless_sdk() {
    local f=$1 n=0 line ep rest prefix is_dialogue dq
    # the double quote, as in _stdin_json_string_into
    printf -v dq '\042'
    while IFS= read -r line; do
        n=$((n + 1))
        is_dialogue=0
        # Expansion tests, not quoted literals in a case pattern (#898
        # round 9, a directory-scanner hold shape).
        rest=${line#*"$dq"message"$dq"}
        if [ "$rest" != "$line" ]; then
            # #803: real dialogue's own "message" field is always the
            # nested {role, content} OBJECT; a bookkeeping record's
            # "message" field (an error/status STRING, possibly empty,
            # on say a queue-operation record) is not. Checked directly
            # by shape rather than by reusing _stdin_json_string_into (its
            # own `[ -n "$value" ]` non-empty guard cannot tell an empty
            # STRING apart from an OBJECT by return code alone --
            # auditor finding, self-review round): everything between
            # the "message" key and the first `"` after it must be only
            # colon and whitespace for the value to be quote-opened
            # (string-shaped, any length, any whitespace width); any
            # other character there (a `{`, most plainly) means the
            # value is an object, and only that shape is real dialogue.
            # A string-shaped "message" falls through to the entrypoint
            # check below like any other bookkeeping line, so it can no
            # longer mask an "entrypoint" field on the same line the way
            # a bare substring skip did. Skipping via `is_dialogue` (an
            # `if`, not a `continue`) -- self-review caught that a
            # `continue` here would jump past the CAP check below on
            # every dialogue line, silently defeating
            # _ENTRYPOINT_SNIFF_CAP for the overwhelmingly common case
            # (ordinary transcripts are mostly dialogue).
            prefix=${rest%%"$dq"*}
            if [[ "$prefix" == *[!:[:space:]]* ]]; then
                is_dialogue=1
            fi
        fi
        if [ "$is_dialogue" -eq 0 ]; then
            if [ "${line#*"$dq"entrypoint"$dq"}" != "$line" ]; then
                _stdin_json_string_into ep entrypoint "$line" 2>/dev/null || return 1
                # `[ ]` prefix test, not a `case` with a catch-all `*)`
                # arm inside this loop (#898 round 7 -- that shape is
                # one the plugin directory's scanner holds a
                # submission on).
                if [ "${ep#sdk-}" != "$ep" ]; then
                    return 0
                else
                    return 1
                fi
            fi
        fi
        [ "$n" -ge "$_ENTRYPOINT_SNIFF_CAP" ] && return 1
    done < "$f" 2>/dev/null
    return 1
}

# How many pluginless-SDK transcripts (#745) the previous-transcript lookup
# below passes over before it gives up (#819).
# `:-`, not a plain `=`, so a value already set in the calling environment
# survives (self-review finding, Explore round: a bare `=20` here
# unconditionally clobbers any caller-supplied override before the
# lookup ever reads it via its own `${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20}`
# fallback below, making the "configurable" cap dead code in production --
# always exactly 20 regardless of what the environment set).
_PREV_TRANSCRIPT_EXCLUDE_CAP="${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20}"

# The file suffix every transcript in SESSIONS_DIR carries; a transcript's
# session id is its file name without it.
_TRANSCRIPT_SUFFIX=.jsonl

# Args: $1 — sessions dir, $2 — this session's id ("" when the payload had
# none). Sets PREV_JSONL and PREV_ID to the previous session's transcript
# path and session id, or both to "" when there is no previous session.
#
# Transcripts are ranked newest first by mtime, compared with bash's own
# `-nt` test builtin -- no `ls -t`, no sort, no fork, however many
# transcripts exist (#691). Equal mtimes keep glob order. The lookup takes
# the best-ranked transcript and passes over it while it is one of:
#   * this session's own: with an id, the transcript named after it is
#     never considered at all; without one, the best-ranked transcript is
#     ASSUMED to be ours and passed over positionally (see the call site);
#   * a pluginless-SDK transcript (#745): no plugin was ever loaded into
#     it, so neither the capture-gap notice nor recovery's force-save may aim
#     at it. At most _PREV_TRANSCRIPT_EXCLUDE_CAP of these are passed over
#     (#819) -- a previous session hidden behind more headless runs than
#     that is not worth finding -- and giving up is logged (#823), because
#     otherwise it reads exactly like "no previous session exists" and the
#     recovery save and the #200 capture-gap check both skip in silence.
#
# Passing over is a floor, not a list: each round takes the best-ranked
# transcript ranked strictly BELOW the last one passed over, so nothing has
# to remember which files were already excluded. The glob runs once; each
# round walks its result, so the worst case is (cap + 2) * n `-nt` tests,
# never n * (however many headless runs sit in the directory) (#819).
_find_previous_transcript() {
    local dir=$1 own_id=$2 floor="" before_floor tries=0 f id
    local assume_newest_is_ours=""
    local -a files
    [ -z "$own_id" ] && assume_newest_is_ours=1
    files=("$dir"/*"$_TRANSCRIPT_SUFFIX")
    while :; do
        PREV_JSONL="" PREV_ID="" before_floor=1
        for f in "${files[@]}"; do
            [ -e "$f" ] || continue
            if [ -n "$floor" ]; then
                # Skip everything ranked at or above the floor: the floor
                # itself, anything newer, and an equal-mtime file that comes
                # before it in glob order.
                if [ "$f" = "$floor" ]; then
                    before_floor=""
                    continue
                fi
                [ "$f" -nt "$floor" ] && continue
                [ -n "$before_floor" ] && ! [ "$floor" -nt "$f" ] && continue
            fi
            id=${f#"$dir"/}
            id=${id%"$_TRANSCRIPT_SUFFIX"}
            [ -n "$own_id" ] && [ "$id" = "$own_id" ] && continue
            if [ -z "$PREV_JSONL" ] || [ "$f" -nt "$PREV_JSONL" ]; then
                PREV_JSONL=$f
                PREV_ID=$id
            fi
        done
        [ -z "$PREV_JSONL" ] && return 0
        if [ -n "$assume_newest_is_ours" ]; then
            assume_newest_is_ours=""
        elif _transcript_is_pluginless_sdk "$PREV_JSONL"; then
            tries=$((tries + 1))
            if [ "$tries" -ge "${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20}" ]; then
                log "hook" "WARNING: _find_previous_transcript gave up after ${_PREV_TRANSCRIPT_EXCLUDE_CAP:-20} pluginless-SDK exclusions in $dir -- recovery and the #200 capture-gap check treat this as no previous session"
                PREV_JSONL="" PREV_ID=""
                return 0
            fi
        else
            return 0
        fi
        floor=$PREV_JSONL
    done
}

# ── Deferred: previous-session recovery and capture-gap detection (#660) ──
# Everything from here to the end of the capture-gap block runs in a detached
# subshell, because nothing in it feeds this hook's stdout and nothing after
# it reads what it sets. Its outputs are: a backgrounded save-session.sh
# (already detached before this change), files under tmp/ whose only reader is
# the UserPromptSubmit hook on the NEXT prompt, and log lines. The foreground path's
# one obligation is the injected context, and this is not part of it.
#
# Why it is worth moving: `_find_previous_transcript` sorts every past transcript
# (0.41s at 2000 of them) and the capture-gap check greps the previous
# transcript to EOF whenever "tool_use" is absent (0.197s on a 100MB one,
# windows-latest) -- both inside a start whose whole budget is a couple of
# seconds on Git Bash.
#
# #1067: no longer a brace group -- a real separate file, invoked (not
# sourced) from session-start-hook.sh, specifically so compile_hooks.py never
# inlines any of it into that hook's own compiled output (see this file's own
# header comment). Every function and variable this span needs (log,
# config_into, REMEMBER_DIR, PROJECT, PROJECT_PATH_SLUG, SESSIONS_DIR) is
# resolved fresh above, in THIS process, the same way save-session.sh already
# resolves them fresh when forked from the recovery site below -- not shared
# with session-start-hook.sh's own copies, by construction.
#
# The redirections are load-bearing, not tidiness -- the same #646 fd
# inheritance that cost that reporter 98s. At this point in the script stdout
# is still the REAL pipe Claude Code reads (the buffer redirect happens later,
# at `exec 3>&1`), so a child that inherits it holds the write end open and
# the client does not see EOF until this subshell finishes -- which would make
# a start that no longer WAITS for this work still BLOCK on it, and look like
# the change did nothing. `3>&-` guards the same way against the fd 3 dup that
# the consolidation spawn below documents in full.
#
# Behaviour change, stated: the capture-gap notice is written slightly later
# than before. A start whose first user prompt arrives before the grep
# finishes now sees the notice on the prompt after it. The notice is about a
# PREVIOUS session and is not time-critical; nothing else observes the file.
# `local`, so an inline run (REMEMBER_DEFER=0, below) does not leave the label
# set for the rest of the hook. Marks everything below as off the foreground
# path, for the profiler and for the reader: a trace cannot tell a deferred
# child from a command substitution by pid alone -- $(...) gets its own pid
# too and the parent WAITS for it -- so "how much is still synchronous" is
# unanswerable without a phase label.
# #700: exported (local -x), so a real child forked from anywhere below
# (the recovery save-session.sh fork, further down) carries the label into
# its own env -- a plain `local` never left this function at all, so no
# tracer outside this process could ever have observed it.
export _REMEMBER_PHASE=deferred

# Records, moved here from their original call sites above (#660). All three
# are pure side effects -- verified mechanically that none of them assigns
# anything read later in this hook -- and their outputs are read by
# the UserPromptSubmit hook and /remember:doctor, never by the start itself.
_remember_write_slug_record
_remember_write_slug_index
_remember_write_case_divergence

# With no CURRENT_SESSION_ID, "not ours" has no meaning and there is no right
# answer to substitute — the lookup's positional guess (newest is ours) is
# correct at resume and wrong at startup, and nothing here can tell which.
# Recovery keeps it unchanged rather than trading one guess for another: its
# failure mode is a save aimed at the wrong session, which the next startup
# can still correct. The capture-gap check gets no such fallback, because its
# failure mode is an accusation — see below.
_find_previous_transcript "$SESSIONS_DIR" "$CURRENT_SESSION_ID"

# Asked ONCE, and before recovery forks (#270). Recovery force-saves in the
# background and the capture-gap check below re-read this same file through
# session_was_saved — so a session being rescued could read as unsaved in the
# very invocation that was rescuing it, or as saved, depending on which process
# won. The question is "was this captured", not "has that fork finished yet",
# and the answer to it does not change while this hook runs.
PREV_WAS_SAVED="no"
if [ -n "$PREV_ID" ] && session_was_saved "$PREV_ID"; then
    PREV_WAS_SAVED="yes"
fi

# ── Recovery: save the most recent missed session ──────────────────────────
# config_into (#665, part of #660) writes into a scratch var directly --
# no command-substitution subshell on top of the flattened-cache-hit table.
_recovery_enabled=""
if [ -n "${REMEMBER_RECOVERY_ENABLED:-}" ]; then
    # #1079: the fast/cache-hit path above never sourced log.sh, so
    # config_into is not even defined here -- the cached scalar IS the
    # answer, already resolved by session-start-hook.sh moments ago.
    _recovery_enabled="$REMEMBER_RECOVERY_ENABLED"
else
    config_into _recovery_enabled '.features.recovery' true
fi
# Tracks whether THIS run actually kicked off a recovery fork for PREV_ID --
# distinct from _recovery_enabled, which reads "true" even when the extra
# preconditions below (SESSIONS_DIR, LAST_SAVE_FILE, an unsaved PREV_ID) are
# not met. The capture-gap notice further down reads THIS flag, not the
# feature flag alone: "enabled" with nothing to attempt is the same
# user-facing case as "disabled", so both get the original wording (#745).
_recovery_attempted=""
if [ "$_recovery_enabled" = "true" ]; then
if [ -d "$SESSIONS_DIR" ] && [ -f "$LAST_SAVE_FILE" ] && [ -n "$PREV_ID" ]; then
    if [ "$PREV_WAS_SAVED" = "no" ]; then
        # REMEMBER_TRANSCRIPT_PATH (exported above, #407) names THIS session's
        # own transcript -- it must not reach a save being forced for a
        # DIFFERENT one. pipeline.extract.find_session() trusts that variable
        # unconditionally once it names a real file, so an inherited value
        # here would make PREV_ID's rescue silently read and summarize the
        # current session's transcript instead, while still labelling the
        # saved record PREV_ID. Cleared in a subshell so it stays exported for
        # this hook's own later use. Pinned by
        # tests/test_recovery_transcript_leak_407.py.
        ( unset REMEMBER_TRANSCRIPT_PATH; "$PLUGIN_ROOT/scripts/save-session.sh" "$PREV_ID" --force ) </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true
        # Set BEFORE we know whether the forked save-session.sh above actually
        # succeeds (#745) -- it runs detached (`& disown`, just above) and this
        # hook can exit long before it finishes, so "attempted" is the only
        # claim honestly available here. The notice below says "is being
        # recovered", never "has been recovered", for the same reason.
        _recovery_attempted="true"
    fi
fi
fi

# ── Capture-gap detection (#200) ──────────────────────────────────────────
# Claude Code reads hook registrations at session start, so a plugin enabled
# MID-session has none of its hooks wired for that session: PostToolUse never
# fires and capture silently does nothing, for hours, with nothing in the logs
# to say so. The reporter lost a day to it and found only a lone session-start
# line.
#
# It cannot be caught while it is happening. Nothing inside a hook can see
# which hooks are registered — no env var, no file, and `/hooks` is a UI a
# script cannot invoke — and SessionStart's `source` field is only
# startup/resume/clear/compact/fork, so a plugin-enable is indistinguishable
# from a fresh start. Afterwards, though, the signature is exact: a session
# where SessionStart ran and PostToolUse never did.
#
# Judged by IDENTITY: the PostToolUse hook writes the session id it saw, and if
# there is no record for the previous session's id then PostToolUse never ran
# for it. Comparing mtimes instead failed — bash 3.2's `-nt` works to the
# second, so a healthy session whose first tool call landed inside the same
# second as a stamp was reported as broken.
#
# By MEMBERSHIP, not equality (#206). The first cut stored one id, last-write-
# wins, which answers "which session most recently made a tool call" — a
# different question, and the two come apart the instant anything writes after
# X did. Two real installs found the seam:
#
#   * `/clear` does not mint a new session id. SessionStart fires while the
#     transcript, the id and the .jsonl all stay the same, and by then the
#     CURRENT session has made tool calls, so the slot holds the current id
#     while PREV_ID resolves to a genuinely older session. The mismatch was
#     structurally guaranteed, regardless of capture health — a warning on
#     every /clear, forever. Same for `compact` and `fork`.
#   * A session captured by the NEXT session's recovery block rather than its
#     own live saves. save-session.sh's recovery path never touches
#     capture-alive, so the slot names something else entirely, while
#     last-save.json records the session as fully saved. With the default
#     delta/cooldown thresholds this is the ordinary case for a short session,
#     not an edge case. (Reported independently by ca-sringert on #206.)
#
# Filtering on SessionStart's `source` — the fix the issue proposed — would
# silence the first and not the second, and would leave the store still unable
# to answer the question it is asked. So the store changed shape instead: a
# per-session marker directory, membership-tested. Three sources are accepted
# as evidence of capture, because a false positive here costs more than a
# false negative: this warning has no second chance to be believed, whereas a
# missed gap is still caught by /remember:doctor and, for the content itself,
# by the recovery block directly above.
#
# Deliberately NOT gated on "have we run before". A first cut required a prior
# session-start stamp, to keep a fresh install from being greeted with a
# warning — which sounds right and defeats the entire purpose: during a
# mid-session enable NO hook runs, so no stamp is written, so the one incident
# this exists to report was the exact case it stayed silent for. It could only
# ever have caught a recurrence.
#
# So the question is just "was the previous session captured", and the answer
# is reported whether or not this plugin has run before. A fresh install does
# see it once per project, which is honest: memory really does start here, and
# the wording says so.
CAPTURE_ALIVE="$REMEMBER_DIR/tmp/capture-alive"
CAPTURE_SEEN_DIR="$REMEMBER_DIR/tmp/capture-alive.d"
CAPTURE_REPORTED="$REMEMBER_DIR/tmp/capture-gap-reported"
CAPTURE_SEEN_KEEP=200

# $(<"$f") -- bash's own special-cased "no fork" command substitution,
# not $(cat "$f" 2>/dev/null) (#679, part of #660): `$(<file)` is ONLY that
# fast path when the substitution's body is EXACTLY `<file` and nothing
# else -- `$(2>/dev/null <file)` looks similar but is an ordinary subshell
# running NO command with two redirections, so it always prints nothing at
# all, file-present or not (a real, reproduced bug in an earlier version
# of this fix: it read as correct on the missing-file path and silently
# broke the present-file one, caught by the full suite rather than by this
# file's own targeted test, which never exercised a file that actually has
# content). So: guard the missing/unreadable case with `[ -f ]` first,
# exactly as this codebase already does everywhere else on this pattern,
# and use the untouched `$(<file)` form only once existence is known.
SEEN_ID=""
[ -f "$CAPTURE_ALIVE" ] && SEEN_ID=$(<"$CAPTURE_ALIVE")

# Args: $1 — session id. Exit 0 if anything can vouch for it having been
# captured. Any one source suffices; they fail independently.
capture_was_seen() {
    # bracket ranges below are byte-wise, not collated (#695)
    local LC_ALL=C
    local _d _ok
    [ -n "$1" ] || return 1
    # 1. Per-session marker from the PostToolUse hook — "PostToolUse ran for this
    #    session", written pre-throttle, so it means WIRED, not saved.
    #    Same id check the writer applies: this is a basename off the
    #    transcript dir, and `..` would make `-e` true for every id.
    #    #898 round 20: the dot comes from printf and the allowed set is a
    #    positive ERE -- the same test as before (`.`/`..` refused, anything
    #    outside A-Za-z0-9 dot underscore hyphen refused), in a shape the
    #    directory's scanner can follow.
    printf -v _d '\056'
    _ok="^[A-Za-z0-9${_d}_-]*\$"
    if [ "$1" = "$_d" ] || [ "$1" = "$_d$_d" ]; then
        :
    elif ! [[ "$1" =~ $_ok ]]; then
        :
    else
        [ -e "$CAPTURE_SEEN_DIR/$1" ] && return 0
    fi
    # 2. The legacy single slot. Kept as evidence rather than dropped so the
    #    first run after an upgrade — old file present, new store empty — is
    #    not itself a false positive. It can still speak for exactly one
    #    session, which is all it ever could.
    [ "$SEEN_ID" = "$1" ] && return 0
    # 3. The save record. Covers the session captured by recovery rather than
    #    by its own live saves, which touches nothing above. Read as it stood
    #    when this hook started, BEFORE the recovery fork above (#270) — asking
    #    again here would be asking a file that a background save is rewriting,
    #    and answering "has the rescue finished" instead of "was it captured".
    [ "$1" = "$PREV_ID" ] && [ "$PREV_WAS_SAVED" = "yes" ] && return 0
    [ "$1" != "$PREV_ID" ] && session_was_saved "$1" && return 0
    return 1
}

# Bounded: one marker per session accumulates in a tmp dir nothing else
# prunes. Newest are kept — those are the only ones the check ever reads.
#
# Args: $1 — directory, $2 — how many entries to keep. Removes the oldest
# entries by mtime until at most $2 remain; an entry rm cannot remove (a
# directory, a permission) is skipped and still counted, so the newest $2
# are never touched and the loop always ends.
#
# Gated on actually being over the threshold (#666): counting is one glob
# walk with no fork, so a store at or under the threshold costs nothing
# beyond it. Over the threshold -- normally by one, the marker the session
# that just ended wrote -- each removal is one more walk comparing mtimes
# with bash's own `-nt` builtin, again with no fork.
# #898 round 20: this replaced a glob ARRAY plus an `ls -t | tail | while
# read` pipeline. Same retention (the newest $2 by mtime); the directory's
# scanner could not follow the script past the old shape. `[ -e ]` drops
# the unmatched literal `*` an empty or absent directory leaves behind, so
# nullglob is not needed either.
_remember_prune_keep_newest() {
    local dir=$1 keep=$2 f n=0 oldest nl skip
    printf -v nl '\n'
    skip=$nl
    for f in "$dir"/*; do
        [ -e "$f" ] && n=$((n + 1))
    done
    while [ "$n" -gt "$keep" ]; do
        oldest=""
        for f in "$dir"/*; do
            [ -e "$f" ] || continue
            [[ "$skip" == *"$nl$f$nl"* ]] && continue
            if [ -z "$oldest" ] || ! [ "$f" -nt "$oldest" ]; then
                oldest=$f
            fi
        done
        [ -n "$oldest" ] || break
        rm -f "$oldest" 2>/dev/null
        [ -e "$oldest" ] && skip="$skip$oldest$nl"
        n=$((n - 1))
    done
    return 0
}
_remember_prune_keep_newest "$CAPTURE_SEEN_DIR" "$CAPTURE_SEEN_KEEP"

# PREV_ID and PREV_JSONL were resolved once, above, for this check and for
# recovery both. Guard against the honest zero-tool session too: a conversation
# with no tool calls produces no PostToolUse either, and warning about that
# would be crying wolf.
CAPTURE_SKIPPED="$REMEMBER_DIR/tmp/capture-gap-skipped"

if [ -z "$CURRENT_SESSION_ID" ]; then
    # Without a session id this check cannot tell our own transcript from the
    # previous session's, and its output is an accusation. A missed gap is
    # still caught by /remember:doctor and, for the content itself, by the
    # recovery block above; a false one accuses a healthy install and spends
    # the credibility this warning needs the one time it is true. It has no
    # second chance to be believed — the same asymmetry that made three
    # independent sources count as evidence of capture rather than one.
    #
    # Silence is a positive claim too, so it is disclosed rather than assumed.
    # "No warning" must not mean both "the previous session was captured" and
    # "the question was never asked": an absence the checker produced, read as
    # an absence in the world, is the defect class this repo keeps filing on
    # (#144, #263, #266). doctor reports the marker.
    printf '%s\n' "the SessionStart payload carried no usable session_id" \
        > "$CAPTURE_SKIPPED" 2>/dev/null || true
    log "hook" "session-start: capture-gap check skipped -- no session_id on stdin"
else
    rm -f "$CAPTURE_SKIPPED" 2>/dev/null || true

    # A gap is a fact about one past session, not a live condition, so it is
    # said once. Restarts are frequent and each re-examines the same previous
    # session; repeating the same true positive on every one is how it gets
    # tuned out. (This dedupe was structurally unable to help while the id was
    # wrong: a wrong id changes on every startup, so every restart minted a
    # fresh unreported id and warned again.)
    # $(<"$f") -- see SEEN_ID's own comment above (#679) for why this is
    # NOT `$(2>/dev/null <"$f")`.
    REPORTED_ID=""
    [ -f "$CAPTURE_REPORTED" ] && REPORTED_ID=$(<"$CAPTURE_REPORTED")

    if [ -n "$PREV_ID" ] && [ "$REPORTED_ID" != "$PREV_ID" ] \
       && ! capture_was_seen "$PREV_ID" \
       && grep -q '"tool_use"' "$PREV_JSONL" 2>/dev/null; then
        # Two wordings (#745): the flat "not captured, run doctor" notice
        # contradicts /remember:doctor's own "capture is working" verdict once
        # the recovery fork above finishes and last-save.json records the
        # rescue -- a reporter's save landed 9h after the session ended, long
        # after this notice had already been written and read. When THIS run
        # kicked off that fork for PREV_ID, say so instead of leaving the two
        # disagree.
        _capture_gap_notice="remember: your previous session was not captured. If you just installed or enabled the plugin, that is expected -- capture starts now. Otherwise its hooks were not registered for that session; run /remember:doctor."
        if [ "$_recovery_attempted" = "true" ]; then
            _capture_gap_notice="remember: your previous session was not captured live -- its hooks did not run. It is being recovered from its transcript now, so nothing should be lost; if this keeps happening for interactive sessions, run /remember:doctor."
        fi
        echo "$_capture_gap_notice" \
            > "$REMEMBER_DIR/tmp/capture-gap-notice" 2>/dev/null || true
        printf '%s' "$PREV_ID" > "$CAPTURE_REPORTED" 2>/dev/null || true
    fi
fi
