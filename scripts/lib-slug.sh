#!/usr/bin/env bash
#
# lib-slug.sh — session_dir_slug, in ONE place (#158).
#
# This used to live in detect-tools.sh, with a second, naive copy inlined in
# lib-memory-dir.sh for callers that had not sourced it. That copy was the
# pre-#144 implementation and kept every bug #156 fixed — and it was not dead:
# user-prompt-hook.sh reaches lib-memory-dir.sh without detect-tools.sh, so in
# external-storage mode ({slug} in data_dir) that one hook resolved a
# DIFFERENT REMEMBER_DIR than every other. Split-brain memory rather than a
# clean failure, which is harder to notice than a silent no-op.
#
# Sourcing detect-tools.sh from lib-memory-dir.sh is not the fix: detect-tools
# runs Python detection at source time and calls `exit 1` when it finds none,
# which would take down whatever sourced it. Hence a file of its own.
#
# The Python side of this algorithm lives in pipeline/slug.py, which is also
# what this calls for the over-long-path hash below.

[ -n "${_REMEMBER_LIB_SLUG_LOADED:-}" ] && return 0
_REMEMBER_LIB_SLUG_LOADED=1

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
    local cont
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

# Where Claude Code keeps its session transcripts. CLAUDE_CONFIG_DIR relocates
# that whole tree, projects/ included — people use it to run a separate account
# per project — and the plugin used to hardcode ~/.claude, so it listed a
# directory with no current transcripts and the save pipeline silently no-oped
# (issue #166: five days, ~2000 hook invocations, zero saves, nothing in the
# logs). Worse than nothing found: a stale transcript left in the default tree
# with enough lines would have been summarized into memory as if it were the
# live session.
claude_projects_dir() {
    local _root="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

    # CLAUDE_CONFIG_DIR is inherited from the environment, so on Windows it
    # arrives in whatever form the user typed — usually the native
    # `C:\Users\x\.claude-alt`. Concatenating "/projects" onto that produced a
    # mixed-separator path, while PROJECT_DIR gets converted deliberately
    # before it is slugged. MSYS translates both forms in syscalls, so this may
    # never have failed — but "probably fine" is what the last four
    # session-directory bugs were, and each one surfaced as memory that simply
    # stopped saving (#144, #166, #157, #169). One conversion costs nothing.
    if command -v cygpath >/dev/null 2>&1; then
        local _converted
        _converted=$(cygpath -u "$_root" 2>/dev/null) && [ -n "$_converted" ] \
            && _root="$_converted"
    fi

    # A trailing separator would give "…//projects", which is harmless on POSIX
    # and not always harmless elsewhere. Strip both kinds; the loop matters
    # because a path can end in more than one.
    #
    # With a floor: a value that is nothing BUT separators would otherwise strip
    # to the empty string and turn "/projects" into a path at the filesystem
    # root. `\` alone is a relative path, and silently promoting it to an
    # absolute one is the kind of quiet reinterpretation every bug in this file
    # has been. Nothing left to keep means keep what we were given.
    local _stripped="$_root"
    while [ "${_stripped%[/\\]}" != "$_stripped" ]; do
        _stripped="${_stripped%[/\\]}"
    done
    if [ -n "$_stripped" ]; then
        _root="$_stripped"
    else
        case "$_root" in
            # "/" or "///" — the filesystem root, and "/projects" is what that
            # means. Empty is the right value to append to.
            /*) _root="" ;;
            # "\" alone is a RELATIVE path. Stripping it to nothing would turn
            # it into an absolute one, quietly pointing somewhere else entirely,
            # so it stays exactly as it arrived.
            *) ;;
        esac
    fi

    printf '%s/projects' "$_root"
}

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
# Should this path be checked for ill-formed UTF-8 (#186)?
#
# Only where such a path can exist. macOS enforces well-formed UTF-8 in
# filenames and Windows paths come from UTF-16; Linux enforces nothing, so it is
# the only platform that can hand us one. $OSTYPE is a bash variable, not a
# fork, and that matters. Measured with scripts/bench-slug.sh (macOS, bash 3.2,
# 200 calls each) — absolute numbers are machine-specific and subprocess-spawn
# dominated; the ratios are the point:
#
#   platform      ASCII    valid non-ASCII    ill-formed
#   non-Linux     2.1ms    2.1ms              2.2ms        (never checked)
#   Linux         2.0ms    8.7ms              71ms         (iconv, then python)
#
# So an ASCII path pays nothing anywhere, a macOS or Windows user pays nothing
# at all, and the ~6.5ms belongs only to a Linux user with a non-ASCII path —
# where the bug is actually reachable. The 71ms case is a path that is already
# broken and would otherwise have saved nothing at all.
#
# REMEMBER_UTF8_STRICT=1 forces it on anyway. Without that the behaviour would
# be untestable everywhere except a Linux runner, and a fix nobody can exercise
# locally is a fix nobody maintains.
_remember_should_check_utf8() {
    [ "${REMEMBER_UTF8_STRICT:-0}" = "1" ] && return 0
    # ${OSTYPE:-}: bash always sets it, but this file is sourced by callers
    # running under `set -u`, and one unguarded expansion there aborts the hook.
    case "${OSTYPE:-}" in
        linux*) return 0 ;;
    esac
    return 1
}

# The two alphabets, for the drive-letter fold below. Held as constants so the
# fold is a pair of parameter expansions and not a fork: session_dir_slug runs
# on every single tool call.
#
# `${x,,}` would be one expansion and is what this used to use — but it is bash
# 4 syntax, a runtime "bad substitution" on macOS's bash 3.2. The CYGPATH_STUB
# in tests/test_config_dir_normalisation.py already avoids it for exactly that
# reason; used here it made the cygpath branch unrunnable on the platform this
# is developed on, so that branch had no local coverage at all.
_REMEMBER_DRIVE_UPPER="ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_REMEMBER_DRIVE_LOWER="abcdefghijklmnopqrstuvwxyz"

session_dir_slug() {
    local path="$1"
    local _drive_at
    if command -v cygpath >/dev/null 2>&1; then
        local winpath
        winpath=$(cygpath -w "$path" 2>/dev/null) || winpath="$path"
        path="$winpath"
    fi
    # Lowercase the drive letter to match Claude Code — unconditionally, not
    # only when cygpath happens to be on PATH (#263).
    #
    # That condition is what let one machine produce both spellings. With
    # cygpath present every shape converges here and there is no bug; with it
    # absent this step was skipped entirely, so a path resolve-paths.sh had
    # uppercased stayed uppercase while a shape it could not match stayed
    # lowercase. The store split in two, and only git could see it.
    #
    # The direction is not a free choice. Git for Windows ships cygpath, so the
    # working majority already has the lowercase spelling on disk; uppercasing
    # would fix one reporter and rename every other store — trading a loud bug
    # for a quiet one. Only the drive letter is touched: every component after
    # it keeps its case, on Windows and in Claude Code's slug alike.
    # `?:*` and not `[A-Z]:*`: a bracket range in a case pattern follows the
    # locale's collation, and under en_US.UTF-8 `[A-Z]` matches lower-case
    # letters too — the same trap that made the slug sed locale-proof by hand
    # above. The membership test below is the range check, done with a literal
    # substring and therefore collation-free: a first character that is not an
    # upper-case ASCII letter leaves the whole prefix intact and nothing is
    # folded. Without that, a locale-widened match would index past the end of
    # the lower-case alphabet and silently DELETE the drive letter.
    case "$path" in
        ?:*)
            _drive_at="${_REMEMBER_DRIVE_UPPER%%"${path:0:1}"*}"
            if [ "$_drive_at" != "$_REMEMBER_DRIVE_UPPER" ]; then
                path="${_REMEMBER_DRIVE_LOWER:${#_drive_at}:1}${path:1}"
            fi
            ;;
    esac
    # The UTF-8 well-formedness table, one expression per row. Ranges matter:
    # a lead byte does not accept every continuation. \355 (U+D800-DFFF, the
    # surrogate block) and the overlong \340/\360 forms are not valid UTF-8,
    # and the decoder emits one replacement character per byte for them — so
    # they must fall through to the catch-all below rather than collapse here.
    # Each lead also takes EXACTLY its own continuations: an unbounded run
    # would swallow the lone continuation bytes after a valid sequence, which
    # again are one replacement character each.
    #
    # Verified against the real regex under node over several thousand generated
    # paths: identical for ALL well-formed UTF-8. What still differs is a lead
    # byte not followed by the continuations it requires — anywhere in the
    # string, not only at the end. The decoder folds that into a single
    # replacement character by the maximal-subpart rule; this gives one dash per
    # byte. Expressing it needs the decoder's state machine, which sed does not
    # have.
    #
    # macOS enforces well-formed UTF-8 and Windows paths come from UTF-16, but
    # Linux enforces nothing: every byte except / and NUL is a legal filename,
    # so a legacy-encoded or corrupted directory name reaches this. Tracked in
    # #186. When it happens the slug misses and the missing-session-directory
    # warning says so, rather than the pipeline sitting silent — which is the
    # difference that matters.
    # A raw newline is a legal byte in a POSIX filename (only / and NUL are
    # not) and is sed's own record separator, so it splits the input before any
    # s/// rule sees it and would survive into the slug untouched — missing the
    # directory exactly the way the locale bug did. Convert it here, where bash
    # still has the string whole.
    local _orig="$path"

    # The byte table below is exact for well-formed UTF-8 and only for that. A
    # lead byte missing its continuations gets one dash per byte here, where the
    # real decoder folds it into a single replacement character by the
    # maximal-subpart rule. macOS enforces well-formed UTF-8 and Windows paths
    # come from UTF-16, but Linux enforces nothing — every byte except / and NUL
    # is a legal filename, so a legacy-encoded or corrupted directory name gets
    # here (#186).
    #
    # Expressing maximal-subpart needs the decoder's state machine, which sed
    # does not have and Python does. So: ask Python, but only for a path that is
    # both non-ASCII AND actually malformed. An all-ASCII path — the overwhelming
    # majority, and this function runs on every tool call — never even reaches
    # the `iconv` check, let alone the subprocess.
    if _remember_should_check_utf8; then
    # Any byte at or above 0x80 — anything that could be part of a multi-byte
    # sequence. The earlier form (`[!$' \t'-~]`) also caught DEL and the C0
    # controls, which are always valid single-byte ASCII and could never be what
    # this looks for; they paid for a check that can only answer yes.
    #
    # The range starts at \001, not \000, and that is not a style choice: bash
    # cannot hold a NUL in a string, so $'\000' expands to NOTHING and the
    # bracket degenerates into `[!-\177]` — "any character except - and DEL" —
    # which matches essentially every path. The tightest-looking form of this
    # test is the loosest one, and it costs a fork per tool call. A NUL cannot
    # appear in an argv string anyway, so starting at \001 loses nothing.
    # Detect under LC_ALL=C, then act.
    #
    # What is established: on the macOS CI runners this same expression matched
    # plain ASCII paths and forked `iconv` on every one, while answering
    # correctly on a local macOS box. Three consecutive runs failed
    # (30235721355, 30236170724, 30237326112) and the run that added LC_ALL=C
    # passed (30237861167), all on the same test.
    #
    # What is NOT established is the mechanism. The obvious candidate is that a
    # bracket RANGE is matched by collation order rather than byte value — the
    # same trap this file already documents for `[a-z]` in the sed program
    # below — but a sweep of all 55 installed locales under bash 3.2 failed to
    # reproduce it, so the runner must differ in bash build or locale data in
    # some way not identified. Forcing byte semantics fixes it either way and
    # costs nothing; the honest label is "empirically necessary, mechanism
    # unconfirmed".
    #
    # The result is stashed in a flag rather than acted on inside the `case`,
    # because the branch below can return early and would leave the caller's
    # locale changed.
    local _high_byte=0 _lc_was_set="${LC_ALL+set}" _lc_prev="${LC_ALL:-}"
    LC_ALL=C
    case "$path" in
        *[!$'\001'-$'\177']*) _high_byte=1 ;;
    esac
    if [ -n "$_lc_was_set" ]; then LC_ALL="$_lc_prev"; else unset LC_ALL; fi

    case "$_high_byte" in
        1)
            if command -v iconv >/dev/null 2>&1 \
                && ! printf '%s' "$path" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; then
                local _py_slug="${PIPELINE_DIR:-}/pipeline/slug.py"
                if [ -f "$_py_slug" ]; then
                    local _decoded
                    _decoded=$("${PYTHON:-python3}" "$_py_slug" "$path" 2>/dev/null) \
                        && [ -n "$_decoded" ] && { printf '%s\n' "$_decoded"; return 0; }
                fi
                # No Python to ask: fall through to the byte table, which is
                # wrong for this input in a known and documented way rather than
                # failing.
            fi
            ;;
    esac
    fi

    path=${path//$'\n'/-}
    local _slug
    _slug=$(printf '%s\n' "$path" | LC_ALL=C sed "${_REMEMBER_SLUG_SED[@]}")

    # Past 200 characters Claude Code keeps the first 200 and appends a hash of
    # the ORIGINAL path. Nothing here truncated, so any project whose slugged
    # path was longer computed a directory Claude Code never created: the hook
    # found no transcript and memory silently never saved (#157). Reachable
    # without anything exotic — deep module nesting under a long home directory
    # gets there.
    #
    # The hash needs signed-32-bit wraparound and base36, which bash does not
    # do well, so it comes from pipeline/slug.py — the same implementation the
    # Python side uses, rather than a second transcription of the algorithm.
    # Only over-long paths pay the subprocess; the common path never forks.
    if [ ${#_slug} -le 200 ]; then
        printf '%s\n' "$_slug"
        return 0
    fi

    local _hash _slug_py="${PIPELINE_DIR:-}/pipeline/slug.py"
    if [ -f "$_slug_py" ]; then
        _hash=$("${PYTHON:-python3}" "$_slug_py" --hash "$_orig" 2>/dev/null) || _hash=""
    else
        _hash=""
    fi

    # The hash comes from another file resolved through PIPELINE_DIR, so a stale
    # or wrong-version plugin copy could return anything at all and it would be
    # appended verbatim — a NEW wrong directory rather than the old wrong one.
    # Base36 is the whole alphabet a real hash can use, so anything else is not
    # one, and falling back is safer than trusting it.
    case "$_hash" in
        *[!0-9a-z]*) _hash="" ;;
    esac

    # No usable hash (no Python, no plugin dir): emit the untruncated slug —
    # wrong, but exactly as wrong as before, and the missing-session-directory
    # warning (#156) will say so rather than leaving it invisible.
    if [ -z "$_hash" ]; then
        printf '%s\n' "$_slug"
        return 0
    fi
    printf '%s-%s\n' "${_slug:0:200}" "$_hash"
}
