#!/usr/bin/env python3
"""Inline a hooks.json-registered hook script's `source`/`.` chain (#900).

The Anthropic plugin directory's release-preview validator inspects only the
command a `hooks/hooks.json` entry names; it never follows a `source`/`.`
statement into a second file, so a hook that pulls in shared library code
that way is held as COMMAND_SCRIPT_NOT_FOLLOWED (jit-context's own
directory-validator write-up, #900). `build_release_tree.py` calls
`compile_hook` on each of the four HOOK_SCRIPT_NAMES scripts before writing
the release tree. Every file in a hook's own `source`/`.` closure becomes
ONE function in the compiled file, defined right after the shebang and
named after the file (`scripts/lib-slug.sh` -> `__remember_src_lib_slug`);
every `source "$X/lib-slug.sh"` statement, wherever it sits, becomes a call
to that function with the same arguments `source` would have passed
(`__remember_src_lib_slug ${1+"$@"}`), everything else on the line kept
exactly as written. Comment-only lines are then stripped so the compiled
file fits the directory's 256 KiB per-file budget; a shebang line (only ever
the file's own first line), a heredoc body, and anything inside an open
quoted string are never touched.

Why a function per file, and not the file's text pasted at each `source`
site (round 4 -- the compiled-hooks CI leg caught the pasted form breaking
post-tool-hook.sh on every cold cache): a function call keeps what `source`
does AT RUN TIME, and pasting does not.

* **Which branch runs.** A library sourced on two branches (post-tool-hook's
  fast path sources lib-slug.sh, its slow path sources detect-tools.sh,
  which sources lib-slug.sh again) is loaded by whichever branch runs. A
  build-time "already inlined" guard cannot know which one that is; pasting
  once and replacing the rest with `:` left the slow path with no
  lib-slug.sh, no lib-memory-dir.sh and no log.sh at all. With one
  definition and a call at every site, each library's own runtime guard
  (`[ -n "${X_LOADED:-}" ] && return 0`) decides, exactly as it does today.
* **`return`.** A sourced file's top-level `return` ends the SOURCING; that
  is how every library's load guard works, and how resolve-paths.sh's soft
  failure (`return 1`, caught by the hook's `|| exit 0`) works. Pasted at a
  script's top level, `return` is an error bash prints and steps over --
  resolution failure carried on with an empty PROJECT_DIR. Pasted inside a
  function (the fast path's lazy log() stub), it returned from THAT
  function. Inside the per-file function it ends exactly what `source`
  ended, and its status reaches the caller's `||` the same way.
* **An assignment prefix** (`REMEMBER_PATHS_SOFT_FAIL=1 source ...`) lasts
  for the duration of `source` in bash's default mode, and for the duration
  of a function call the same way; pasting had to hoist it into a statement
  that outlived the call.

So no trailing guard (`|| exit 0`, `2>/dev/null`) and no gate (`COND ||`)
is dropped or rewritten any more: each applies to the function call exactly
as it applied to `source`. Two differences between a function and a sourced
file remain, and neither occurs in a library here: a top-level
`local`/`declare` would become local to the function (refused loudly,
InlineError, by `_function_unsafe_lines`), and `${BASH_SOURCE[0]}` names the
compiled hook rather than the library -- the same scripts/ directory, which
is all any library here derives from it.

The detector below never tests any text inside a quote or a heredoc body:
it walks the file once, tracking quote/heredoc state exactly as
`strip_whole_line_comments` does, and only looks for a source statement
within an UNQUOTED segment of a line, at a position that is genuinely a
shell command boundary (self-review on round 1 found a line-wide regex
matching INSIDE an unrelated single-quoted jq filter --
`scripts/lib-memory-dir.sh`'s merge-config script contains "; . * $x").

A `source`/`.` whose target is not a plain double-quoted string (a bare
`source $X/lib.sh` or a single-quoted one) does not occur anywhere in this
repo's own scripts today, but is refused loudly (InlineError) rather than
silently passed through unresolved if one ever appears.

The *source tree* keeps every hook script and library exactly as written --
this module changes only what a compiled hook's TEXT looks like; callers
decide where that text ends up (the release tree, or a CI scratch copy used
to run the existing hook test suites against a compiled hook -- see
docs/releasing.md).

This file is importable standalone (`python3 compile_hooks.py --repo .`
prints each hook's compiled size without writing anything) and is imported by
both build_release_tree.py (to produce the shipped bytes) and
check_release_tree.py (HOOK_SCRIPT_NAMES and unresolved_sources, to FAIL a
shipped hook that still sources something -- see that file's
_check_hook_still_sources)."""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
from pathlib import Path

# The four hooks.json-registered scripts this module compiles. Scoped to
# exactly these -- what the directory's hold actually named -- not every
# script under scripts/ that happens to `source` a sibling: a non-hook
# script (save-session.sh, doctor.sh, ...) still sources normally in both
# the source tree and the release tree, and that is fine; the directory
# never inspects it because hooks.json never names it as a command.
HOOK_SCRIPT_NAMES = ("session-start-hook.sh", "session-end-hook.sh",
                     "user-prompt-hook.sh", "post-tool-hook.sh")


class InlineError(Exception):
    """A hook's source chain could not be safely inlined."""


# -- pattern pieces --------------------------------------------------------

_ASSIGN_WORD_SRC = (
    r'[A-Za-z_][A-Za-z0-9_]*=(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s"\']*)'
)
_SOURCE_BODY = (
    r'(?P<assigns>(?:' + _ASSIGN_WORD_SRC + r'[ \t]+)*)'
    r'(?P<kw>source|\.)[ \t]+"(?P<target>[^"\n]*)"(?P<rest>.*)$'
)
_SOURCE_UNHANDLED_BODY = (
    r'(?:' + _ASSIGN_WORD_SRC + r'[ \t]+)*'
    r'(?:source|\.)[ \t]+(?!")\S'
)

# A candidate statement-start position is the true start of the line
# (offset 0) or the position right after a command separator (';', '{',
# '(', '|', '||', '&&') -- each found by scanning only WITHIN an unquoted
# segment of the line (computed by _unquoted_segments). The actual match
# attempt below then runs against the real line text from that position
# onward, so the statement's own quoted target is visible to the regex
# exactly as written; only the SEARCH for where a statement could start
# ever avoids quoted text, never the statement's own argument.
_SEGMENT_SEPARATOR = re.compile(r'\|\||&&|[;{(|]')
_SOURCE_BODY_ONLY = re.compile(r'[ \t]*' + _SOURCE_BODY)
_SOURCE_UNHANDLED_BODY_ONLY = re.compile(r'[ \t]*' + _SOURCE_UNHANDLED_BODY)

_TRAILING_SH_NAME = re.compile(r'[A-Za-z0-9_.\-]+\.sh$')

# Every function a compiled hook defines for one of its sourced files starts
# with this, so tree_shake can tell such a wrapper (whose body is a
# library's TOP-LEVEL code, run whenever the wrapper is called) from an
# ordinary function.
WRAPPER_PREFIX = "__remember_src_"
# Written as the compiled file's second line (right after the shebang).
# tests/_compiled_hooks.py keys off the same string so a source-text pin can
# tell a compiled hook (the compiled CI leg compiles in place) from source.
COMPILED_MARKER = "# Compiled by .github/scripts/compile_hooks.py (#900)"
# What `source FILE` passes FILE when given no arguments of its own: the
# caller's positional parameters, unchanged. `${1+"$@"}` rather than `"$@"`
# because bash 3.2 (macOS /bin/bash) under `set -u` calls a bare "$@" with
# no positional parameters an unbound variable.
_CALL_ARGS = '${1+"$@"}'


def target_basename(target: str) -> str | None:
    """The trailing `name.sh` filename TARGET resolves to, or None if it
    does not end in a plain `.sh` name (nothing to look up under
    scripts/)."""
    m = _TRAILING_SH_NAME.search(target)
    return m.group(0) if m else None


def wrapper_name(key: str) -> str:
    """The function a compiled hook defines for CONTENTS key KEY
    (`scripts/lib-slug.sh` -> `__remember_src_lib_slug`)."""
    base = posixpath.basename(key).removesuffix(".sh")
    return WRAPPER_PREFIX + re.sub(r'[^A-Za-z0-9_]', '_', base)


# -- quote/heredoc-aware scanning (shared by inlining and comment stripping) --

def _scan_line(line: str, in_squote: bool, in_dquote: bool):
    """Scan one line of shell, carrying quote state in from the previous
    line. Returns (heredoc_term, heredoc_strip_tabs, in_squote, in_dquote):
    the heredoc this line opens (None if none), and the quote state this
    line leaves open for the next one. A `#` reached while not inside any
    quote ends the scan for this line (the rest is a real, trailing
    comment -- inline comments are never stripped, only whole-line ones;
    scanning stops because nothing after a real `#` can start a heredoc or
    a quote that matters)."""
    i, n = 0, len(line)
    heredoc_term = None
    heredoc_strip_tabs = False
    while i < n:
        c = line[i]
        if in_squote:
            if c == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_dquote = False
            i += 1
            continue
        if c == "#":
            break
        if c == "'":
            in_squote = True
            i += 1
            continue
        if c == '"':
            in_dquote = True
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if heredoc_term is None and line[i:i + 2] == "<<":
            j = i + 2
            strip_tabs = False
            if j < n and line[j] == "-":
                strip_tabs = True
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            quote_char = line[j] if j < n and line[j] in "'\"" else None
            if quote_char:
                j += 1
            start = j
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            ident = line[start:j]
            if quote_char and j < n and line[j] == quote_char:
                j += 1
            if ident:
                heredoc_term, heredoc_strip_tabs = ident, strip_tabs
            i = j
            continue
        i += 1
    return heredoc_term, heredoc_strip_tabs, in_squote, in_dquote


def _unquoted_segments(line: str, in_squote: bool, in_dquote: bool):
    """The unquoted (start, end) ranges of LINE, given incoming quote
    state -- plus (heredoc_term, heredoc_strip_tabs, out_squote,
    out_dquote), the same outputs `_scan_line` reports. A segment that
    starts at offset 0 is the true start of the line (a valid statement
    boundary on its own); any later segment starts right after a quote
    closed mid-line, which is NOT a statement boundary by itself -- only an
    explicit separator inside that segment is."""
    segments = []
    seg_start = 0 if not (in_squote or in_dquote) else None
    i, n = 0, len(line)
    heredoc_term = None
    heredoc_strip_tabs = False
    while i < n:
        c = line[i]
        if in_squote:
            if c == "'":
                in_squote = False
                seg_start = i + 1
            i += 1
            continue
        if in_dquote:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_dquote = False
                seg_start = i + 1
            i += 1
            continue
        if c == "#":
            if seg_start is not None:
                segments.append((seg_start, i))
                seg_start = None
            break
        if c == "'":
            if seg_start is not None:
                segments.append((seg_start, i))
                seg_start = None
            in_squote = True
            i += 1
            continue
        if c == '"':
            if seg_start is not None:
                segments.append((seg_start, i))
                seg_start = None
            in_dquote = True
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if heredoc_term is None and line[i:i + 2] == "<<":
            j = i + 2
            strip_tabs = False
            if j < n and line[j] == "-":
                strip_tabs = True
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            quote_char = line[j] if j < n and line[j] in "'\"" else None
            if quote_char:
                j += 1
            start = j
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            ident = line[start:j]
            if quote_char and j < n and line[j] == quote_char:
                j += 1
            if ident:
                heredoc_term, heredoc_strip_tabs = ident, strip_tabs
            i = j
            continue
        i += 1
    if seg_start is not None:
        segments.append((seg_start, n))
    return segments, heredoc_term, heredoc_strip_tabs, in_squote, in_dquote


def _candidate_starts(line: str, seg_start: int, seg_end: int):
    """(sep_start, match_start, sep_text) for every position within
    [seg_start, seg_end) of LINE where a new shell command could start:
    SEG_START itself (sep_text="") if it is the true start of the line,
    plus the position right after every separator token found within the
    segment, in order."""
    cands = []
    if seg_start == 0:
        cands.append((0, 0, ""))
    for m in _SEGMENT_SEPARATOR.finditer(line, seg_start, seg_end):
        cands.append((m.start(), m.end(), m.group(0)))
    return cands


def _find_source_in_line(line: str, in_squote: bool, in_dquote: bool):
    """(match_or_"unhandled"_or_None, sep_start, sep_text, meta) for the
    first genuine source/. statement found in LINE. The search for WHERE a
    statement could start only ever looks within an unquoted segment of
    the line; the match itself runs against the real line text from that
    position onward (so the statement's own quoted target, itself a
    quote, is seen normally by the regex -- only unrelated quoted text
    elsewhere on the line is skipped). meta is always returned so callers
    can advance quote/heredoc state regardless of whether a match was
    found."""
    segments, heredoc_term, heredoc_strip_tabs, out_sq, out_dq = _unquoted_segments(
        line, in_squote, in_dquote)
    meta = (heredoc_term, heredoc_strip_tabs, out_sq, out_dq)
    for seg_start, seg_end in segments:
        for sep_start, cand, sep_text in _candidate_starts(line, seg_start, seg_end):
            m = _SOURCE_BODY_ONLY.match(line, cand)
            if m:
                return m, sep_start, sep_text, meta
            if _SOURCE_UNHANDLED_BODY_ONLY.match(line, cand):
                return "unhandled", sep_start, sep_text, meta
    return None, None, None, meta


def unresolved_sources(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every remaining `source`/`.` statement in
    TEXT -- what a hook that did not fully compile still carries. Used by
    check_release_tree.py's FAIL guard; exposed here too so a build-time
    sanity check and the release-tree check apply the identical,
    quote/heredoc-aware pattern (a bare line-wide regex would also flag a
    jq/awk script embedded in a shipped file's own quoted arguments)."""
    found = []
    in_squote = in_dquote = False
    heredoc_term = None
    heredoc_strip_tabs = False
    for n, line in enumerate(text.split("\n"), 1):
        if heredoc_term is not None:
            check = line.strip() if heredoc_strip_tabs else line
            if check == heredoc_term:
                heredoc_term = None
            continue
        m, _sep_start, _sep_text, meta = _find_source_in_line(line, in_squote, in_dquote)
        heredoc_term, heredoc_strip_tabs, in_squote, in_dquote = meta
        if m is not None:
            found.append((n, line))
    return found


# -- comment stripping -------------------------------------------------------

def strip_whole_line_comments(text: str) -> str:
    """Drop every comment-only line (first non-whitespace character `#`)
    from a shell script, except the file's own first line when it is a
    shebang. Never touches a heredoc body, or a line whose leading `#`
    turns out to sit inside a single/double-quoted string left open by an
    earlier line -- quote and heredoc state is tracked across the whole
    file, not just within one line, so a multi-line quoted string
    containing a line that merely *looks* like a comment is kept intact."""
    drop = {n for n, _ in whole_line_comments(text)}
    return "\n".join(line for n, line in enumerate(text.split("\n"), 1) if n not in drop)


def whole_line_comments(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line) for every line strip_whole_line_comments
    drops -- the one quote- and heredoc-aware pass both the stripper and
    check_release_tree.py's shell-comment guard (#900) read, so the check
    can never disagree with the build about what a comment line is."""
    found = []
    heredoc_term = None
    heredoc_strip_tabs = False
    in_squote = in_dquote = False
    for idx, line in enumerate(text.split("\n")):
        if heredoc_term is not None:
            check = line.strip() if heredoc_strip_tabs else line
            if check == heredoc_term:
                heredoc_term = None
            continue
        if in_squote or in_dquote:
            heredoc_term, heredoc_strip_tabs, in_squote, in_dquote = _scan_line(
                line, in_squote, in_dquote)
            continue
        stripped = line.lstrip()
        if idx == 0 and stripped.startswith("#!"):
            continue
        if stripped.startswith("#"):
            found.append((idx + 1, line))
            continue
        heredoc_term, heredoc_strip_tabs, in_squote, in_dquote = _scan_line(
            line, in_squote, in_dquote)
    return found


# -- linking -------------------------------------------------------------

def link_sources(name: str, contents: dict[str, str],
                 script_dir: str = "scripts") -> tuple[str, dict[str, str]]:
    """(body, libraries) for hook NAME (a key into CONTENTS, e.g.
    "scripts/post-tool-hook.sh").

    BODY is NAME's own text with every `source`/`.` statement's
    `source "TARGET"` words replaced by a call to TARGET's wrapper function
    (`wrapper_name`) passing `${1+"$@"}` -- anything before it on the line
    (an assignment prefix, a `COND ||` gate) and after it (a redirect, an
    `|| exit 0`) is left exactly as written, since each applies to the call
    the same way it applied to `source`. LIBRARIES maps every file in NAME's
    source closure (CONTENTS key -> that file's text, rewritten the same
    way) in the order first reached; each becomes one wrapper function.

    One wrapper per file however many sites source it: whether a second
    `source` of a library is a no-op is that library's own RUNTIME guard's
    call, not this module's -- see the module docstring for what the
    build-time include guard this replaces got wrong.

    Only an UNQUOTED source/. statement is ever matched (see
    _find_source_in_line); quote and heredoc state is tracked across the
    whole file.

    Raises InlineError on a source statement this function cannot resolve
    against CONTENTS, on a cycle, or on a target that is not a plain
    double-quoted string."""
    libraries: dict[str, str] = {}

    def rewrite(key: str, stack: tuple[str, ...]) -> str:
        if key not in contents:
            raise InlineError(f"{key}: not found")
        out_lines = []
        in_squote = in_dquote = False
        heredoc_term = None
        heredoc_strip_tabs = False
        for line in contents[key].split("\n"):
            if heredoc_term is not None:
                out_lines.append(line)
                check = line.strip() if heredoc_strip_tabs else line
                if check == heredoc_term:
                    heredoc_term = None
                continue

            incoming = (in_squote, in_dquote)
            while True:
                m, _sep_start, _sep_text, meta = _find_source_in_line(line, *incoming)
                if m is None:
                    break
                if m == "unhandled":
                    raise InlineError(
                        f"{key}: a source/. statement's target is not a plain "
                        f"double-quoted string (bare or single-quoted) -- refusing "
                        f"to guess at it rather than leaving it silently unresolved: "
                        f"{line.strip()!r}"
                    )
                target = m.group("target")
                base = target_basename(target)
                if base is None:
                    raise InlineError(f"{key}: cannot resolve source target {target!r} "
                                       f"(not a plain sibling '*.sh' path)")
                candidate = posixpath.join(script_dir, base)
                if candidate not in contents:
                    raise InlineError(f"{key}: sources {candidate!r}, not present in "
                                       f"this build")
                if candidate in stack or candidate == key:
                    raise InlineError(
                        f"source cycle: {' -> '.join(stack + (key, candidate))}")
                if candidate not in libraries:
                    libraries[candidate] = ""  # reserve: first-reached order
                    libraries[candidate] = rewrite(candidate, stack + (key,))
                line = (line[:m.start("kw")] + wrapper_name(candidate) + " "
                        + _CALL_ARGS + line[m.start("rest"):])
            heredoc_term, heredoc_strip_tabs, in_squote, in_dquote = meta
            out_lines.append(line)
        return "\n".join(out_lines)

    body = rewrite(name, ())
    return body, libraries


_FUNC_START_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\(\) \{[ \t]*$')
_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
# A command position (start of line, or right after a real shell separator)
# occupied by nothing but a bare or quoted variable expansion whose NAME is
# not all-uppercase. All-caps names (PYTHON, JQ, PIPELINE_DIR, ...) are this
# codebase's own convention for an external tool or config value, never one
# of its own snake_case/leading-underscore functions, so a call through one
# of those cannot resolve to a function this analysis is shaking -- keeping
# it out of the dynamic-dispatch trigger is what lets tree-shaking do
# anything at all against real files, all four of which call $PYTHON/$JQ
# this way. Matched only against a MASKED line (quotes and comments already
# blanked out by _scan_line_braces) -- a raw per-line regex match caught a
# continuation line INSIDE a multi-line double-quoted string
# (user-prompt-hook.sh:386, `$_notice_body"`) as if it were code, which
# this masking exists specifically to rule out.
_DYNAMIC_CALL_RE = re.compile(
    r'(?:^|[;&|(]|\bthen\b|\bdo\b|\belse\b)\s*'
    r'"?\$\{?([a-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}?"?(?=\s|$)'
)


def _cmdsub_continue(line, start, depth, sq, dq):
    """The character-by-character scan _command_substitution_end uses for
    a fresh '$(', also re-entered at the start of a physical line that
    CONTINUES an unresolved '$(...)' carried from an earlier line (#900
    round 3) -- same rules, generalized to accept an already-nonzero
    DEPTH/SQ/DQ instead of always starting at depth 1. Returns (end,
    depth, sq, dq): END is the index just past the matching ')' once
    DEPTH returns to 0 on this line; otherwise None, with DEPTH/SQ/DQ the
    state to carry into the next physical line."""
    n = len(line)
    j = start
    while j < n:
        c = line[j]
        if sq:
            if c == "'":
                sq = False
            j += 1
            continue
        if dq:
            if c == "\\" and j + 1 < n:
                j += 2
                continue
            if c == '"':
                dq = False
            j += 1
            continue
        if c == "\\" and j + 1 < n:
            j += 2
            continue
        if c == "'":
            sq = True
        elif c == '"':
            dq = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1, 0, sq, dq
        j += 1
    return None, depth, sq, dq


def _command_substitution_end(line, i):
    """If line[i:i+2] is '$(' (not '$((', arithmetic expansion), scans for
    its matching ')', tracking the substitution's own LOCAL quote state
    independently of whatever quote the substitution itself sits inside --
    `$(...)` establishes its own nested lexical scope in real bash, so a
    '\"' inside it (e.g. `$(command -v "$_first" 2>/dev/null)`, embedded in
    an OUTER double-quoted string) must never toggle the outer string's own
    quote state. Treating every '\"' uniformly, with no notion of this
    nested scope, is exactly what produced a false "this text is code, not
    still inside the outer string" read during this module's own testing
    (scripts/user-prompt-hook.sh:386's `$_notice_body\"` continuation line).

    Returns (end, depth, sq, dq). END is None if there is no '$(' here (in
    which case depth/sq/dq are always 0/False/False); otherwise END is the
    index just past the matching ')' if found on this same physical line,
    or None with DEPTH/SQ/DQ set to the substitution's own open state --
    the caller carries those three into _cmdsub_continue on the next
    physical line (#900 round 3: a substitution spanning multiple lines,
    e.g. a `<<<` here-string reading a multi-line Python script inside
    `"$(... <<< '...')"`, used to fall through to this module's ordinary
    per-character handling the moment it went unresolved here -- not a
    braces/parens miscount directly, since neither is counted by this
    module's own brace-delta tracking, but a QUOTE-parity drift: the
    substitution's own internal quotes leaked into and desynced the OUTER
    quote state they are supposed to be opaque to, which silently swallowed
    one genuine top-level '}' several lines later in this repo's own
    session-start-hook.sh `session_was_saved` -- the unbalanced-braces
    false alarm this fixes)."""
    if line[i:i + 2] != "$(" or line[i:i + 3] == "$((":
        return None, 0, False, False
    return _cmdsub_continue(line, i + 2, 1, False, False)


def _scan_line_braces(line, in_squote, in_dquote, brace_stack, cmdsub=None):
    """Like compile_hooks._scan_line, but also returns the net count of
    unquoted '{' minus '}' characters on this line, and a MASKED copy of
    the line (same length) with every quoted span and real trailing
    comment replaced by spaces -- so a later scan for code-shaped patterns
    (the dynamic-dispatch check) never mistakes the contents of a string or
    comment for a command. BRACE_STACK is a list of bool, mutated in place
    and threaded across lines exactly like IN_SQUOTE/IN_DQUOTE: True means
    the innermost currently-open unquoted '{' is part of a '${' parameter
    expansion, False means an ordinary block/group open.

    CMDSUB, when not None, is a (depth, sq, dq) triple carried from a
    '$(...)' left open by the END of a PREVIOUS physical line (#900 round
    3) -- this line is scanned as that substitution's own continuation
    first (via _cmdsub_continue), before anything else on it is treated as
    ordinary bash, since the whole line still belongs to the substitution's
    nested lexical scope until its own depth returns to 0.

    That distinction is why '#' does not always start a comment: inside a
    parameter expansion (`${raw#pattern}`, `${raw##pattern}`) '#' is a
    pattern-removal OPERATOR, not a comment marker -- treating it as one
    stops the scan before the expansion's own closing '}' (and anything
    after it on the line, including a real quote), which is exactly the
    shape that produced an "unbalanced braces" false alarm against this
    repo's own scripts/session-end-hook.sh:79 (`rest=${raw#*\\"$key\\"}`)
    during this module's own testing. A '#' while the innermost open
    bracket is an ordinary block (`{ # comment`) is still a real comment,
    so the stack records WHICH kind is open, not just whether one is.

    Returns an 8-tuple: the usual (delta, heredoc_term, heredoc_strip_tabs,
    in_squote, in_dquote, masked_line, continuation), plus a new CMDSUB
    carry (None once any open substitution has resolved, else the
    (depth, sq, dq) to pass back in on the next physical line)."""
    i, n = 0, len(line)
    heredoc_term = None
    heredoc_strip_tabs = False
    delta = 0
    masked = ["\x00"] * n
    if cmdsub is not None:
        end, cs_depth, cs_sq, cs_dq = _cmdsub_continue(line, 0, *cmdsub)
        if end is None:
            # The whole line is still inside the carried-over substitution
            # -- stays masked (NUL), not revealed: unlike a same-line
            # $(...) (always a short bash expression in this codebase),
            # a substitution spanning multiple physical lines is the
            # embedded-Python/jq-script shape, and revealing THAT content
            # to the dynamic-dispatch scan below risks a foreign-language
            # token (jq's own "(" . "$lowercase_name" syntax, say)
            # matching a pattern meant only for real bash command words.
            masked_line = "".join(masked)
            return (delta, heredoc_term, heredoc_strip_tabs, in_squote,
                    in_dquote, masked_line, False, (cs_depth, cs_sq, cs_dq))
        # The substitution's own closing span stays masked for the same
        # reason -- only code AFTER "i = end" is ordinary bash again.
        i = end
    while i < n:
        c = line[i]
        if in_squote:
            if c == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            if c == "\\" and i + 1 < n:
                masked[i] = c
                masked[i + 1] = line[i + 1]
                i += 2
                continue
            if c == '"':
                in_dquote = False
                masked[i] = c
                i += 1
                continue
            if c == "$":
                cmdsub_end, cs_depth, cs_sq, cs_dq = _command_substitution_end(line, i)
                if cmdsub_end is not None:
                    # $(...) is its own nested lexical scope -- skip it as
                    # one opaque unit (masked, not revealed) so its OWN
                    # internal quotes never toggle the OUTER string's
                    # in_dquote state. See _command_substitution_end's own
                    # docstring for the real false-positive this fixes.
                    i = cmdsub_end
                    continue
                if line[i:i + 2] == "$(" and line[i:i + 3] != "$((":
                    # This $(...) does not close on this physical line --
                    # the whole rest of the line still belongs to its own
                    # nested scope and stays masked (same reasoning as the
                    # top-of-function carry entry above: it is the
                    # embedded-script shape, not a short bash expression),
                    # carrying the open state into the next line rather
                    # than falling through to ordinary per-character
                    # handling (#900 round 3: see
                    # _command_substitution_end's own docstring for the
                    # quote-parity drift that produced).
                    masked_line = "".join(masked)
                    return (delta, heredoc_term, heredoc_strip_tabs, in_squote,
                            in_dquote, masked_line, False, (cs_depth, cs_sq, cs_dq))
                # Reveal a variable expansion even though it sits inside a
                # double-quoted string: bash expands $VAR/${VAR} identically
                # whether quoted or not, and a command-position check that
                # could never see "$fn" would miss every quoted dynamic
                # dispatch there is. Everything else inside the quote
                # (literal string data) stays masked -- only the $-token
                # itself is copied through.
                j = i + 1
                if j < n and line[j] == "{":
                    # Depth-counted, not "find the first '}'": a
                    # parameter expansion's own pattern can legally
                    # nest another one (`${X%%"${path:0:1}"*}`), and
                    # stopping at the FIRST '}' truncates mid-expansion
                    # -- the exact bug that left in_dquote stuck True
                    # for the rest of this repo's own
                    # session-start-hook.sh (line 408's drive-letter
                    # check) during this module's own testing. Quotes
                    # inside this span are not separately tracked: the
                    # whole '${...}' is skipped as one atomic unit, the
                    # same treatment _command_substitution_end already
                    # gives '$(...)'.
                    depth = 1
                    j += 1
                    inner_start = j
                    while j < n and depth > 0:
                        if line[j] == "{":
                            depth += 1
                        elif line[j] == "}":
                            depth -= 1
                        j += 1
                    inner = line[inner_start:j - 1]
                    if inner and inner[0].isalpha() or inner.startswith("_"):
                        is_plain_name = all(c == "_" or c.isalnum() for c in inner)
                    else:
                        is_plain_name = False
                    if is_plain_name:
                        # A PLAIN `${NAME}` (no `:-`/`:+`/`#`/`%`/`/`
                        # modifier) is the only shape revealed -- a
                        # literal "(" or ";" inside a modifier's own
                        # alternate-value text
                        # (`${X:+ (${Y} bytes)}`) is DATA, not a
                        # separator, and revealing the whole span let
                        # that literal "(" masquerade as a real command
                        # boundary during this module's own testing
                        # (session-start-hook.sh's handoff-size notice
                        # message). Anything else stays masked.
                        masked[i:j] = line[i:j]
                else:
                    while j < n and (line[j].isalnum() or line[j] == "_"):
                        j += 1
                    masked[i:j] = line[i:j]
                i = j
                continue
            i += 1
            continue
        if c == "#":
            if brace_stack and brace_stack[-1]:
                masked[i] = c
                i += 1
                continue
            break
        if c == "'":
            in_squote = True
            masked[i] = c
            i += 1
            continue
        if c == '"':
            in_dquote = True
            masked[i] = c
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            masked[i] = c
            masked[i + 1] = line[i + 1]
            i += 2
            continue
        if heredoc_term is None and line[i:i + 2] == "<<":
            j = i + 2
            strip_tabs = False
            if j < n and line[j] == "-":
                strip_tabs = True
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            quote_char = line[j] if j < n and line[j] in "'\"" else None
            if quote_char:
                j += 1
            start = j
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            ident = line[start:j]
            if quote_char and j < n and line[j] == quote_char:
                j += 1
            if ident:
                heredoc_term, heredoc_strip_tabs = ident, strip_tabs
            masked[i:j] = line[i:j]
            i = j
            continue
        if c == "$":
            cmdsub_end, cs_depth, cs_sq, cs_dq = _command_substitution_end(line, i)
            if cmdsub_end is not None:
                # Same nested-scope reasoning as the in_dquote branch above
                # -- a $(...) outside any quote still has its own internal
                # quotes that must not leak into whatever comes after it.
                masked[i:cmdsub_end] = line[i:cmdsub_end]
                i = cmdsub_end
                continue
            if line[i:i + 2] == "$(" and line[i:i + 3] != "$((":
                # Same multi-line carry as the in_dquote branch above --
                # a $(...) started in CODE state (not inside any outer
                # quote) that does not close on this line still must not
                # let its own internal quotes leak into in_squote/
                # in_dquote on the next physical line, and stays masked
                # for the same dynamic-dispatch-noise reason.
                masked_line = "".join(masked)
                return (delta, heredoc_term, heredoc_strip_tabs, in_squote,
                        in_dquote, masked_line, False, (cs_depth, cs_sq, cs_dq))
        masked[i] = c
        if c == "{":
            brace_stack.append(i > 0 and line[i - 1] == "$")
            delta += 1
            i += 1
            continue
        if c == "}":
            if brace_stack:
                brace_stack.pop()
            delta -= 1
            i += 1
            continue
        i += 1
    masked_line = "".join(masked)
    # A lone trailing backslash in CODE state (not inside any quote) means
    # the LOGICAL statement continues onto the next physical line:
    # `log "hook" \` / `    "$_legacy_dir" >&2` is one statement, and that
    # second physical line is not a fresh statement boundary even though
    # it is the literal start of its own line. tree_shake uses this to
    # avoid reading a continuation line's own leading argument as "a bare
    # quoted variable occupying command position" -- every multi-argument
    # log/report call this repo's own real hooks wrap across lines did
    # exactly that before this fix.
    continuation = (not in_squote and not in_dquote and n > 0 and line[-1] == "\\")
    return delta, heredoc_term, heredoc_strip_tabs, in_squote, in_dquote, masked_line, continuation, None


def tree_shake(text):
    """Drop every top-level `name() { ... }` function TEXT never reaches
    from its own root-level code (outside any function), transitively
    through kept functions' own bodies.

    Reachability is textual and deliberately coarse, matching jit-context's
    own compile_scripts.py::tree_shake: a function is kept the moment its
    name appears as a bare identifier ANYWHERE outside a function
    definition's start/end lines, or inside an already-kept function's
    body -- including inside a string, a comment, or an assigned value
    (`NAME="do_thing"` keeps do_thing). This over-keeps rather than
    under-keeps, which is the safe direction for a change whose failure
    mode is "the directory still can't follow this call", not "the file is
    a little bigger than it needed to be".

    A command position occupied by nothing but a lowercase/mixed-case
    variable (_DYNAMIC_CALL_RE) names a target this analysis cannot see at
    all -- the function it resolves to at runtime is not necessarily
    spelled anywhere in the source. Finding one anywhere in TEXT means
    shaking is not provably safe for this file: nothing is dropped, and the
    report says so (dynamic_dispatch=True) rather than silently proceeding
    as if the textual scan had seen everything a real interpreter would.

    Functions defined DIRECTLY inside a compiled hook's per-file wrapper
    (WRAPPER_PREFIX -- each wrapper's body is one library's top-level code)
    are shaken exactly like top-level ones; the wrapper itself is never
    dropped, and its body's own non-function lines count as root code (a
    wrapper runs whenever its library is sourced -- over-keeping again).

    Returns (new_text, report) where report has keys: shaken (bool), kept
    (sorted list of function names), dropped (sorted list), dynamic_dispatch
    (bool), reason (str, only set when shaken is False)."""
    scan = _scan_structure(text)
    if isinstance(scan, str):
        return text, {"shaken": False, "kept": [], "dropped": [],
                       "dynamic_dispatch": False, "reason": scan}
    lines, deltas, in_heredoc, masked_lines = scan
    spans = _function_spans(lines, deltas, in_heredoc)
    if isinstance(spans, str):
        return text, {"shaken": False, "kept": [], "dropped": [],
                       "dynamic_dispatch": False, "reason": spans}
    funcs, _wrappers = spans
    if not funcs:
        return text, {"shaken": True, "kept": [], "dropped": [],
                       "dynamic_dispatch": False, "reason": ""}

    in_func_line = set()
    for s, e in funcs.values():
        in_func_line.update(range(s, e + 1))

    dynamic_dispatch = any(_DYNAMIC_CALL_RE.search(ml) for ml in masked_lines if ml)

    if dynamic_dispatch:
        return text, {"shaken": False, "kept": sorted(funcs), "dropped": [],
                       "dynamic_dispatch": True,
                       "reason": ("a command position occupied by a bare/quoted "
                                  "lowercase variable was found -- its call target is "
                                  "not provably resolvable from the source text, so "
                                  "no function in this file can be proven unreachable")}

    root_words = set()
    for i, line in enumerate(lines):
        if i not in in_func_line:
            root_words.update(_IDENT_RE.findall(line))
    keep = {name for name in funcs if name in root_words}
    frontier = list(keep)
    while frontier:
        s, e = funcs[frontier.pop()]
        body = "\n".join(lines[s + 1:e])
        for word in set(_IDENT_RE.findall(body)):
            if word in funcs and word not in keep:
                keep.add(word)
                frontier.append(word)
    drop_lines = set()
    for name, (s, e) in funcs.items():
        if name not in keep:
            drop_lines.update(range(s, e + 1))
    new_text = "\n".join(line for i, line in enumerate(lines) if i not in drop_lines)
    dropped = sorted(set(funcs) - keep)
    return new_text, {"shaken": True, "kept": sorted(keep), "dropped": dropped,
                       "dynamic_dispatch": False, "reason": ""}


def _scan_structure(text):
    """(lines, deltas, in_heredoc, masked_lines) for TEXT -- per line: the
    net unquoted brace count, whether it is a heredoc body line, and the
    masked text the dynamic-dispatch search reads -- or a str saying why
    the file could not be scanned (a quote, heredoc or command substitution
    left open at end of file)."""
    lines = text.split("\n")
    n = len(lines)
    deltas = [0] * n
    in_heredoc = [False] * n
    masked_lines = [""] * n
    heredoc_term = None
    heredoc_strip_tabs = False
    in_squote = in_dquote = False
    brace_stack = []
    pending_continuation = False
    pending_cmdsub = None
    for i, line in enumerate(lines):
        if heredoc_term is not None:
            in_heredoc[i] = True
            check = line.strip() if heredoc_strip_tabs else line
            if check == heredoc_term:
                heredoc_term = None
            continue
        entering_in_quote = in_squote or in_dquote
        entering_in_cmdsub = pending_cmdsub is not None
        (delta, heredoc_term, heredoc_strip_tabs, in_squote, in_dquote, masked,
         continuation, pending_cmdsub) = _scan_line_braces(
            line, in_squote, in_dquote, brace_stack, pending_cmdsub)
        deltas[i] = delta
        # Neither a backslash-continued line NOR a line that starts
        # already inside a quote carried over from an earlier one (a
        # double-quoted string containing a literal embedded newline --
        # `case "\n${VAR}" in`, or a multi-line printf/echo message) NOR
        # a line that continues a '$(...)' left open by an earlier one
        # (#900 round 3) is a fresh statement boundary, even though its
        # revealed $-expansion can otherwise land at offset 0 of the
        # masked text and look exactly like one. Prefixing one NUL byte
        # makes any of these shapes unmatchable by _DYNAMIC_CALL_RE's `^`
        # alternative without disturbing any OTHER separator the line may
        # still contain (';', '||', ...), and without shifting any other
        # position-sensitive use of masked_lines (there is none -- it is
        # read only by the dynamic-dispatch search and
        # _function_unsafe_lines).
        masked_lines[i] = (("\x00" + masked)
                            if (pending_continuation or entering_in_quote or entering_in_cmdsub)
                            else masked)
        pending_continuation = continuation
    if in_squote or in_dquote or heredoc_term is not None or pending_cmdsub is not None:
        return "a quote, heredoc or command substitution never closed by end of file"
    return lines, deltas, in_heredoc, masked_lines


def _function_spans(lines, deltas, in_heredoc):
    """(funcs, wrappers): name -> (start, end) line index for every
    `name() {` opened at column 0 at brace depth 0, or at depth 1 directly
    inside a WRAPPER_PREFIX wrapper (which goes in WRAPPERS, everything else
    in FUNCS) -- or a str saying why not (unbalanced braces, a name defined
    twice)."""
    funcs: dict = {}
    wrappers: dict = {}
    depth = 0
    stack = []  # (name, start, depth_before, is_wrapper)
    duplicate = None
    for i, line in enumerate(lines):
        if in_heredoc[i]:
            continue
        top = not stack and depth == 0
        in_wrapper = len(stack) == 1 and stack[0][3] and depth == stack[0][2] + 1
        if top or in_wrapper:
            m = _FUNC_START_RE.match(line)
            if m:
                name = m.group(1)
                stack.append((name, i, depth, top and name.startswith(WRAPPER_PREFIX)))
        depth += deltas[i]
        while stack and depth == stack[-1][2]:
            name, start, _, is_wrapper = stack.pop()
            if name in funcs or name in wrappers:
                duplicate = name
            (wrappers if is_wrapper else funcs)[name] = (start, i)
    if depth != 0 or stack:
        return f"unbalanced braces by end of file (final depth {depth}) -- refusing to guess"
    if duplicate:
        return f"duplicate top-level function name {duplicate!r}"
    return funcs, wrappers


# A statement whose meaning changes when the code around it moves from a
# sourced file's top level into a function body: `local`/`declare`/`typeset`
# would declare a function-LOCAL variable where the sourced file declared a
# global one (`local` at a sourced file's top level is an error; inside the
# wrapper it would silently succeed), and `shift`/`set --` would rewrite the
# wrapper's positional parameters rather than the caller's. A query
# (`declare -F name`, `declare -f`, `declare -p`) changes nothing and is
# fine. Matched against the MASKED line, so text inside a quote never counts.
_ANY_FUNC_START_RE = re.compile(r'^[ \t]*[A-Za-z_][A-Za-z0-9_]*\(\)[ \t]*\{[ \t]*$')
_FUNCTION_UNSAFE = re.compile(
    r'(?:^|[;&|(]|\bthen\b|\bdo\b|\belse\b|\{)\s*'
    r'(?:local\b|typeset\b|declare\b(?!\s+-[Ffp]\b)|shift\b|set\s+--)'
)


def _function_unsafe_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every statement in TEXT's own top-level code
    (outside every function it defines) that would mean something different
    inside a function body -- see _FUNCTION_UNSAFE. TEXT is one library
    about to become a wrapper function's body; a non-empty answer fails the
    build (InlineError) rather than shipping a changed meaning. A file this
    cannot scan is reported as unsafe on line 0, for the same reason."""
    scan = _scan_structure(text)
    if isinstance(scan, str):
        return [(0, scan)]
    lines, deltas, in_heredoc, masked_lines = scan
    # Every function body, at any indentation and any nesting -- a library
    # can define one inside an `if` (detect-tools.sh's lazy
    # `_remember_python`), and a `local` there is the function's own.
    in_func = set()
    open_at: list[int] = []
    depth = 0
    for i, line in enumerate(lines):
        if in_heredoc[i]:
            if open_at:
                in_func.add(i)
            continue
        if _ANY_FUNC_START_RE.match(line) or open_at:
            in_func.add(i)
            if _ANY_FUNC_START_RE.match(line):
                open_at.append(depth)
        depth += deltas[i]
        while open_at and depth == open_at[-1]:
            open_at.pop()
    if open_at or depth != 0:
        return [(0, f"unbalanced braces by end of file (final depth {depth})")]
    found = []
    for i, masked in enumerate(masked_lines):
        if i in in_func or in_heredoc[i]:
            continue
        if _FUNCTION_UNSAFE.search(masked.lstrip("\x00")):
            found.append((i + 1, lines[i]))
    return found


def _link(name: str, contents: dict[str, str], script_dir: str) -> str:
    """NAME's text with one wrapper function per sourced file defined right
    after its shebang and every `source` turned into a call to one -- see
    link_sources and the module docstring."""
    body, libraries = link_sources(name, contents, script_dir)
    for key, text in libraries.items():
        bad = _function_unsafe_lines(text)
        if bad:
            n, line = bad[0]
            raise InlineError(
                f"{key}:{n}: {line.strip()!r} would mean something different "
                f"inside the function this file is compiled into (a top-level "
                f"local/declare/typeset/shift/set --) -- refusing to change "
                f"its meaning silently")
    lines = body.split("\n")
    head = lines[:1] if lines and lines[0].startswith("#!") else []
    defs = []
    for key, text in libraries.items():
        # `:` first: a wrapper whose every line is later shaken or stripped
        # must still be a syntactically valid (empty) function body.
        defs.append(f"{wrapper_name(key)}() {{")
        defs.append(":")
        defs.extend(text.split("\n"))
        defs.append("}")
    return "\n".join(head + defs + lines[len(head):])


def compile_hook(name: str, contents: dict[str, str], script_dir: str = "scripts") -> str:
    """The self-contained, comment-stripped, tree-shaken text for hook
    NAME -- no `source`/`.` statement pointing at another file should
    survive this (check_release_tree.py's _check_hook_still_sources FAILs
    the release build if one does), and no function the hook itself never
    reaches should either (a directory scan that holds a plugin for
    something only an UNUSED inlined helper contains -- "perl code",
    `jit-doctor.sh`'s own #461 finding -- is a hold the hook's own code
    never earns). tree_shake's own report (which functions were dropped,
    or why none were) is discarded here; compile_hook_report below returns
    it for callers that want to log it."""
    return compile_hook_report(name, contents, script_dir)[0]


def compile_hook_report(name: str, contents: dict[str, str],
                         script_dir: str = "scripts") -> tuple[str, dict]:
    """Like compile_hook, but also returns tree_shake's own report dict
    (shaken, kept, dropped, dynamic_dispatch, reason) -- for a caller that
    wants to log how many functions were dropped per hook, or why shaking
    was skipped for one. COMPILED_MARKER is written as the line right after
    the shebang (the first line, if there is none)."""
    text = strip_whole_line_comments(_link(name, contents, script_dir))
    shaken, report = tree_shake(text)
    lines = shaken.split("\n")
    at = 1 if lines and lines[0].startswith("#!") else 0
    lines.insert(at, COMPILED_MARKER)
    return "\n".join(lines), report


# -- CLI (local dev use: see docs/releasing.md) ---------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".", help="repo root (scripts/ lives under it)")
    ap.add_argument("--script-dir", default="scripts")
    ap.add_argument("--apply", action="store_true",
                     help="overwrite each hook script in place with its compiled "
                          "text -- for a disposable CI/test checkout only, never "
                          "for a real working tree")
    args = ap.parse_args(argv)
    repo = Path(args.repo)
    script_dir = repo / args.script_dir
    contents: dict[str, str] = {}
    for p in script_dir.glob("*.sh"):
        contents[f"{args.script_dir}/{p.name}"] = p.read_text(encoding="utf-8")
    status = 0
    for hname in HOOK_SCRIPT_NAMES:
        key = f"{args.script_dir}/{hname}"
        if key not in contents:
            print(f"compile_hooks: {key}: not found", file=sys.stderr)
            status = 1
            continue
        try:
            compiled = compile_hook(key, contents, args.script_dir)
        except InlineError as exc:
            print(f"compile_hooks: {key}: {exc}", file=sys.stderr)
            status = 1
            continue
        remaining = unresolved_sources(compiled)
        if remaining:
            n, line = remaining[0]
            print(f"compile_hooks: {key}: still sources another file after "
                  f"compiling, line {n}: {line.strip()}", file=sys.stderr)
            status = 1
            continue
        size = len(compiled.encode("utf-8"))
        print(f"{key}: {size} bytes compiled ({len(contents[key].encode('utf-8'))} "
              f"before inlining+stripping)")
        if args.apply:
            # open(..., newline=) rather than Path.write_text(newline=):
            # that keyword is 3.10+, and CI's 3.9 leg runs this --apply path.
            with open(script_dir / hname, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(compiled)
    return status


if __name__ == "__main__":
    sys.exit(main())
