#!/usr/bin/env python3
"""Inline a hooks.json-registered hook script's `source`/`.` chain (#900).

The Anthropic plugin directory's release-preview validator inspects only the
command a `hooks/hooks.json` entry names; it never follows a `source`/`.`
statement into a second file, so a hook that pulls in shared library code
that way is held as COMMAND_SCRIPT_NOT_FOLLOWED (jit-context's own
directory-validator write-up, #900). `build_release_tree.py` calls
`compile_hook` on each of the four HOOK_SCRIPT_NAMES scripts before writing
the release tree: every file in a hook's own `source`/`.` closure is
substituted in place, recursively, each file inlined at most once per hook
(include-guard semantics -- a repeat of an already-inlined path is simply
dropped rather than inlined a second time). Comment-only lines are then
stripped from the result so the compiled file fits the directory's 256 KiB
per-file budget; a shebang line (only ever the file's own first line, never
one spliced in from an included file -- see below), a heredoc body, and
anything inside an open quoted string are never touched.

A `source`/`.` statement in this repo's own scripts appears in three shapes,
all handled by `inline_sources` below -- self-review found the first
version of this module matched only the first shape, silently leaving every
hook still sourcing `resolve-paths.sh` (and, in post-tool-hook.sh,
`detect-tools.sh`) after "compiling", and a second pass that widened the
matching to catch those two shapes then matched INSIDE an unrelated
single-quoted jq filter on the same line as a real statement elsewhere in
the file (`scripts/lib-memory-dir.sh`'s merge-config jq script contains the
literal text "; . * $x" as jq syntax, which a naive line-wide regex search
read as a shell `;`-separated `.`-source statement). The detector below
never tests any text inside a quote or a heredoc body: it walks the file
once, tracking quote/heredoc state exactly as `strip_whole_line_comments`
does, and only looks for a source statement within an UNQUOTED segment of
a line, at a position that is genuinely a shell command boundary.

1. **Plain, unconditional**: `source "$X/lib.sh"` -- the statement is the
   whole line (or whole unquoted segment). Replaced by the included file's
   body in place.
2. **Assignment-prefixed, trailing-guarded**:
   `VAR=1 source "$X/lib.sh" || exit 0`. A leading `VAR=value` prefix before
   a shell BUILTIN (`source`/`.` is one) persists in the current shell after
   the builtin returns -- unlike before an external command -- so `VAR=1` is
   emitted as its own persisting statement ahead of the inlined body. The
   trailing `|| exit 0`/`2>/dev/null`/`|| return 0` guards against sourcing
   FAILING; inlining guarantees the equivalent of success (the content is
   embedded at build time, not loaded at run time), so that guard is dead
   code once inlined and is dropped -- but only when it provably has that
   shape (`_droppable_trailing`); anything else fails the build loudly
   rather than being silently discarded.
3. **Conditionally gated**: `COND || source "$X/lib.sh"` (or `COND &&`). This
   is not a success/failure guard on sourcing -- it decides WHETHER sourcing
   happens at all (a lazy-init / cost-avoidance gate: post-tool-hook.sh uses
   exactly this shape to skip detect-tools.sh's own cost on a path that
   already knows $PYTHON). Dropping the condition would be a performance
   regression disguised as a correctness fix. The condition is preserved
   exactly, and the inlined body is wrapped in a `{ ...; }` group as the
   right-hand side of the same `||`/`&&` -- `{ }` runs in the current shell
   (unlike a `( )` subshell), so persisting assignments inside it still
   persist, and the short-circuit semantics of `||`/`&&` are unchanged.

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
    r'(?:source|\.)[ \t]+"(?P<target>[^"\n]*)"(?P<rest>.*)$'
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

_ASSIGN_WORD = re.compile(_ASSIGN_WORD_SRC)
_TRAILING_SH_NAME = re.compile(r'[A-Za-z0-9_.\-]+\.sh$')

# What is safe to drop from a TRAILING (non-gated) source statement once it
# is inlined: a redirect (`2>/dev/null`) and/or a post-condition on the
# source command's own exit status (`|| exit 0`, `|| return 0`, `|| true`,
# `|| :`). Inlining guarantees the equivalent of "sourcing succeeded", so
# this is dead code once applied -- anything that does not fullmatch this
# shape is NOT guessed at; `inline_sources` raises InlineError instead.
_DROPPABLE_TRAILING = re.compile(
    r'(?:[0-9]*(?:>>?|<)[ \t]*\S+[ \t]*)*'
    r'(?:\|\|[ \t]*(?:exit|return|true|:)\b[ \t]*[0-9]*[ \t]*)?'
)


def target_basename(target: str) -> str | None:
    """The trailing `name.sh` filename TARGET resolves to, or None if it
    does not end in a plain `.sh` name (nothing to look up under
    scripts/)."""
    m = _TRAILING_SH_NAME.search(target)
    return m.group(0) if m else None


def _droppable_trailing(rest: str) -> bool:
    rest = rest.strip()
    if not rest:
        return True
    return _DROPPABLE_TRAILING.fullmatch(rest) is not None


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
    lines = text.split("\n")
    out = []
    heredoc_term = None
    heredoc_strip_tabs = False
    in_squote = in_dquote = False
    for idx, line in enumerate(lines):
        if heredoc_term is not None:
            out.append(line)
            check = line.strip() if heredoc_strip_tabs else line
            if check == heredoc_term:
                heredoc_term = None
            continue
        if in_squote or in_dquote:
            out.append(line)
            heredoc_term, heredoc_strip_tabs, in_squote, in_dquote = _scan_line(
                line, in_squote, in_dquote)
            continue
        stripped = line.lstrip()
        if idx == 0 and stripped.startswith("#!"):
            out.append(line)
            continue
        if stripped.startswith("#"):
            continue
        out.append(line)
        heredoc_term, heredoc_strip_tabs, in_squote, in_dquote = _scan_line(
            line, in_squote, in_dquote)
    return "\n".join(out)


# -- inlining ------------------------------------------------------------

def inline_sources(name: str, contents: dict[str, str], script_dir: str = "scripts",
                    _seen: set[str] | None = None, _stack: tuple[str, ...] = ()) -> str:
    """NAME's text (a key into CONTENTS, e.g. "scripts/session-start-hook.sh")
    with every `source`/`.` statement in its own body recursively replaced
    by the body of the file it names -- see the module docstring for the
    three shapes this handles and why each is transformed the way it is.

    Include-guard semantics: a file already inlined earlier in this same
    top-level call (tracked in _seen, by its CONTENTS key) has its repeat
    source statement replaced with a no-op rather than inlined a second
    time. This matches the runtime guard most of this repo's own shared
    libraries already carry (`[ -n "${X_LOADED:-}" ] && return 0`):
    re-sourcing one of them today is already a no-op past the first time,
    so compiling away the repeat inclusion changes nothing observable.

    Only an UNQUOTED source/. statement is ever matched (see
    _find_source_in_line); quote and heredoc state is tracked across the
    whole file.

    Raises InlineError on a source statement this function cannot resolve
    against CONTENTS, on a cycle, on a target that is not a plain
    double-quoted string, or on trailing text after a (non-gated) source
    statement that is not a recognised redirect/exit-status guard."""
    if _seen is None:
        _seen = set()
    if name in _stack:
        raise InlineError(f"source cycle: {' -> '.join(_stack)} -> {name}")
    if name not in contents:
        raise InlineError(f"{name}: not found")

    out_lines = []
    in_squote = in_dquote = False
    heredoc_term = None
    heredoc_strip_tabs = False
    for line in contents[name].split("\n"):
        if heredoc_term is not None:
            out_lines.append(line)
            check = line.strip() if heredoc_strip_tabs else line
            if check == heredoc_term:
                heredoc_term = None
            continue

        m, sep_start, sep_text, meta = _find_source_in_line(line, in_squote, in_dquote)
        heredoc_term, heredoc_strip_tabs, in_squote, in_dquote = meta

        if m is None:
            out_lines.append(line)
            continue
        if m == "unhandled":
            raise InlineError(
                f"{name}: a source/. statement's target is not a plain "
                f"double-quoted string (bare or single-quoted) -- refusing "
                f"to guess at it rather than leaving it silently unresolved: "
                f"{line.strip()!r}"
            )

        target = m.group("target")
        base = target_basename(target)
        if base is None:
            raise InlineError(f"{name}: cannot resolve source target {target!r} "
                               f"(not a plain sibling '*.sh' path)")
        candidate = posixpath.join(script_dir, base)
        if candidate not in contents:
            raise InlineError(f"{name}: sources {candidate!r}, not present in "
                               f"this build")

        before = line[:sep_start]
        gated = sep_text in ("||", "&&")

        rest = m.group("rest")
        if not gated and not _droppable_trailing(rest):
            raise InlineError(
                f"{name}: a source statement is followed by {rest!r}, which is "
                f"not a recognised redirect/exit-status guard -- refusing to "
                f"guess whether it is safe to drop"
            )

        assign_words = [a.group(0) for a in _ASSIGN_WORD.finditer(m.group("assigns") or "")]

        if candidate in _seen:
            body_lines = [":"]
        else:
            _seen.add(candidate)
            included = inline_sources(candidate, contents, script_dir, _seen, _stack + (name,))
            body_lines = included.split("\n")

        if gated:
            out_lines.append(before + sep_text + " {")
            out_lines.extend(assign_words)
            out_lines.extend(body_lines)
            tail = rest.strip()
            out_lines.append("}" + (" " + tail if tail else ""))
        else:
            if before.strip():
                out_lines.append(before)
            out_lines.extend(assign_words)
            out_lines.extend(body_lines)
    return "\n".join(out_lines)


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

    Returns (new_text, report) where report has keys: shaken (bool), kept
    (sorted list of function names), dropped (sorted list), dynamic_dispatch
    (bool), reason (str, only set when shaken is False)."""
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
        # read only by the dynamic-dispatch search below).
        masked_lines[i] = (("\x00" + masked)
                            if (pending_continuation or entering_in_quote or entering_in_cmdsub)
                            else masked)
        pending_continuation = continuation
    if in_squote or in_dquote or heredoc_term is not None or pending_cmdsub is not None:
        return text, {"shaken": False, "kept": [], "dropped": [],
                       "dynamic_dispatch": False,
                       "reason": "a quote, heredoc or command substitution never "
                                 "closed by end of file"}

    funcs = {}
    depth = 0
    stack = []
    duplicate = None
    for i, line in enumerate(lines):
        if in_heredoc[i]:
            continue
        if depth == 0 and not stack:
            m = _FUNC_START_RE.match(line)
            if m:
                stack.append((m.group(1), i, depth))
        depth += deltas[i]
        while stack and depth == stack[-1][2]:
            name, start, _ = stack.pop()
            if name in funcs:
                duplicate = name
            funcs[name] = (start, i)
    if depth != 0 or stack:
        return text, {"shaken": False, "kept": [], "dropped": [],
                       "dynamic_dispatch": False,
                       "reason": f"unbalanced braces by end of file (final depth {depth}) -- refusing to guess"}
    if duplicate:
        return text, {"shaken": False, "kept": [], "dropped": [],
                       "dynamic_dispatch": False,
                       "reason": f"duplicate top-level function name {duplicate!r}"}
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
    text = strip_whole_line_comments(inline_sources(name, contents, script_dir))
    shaken, _report = tree_shake(text)
    return shaken


def compile_hook_report(name: str, contents: dict[str, str],
                         script_dir: str = "scripts") -> tuple[str, dict]:
    """Like compile_hook, but also returns tree_shake's own report dict
    (shaken, kept, dropped, dynamic_dispatch, reason) -- for a caller that
    wants to log how many functions were dropped per hook, or why shaking
    was skipped for one."""
    text = strip_whole_line_comments(inline_sources(name, contents, script_dir))
    shaken, report = tree_shake(text)
    return shaken, report


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
            (script_dir / hname).write_text(compiled, encoding="utf-8", newline="\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
