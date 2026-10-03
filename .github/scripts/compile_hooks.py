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


def compile_hook(name: str, contents: dict[str, str], script_dir: str = "scripts") -> str:
    """The self-contained, comment-stripped text for hook NAME -- no
    `source`/`.` statement pointing at another file should survive this
    (check_release_tree.py's _check_hook_still_sources FAILs the release
    build if one does)."""
    return strip_whole_line_comments(inline_sources(name, contents, script_dir))


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
