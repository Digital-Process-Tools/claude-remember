#!/usr/bin/env python3
"""Strip comments and docstrings from a shipped .py file at release-build time (#900).

The Anthropic plugin directory's release-preview scanner reads Python comments and
docstrings as code. Probe hD (claude-directory-publishing triggers.md) put
pipeline/haiku.py through a comment-and-docstring strip and nothing else: the
credential hold's citation changed from "an environment variable named at run time"
-- prose in a comment -- to "the whole environment object". So every .py the release
tree ships goes through `strip_python`; the source on `main` keeps every word.

How, and why not `ast.unparse`: the source TEXT is edited in place. COMMENT tokens
(from `tokenize`) are cut, together with the blanks before them on their line; the
span of each docstring statement (from `ast`) is cut, and replaced by `pass` when it
was its body's only statement. Then every line left blank is dropped, except one
inside a multi-line string literal. Everything else -- each other string literal,
f-strings, formatting, the order of statements -- is the source's own bytes, on any
interpreter the build runs on. `ast.unparse` would re-render every line from the tree
of whatever Python runs the build, which is not the Python 3.9 a user may run it on.

Kept on purpose: a `#!` first line, and a `coding` cookie on a comment-only line 1 or
2 (both change how the file is run or decoded, not what the scanner should read).

Proof, per file, or the build fails (`StripError`):
- the result parses with `feature_version=(3, 9)`;
- `ast.dump` of the result equals `ast.dump` of the source with its docstrings
  removed (a body left empty holds a lone `pass`) -- `docstring_free_dump`;
- `leftovers` finds no comment and no docstring in the result.
A file that reads `__doc__` is refused outright: stripping its docstring would
change what it prints.

Usage (local, read-only): strip_python.py FILE... prints each file's size before and
after; --print writes the stripped text of a single FILE to stdout.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize

_CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
_DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


class StripError(Exception):
    """A .py file the release build must not ship stripped."""


def _docstrings(tree: ast.AST) -> list:
    """(owner, Expr) for every docstring statement in TREE."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, _DOC_OWNERS) or not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            found.append((node, first))
    return found


def docstring_free_dump(src: str) -> str:
    """ast.dump of SRC with every docstring statement removed: a class or function
    body left empty holds a lone `pass`, a module left empty holds nothing."""
    tree = ast.parse(src)
    for owner, _doc in _docstrings(tree):
        rest = owner.body[1:]
        owner.body = rest if (rest or isinstance(owner, ast.Module)) else [ast.Pass()]
    return ast.dump(tree)


def _kept_comment(row: int, text: str, lines: list) -> bool:
    if row == 1 and text.startswith("#!"):
        return True
    return row <= 2 and bool(_CODING.match(text)) and lines[row - 1].lstrip().startswith("#")


def _tokens(src: str) -> list:
    try:
        return list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError) as exc:
        raise StripError(f"does not tokenize: {exc}") from None


def leftovers(src: str) -> list:
    """(line, "comment"|"docstring", text) for every comment token and docstring
    statement in SRC -- the shebang and a coding cookie are not counted."""
    lines = src.split("\n")
    found = []
    for tok in _tokens(src):
        if tok.type == tokenize.COMMENT and not _kept_comment(tok.start[0], tok.string, lines):
            found.append((tok.start[0], "comment", tok.string))
    for _owner, doc in _docstrings(ast.parse(src)):
        found.append((doc.lineno, "docstring", doc.value.value.strip().split("\n")[0]))
    return sorted(found)


def strip_source(src: str) -> str:
    """SRC with every comment and docstring removed; see the module docstring."""
    if "\r" in src:
        raise StripError("carries a carriage return; only \\n line endings are stripped")
    tree = ast.parse(src)
    lines = src.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)

    def at_char(row: int, col: int) -> int:
        return starts[row - 1] + col

    def at_byte(row: int, col: int) -> int:
        prefix = lines[row - 1].encode("utf-8")[:col].decode("utf-8")
        return starts[row - 1] + len(prefix)

    cut = bytearray(len(src))
    insert: dict = {}
    toks = _tokens(src)

    for tok in toks:
        if tok.type != tokenize.COMMENT or _kept_comment(tok.start[0], tok.string, lines):
            continue
        s, e = at_char(*tok.start), at_char(*tok.end)
        while s > starts[tok.start[0] - 1] and src[s - 1] in " \t\f":
            s -= 1
        cut[s:e] = b"\1" * (e - s)

    for owner, doc in _docstrings(tree):
        s = at_byte(doc.lineno, doc.col_offset)
        e = at_byte(doc.end_lineno, doc.end_col_offset)
        alone = len(owner.body) == 1 and not isinstance(owner, ast.Module)
        if alone:
            insert[s] = "pass"
        else:
            nxt = next((t for t in toks if at_char(*t.start) >= e
                        and t.type != tokenize.COMMENT), None)
            if nxt is not None and nxt.type == tokenize.OP and nxt.string == ";":
                e = at_char(*nxt.end)
                while e < len(src) and src[e] in " \t\f":
                    e += 1
        cut[s:e] = b"\1" * (e - s)

    out = []
    for i, ch in enumerate(src):
        if i in insert:
            out.append(insert[i])
        if not cut[i]:
            out.append(ch)
    text = "".join(out)

    try:
        stripped_tree = ast.parse(text)
    except SyntaxError:
        return text
    inside: set = set()
    for node in ast.walk(stripped_tree):
        if isinstance(node, (ast.Constant, ast.JoinedStr)) and \
                getattr(node, "end_lineno", None) and node.end_lineno > node.lineno:
            inside.update(range(node.lineno + 1, node.end_lineno + 1))
    kept = [line for n, line in enumerate(text.split("\n"), 1)
            if n in inside or line.strip(" \t\f")]
    return "\n".join(kept) + "\n" if kept else ""


def _reads_doc(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "__doc__":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "__doc__":
            return True
    return False


def verify(src: str, out: str, path: str = "<file>") -> None:
    """Raise StripError unless OUT is SRC minus comments and docstrings, exactly."""
    try:
        got = ast.parse(out, feature_version=(3, 9))
    except SyntaxError as exc:
        raise StripError(f"{path}: the stripped file does not parse as Python 3.9: "
                         f"{exc.msg} (line {exc.lineno})") from None
    if ast.dump(got) != docstring_free_dump(src):
        raise StripError(f"{path}: the stripped file's AST differs from the source's "
                         "(docstrings removed) -- the stripper changed code")
    left = leftovers(out)
    if left:
        n, kind, text = left[0]
        raise StripError(f"{path}:{n}: a {kind} survived the strip: {text[:60]}")


def strip_python(src: str, path: str = "<file>") -> str:
    """Strip SRC, prove the result (see verify), return it. Raises StripError."""
    try:
        tree = ast.parse(src, feature_version=(3, 9))
    except SyntaxError as exc:
        raise StripError(f"{path}: the source does not parse as Python 3.9: "
                         f"{exc.msg} (line {exc.lineno})") from None
    if _reads_doc(tree):
        raise StripError(f"{path}: reads __doc__ -- stripping its docstrings would change "
                         "what it does; give it an explicit string instead")
    try:
        out = strip_source(src)
    except StripError as exc:
        raise StripError(f"{path}: {exc}") from None
    verify(src, out, path)
    return out


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Strip comments and docstrings from .py files "
                                             "(read-only: prints sizes, or one file).")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--print", action="store_true", dest="show")
    args = ap.parse_args(argv)
    status = 0
    for name in args.files:
        with open(name, encoding="utf-8", newline="") as fh:
            src = fh.read()
        try:
            out = strip_python(src, name)
        except StripError as exc:
            print(f"strip_python: {exc}", file=sys.stderr)
            status = 1
            continue
        if args.show:
            sys.stdout.write(out)
        else:
            print(f"{name}: {len(src.encode())} -> {len(out.encode())} bytes")
    return status


if __name__ == "__main__":
    sys.exit(main())
