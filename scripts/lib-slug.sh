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
session_dir_slug() {
    local path="$1"
    if command -v cygpath >/dev/null 2>&1; then
        local winpath
        winpath=$(cygpath -w "$path" 2>/dev/null) || winpath="$path"
        # Lowercase the drive letter (first character) to match Claude Code.
        path="${winpath:0:1}"
        path="${path,,}${winpath:1}"
    fi
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
