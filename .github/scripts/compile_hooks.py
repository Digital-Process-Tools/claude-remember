#!/usr/bin/env python3
"""Inline a hooks.json-registered hook script's `source`/`.` chain (#900).

The Anthropic plugin directory's release-preview validator inspects only the
command a `hooks/hooks.json` entry names; it does not follow a `source`/`.`
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

The *source tree* keeps every hook script and library exactly as written --
this module changes only what a compiled hook's TEXT looks like; callers
decide where that text ends up (the release tree, or a CI scratch copy used
to run the existing hook test suites against a compiled hook -- see
docs/releasing.md).

This file is importable standalone (`python3 compile_hooks.py --repo . --check`
prints each hook's compiled size without writing anything) and is imported by
both build_release_tree.py (to produce the shipped bytes) and
check_release_tree.py (SOURCE_LINE/HOOK_SCRIPT_NAMES, to FAIL a shipped hook
that still sources something -- see that file's _check_hook_still_sources)."""

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

# A `source`/`.` statement naming a sibling script, e.g.:
#   source "${CLAUDE_PLUGIN_ROOT}/scripts/lib-env-cache.sh"
#   .      "${CLAUDE_PLUGIN_ROOT}/scripts/resolve-paths.sh" 2>/dev/null || true
#   source "$_HOOK_DIR/log.sh"
# Matched only at the start of a line (leading whitespace only) -- never
# mid-line and never inside a string: a function-call argument literally
# spelled "source" (e.g. `_stdin_json_string_into VAR source "$HOOK_STDIN"`)
# does not start the line with the word `source`/`.`, so it is not matched.
# The target is always double-quoted in every source/.  statement this
# repo's own scripts write; a bare/unquoted or single-quoted target is left
# alone rather than guessed at -- inline_sources() raises InlineError on one
# of those rather than silently leaving it unresolved.
SOURCE_LINE = re.compile(
    r'^[ \t]*(?:source|\.)[ \t]+"(?P<target>[^"\n]*)"(?P<rest>.*)$'
)

# The trailing `name.sh` component of a (possibly `$VAR`-prefixed) source
# target -- what SOURCE_LINE's own `target` group is resolved against the
# sibling scripts/ directory with.
_TRAILING_SH_NAME = re.compile(r'[A-Za-z0-9_.\-]+\.sh$')


class InlineError(Exception):
    """A hook's source chain could not be safely inlined."""


def target_basename(target: str) -> str | None:
    """The trailing `name.sh` filename TARGET resolves to, or None if it
    does not end in a plain `.sh` name (nothing to look up under
    scripts/)."""
    m = _TRAILING_SH_NAME.search(target)
    return m.group(0) if m else None


# -- comment stripping -------------------------------------------------------

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
    with every `source`/`.` line in its own body recursively replaced by the
    body of the file it names.

    Include-guard semantics: a file already inlined earlier in this same
    top-level call (tracked in _seen, by its CONTENTS key) has its repeat
    source line dropped rather than inlined a second time. This matches the
    runtime guard most of this repo's own shared libraries already carry
    (`[ -n "${X_LOADED:-}" ] && return 0`): re-sourcing one of them today is
    already a no-op past the first time, so compiling away the repeat
    inclusion changes nothing observable.

    Raises InlineError on a source line naming something this function
    cannot resolve against CONTENTS, or on a cycle."""
    if _seen is None:
        _seen = set()
    if name in _stack:
        raise InlineError(f"source cycle: {' -> '.join(_stack)} -> {name}")
    if name not in contents:
        raise InlineError(f"{name}: not found")
    out_lines = []
    for line in contents[name].split("\n"):
        m = SOURCE_LINE.match(line)
        if not m:
            out_lines.append(line)
            continue
        target = m.group("target")
        base = target_basename(target)
        if base is None:
            raise InlineError(f"{name}: cannot resolve source target {target!r} "
                               f"(not a plain sibling '*.sh' path)")
        candidate = posixpath.join(script_dir, base)
        if candidate not in contents:
            raise InlineError(f"{name}: sources {candidate!r}, not present in "
                               f"this build")
        if candidate in _seen:
            # include-guard: already inlined earlier in this same hook --
            # drop the repeat source line rather than inlining it again.
            continue
        _seen.add(candidate)
        included = inline_sources(candidate, contents, script_dir, _seen, _stack + (name,))
        out_lines.extend(included.split("\n"))
    return "\n".join(out_lines)


def compile_hook(name: str, contents: dict[str, str], script_dir: str = "scripts") -> str:
    """The self-contained, comment-stripped text for hook NAME -- no
    `source`/`.` statement pointing at another file should survive this
    (check_release_tree.py's _check_hook_still_sources FAILs the release
    build if one does)."""
    return strip_whole_line_comments(inline_sources(name, contents, script_dir))


def unresolved_sources(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every remaining `source`/`.` statement in
    TEXT -- what a hook that did not fully compile still carries. Used by
    check_release_tree.py's FAIL guard; exposed here too so a build-time
    sanity check and the release-tree check apply the identical pattern."""
    return [(n, line) for n, line in enumerate(text.split("\n"), 1)
            if SOURCE_LINE.match(line)]


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
            print(f"compile_hooks: {key}: not found, skipping", file=sys.stderr)
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
            (script_dir / hname).write_text(compiled, encoding="utf-8")
    return status


if __name__ == "__main__":
    sys.exit(main())
