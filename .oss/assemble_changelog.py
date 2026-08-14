#!/usr/bin/env python3
# Managed by the oss plugin. This file is OVERWRITTEN every time /oss:scaffold
# runs, so an edit here is lost at the next update. To change what it does,
# copy it somewhere outside .oss/ and point at your copy.

"""Assemble `changelog.d/` fragments into a release section of `CHANGELOG.md`.

55 of the last 60 merged PRs touched `CHANGELOG.md`, all of them appending to the
same place. With N open PRs each merge re-conflicts the other N-1. One file per
change removes the shared path, so the conflict class disappears rather than
being merged around — `merge=union` is not available here, because it silently
reparents unreleased work under a tagged release.

**Three states, never two.** This script can `ok`, it can produce a `finding`
(refused, naming the file), and it can `skip` — and it says which, every run.
An assembler that finds no fragments and exits 0 has reported "released" when
what happened is "nothing to release", which is the defect class this tracker
is full of: an absence produced by a tool read as an absence in the world.

Stdlib plus `markdown-it-py`, which is the one dependency and is the point.
`towncrier` and `scriv` both solve the assembling half and both are
dependencies; this repo still ships one file and no install step, and nothing
a user installs imports this — it is a repo-internal release tool.

    python3 scripts/assemble_changelog.py --version 0.24.0 --dir changelog.d --changelog CHANGELOG.md
    python3 scripts/assemble_changelog.py --check     # CI: names *and* bodies
    python3 scripts/assemble_changelog.py --count     # exact fragment count

**The fold names its own target; the read-only modes derive one.** `REPO` is
found by walking up from *this file* for a `.git`, which answers "which
repository am I stored in" -- not "which repository is being released". Those
coincide for the copy vendored into a managed repo at
`.oss/assemble_changelog.py` and they do not coincide for the copy shipped
inside the plugin, whose own checkout is always a clone: the walk there always
succeeds, and always on the wrong repository, so the `None` arm that refuses
cleanly is unreachable in precisely the deployment where the guess is wrong.

The requirement is **unconditional** rather than applied to one copy, and that
is a decision, not an oversight. Nothing observable distinguishes the two: both
copies sit at some depth inside a real clone, and the difference between them
is what the caller meant, which is not on disk. A detector would be guessing,
and the two directions of a wrong guess are not comparable -- a false negative
rewrites `CHANGELOG.md` and deletes every fragment in a repository nobody
named, while a false positive costs one line of typing that the refusal itself
prints. So the vendored copy pays the same two flags, and every invocation this
plugin generates passes them. The read-only modes keep the derived default:
they only read, and requiring the flags there would break the `--check` gate in
every managed repo at once.

Exit codes: 0 ok, 1 skipped (nothing to do, or nothing *provable* — stated
either way), 2 refused (a finding).

**1 is never returned by a run that changed the tree.** Every mutation happens
inside one guarded block whose only failure exit is 2, because "nothing to do"
is the one answer a caller must never be given about a release that already
half-happened. A refusal from that block says so in as many words, on stderr,
and does not claim CHANGELOG.md is untouched — which every other refusal here
does claim, and means.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import (AbstractSet, Dict, Iterator, List, Optional, Sequence, Set,
                    Tuple)

try:
    import markdown_it as _markdown_it
    from markdown_it import MarkdownIt as _MarkdownIt
except Exception as _import_error:  # pragma: no cover - exercised by monkeypatch
    _markdown_it = None
    _MarkdownIt = None
    _MD_IMPORT_ERROR = "{0}: {1}".format(type(_import_error).__name__, _import_error)
else:
    _MD_IMPORT_ERROR = None

_MD_VERSION = getattr(_markdown_it, "__version__", "unknown")

def _find_repo_root(start: Path) -> Optional[Path]:
    """Walk upward from *start* for a `.git` entry -- a directory in an
    ordinary clone, a file (`gitdir: ...`) inside a worktree. Stops at the
    first match instead of assuming a fixed number of parents: the script
    lives at `scripts/` here but is vendored into scaffolded repos as
    `.oss/assemble_changelog.py`, a different depth, and a hardcoded parent
    count is the same bug with a different number. Returns None
    rather than guessing when no `.git` is found anywhere above."""
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


REPO = _find_repo_root(Path(__file__).resolve().parent)

#: Keep a Changelog 1.1.0, in the order the spec lists them. The order is data,
#: not a sort: "Added" before "Fixed" is a convention readers rely on, and
#: alphabetical would put Security second.
SECTIONS = ("added", "changed", "deprecated", "removed", "fixed", "security")

#: `<issue>.<section>[.<slug>].md`. The slug exists so one issue can file two
#: entries in one section without the two PRs colliding on a path again.
#: `\Z` and not `$`. A POSIX filename may end in a newline, and `$` matched
#: before one — so `1188.fixed.md\n` parsed as a fragment for issue 1188, got
#: folded into the release and then deleted as consumed.
_NAME_RE = re.compile(r"^(\d+)\.([a-z]+)(?:\.([A-Za-z0-9][A-Za-z0-9._-]*))?\.md\Z")

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\Z")  # \Z, not $ (a POSIX filename may end in a newline)

#: Not fragments, and not mistakes either — refusing these would make the
#: directory unable to document itself.
_IGNORED = {"README.md", ".gitkeep", ".gitignore"}

_UNRELEASED_LINK_RE = re.compile(  # anchored-ok: matched per line of CHANGELOG.md; the newline is the delimiter
    r"^\[Unreleased\]:\s*(?P<base>\S+?)/compare/v(?P<prev>[0-9][^.\s]*(?:\.[^.\s]+)*)\.\.\.HEAD\s*$"
)

#: Any link-reference definition, used to find the block of them at the bottom.
#: 0-3 leading spaces, like everything else CommonMark calls a link ref.
#: This pattern decides where the trailing block *starts*, and anchored at column
#: 0 it stopped its backward walk at an indented definition — truncating the block
#: or missing it entirely, so the release advanced no link at all and shipped a
#: `## [x.y.z]` heading whose link resolves to nothing, under a receipt that said
#: only "no compare line found". Recognising an indented line as part of the block
#: is not permission to write it: `_UNRELEASED_LINK_RE` stays anchored at column
#: 0, which is where the assembler's own line is, so an indented look-alike inside
#: the block cannot capture the rewrite.
_LINK_REF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*\S")

#: Any `[Unreleased]` definition, whatever it points at. A repo that has never
#: released cannot have a `compare/vX...HEAD` line — there is no earlier tag to
#: compare from — so `_UNRELEASED_LINK_RE` matches nothing on a first release
#: and the base URL has to come from whatever the definition does hold.
#: Anchored at column 0 for the same reason that one is: this is a line the
#: assembler rewrites, and an indented look-alike inside the block must not
#: capture the rewrite.
_UNRELEASED_ANY_LINK_RE = re.compile(r"^\[Unreleased\]:\s*(?P<url>\S+)\s*$")

#: The repository URL under a forge path. `commits/HEAD` is what Keep a
#: Changelog's own template writes for a project with no releases; the others
#: are what hand-written files carry. A URL with none of these segments is not
#: something to guess a base from, and that case is reported rather than
#: patched over.
_FORGE_BASE_RE = re.compile(
    r"^(?P<base>\S+?)/(?:commits|commit|compare|tree|releases)(?:/\S*)?\Z")

#: The guard and the reader are the same parser now.
#:
#: Three rounds of hand-written Markdown scanning produced three bypasses, and
#: each fix opened the next hole. The first attempt anchored its patterns at
#: column 0, and an audit found three ways past it. The second attempt widened
#: them to `^ {0,3}` and made labels case-insensitive, and the next audit found
#: six more plus a false refusal plus a prescribed remedy that was itself an
#: injection. The third attempt inverted to a whitelist resting on a
#: positional guarantee and its own fence state machine, and a further audit
#: walked straight through the fence: a column-0 line inside an open fence was
#: `continue`d with no indent check and no opener check, so
#: `# INJECTED HEADING` and `[Unreleased]: https://evil.example/pwned` were
#: copied verbatim into the released file under a receipt that said `ok`.
#:
#: Every one of those is the same shape — **our scanner disagreed with
#: CommonMark.** Column 0 versus 0-3 leading spaces; ATX versus setext; our
#: fence state machine versus the real one; our info-string handling versus
#: the spec's, which forbids a backtick inside a backtick fence's info string,
#: so ``` `x` is an ordinary paragraph to a reader and was an open fence here.
#: That race is not winnable by patching patterns, and the fourth attempt at
#: patterns would have lost it the same way, so this stops running it.
#:
#: `markdown-it-py` is a CommonMark reference implementation and was already
#: this file's test oracle. It is the guard itself now. The guard and the
#: reader agree by construction, which is the only property that closes the
#: class rather than the instance.
#:
#: **What the guard establishes**, which is also everything it claims: parsed
#: as CommonMark, the fragment produces no heading, no link-reference
#: definition and no raw HTML at any depth; every fence it opens closes inside
#: it; and its top level is one `-` bullet list, which is what `_entry_count`
#: is counting when the balance guard proves nothing was lost.
#:
#: **What it does not establish** is that the released file is sound, because
#: a fragment is validated alone and inserted into a document. So the write is
#: verified separately, against the assembled text — see `_verify_written`.
#: One guard has now been wrong three times; the second layer is what makes
#: the fourth time survivable.
_BULLET = "- "

#: Token types that restructure a document, at any depth. `heading_open`
#: covers ATX and setext alike because the parser has already resolved which
#: is which. `html_inline` is here because `<h1>` mid-paragraph is not an
#: `html_block` and renders the same heading — the previous guard refused a
#: line *starting* with `<` and said so in its message, and put the same tag
#: after a word to sail past. Link-reference definitions are not tokens at
#: all; they are collected into the parse environment, which is checked
#: alongside these.
_REFUSABLE = {
    "heading_open": "a Markdown heading",
    "html_block": "a raw HTML block, which renders as a heading without being one",
    "html_inline": "raw HTML inside a paragraph, which renders a heading tag",
}

#: Where a fragment's links may point. `http` and `https` are what a changelog
#: cites; `mailto` opens a composer and fetches nothing. A destination carrying
#: no scheme at all is a path inside this repository and is allowed unlisted,
#: which is the case the allowlist exists to keep cheap.
#:
#: Images are not on this list and get no scheme of their own, deliberately. A
#: link is inert until a reader clicks it; an image is fetched by whatever
#: renders CHANGELOG.md, without anyone deciding to fetch it, so an off-repo
#: image reports every reader of the release notes to whoever serves it. That
#: is true of a perfectly ordinary `https` URL, which is why a scheme allowlist
#: alone does not reach this and images are held to `local only` instead.
_LINK_SCHEMES = ("http", "https", "mailto")

#: RFC 3986 section 3.1, applied to a destination the parser has already found,
#: decoded and normalised. This is not a fourth round of the hand-written
#: Markdown scanning the note above retired: every bypass in that history was a
#: disagreement with CommonMark about *where a construct begins in the source*,
#: and locating the destination is still the parser's job here. What is left is
#: reading a scheme off an isolated URI, whose grammar is one line long and is
#: not Markdown's.
_SCHEME_RE = re.compile(r"\A([A-Za-z][A-Za-z0-9+.\-]*):")

#: Characters a browser's URL parser discards before it decides what scheme it
#: is looking at. `java<TAB>script:` is `javascript:` to a reader, and the
#: Markdown parser percent-encodes that tab rather than dropping it, so both the
#: literal control character and its escape have to go before the front of the
#: string means anything. Used only to classify -- never to rewrite what an
#: author wrote.
_DISCARDED_IN_URL = re.compile(r"%0[0-9aAdD]|%1[0-9a-fA-F]|%20|%7[fF]|[\x00-\x20\x7f]")

_URL_SHAPE = ("a fragment's links may point at http, https, mailto or a path "
              "inside this repository, and its images at a path inside this "
              "repository only")

_REMEDY_LINK = ("Write the destination as an http or https URL, or as a path "
                "relative to the repository root. To *show* a scheme rather "
                "than link with it, put it in backticks: CHANGELOG.md is "
                "rendered by tools this repository does not choose, and a "
                "scheme one renderer strips is live in the next one, so what "
                "is inert where it was tested is not inert where it is read.")

_REMEDY_REMOTE_IMAGE = ("Commit the image into this repository and reference it "
                        "by relative path, or link to it instead of embedding "
                        "it -- `[screenshot](https://...)` is allowed, because "
                        "a link waits to be clicked and an image does not. The "
                        "URL a pull request gives you for a dragged-in "
                        "screenshot is off-repo and lands here.")

_REMEDY_DATA_IMAGE = ("A `data:` image is not remote, and that is the whole of "
                      "what can be said for it: it is an unbounded opaque blob "
                      "in a file whose diff is its review, so nobody reviewing "
                      "the release can see what shipped. Commit the image and "
                      "reference it by relative path.")

_SHAPE = ("a fragment is `- ` bullets at column 0 plus lines indented under "
          "them, and parsed as CommonMark it may hold no heading, no link ref "
          "definition and no raw HTML at any depth")

_REMEDY = ("To show one in an entry, put it in a fenced code block at the "
           "bullet's own indent (```), which is what every fenced example in "
           "CHANGELOG.md already does — and close the fence at that same "
           "indent, because a line reaching column 0 ends the bullet, the "
           "fence and the list, whatever the fence was meant to be hiding. "
           "Indenting further is not a remedy: inside a `- ` bullet an "
           "indented line is still a live heading and a live definition, which "
           "is what the advice this message used to give got wrong.")

#: Said in full wherever a run cannot validate, because the alternative is a
#: receipt with nothing behind it — which is the thing this file exists to
#: stop being possible.
_NO_PARSER = (
    "markdown-it-py is not importable ({0}), so nothing can be established "
    "about these fragments and nothing is claimed. Install it — "
    "`pip install markdown-it-py` — and run again; in CI, install it in the "
    "same job that runs this, because a job that skipped it has not checked "
    "anything it is about to report on. "
    "There is deliberately no text-scanning fallback: three of them shipped "
    "and all three were bypassed within one audit, so a "
    "fallback here would be the same bug wearing a receipt.")


class CannotValidate(Exception):
    """The tool cannot answer. Not a finding, and emphatically not an `ok`."""


def _parser():
    if _MD_IMPORT_ERROR is not None or _MarkdownIt is None:
        raise CannotValidate(_NO_PARSER.format(_MD_IMPORT_ERROR or "unavailable"))
    return _MarkdownIt("commonmark")


def _scanning_parser():
    """The same parser with its own link sanitiser switched off.

    markdown-it refuses a `javascript:` destination itself: it declines to build
    a link and leaves the source as literal text. That is the right default for
    a renderer and it is the wrong one for a guard, because it means there is no
    `link_open` token to inspect for precisely the destinations worth
    inspecting. A scheme allowlist walked over a stock parse would have refused
    nothing at all in that case while reading, in CI, as though it worked.

    Refusing to build the link also does not make the text safe. `render` copies
    the fragment body verbatim, so the source reaches CHANGELOG.md either way,
    and CHANGELOG.md is rendered by tools this repository does not choose --
    each with its own idea of which schemes to strip.

    So the parser keeps the job it is here for, finding where a destination
    begins and ends and decoding it, and the allowlist below decides. Turning
    the sanitiser off widens what the guard *sees*; it never widens what the
    guard *permits*.
    """
    md = _parser()
    md.validateLink = lambda url: True
    return md


def _flatten(tokens: Sequence, line: Optional[int] = None) -> Iterator[Tuple[object, int]]:
    """Every token in document order, each with the nearest line it maps to.

    Inline tokens carry no map of their own, so they inherit their block's.
    A finding without a line number sends the author hunting, and the author
    is the person standing in CI when this fires.
    """
    for token in tokens:
        at = token.map[0] if token.map else line
        yield token, (at if at is not None else 0)
        if token.children:
            for pair in _flatten(token.children, at):
                yield pair


def _finding(name: str, number: int, what: str, line: str) -> str:
    """One refusal, naming the file, the line number, the shape and the remedy."""
    return ("{0}:{1}: {2} — {3}. Inserted verbatim into CHANGELOG.md, this line "
            "becomes one. {4} Line: {5}"
            .format(name, number, what, _SHAPE, _REMEDY, line.strip()[:120]))


def _url_finding(name: str, number: int, what: str, remedy: str,
                 line: str) -> str:
    """One refusal about where something points, rather than about shape.

    Separate from `_finding` because that one ends in advice about fenced code
    blocks and indentation, which answers nothing an author asked when their
    screenshot URL was turned away.
    """
    return ("{0}:{1}: {2} — {3}. {4} Line: {5}"
            .format(name, number, what, _URL_SHAPE, remedy, line.strip()[:120]))


def _line_of_reference(md, lines: Sequence[str], label: str) -> int:
    """The first line at which `label` becomes a definition, per the parser.

    Bisecting the parse rather than matching a pattern: a definition's label
    may run across lines and may carry escaped brackets, and every regex this
    file has owned for that shape has been wrong. Fragments are a handful of
    lines, so the cost of re-parsing prefixes is not worth a cleverer answer.
    """
    for count in range(1, len(lines) + 1):
        env: Dict = {}
        md.parse("\n".join(lines[:count]) + "\n", env)
        if label not in env.get("references", {}):
            continue
        # `count` is where it *ends*. Its own first line is the largest start
        # whose slice still defines the label, so a definition split across
        # lines is reported where the author began writing it rather than
        # where the parser happened to finish reading it.
        for start in range(count, 0, -1):
            env = {}
            md.parse("\n".join(lines[start - 1:count]) + "\n", env)
            if label in env.get("references", {}):
                return start
        return count
    return 1


def _fence_is_closed(lines: Sequence[str], token) -> bool:
    """Whether a fence token's own last line is its closer.

    markdown-it closes an unterminated fence at the end of its container and
    reports no error, so a fence that runs on is indistinguishable from one
    that closed unless the source is consulted. A one-line fence never closed;
    otherwise the last line of the token's span has to be a bare run of the
    opening character, at least as long as the opener.
    """
    if not token.map or token.map[1] - token.map[0] < 2:
        return False
    closer = lines[token.map[1] - 1].strip()
    marker = (token.markup or "`")[0]
    return bool(closer) and set(closer) == {marker} and len(closer) >= len(token.markup)


def _structure_findings(name: str, lines: Sequence[str], tokens: Sequence) -> List[str]:
    """The shape rule, derived from the parse instead of from line prefixes.

    `_entry_count` counts lines beginning `- ` and the balance guard trusts
    that count to prove the cut lost nothing. So the top level has to be one
    `-` bullet list whose items start at column 0, or the arithmetic and the
    document disagree and a lossy cut reports as a clean one. Asking the
    parser rather than the first two characters is what now catches an ordered
    list and a bare table, which the prefix test waved through.
    """
    findings: List[str] = []
    # `nesting >= 0` is openers *and* leaf blocks. A fenced code block is a
    # leaf — `nesting == 0`, no closing token — so counting openers alone
    # counted a column-0 fence as no block at all, which is the shape
    # identified as the likely accidental trigger.
    top = [t for t in tokens if t.level == 0 and t.nesting >= 0]
    if len(top) != 1 or top[0].type != "bullet_list_open":
        at = top[1].map[0] + 1 if len(top) > 1 and top[1].map else 1
        return [_finding(name, at,
                         "a fragment whose top level is not a single `- ` bullet list",
                         lines[at - 1] if at <= len(lines) else "")]
    if top[0].markup != "-":
        return [_finding(name, (top[0].map[0] + 1) if top[0].map else 1,
                         "a list marked `{0}`, which `_entry_count` does not count"
                         .format(top[0].markup),
                         lines[top[0].map[0]] if top[0].map else "")]
    for token in tokens:
        if token.type == "list_item_open" and token.level == 1 and token.map:
            if not lines[token.map[0]].startswith(_BULLET):
                findings.append(_finding(
                    name, token.map[0] + 1,
                    "a top-level list item that does not begin `- `",
                    lines[token.map[0]]))
    return findings


def _classify(url: str) -> Tuple[str, str]:
    """(shape, scheme) of a destination: `scheme`, `network-relative` or `local`.

    `//evil.example/x` carries no scheme and is not local either, so `no scheme
    means relative` is one case short of correct and the short version lets a
    remote destination through an allowlist that never looked at it.
    """
    bare = _DISCARDED_IN_URL.sub("", url)
    match = _SCHEME_RE.match(bare)
    if match:
        return "scheme", match.group(1).lower()
    if bare.startswith("//"):
        return "network-relative", ""
    return "local", ""


def _destination_refusal(kind: str, url: str) -> Tuple[Optional[str], str]:
    """Why this destination is refused for a link or an image, and the remedy.

    `(None, "")` when it is allowed. The asymmetry between the two kinds is the
    decision this function exists to hold: an `https` URL is allowed as a link
    and refused as an image.
    """
    shape, scheme = _classify(url)
    shown = url.strip()[:80]
    if kind == "image":
        if shape == "local":
            return None, ""
        if shape == "scheme" and scheme == "data":
            return ("an image inlined as a `data:` URL", _REMEDY_DATA_IMAGE)
        return ("an image loaded from off this repository (`{0}`), which every "
                "reader of the release notes fetches, reporting themselves to "
                "whoever serves it".format(shown), _REMEDY_REMOTE_IMAGE)
    if shape == "local" or (shape == "scheme" and scheme in _LINK_SCHEMES):
        return None, ""
    if shape == "network-relative":
        return ("a link to a scheme-relative destination (`{0}`), which is off "
                "this repository however the reader arrived at the file"
                .format(shown), _REMEDY_LINK)
    return ("a link with the `{0}:` scheme (`{1}`), which is not one of {2}"
            .format(scheme, shown,
                    ", ".join("`{0}`".format(s) for s in _LINK_SCHEMES)),
            _REMEDY_LINK)


def _destination_findings(name: str, lines: Sequence[str],
                          tokens: Sequence) -> List[str]:
    """Findings for every link and image destination in the fragment."""
    findings: List[str] = []
    for token, at in _flatten(tokens):
        if token.type == "link_open":
            kind, url = "link", token.attrGet("href") or ""
        elif token.type == "image":
            kind, url = "image", token.attrGet("src") or ""
        else:
            continue
        what, remedy = _destination_refusal(kind, url)
        if what is not None:
            findings.append(_url_finding(
                name, at + 1, what, remedy,
                lines[at] if at < len(lines) else ""))
    return findings


def scan_fragment_body(name: str, text: str) -> List[str]:
    """Findings for one fragment's content, each naming the file and the line.

    Raises `CannotValidate` when the parser is absent. It does not return an
    empty list in that case: an empty list means "looked, found nothing", and
    conflating that with "did not look" is the defect this tracker is full of.
    """
    md = _scanning_parser()
    lines = text.splitlines()
    env: Dict = {}
    tokens = md.parse(text, env)

    findings = _structure_findings(name, lines, tokens)
    findings.extend(_destination_findings(name, lines, tokens))

    if "\t" in text:
        at = next(i for i, line in enumerate(lines) if "\t" in line)
        findings.append(_finding(
            name, at + 1,
            "a tab, which the shipped CHANGELOG.md contains none of and which "
            "reaches a different column in every renderer",
            lines[at]))

    for token, at in _flatten(tokens):
        what = _REFUSABLE.get(token.type)
        if what is not None:
            findings.append(_finding(name, at + 1, what,
                                     lines[at] if at < len(lines) else ""))
        elif token.type == "fence" and not _fence_is_closed(lines, token):
            findings.append(_finding(
                name, at + 1,
                "a fenced code block that is never closed at the indent it "
                "opened, which swallows what follows it in CHANGELOG.md",
                lines[at] if at < len(lines) else ""))

    for label in env.get("references", {}):
        at = _line_of_reference(md, lines, label)
        findings.append(_finding(
            name, at,
            "a link ref definition of `[{0}]` — the first definition of a "
            "label is the one that resolves, and a fragment lands above the "
            "genuine block at the bottom of the file".format(label),
            lines[at - 1] if at <= len(lines) else ""))

    return sorted(set(findings), key=findings.index)

OK, SKIPPED, REFUSED = 0, 1, 2


class BadFragment(Exception):
    """A fragment this script will not guess about. The message names the file."""


@dataclass(frozen=True)
class Fragment:
    issue: int
    section: str
    slug: str
    path: Optional[Path] = None

    @property
    def sort_key(self) -> Tuple[int, int, str]:
        return (SECTIONS.index(self.section), self.issue, self.slug)


def parse_fragment_name(name: str) -> Fragment:
    """Parse a fragment filename, or refuse by name.

    Refusing rather than skipping is the whole point: a file the release tool
    silently passed over is an entry that never ships and that nobody is told
    about.
    """
    match = _NAME_RE.match(name)
    if not match:
        raise BadFragment(
            f"{name}: filename does not parse as <issue>.<section>[.<slug>].md "
            f"(e.g. 906.added.md, 878.fixed.second-entry.md)"
        )
    section = match.group(2)
    if section not in SECTIONS:
        raise BadFragment(
            f"{name}: unknown section {section!r} — expected one of: {', '.join(SECTIONS)}"
        )
    return Fragment(issue=int(match.group(1)), section=section, slug=match.group(3) or "")


#: How a body may name its own issue: `#NNN`, or a tracker URL ending in the
#: number. Both are forms an author writes on purpose. A bare `NNN` is not —
#: one fragment was findable only because it happened to cite a test file whose
#: name embedded the issue number, which no reader would aim at and no author
#: could be told to produce.
_SELF_REF = r"(?:#|/(?:issues|pull)/){0}(?![0-9])"


def self_reference_finding(name: str, text: str) -> Optional[str]:
    """One finding if the body never names the issue in its own filename.

    `changelog.d/<issue>.<section>.md` holds the number in exactly one
    structural place, and assembly writes the *body* and deletes the file. So
    the number survives the release only when the author typed it into the
    prose, which made findability a property of author habit: measured on the
    fragments as they stood at each release commit, **8 of 20 entries in
    v0.32.0** and **6 of 28 in v0.33.0** named every issue but their own, and
    only two of the twenty had a `test_the_change_is_findable` to say so.

    Refusing here rather than appending a reference during assembly is a
    choice about where the rule lives. An append needs an "is it already
    there?" test, and that test cannot tell a self-citation from a coincidence
    — one fragment was findable only because a *different* fragment in the
    same release mentioned it. Refusing costs the author one `(#N)` in a PR
    instead of a release-time repair across thirteen legs, and it is the
    `(#N)` form fragments are expected to use.

    Returns `None` for a name that does not parse: `collect` already reports
    that from `parse_fragment_name`, and a second complaint about one file
    would give the write-time validator a different count from `--check`.
    """
    try:
        issue = parse_fragment_name(name).issue
    except BadFragment:
        return None
    number = str(issue)
    if re.search(_SELF_REF.format(number), text):
        return None
    lines = text.splitlines()
    at = next((i + 1 for i, line in enumerate(lines) if line.strip()), 1)
    return (
        "{0}:{1}: the entry never names #{2} — the issue number is in the "
        "filename, and the release consumes the file, so nothing carries it "
        "into CHANGELOG.md. Write `(#{2})` into the entry — a link to the "
        "issue counts too. Line: {3}"
        .format(name, at, number, lines[at - 1] if at <= len(lines) else ""))


def collect(directory: Path) -> List[Fragment]:
    """Every fragment in `directory`, sorted deterministically.

    All findings are gathered before raising: a release cut is a one-shot
    operation and reporting one bad name per run turns it into a queue.
    """
    if not directory.is_dir():
        raise BadFragment(f"{directory}: fragment directory does not exist")

    fragments: List[Fragment] = []
    findings: List[str] = []
    for path in sorted(directory.iterdir()):
        if path.is_dir() or path.name in _IGNORED or path.name.startswith("."):
            continue
        try:
            frag = parse_fragment_name(path.name)
        except BadFragment as exc:
            findings.append(str(exc))
            continue
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            findings.append(f"{path.name}: fragment is empty — an entry nobody would ever read")
            continue
        # Ahead of the body scan, which is the arm that needs `markdown-it-py`:
        # this finding needs no parser, and a definite refusal must not be lost
        # behind a `CannotValidate` raised by the check after it. It does not
        # `continue`, though — preempting the shape scan would answer a
        # malformed fragment with a note about its issue number and say nothing
        # about the malformation, which is one round-trip per finding for the
        # author and the reason `collect` gathers rather than stopping.
        self_ref = self_reference_finding(path.name, text)
        if self_ref is not None:
            findings.append(self_ref)
        try:
            body_findings = scan_fragment_body(path.name, text)
        except CannotValidate:
            # A refusal that needed no parser outranks "could not look". The
            # alternative loses the definite answer to report the absent one,
            # which is the shape `validators/changelog-fragment` already names.
            if self_ref is None:
                raise
            continue
        if self_ref is not None or body_findings:
            findings.extend(body_findings)
            continue
        fragments.append(Fragment(frag.issue, frag.section, frag.slug, path))

    if findings:
        raise BadFragment("\n".join(findings))
    return sorted(fragments, key=lambda f: f.sort_key)


def _trim(block: List[str]) -> List[str]:
    """Drop leading and trailing blank lines, keep the ones in the middle."""
    while block and not block[0].strip():
        block.pop(0)
    while block and not block[-1].strip():
        block.pop()
    return block


def _subsections(body: Sequence[str]) -> Tuple[List[str], List[Tuple[str, List[str]]]]:
    """Split a section body into loose preamble and `### Heading` -> its lines.

    Indented continuation paragraphs stay with the entry above them: nothing is
    re-wrapped or re-parsed, lines are carried across verbatim. Entries in this
    changelog run to several paragraphs, and a fold that kept only the bullet
    would be loss reported as success.
    """
    preamble: List[str] = []
    sections: List[Tuple[str, List[str]]] = []
    for line in body:
        if line.startswith("### "):
            sections.append((line.strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return _trim(list(preamble)), [(title, _trim(block)) for title, block in sections]


def _merge_by_title(sections: Sequence[Tuple[str, List[str]]]) -> dict:
    """Fold same-named `###` blocks together, keyed case-insensitively.

    An `[Unreleased]` section that already carries two `### Fixed` headings is
    a live bug: a duplicated heading reparents everything between the two
    copies. Emitting both again would carry the defect into a tagged release,
    so they merge here.
    """
    merged: dict = {}
    for title, block in sections:
        key = title.lower()
        if key in merged:
            if block:
                merged[key][1].extend([""] + block)
        else:
            merged[key] = [title, list(block)]
    return merged


def _entry_count(lines: Sequence[str]) -> int:
    """Top-level `- ` bullets. Continuation paragraphs indent, so they do not count."""
    return sum(1 for line in lines if line.startswith("- "))


def render(fragments: Sequence[Fragment], version: str, date: str,
           residue_preamble: Sequence[str] = (),
           residue_sections: Sequence[Tuple[str, List[str]]] = ()
           ) -> Tuple[str, List[str]]:
    """The release section as text, and the heading lines it wrote.

    Sections in Keep a Changelog order; within each, the folded `[Unreleased]`
    residue first (it has been pending longer), then the fragments in issue
    order. One heading per section whichever side supplied it.

    The second return value is the point of the signature: `_verify_written`
    re-parses the assembled file and needs to know which headings this
    function is *entitled* to have added, so that anything else in the result
    is a finding. Deriving that list by pattern-matching the output would put
    the verifier back on the same footing as the guard it exists to backstop.
    """
    out = ["## [{0}] - {1}".format(version, date), ""]
    emitted = [out[0]]
    if any(line.strip() for line in residue_preamble):
        out.extend(residue_preamble)
        out.append("")

    merged = _merge_by_title(residue_sections)
    used = set()
    for section in SECTIONS:
        title = "### {0}".format(section.capitalize())
        residue = merged.get(title.lower())
        chosen = [f for f in fragments if f.section == section]
        if not residue and not chosen:
            continue
        used.add(title.lower())
        out.append(title)
        emitted.append(title)
        out.append("")
        if residue and residue[1]:
            out.extend(residue[1])
            out.append("")
        for frag in chosen:
            assert frag.path is not None
            out.append(frag.path.read_text(encoding="utf-8").strip("\n").rstrip())
            out.append("")

    # Headings the spec does not list are content, not a parse failure. They keep
    # their own order, after the six known ones.
    for key, (title, block) in merged.items():
        if key in used or not block:
            continue
        out.append(title)
        emitted.append(title)
        out.append("")
        out.extend(block)
        out.append("")
    return "\n".join(out), emitted


def _document_facts(text: str) -> Tuple[Counter, Dict[str, str], int]:
    """(heading multiset, label -> destination, raw-HTML count) of a document.

    The three properties a fragment can forge, read off a real parse of the
    whole file rather than inferred from the fragment that went into it.
    """
    md = _parser()
    env: Dict = {}
    flat = [token for token, _ in _flatten(md.parse(text, env))]
    headings: Counter = Counter()
    for index, token in enumerate(flat):
        if token.type == "heading_open":
            title = flat[index + 1].content if index + 1 < len(flat) else ""
            headings[(token.tag, title)] += 1
    refs = {label: value.get("href")
            for label, value in env.get("references", {}).items()}
    raw = sum(1 for token in flat if token.type in ("html_block", "html_inline"))
    return headings, refs, raw


def _disallowed_destinations(text: str) -> Counter:
    """Multiset of the link and image destinations a document holds that a
    fragment would not be allowed to carry.

    Read off the assembled file so the second layer covers destinations too. It
    is a *delta* against the file as it stood, like every other check in
    `_verify_written`: CHANGELOG.md's own preamble links and the compare URLs a
    release rewrites are already there and are not this release's doing.
    """
    found: Counter = Counter()
    for token, _ in _flatten(_scanning_parser().parse(text, {})):
        if token.type == "link_open":
            kind, url = "link", token.attrGet("href") or ""
        elif token.type == "image":
            kind, url = "image", token.attrGet("src") or ""
        else:
            continue
        what, _remedy = _destination_refusal(kind, url)
        if what is not None:
            found[(kind, url)] += 1
    return found


def _headings(text: str) -> List[Tuple[int, str, str]]:
    """(line index, tag, title) for every heading the parser actually sees.

    `line.startswith("## [")` was the old test, and it does not survive a
    fenced example of a release heading: inert to a reader, it was mistaken
    for an anchor to this file. A fenced block is the documented way to quote
    a heading in an entry, so CHANGELOG.md acquires such lines by design, not
    by attack.
    """
    md = _parser()
    flat = [token for token, _ in _flatten(md.parse(text, {}))]
    found = []
    for index, token in enumerate(flat):
        if token.type == "heading_open" and token.map:
            found.append((token.map[0], token.tag,
                          flat[index + 1].content if index + 1 < len(flat) else ""))
    return found


def _inert_lines(text: str) -> Set[int]:
    """Line indices inside a code block or raw HTML block, per the parser.

    Every positional scanner in this file used to read these lines as live.
    They are the lines a reader's parser will not act on, so they are the
    lines a release must not act on either.
    """
    inert: Set[int] = set()
    for token, _ in _flatten(_parser().parse(text, {})):
        if token.type in ("fence", "code_block", "html_block") and token.map:
            inert.update(range(token.map[0], token.map[1]))
    return inert


def _crowded_headings(text: str) -> Set[str]:
    """Titles of headings written directly against the line above them.

    CommonMark lets an ATX heading interrupt a paragraph, so GitHub renders
    one of these correctly and nothing looks wrong; it is only wrong in the
    source, and only to a stricter parser, which folds the heading into the
    paragraph before it. The artefact that breaks is the one users read to
    decide whether to upgrade.

    Keyed by title rather than by line, because the caller subtracts the
    before-set from the after-set. The four instances already in the file
    shipped inside tags and GitHub release notes; repairing them would make
    CHANGELOG.md stop matching what was published, so they are carried
    forward and only a *new* one is a finding.

    Positional on purpose: the blank line is a property of the bytes, which is
    what the stricter parser reads. The *set of headings* still comes from the
    parser, so a fenced example of a release heading is not one of these.
    """
    lines = text.splitlines()
    return {title for index, _, title in _headings(text)
            if index and lines[index - 1].strip()}


def _section_lines(section: str) -> List[str]:
    """A rendered release section as lines, ending in exactly one blank.

    `render` builds its list ending in `""` and joins it, so the text ends in
    a newline — and `str.splitlines()` drops the empty field that newline
    produces. The section's last body line then landed directly against the
    `## [x.y.z]` heading it was spliced above, on every release since 0.25.0.
    `split("\n")` keeps that field; the normalisation below states the
    invariant the splice depends on rather than inheriting it from `render`.
    """
    lines = section.split("\n")
    while len(lines) > 1 and not lines[-1] and not lines[-2]:
        lines.pop()
    if not lines or lines[-1]:
        lines.append("")
    return lines


def _anchor(headings: Sequence[Tuple[int, str, str]]) -> int:
    """Where the new release section goes: above the newest existing release.

    The first `h2` whose title opens `[` and is not `[Unreleased]`. Everything
    between the `[Unreleased]` heading and this line is residue that gets
    folded into the release being cut — `[Unreleased]` means "goes out next",
    so it does.
    """
    for index, tag, title in headings:
        if tag == "h2" and title.startswith("[") and not title.startswith("[Unreleased]"):
            return index
    raise BadFragment(
        "CHANGELOG.md has no `## [x.y.z]` release heading to insert above — "
        "refusing rather than guessing where a release section belongs"
    )


def _first_release_anchor(lines: Sequence[str],
                          headings: Sequence[Tuple[int, str, str]],
                          inert: Set[int]) -> int:
    """Where a *first* release section goes, when there is no release to
    insert above. Raises when the file's shape cannot defend a position.

    Reached only when `_anchor` raised, which happens only when the document
    holds no `## [x.y.z]` heading at all. A repo that has released has an
    unambiguous insertion point and keeps using it; this relaxation is for the
    one cut where the anchor cannot exist yet, and the alternative it replaces
    was a maintainer hand-writing a release section for a version that never
    shipped, so that the parser would find something to sit above.

    **"The top" is not one place.** A changelog opens with an `h1`, usually a
    Keep a Changelog blurb, sometimes a policy paragraph, and closes with a
    link-ref block. So the position is not chosen from the top of the file; it
    is read off the one structure that already means "goes out next" — the
    `## [Unreleased]` heading. The new section goes directly below it, which is
    exactly where every later release goes, and the body between is folded in
    exactly as it is on every later release. Nothing about the first cut is
    special-cased except the boundary this returns.

    That boundary is the first thing below `[Unreleased]` that is not part of
    it: the next `h1`/`h2`, or the trailing link-ref block, whichever comes
    first, or the end of the file. Without it the fold would swallow a
    `## Notes` section and the link refs into the release.

    Two shapes are refusals, not defaults. **No `## [Unreleased]` heading**:
    there is no structure to read a position off, and picking one would be the
    guess this script exists to not make. **More than one**: which of them a
    first release belongs under is a coin toss. Both name what would make it
    decidable, because a refusal a maintainer cannot act on is a dead end at
    the exact moment they are least able to tell a tool limitation from a
    mistake of their own.
    """
    unreleased = [index for index, tag, title in headings
                  if tag == "h2" and title.startswith("[Unreleased]")]
    if not unreleased:
        raise BadFragment(
            "CHANGELOG.md has no `## [x.y.z]` release heading to insert above, "
            "and no `## [Unreleased]` heading to cut a first release below "
            "either — so there is no position in it this script can defend, "
            "and it will not pick between the preamble, the blurb and the "
            "link-ref block. Add a `## [Unreleased]` heading and re-run: with "
            "no release heading anywhere, the first release is cut directly "
            "below it.")
    if len(unreleased) > 1:
        raise BadFragment(
            "CHANGELOG.md has no `## [x.y.z]` release heading to insert above "
            "and {0} `## [Unreleased]` headings (lines {1}) to cut a first "
            "release below, so which one it belongs under is a guess. Leave "
            "exactly one `## [Unreleased]` heading and re-run.".format(
                len(unreleased), ", ".join(str(i + 1) for i in unreleased)))
    start = unreleased[0]
    ends = [index for index, tag, _ in headings
            if index > start and tag in ("h1", "h2")]
    block = _link_ref_block(lines, inert)
    if block and block[0] > start:
        ends.append(block[0])
    return min(ends) if ends else len(lines)


def _unreleased_span(lines: Sequence[str], headings: Sequence[Tuple[int, str, str]],
                     anchor: int) -> Tuple[Optional[int], List[str]]:
    """The `## [Unreleased]` heading's index and its body, above `anchor`."""
    for index, tag, title in headings:
        if index < anchor and tag == "h2" and title.startswith("[Unreleased]"):
            return index, list(lines[index + 1:anchor])
    return None, []


def _link_ref_block(lines: Sequence[str], inert: Set[int]) -> Optional[Tuple[int, int]]:
    """The trailing run of link-reference definitions, inclusive, or None.

    The link refs of a Keep a Changelog document are one block at the bottom.
    Anything above it that looks like one is prose — a quoted example, a
    previous bad cut's residue, an entry about link refs — and prose is not
    where a release writes.

    Fenced lines are stepped over rather than stopped at. An entry that ends
    the file with a fenced example of a link-ref block used to end the walk on
    the closing fence, so the block was never found and the release advanced
    no link at all while reporting `links none — ... left alone`: a receipt
    that named the absence and not the reason for it.
    """
    index = len(lines) - 1
    while index >= 0 and (not lines[index].strip() or index in inert):
        index -= 1
    end = index
    while index >= 0 and index not in inert and _LINK_REF_RE.match(lines[index]):
        index -= 1
    return (index + 1, end) if index + 1 <= end else None


def _rewrite_links(lines: List[str], version: str
                   ) -> Optional[Tuple[str, List[str]]]:
    """Point `[Unreleased]` at the new tag and add the new version's link ref.

    Scoped to the bottom link-ref block: this used to return on its
    first match anywhere in the file, and fragment bodies land near the top, so
    one `[Unreleased]: .../compare/v...HEAD` line inside an entry decided the
    base URL of the tag ref the release shipped — durably, since the line is
    still there and still matched first on the next cut.

    Returns the summary and the definition lines it wrote, which is what
    `_verify_written` compares the released file's own link table against.

    A first release goes through `_first_release_links` instead: there is no
    earlier tag, so there is no `compare/vX...HEAD` line here to advance, and
    this would report `links none` about a table that is one release behind
    rather than one that was genuinely left alone on purpose.
    """
    span = _link_ref_block(lines, _inert_lines("\n".join(lines)))
    if span is None:
        return None
    start, end = span
    for index in range(start, end + 1):
        line = lines[index]
        match = _UNRELEASED_LINK_RE.match(line)
        if not match:
            continue
        base = match.group("base")
        lines[index] = "[Unreleased]: {0}/compare/v{1}...HEAD".format(base, version)
        lines.insert(index + 1, "[{0}]: {1}/releases/tag/v{0}".format(version, base))
        return ("[Unreleased] -> compare/v{0}...HEAD, added [{0}] tag ref".format(version),
                [lines[index], lines[index + 1]])
    return None


def _dominant_newline(raw: bytes) -> str:
    """The line ending the file on disk already uses.

    `read_text` translates every ending to LF on the way in, and text-mode
    `write_text` translates it back to the *platform's* ending on the way out.
    So a fold rewrote a CRLF changelog as LF on POSIX and an LF changelog as
    CRLF on Windows -- a diff of every line in the file, produced by a tool that
    edited three of them, and invisible in a review that reads the rendered
    diff. Neither ending is the right one to impose; the file already answered
    the question and this reads the answer back.

    A mixture is a majority vote, and a file with no line ending at all resolves
    to LF -- it is what the assembled string already holds and what every line
    this script writes ends with. So a mostly-CRLF file with a few stray LF
    lines comes back CRLF, which is the ending its next reader will diff
    against; there is no third answer that leaves such a file alone, because
    this rewrites the whole document either way.
    """
    crlf = raw.count(b"\r\n")
    return "\r\n" if crlf and crlf * 2 > raw.count(b"\n") else "\n"


def _unwritable_links_refusal(lines: Sequence[str], version: str
                              ) -> Tuple[str, List[str]]:
    """Why `_rewrite_links` wrote nothing, for a release that is not the first.

    Reaching here means the fold is about to add a `## [x.y.z]` heading with no
    link ref behind it: it renders as literal bracketed text, and `[Unreleased]`
    stays pointed at the previous tag or at nothing. That used to be reported as
    `links none ... left alone` under an `ok` receipt -- a sentence naming the
    absence, filed under a state meaning there was nothing to do. Two releases
    shipped through it.

    A first release goes to `_first_release_links` instead and is *allowed* to
    write nothing: a repo that has never released has no published table to be
    consistent with and, in the shapes that function names, nothing on disk to
    derive a URL from. A repo on its second release has both, so the same
    absence is a refusal here.

    Nothing is guessed. This script is handed a changelog, not a repository, so
    the only place a forge URL can come from is the file's own link-ref block;
    when that cannot supply one, the refusal names which of the three shapes it
    found, because all three are fixed differently.
    """
    #: What to do about it, differing only in whether a definition is being
    #: added or replaced. "Add" told against a file that already holds an
    #: `[Unreleased]:` line leaves the old one in place beside the new: two
    #: definitions of one reference, which no parse reports, and the reader is
    #: shown whichever is read first.
    add = ("add       `[Unreleased]: <repo>/compare/v<newest released "
           "version>...HEAD` as the first line of the block at the bottom of "
           "CHANGELOG.md, then re-run")
    replace = ("replace   the `[Unreleased]:` line at the bottom of "
               "CHANGELOG.md with `[Unreleased]: <repo>/compare/v<newest "
               "released version>...HEAD` — replace it rather than adding a "
               "second definition of the same reference, then re-run")

    span = _link_ref_block(lines, _inert_lines("\n".join(lines)))
    remedy = add
    if span is None:
        reason = ("CHANGELOG.md has no trailing link-reference block at all, so "
                  "there is no `[Unreleased]` definition to advance and no "
                  "repository URL to write `[{0}]` from".format(version))
    else:
        start, end = span
        current = None
        for index in range(start, end + 1):
            match = _UNRELEASED_ANY_LINK_RE.match(lines[index])
            if match:
                current = match.group("url")
                break
        if current is None:
            reason = ("the trailing link-reference block has no `[Unreleased]:` "
                      "definition, so there is nothing to advance and no "
                      "repository URL to write `[{0}]` from".format(version))
        else:
            reason = ("`[Unreleased]` resolves to {0}, which is not a "
                      "`<repo>/compare/vX.Y.Z...HEAD` line — this rewrites that "
                      "line and will not reshape one it does not recognise"
                      .format(current))
            remedy = replace
    return ("`## [{0}]` would have no link ref and would render as literal "
            "bracketed text".format(version),
            ["reason    " + reason,
             remedy + ": this advances `[Unreleased]` to "
             "`compare/v{0}...HEAD` and writes `[{0}]: "
             "<repo>/releases/tag/v{0}` beside it".format(version),
             "why       a heading with no definition behind it is not a broken "
             "link, it is text that never looked like one — nobody reviewing "
             "the rendered page sees a failure to click",
             "untouched CHANGELOG.md was not written and no fragment was "
             "consumed"])


def _first_release_links(lines: List[str], version: str) -> Tuple[str, List[str]]:
    """What a first release can honestly do to the link-ref table, and say.

    There is no previous tag, so there is no `[Unreleased]: .../compare/vX...HEAD`
    line to advance: `_rewrite_links` finds nothing and reports `links none — no
    compare line found ... left alone`, which reads as "there was nothing to do"
    when what happened is "the table is now a release behind the file". That is
    this tracker's defect class applied to link refs — an absence the tool
    produced, read as an absence in the world.

    The base URL is still there to be read. Keep a Changelog's own template for
    a project with no releases writes `[Unreleased]: <repo>/commits/HEAD`, and
    hand-written files carry a `tree/` or `compare/` variant of the same thing.
    When one of those is present both definitions are written exactly as every
    later release writes them, so the file passes `--check-links` immediately
    after its first cut rather than acquiring a finding at birth.

    When it is not present — no trailing block, no `[Unreleased]` definition, or
    a URL with no forge segment to take a repository root from — nothing is
    written and the summary names which of the three it was and what to add.
    Returns `(summary, definitions written)`; the summary is never empty, which
    is the difference between this and the `None` it replaces.
    """
    span = _link_ref_block(lines, _inert_lines("\n".join(lines)))
    if span is None:
        return ("none — first release: CHANGELOG.md has no trailing "
                "link-reference block, so there was no `[Unreleased]` "
                "definition to take a repository URL from. Both "
                "`[{0}]: <repo>/releases/tag/v{0}` and "
                "`[Unreleased]: <repo>/compare/v{0}...HEAD` are missing, and "
                "`## [{0}]` renders as literal bracketed text until they are "
                "added".format(version), [])
    start, end = span
    for index in range(start, end + 1):
        match = _UNRELEASED_ANY_LINK_RE.match(lines[index])
        if not match:
            continue
        url = match.group("url")
        base = _FORGE_BASE_RE.match(url)
        if not base:
            return ("none — first release: `[Unreleased]` resolves to {0}, "
                    "which has no `/commits/`, `/commit/`, `/compare/`, "
                    "`/tree/` or `/releases/` segment to take a repository URL "
                    "from, so "
                    "nothing was rewritten and `## [{1}]` has no link ref — "
                    "add `[{1}]: <repo>/releases/tag/v{1}`".format(url, version),
                    [])
        root = base.group("base")
        lines[index] = "[Unreleased]: {0}/compare/v{1}...HEAD".format(root, version)
        lines.insert(index + 1, "[{0}]: {1}/releases/tag/v{0}".format(version, root))
        return ("first release: [Unreleased] -> compare/v{0}...HEAD (it pointed "
                "at {1}, there being no earlier tag to compare from), added "
                "[{0}] tag ref".format(version, url),
                [lines[index], lines[index + 1]])
    return ("none — first release: the trailing link-reference block has no "
            "`[Unreleased]:` definition to take a repository URL from, so "
            "nothing was rewritten and `## [{0}]` has no link ref — add "
            "`[{0}]: <repo>/releases/tag/v{0}`".format(version), [])


# Versions with a `## [x.y.z]` section and no tag anywhere — nothing was ever
# pushed for them, so there is no release page to link to and a
# `releases/tag/vX.Y.Z` URL invented for one is a 404 that renders as a working
# link. This is the audit's third state made explicit: not "ok", not a finding,
# but "there is no answer to give".
#
# EMPTIED ON VENDORING, and it stays empty. Upstream this set lists ITS OWN
# untagged releases, and the copy arrived here still naming 0.11.0 through
# 0.19.0 -- versions this repo has never had. The audit duly reported nine
# findings about another project's release history, in a tool whose whole
# purpose is to catch exactly that kind of confident wrong statement.
#
# It is per-repo state and does not belong in a shared tool, so the declaration
# is made by the caller instead: `--check-links --untagged 0.1.0,0.2.0`, in
# whatever CI leg or command that repository runs the audit from, where the
# versions sit beside the repository they are true of. This constant is the
# fallback for a caller that declared nothing, and an empty set means "no
# version is declared untagged", which is true of every repo by default.
#
# Nothing here declares a floor above which the set may not grow. Upstream's
# comment cited a test enforcing one; that test was not vendored with the
# script, and a citation to a file this repository does not have reads exactly
# like a guard that runs.
UNTAGGED_RELEASES = frozenset()

_COMPARE_HREF_RE = re.compile(r"/compare/v(?P<version>\d+\.\d+\.\d+)\.\.\.HEAD$")


def release_versions(text: str) -> List[str]:
    """Every `## [x.y.z]` release version, newest first, off a real parse.

    A parse and not a line prefix: this file quotes release headings inside
    fenced blocks by house style, so the characters `## [` appear in it
    without a heading being there — and a line-prefix test cannot tell the
    difference.
    """
    versions = []
    for _, tag, title in _headings(text):
        if tag != "h2" or not title.startswith("[") or "]" not in title:
            continue
        label = title[1:title.index("]")]
        if _VERSION_RE.match(label):
            versions.append(label)
    return versions


def audit_link_refs(text: str,
                    untagged: Optional[AbstractSet[str]] = None) -> List[str]:
    """What the link-ref table at the bottom disagrees with the file about.

    The assembler writes one definition per cut, which keeps the *next* release
    honest and says nothing about the state it inherited — `[0.24.0]` and
    `[0.25.0]` shipped with none, and `[Unreleased]` sat two tags behind
    twice. Both are the same defect: a link that resolves, returns a real page,
    and answers a different question than the one the reader asked.

    Raises rather than returning `[]` when there is no release heading at all.
    An empty finding list from a document that could not be audited is the
    absence-read-as-an-all-clear this file exists to not do.
    """
    declared = UNTAGGED_RELEASES if untagged is None else untagged
    versions = release_versions(text)
    if not versions:
        raise CannotValidate(
            "no `## [x.y.z]` release heading was found, so there is nothing to "
            "audit the link refs against — 0 findings here would read as a "
            "clean table rather than as a table nobody looked at.")
    _, refs, _ = _document_facts(text)

    findings: List[str] = []
    for version in versions:
        href = refs.get(version.upper())
        if version in declared and href:
            findings.append(
                "[{0}] is declared as never tagged but has a link ref ({1}) — "
                "one of the two is wrong, and a `releases/tag/v{0}` for a tag "
                "that was never pushed is a 404 that reads as a working link"
                .format(version, href))
        elif version not in declared and not href:
            findings.append(
                "`## [{0}]` has no link ref, so it renders as literal bracketed "
                "text instead of a link to the release — add "
                "`[{0}]: <repo>/releases/tag/v{0}` to the block at the bottom"
                .format(version))

    present = set(versions)
    for version in sorted(declared - present):
        findings.append(
            "[{0}] is declared as never tagged but has no `## [{0}]` section in "
            "the file — a stale declaration is where a genuinely missing ref "
            "gets filed away without anyone deciding to".format(version))

    unreleased = refs.get("UNRELEASED")
    if not unreleased:
        findings.append(
            "[Unreleased] has no link ref — the heading a reader clicks to see "
            "what is pending links nowhere")
    else:
        match = _COMPARE_HREF_RE.search(unreleased)
        if not match:
            findings.append(
                "[Unreleased] does not resolve to a `compare/vX.Y.Z...HEAD` "
                "link: {0}".format(unreleased))
        elif match.group("version") != versions[0]:
            findings.append(
                "[Unreleased] compares from v{0} but the newest release section "
                "is [{1}] — that link resolves and shows everything released "
                "since v{0} as unreleased work".format(
                    match.group("version"), versions[0]))
    return findings


def check_links(changelog: Path,
                untagged: Optional[AbstractSet[str]] = None) -> int:
    """`--check-links`: audit the table, and say which of the three it did.

    *untagged* is the caller's `--untagged`, and it is the whole reason that
    flag exists rather than a constant in this file: which of a repository's
    release sections were never tagged is a fact about **one** repository, and
    this script is vendored into many. The constant it replaces arrived in this
    copy still naming another project's nine versions, and reported nine
    findings about releases this repository never had.

    `None` means the caller declared nothing, which is not the same as
    declaring that every section is tagged — but it is the only reading
    available, and the receipt below says so by naming what was declared.
    """
    declared = UNTAGGED_RELEASES if untagged is None else untagged
    try:
        text = changelog.read_text(encoding="utf-8")
    except OSError as exc:
        _receipt("skipped", "cannot read {0}: {1} — nothing was audited"
                 .format(changelog, exc))
        return SKIPPED
    try:
        findings = audit_link_refs(text, declared)
    except CannotValidate as exc:
        _receipt("skipped", "{0}".format(exc))
        return SKIPPED
    if findings:
        _receipt("refused", "{0} finding(s) in {1}'s link ref table"
                 .format(len(findings), changelog.name), findings)
        return REFUSED
    versions = release_versions(text)
    _receipt("ok", "{0} release section(s) in {1}, parsed with markdown-it-py "
                   "{2}: each has a link ref or is declared untagged, and "
                   "[Unreleased] compares from v{3}"
             .format(len(versions), changelog.name, _MD_VERSION, versions[0]),
             ["untagged  {0} — declared by the caller as having no tag, so no "
              "`releases/tag/v...` link was expected for "
              "{1}".format(", ".join(sorted(declared & set(versions))),
                           "them" if len(declared & set(versions)) > 1 else "it")]
             if declared & set(versions) else [])
    return OK


def _verify_written(before: str, after: str, emitted: Sequence[str],
                    written_refs: Sequence[str]) -> List[str]:
    """Re-parse the file about to be written and report what it gained.

    The second layer, and the reason there is one: a fragment is validated
    alone and inserted into a document, and one guard over this file has now
    been wrong three times running. This does not consult the fragments at
    all. It asks the parser what the assembled document *is*, and refuses
    unless its heading table is the old one plus exactly the headings `render`
    reports writing, its link-reference table is the old one plus exactly the
    definitions `_rewrite_links` reports writing, and it gained no raw HTML.

    That holds whatever the per-fragment guard missed, which is the property
    the previous three rounds each shipped a receipt for without having.
    """
    before_headings, before_refs, before_raw = _document_facts(before)
    after_headings, after_refs, after_raw = _document_facts(after)
    allowed, _, _ = _document_facts("\n".join(emitted) + "\n")

    expected_refs = dict(before_refs)
    if written_refs:
        _, added, _ = _document_facts("\n".join(written_refs) + "\n")
        expected_refs.update(added)

    findings: List[str] = []
    surplus = after_headings - (before_headings + allowed)
    if surplus:
        findings.append(
            "re-parse of the assembled file found {0} heading(s) this release "
            "did not write: {1}".format(
                sum(surplus.values()),
                ", ".join("<{0}>{1}".format(tag, title[:60])
                          for tag, title in sorted(surplus))))
    if after_refs != expected_refs:
        differing = sorted(set(after_refs) ^ set(expected_refs)) or sorted(
            label for label in after_refs if after_refs[label] != expected_refs.get(label))
        pre_existing = all(after_refs.get(label) == before_refs.get(label)
                           for label in differing)
        findings.append(
            "re-parse of the assembled file found a link ref table this release "
            "did not write — label(s) {0}. First definition of a label wins, so a "
            "definition earlier in the file beats the block at the bottom that "
            "this release rewrites: {1}. {2}".format(
                ", ".join(differing),
                "; ".join("[{0}] resolves to {1}, this release wrote {2}".format(
                    label, after_refs.get(label, "nothing"),
                    expected_refs.get(label, "nothing")) for label in differing),
                "That earlier definition is already in CHANGELOG.md and no "
                "fragment introduced it — fix the file, then cut."
                if pre_existing else
                "A fragment consumed by this run introduced it."))
    if after_raw > before_raw:
        findings.append(
            "re-parse of the assembled file found {0} new raw HTML token(s), "
            "which render as structure a reader will trust".format(after_raw - before_raw))
    gained = _disallowed_destinations(after) - _disallowed_destinations(before)
    if gained:
        findings.append(
            "re-parse of the assembled file found {0} link or image "
            "destination(s) this release added that a fragment may not carry: "
            "{1}. The per-fragment guard should have refused these; that it did "
            "not is itself the finding".format(
                sum(gained.values()),
                ", ".join("{0} -> {1}".format(kind, url[:80])
                          for kind, url in sorted(gained))))
    crowded = sorted(_crowded_headings(after) - _crowded_headings(before))
    if crowded:
        findings.append(
            "the assembled file writes {0} heading(s) with no blank line above "
            "them, which a stricter Markdown parser folds into the paragraph "
            "before rather than rendering as a heading: {1}. The ones already "
            "in CHANGELOG.md are carried forward untouched — they already "
            "shipped in tags".format(len(crowded), ", ".join(crowded)))
    return findings

def _line(stream, text: str) -> None:
    """Write one line to *stream* in a way a console cannot refuse.

    `print` encodes with the console's codepage, and a character that codepage
    has no byte for raises. That raise is the whole of the failure this
    function exists to remove: the receipt is the only thing that reports the
    mutation, so a reporter that can raise is a mutation that can go
    unreported. Degrading the character loses one glyph; raising loses the
    report.

    `backslashreplace` rather than `replace`, because an escape still reads as
    *something was here and this is which character it was*, while `?` is
    indistinguishable from a question mark the prose meant to write.

    A guard on one codepage is not this: cp1252 is what CI measures, and cp437
    and cp850 are real Windows consoles with no byte for the em dashes that
    guard deliberately permits. This does not care which codepage it is.
    """
    try:
        print(text, file=stream)
        return
    except UnicodeEncodeError:
        pass
    encoding = getattr(stream, "encoding", None) or "ascii"
    data = (text + "\n").encode(encoding, "backslashreplace")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        # Flush the text layer first, and the binary one after. Writing to
        # `.buffer` goes underneath `stream`'s own buffer, so without this the
        # degraded line overtakes every line still sitting in it and the
        # receipt arrives shuffled -- a repair that damages the thing it was
        # repairing. Caught by the ascii-console test, which asserts on the
        # first line of the receipt rather than on its contents.
        stream.flush()
        buffer.write(data)
        buffer.flush()
    else:
        # A captured or wrapped stream with no binary half. Everything in
        # `data` is representable by construction, so this cannot raise for
        # the reason we are here.
        stream.write(data.decode(encoding, "replace"))


def _receipt(state: str, summary: str, details: Sequence[str] = ()) -> None:
    _line(sys.stdout, "assemble    : {0:<11} ({1})".format(state, summary))
    for line in details:
        _line(sys.stdout, "  {0}".format(line))


def _alarm(lines: Sequence[str]) -> None:
    """The receipt for a run whose receipt failed, on stderr and best-effort.

    Reached only when something after the first write raised, which includes
    the case of stdout itself being the thing that raised -- so this does not
    use it. Nothing here may raise: the exit code is the part of this that has
    to survive, and an alarm that dies while sounding takes it with it.
    """
    try:
        for line in lines:
            _line(sys.stderr, line)
        sys.stderr.flush()
    except Exception:  # pragma: no cover - the stream is already gone
        pass


def assemble(changelog: Path, directory: Path, version: str, date: str,
             dry_run: bool = False, keep: bool = False) -> int:
    if not _VERSION_RE.match(version):
        _receipt("refused", "--version {0!r} is not x.y.z".format(version))
        return REFUSED

    try:
        fragments = collect(directory)
    except CannotValidate as exc:
        _receipt("skipped", "{0} CHANGELOG.md untouched, nothing consumed".format(exc))
        return SKIPPED
    except BadFragment as exc:
        findings = str(exc).splitlines()
        _receipt("refused", "{0} finding(s) — CHANGELOG.md untouched, nothing consumed"
                 .format(len(findings)),
                 ["{0}/{1}".format(directory.name, line) for line in findings])
        return REFUSED

    if not fragments:
        _receipt("skipped", "no fragments in {0}/ — nothing to assemble; "
                            "CHANGELOG.md untouched".format(directory.name))
        return SKIPPED

    # Every other failure below answers in one of three states. This read did
    # not: it raised whatever the filesystem raised, and the commonest case --
    # the file simply not being there -- is the state a freshly scaffolded repo
    # is in until somebody writes it, so the traceback landed on the maintainer
    # least able to tell a bug in this script from a mistake of their own.
    # `--dry-run` raised identically, which took away the way to find out.
    # Caught as `OSError`, not `FileNotFoundError`: a directory at that path or
    # a mode we cannot read is the same answer, we could not read it.
    try:
        raw = changelog.read_bytes()
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except OSError as exc:
        _receipt("skipped", "cannot read {0}: {1} — nothing was written, "
                            "nothing consumed".format(changelog, exc),
                 ["a changelog this script can assemble into needs a "
                  "`## [Unreleased]` heading. With one or more `## [x.y.z]` "
                  "release headings below it the new section is inserted above "
                  "the newest of them; with none, this is a first release and "
                  "the section is cut directly below `## [Unreleased]`",
                  "what it will not do is guess: with no `## [Unreleased]` "
                  "heading there is no position in the file it can defend, and "
                  "it refuses rather than picking one. Seeding a release "
                  "section for a version that never shipped is no longer the "
                  "way to cut a first release, and never was a good one"])
        return SKIPPED
    lines = text.splitlines()

    try:
        headings = _headings(text)
    except CannotValidate as exc:
        _receipt("skipped", "{0} CHANGELOG.md untouched".format(exc))
        return SKIPPED

    # A *heading*, not the substring, and not a line that looks like one
    # either. Entries in this file quote release headings — a fenced block is
    # the documented way to do that — so `"## [x]" in text` and
    # `line.startswith("## [")`
    # both answer a question about characters when the question is about
    # structure. The parser is asked instead.
    if any(tag == "h2" and title.startswith("[{0}]".format(version))
           for _, tag, title in headings):
        _receipt("refused", "CHANGELOG.md already has a `## [{0}]` section — "
                            "assembling again would duplicate a release heading".format(version))
        return REFUSED

    # Three states here, not two. A repo with releases has an unambiguous
    # anchor and keeps using it. A repo cutting its genuine first release has
    # no anchor and never will have had one, and refusing that left the
    # documented way forward as hand-writing a release section for a version
    # that never shipped — a maintainer inventing history to satisfy a parser,
    # from a tool whose whole design is not doing that. A file whose shape
    # cannot defend either position is still refused, and says what would
    # decide it.
    first_release = False
    try:
        anchor = _anchor(headings)
    except BadFragment:
        try:
            anchor = _first_release_anchor(lines, headings, _inert_lines(text))
        except BadFragment as undecidable:
            _receipt("refused", str(undecidable))
            return REFUSED
        first_release = True

    # `[Unreleased]` means "goes out in the next release", so it goes out in it.
    # Leaving it behind strands the entries twice over: the tag ships silently
    # omitting work that is in the tag, and the work still reads as pending.
    unreleased_at, residue_body = _unreleased_span(lines, headings, anchor)
    preamble, residue_sections = _subsections(residue_body)
    folded = _entry_count(residue_body)

    section, emitted = render(fragments, version, date, preamble, residue_sections)

    # Arithmetic, not trust: every entry on either side has to be in the result.
    # A merge that dropped one would otherwise be indistinguishable from a clean
    # run, which is the whole failure mode this file is built against.
    expected = folded + sum(
        _entry_count(f.path.read_text(encoding="utf-8").splitlines())
        for f in fragments if f.path)
    produced = _entry_count(section.splitlines())
    if produced != expected:
        _receipt("refused", "entry count does not balance: {0} folded + fragments = {1} "
                            "expected, {2} produced — refusing to write a lossy changelog"
                 .format(folded, expected, produced))
        return REFUSED

    if unreleased_at is None:
        body = list(lines[:anchor]) + _section_lines(section) + list(lines[anchor:])
    else:
        body = (list(lines[:unreleased_at + 1]) + [""] + _section_lines(section)
                + list(lines[anchor:]))
    if first_release and anchor >= len(lines):
        # Nothing followed `[Unreleased]`, so the section is now the tail of the
        # file and `_section_lines`' trailing blank would become a trailing blank
        # line in the released file.
        while len(body) > 1 and not body[-1]:
            body.pop()
    if first_release:
        links, written_refs = _first_release_links(body, version)
    else:
        rewritten = _rewrite_links(body, version)
        if rewritten is None:
            # Before the write and before a single fragment is consumed. The
            # heading this fold is about to add would have no link ref behind
            # it, which is the one outcome the release cannot be corrected into
            # after the fact without a second commit on top of the tag.
            summary, why = _unwritable_links_refusal(body, version)
            _receipt("refused", summary, why)
            return REFUSED
        links, written_refs = rewritten

    assembled = "\n".join(body) + "\n"
    try:
        structural = _verify_written(text, assembled, emitted, written_refs)
    except CannotValidate as exc:
        _receipt("skipped", "{0} CHANGELOG.md untouched".format(exc))
        return SKIPPED
    if structural:
        _receipt("refused", "{0} finding(s) in the assembled file — CHANGELOG.md "
                            "untouched, nothing consumed".format(len(structural)),
                 structural)
        return REFUSED

    details = [
        "consumed  " + ", ".join(f.path.name for f in fragments if f.path),
        "sections  " + ", ".join(
            "{0} ({1})".format(name.capitalize(), sum(1 for f in fragments if f.section == name))
            for name in SECTIONS if any(f.section == name for f in fragments)),
    ]
    if first_release:
        # Two lines, not one, and the second is the load-bearing half.
        #
        # The first states an inference about the *repository* — "this is its
        # first release" — drawn from evidence about a *file*. Those are not
        # the same claim, and a changelog that was rewritten, truncated or
        # regenerated by hand while tags exist presents the identical shape.
        #
        # The changelog stays this script's sole source of truth, deliberately.
        # It is handed a `--changelog` path and a `--dir`, not a repository, and
        # it is vendored into repos it knows nothing about; the root it could
        # derive is the one above *its own file*, which under a plugin is a
        # different repo than the changelog. And tags are not release headings:
        # a repo that tags `nightly`, tags release candidates, or adopted a
        # changelog after it had already shipped is in "tags exist, no release
        # heading" legitimately. Refusing there would refuse a real first cut,
        # and the only remedy such a refusal could name is hand-writing a
        # release heading for a version that already shipped — the invented
        # history `_first_release_anchor` exists to abolish.
        #
        # What that decision owes the reader is not silence about the gap. The
        # receipt names the source it read, names the second source it did not,
        # and says what to do with it — at the moment of the claim, which is
        # where a limit is worth a sentence and a doc is not.
        details.insert(0, (
            "first     no `## [x.y.z]` release heading in CHANGELOG.md, so this "
            "is its first release: the section was inserted directly below the "
            "`## [Unreleased]` heading on line {0}, the one position in the file "
            "this script can defend. Detected, not assumed — a single existing "
            "release heading would have anchored it instead."
        ).format(unreleased_at + 1))
        details.insert(1, (
            "source    CHANGELOG.md, and nothing else. This script is handed a "
            "file rather than a repository, so `git tag` is a second source it "
            "does not read: a changelog rewritten, truncated or regenerated by "
            "hand while tags exist has exactly this shape and is cut here "
            "rather than refused. Before you push this, check that `git tag` "
            "lists no release — if it lists one, this is not a first release "
            "and the section is in the wrong place."
        ))
    if links:
        details.append("links     " + links)
    else:
        details.append("links     none — no `[Unreleased]: .../compare/vX...HEAD` line found "
                       "in the trailing definition block, so the link refs were left alone")
    if folded:
        details.append(
            "folded    {0} entr{1} from `## [Unreleased]` into [{2}], above the fragments. "
            "The heading stays as the compare-link anchor; its body is now empty."
            .format(folded, "y" if folded == 1 else "ies", version))
    else:
        details.append("folded    0 — `## [Unreleased]` was already empty")
    details.append(
        "verified  the assembled file was re-parsed with markdown-it-py {0}: its "
        "headings are the ones already there plus the {1} this run wrote, its link "
        "ref table is the one already there plus what this run wrote, and it gained "
        "no raw HTML".format(_MD_VERSION, len(emitted)))

    if dry_run:
        _receipt("ok", "dry-run: {0} fragment(s) would become `## [{1}] - {2}`; "
                       "nothing written".format(len(fragments), version, date), details)
        return OK

    # ------------------------------------------------------------------
    # Everything below this line moves the tree, and the only thing that
    # reports the move comes after it. That ordering is the bug this guard
    # closes: a release was cut, the fragments were consumed, and the process
    # then died printing its own receipt -- leaving exit 1, which this
    # script's contract defines as SKIPPED, "nothing to do, or nothing
    # provable". A caller was told to carry on past a tree that had already
    # changed under it.
    #
    # So every exception from here on is REFUSED, not a traceback. REFUSED is
    # the code a caller stops at, and stopping is right whichever half failed:
    # a write that raised may have left a partial file, and a run that cannot
    # say what it did has not proved the tree is untouched. The receipt's
    # other refusals all say "CHANGELOG.md untouched" and mean it; this one
    # says the opposite in as many words, because that is the fact a
    # maintainer needs before re-running anything.
    #
    # `Exception`, not `BaseException`: Ctrl-C and SystemExit keep propagating.
    # A signal mid-unlink leaves the same torn state and is not fixable here --
    # the interpreter is not guaranteed to reach any handler we write.
    # ------------------------------------------------------------------
    wrote = False
    removed = 0
    try:
        # `open(newline=...)`, not `write_text(newline=...)`: the keyword
        # arrived on `Path.write_text` in 3.10 and this file runs on 3.9.
        # Without it the platform decides, and the platform is not what the
        # file on disk already said.
        with changelog.open("w", encoding="utf-8",
                            newline=_dominant_newline(raw)) as handle:
            handle.write(assembled)
        wrote = True
        if not keep:
            for frag in fragments:
                if frag.path:
                    frag.path.unlink()
                    removed += 1
            details.append("removed   {0} fragment file(s) from {1}/"
                           .format(len(fragments), directory.name))
        else:
            details.append("kept      --keep: {0} fragment file(s) left in {1}/ — they will ship "
                           "twice if the next release also consumes them"
                           .format(len(fragments), directory.name))

        _receipt("ok", "{0} fragment(s) -> `## [{1}] - {2}` in {3}"
                 .format(len(fragments), version, date, changelog.name), details)
        # Flushed inside the guard on purpose. A receipt that only reached a
        # buffer has not been delivered, and a pipe that closed under it
        # raises when the interpreter flushes at shutdown -- after the exit
        # code has been decided, which is exactly too late to change it.
        sys.stdout.flush()
    except Exception as exc:
        # What the alarm may claim is exactly what `wrote` and `removed`
        # establish, and no more. An earlier draft asserted "CHANGELOG.md now
        # holds the release" from inside a block that also wraps the write --
        # so the one case the fragment for this change names by name, a full
        # disk on the redirect, would have produced a confident sentence about
        # a file that was never written. Reporting a mutation that did not
        # happen is the same defect as denying one that did.
        _alarm([
            "assemble    : refused     ({0}: {1}: {2})".format(
                "this run changed the tree and then could not report it"
                if wrote or removed else
                "this run could not complete and cannot prove it changed "
                "nothing",
                type(exc).__name__, exc),
            "  written   " + (
                "{0} now holds `## [{1}] - {2}`".format(changelog, version, date)
                if wrote else
                "the write to {0} did not complete. Whether it holds the "
                "release, a truncated file or the original is not established "
                "here -- read it before anything else".format(changelog)),
            "  fragments " + (
                "left in place (--keep)" if keep else
                "{0} of {1} consumed fragment(s) deleted from {2}/".format(
                    removed, len(fragments), directory.name)),
            "  exit      refused, not skipped. Skipped means the tree is "
            "untouched, and this run cannot prove that. Read the two paths "
            "above, then either commit the cut or restore both from git; "
            "re-running is not the move.",
        ])
        return REFUSED
    return OK


def check(directory: Path) -> int:
    try:
        fragments = collect(directory)
    except CannotValidate as exc:
        # Three states, applied to the gate itself. `--check` is what a
        # reviewer trusts *instead of* reading the fragment, so a run that
        # established nothing has to say nothing was established — and exit
        # non-zero, because a green CI leg that validated nothing is the same
        # false assurance three rounds of this file already shipped.
        _receipt("skipped", str(exc))
        return SKIPPED
    except BadFragment as exc:
        findings = str(exc).splitlines()
        _receipt("refused", "{0} fragment(s) will not assemble".format(len(findings)),
                 ["{0}/{1}".format(directory.name, line) for line in findings])
        return REFUSED
    if not fragments:
        _receipt("skipped", "{0}/ holds 0 fragments — nothing to validate"
                 .format(directory.name))
        return OK
    # The receipt states what was established and names what established it,
    # which the last three did not. "no body writes at column 0" stayed
    # literally true through three bypasses; "none can open a heading or a
    # link ref, at any indent or nesting" was true of the scanner's own model
    # of CommonMark and false of CommonMark. This claim is checkable by the
    # person reading it: it is what markdown-it-py saw.
    _receipt("ok", "{0} fragments, all names parse, each body names the issue "
                   "in its own filename; each body parsed with "
                   "markdown-it-py {1}, whose token stream holds no heading, no "
                   "link ref definition and no raw HTML at any depth, whose "
                   "fences all close inside the fragment, whose top level is "
                   "one `- ` bullet list, and every link and image destination "
                   "in which is on the allowlist"
             .format(len(fragments), _MD_VERSION),
             ["{0}  {1}".format(f.path.name if f.path else "?", f.section) for f in fragments])
    return OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", help="the version being cut, x.y.z")
    parser.add_argument("--date", default=datetime.date.today().isoformat(),
                        help="release date, YYYY-MM-DD (default: today)")
    # No argparse default on either. The derived value is applied per mode
    # below -- read-only modes take it, the fold refuses without an explicit
    # one -- and an argparse default would erase the distinction between "the
    # caller named this" and "we guessed it", which is the whole question.
    parser.add_argument("--changelog", default=None,
                        help="path to CHANGELOG.md; required to fold, derived "
                             "from this script's own repository for the "
                             "read-only modes")
    parser.add_argument("--dir", dest="directory", default=None,
                        help="path to the fragment directory; required to "
                             "fold, derived for the read-only modes")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--keep", action="store_true", help="do not delete consumed fragments")
    parser.add_argument("--check", action="store_true",
                        help="validate every fragment name and body; write nothing")
    parser.add_argument("--check-links", dest="check_links", action="store_true",
                        help="audit CHANGELOG.md's link ref table; write nothing")
    parser.add_argument("--untagged", default=None,
                        help="comma-separated versions that have a `## [x.y.z]` "
                             "section but were never tagged, so no "
                             "`releases/tag/vX.Y.Z` link is expected for them "
                             "and one written anyway is a 404 that reads as a "
                             "working link. Per-repository, which is why it is "
                             "a flag and not a constant in this file")
    parser.add_argument("--count", action="store_true",
                        help="print the fragment count as a bare integer, and nothing else")
    args = parser.parse_args(list(argv) if argv is not None else None)

    def _resolve(value: Optional[str], flag: str,
                 derived: Optional[Path]) -> Optional[Path]:
        """Read-only modes only. Take what the caller passed, else the value
        derived from this script's own location.

        No `--dir`/`--changelog` given, and no `.git` found above this script
        to derive a default from: say so, rather than composing a path out of
        a guess and failing on that instead.
        """
        if value is not None:
            return Path(value)
        if derived is None:
            _receipt("skipped",
                     "could not find the repository root above {0} "
                     "(no .git there or in any parent) to derive a default "
                     "for {1}; pass it explicitly"
                     .format(Path(__file__).resolve(), flag))
            return None
        return derived

    def _fold_target() -> Optional[Tuple[Path, Path]]:
        """The fold's target, or a refusal that names what to pass.

        Deliberately does not fall back to `REPO`. See the module docstring:
        the derivation says which repository this file is *stored* in, the
        fold needs the one being *released*, and no copy of this script can
        tell whether those are the same. A refusal that only reported
        something missing would turn a wrong-target write into a dead end, so
        this prints the flags and a whole invocation.
        """
        missing = [flag for flag, value in (("--dir", args.directory),
                                            ("--changelog", args.changelog))
                   if value is None]
        if not missing:
            return Path(args.changelog), Path(args.directory)
        _receipt("refused",
                 "the fold rewrites CHANGELOG.md and deletes every consumed "
                 "fragment, so it will not choose its own target: {0} {1} "
                 "required and not given"
                 .format(" and ".join(missing),
                         "is" if len(missing) == 1 else "are"),
                 ["pass      --dir <fragment directory> --changelog <changelog "
                  "file>, both read relative to the directory you run this from",
                  "example   --version {0} --dir changelog.d --changelog "
                  "CHANGELOG.md".format(args.version),
                  "why       the fold derives no default, in this copy or the "
                  "one vendored into a managed repo. This file finds a "
                  "repository root by walking up from itself, which names the "
                  "repository it is stored in and not the one you are "
                  "releasing; nothing on disk says whether those are the same",
                  "untouched CHANGELOG.md was not read or written, and no "
                  "fragment was consumed"])
        return None

    #: What the read-only modes fall back to. Composed here rather than at
    #: argparse time so that `args.directory is None` still means "the caller
    #: named nothing", which is what the fold gate reads.
    derived_dir = (REPO / "changelog.d") if REPO else None
    derived_changelog = (REPO / "CHANGELOG.md") if REPO else None

    # `--untagged` is read by `--check-links` and by nothing else. Accepting it
    # silently on the fold, `--check` or `--count` would make a declaration that
    # was never consulted look exactly like one that was honoured -- including a
    # value that is not `x.y.z`, which `--check-links` refuses and every other
    # mode would have ignored.
    if args.untagged is not None and not args.check_links:
        _receipt("refused",
                 "--untagged is read by --check-links only, and this run is "
                 "not one — nothing was audited, written or consumed",
                 ["pass      --check-links alongside it, or drop it",
                  "why       the versions it declares are compared against the "
                  "link ref table, which no other mode reads. Silently ignored "
                  "here, a declaration that never applied would be "
                  "indistinguishable from one that did"])
        return REFUSED

    if args.count:
        directory = _resolve(args.directory, "--dir", derived_dir)
        if directory is None:
            return SKIPPED
        try:
            print(len(collect(directory)))
        except CannotValidate as exc:
            # Not a count of 0 on stdout. A caller piping this into arithmetic
            # would read "nothing pending" from "could not look".
            print(exc, file=sys.stderr)
            return SKIPPED
        except BadFragment as exc:
            print(exc, file=sys.stderr)
            return REFUSED
        return OK

    if args.check_links:
        changelog = _resolve(args.changelog, "--changelog", derived_changelog)
        if changelog is None:
            return SKIPPED
        untagged = None
        if args.untagged is not None:
            untagged = frozenset(
                part.strip() for part in args.untagged.split(",") if part.strip())
            bad = sorted(v for v in untagged if not _VERSION_RE.match(v))
            if bad:
                # Refused, not dropped. A typo silently ignored means the
                # version it was meant to declare is still expected to have a
                # link ref, the audit reports a finding about it, and the
                # maintainer reads that finding as disagreement with a
                # declaration that was never made.
                _receipt("refused",
                         "--untagged {0!r}: {1} is not x.y.z — nothing was "
                         "audited".format(args.untagged, ", ".join(bad)))
                return REFUSED
        return check_links(changelog, untagged)

    if args.check:
        directory = _resolve(args.directory, "--dir", derived_dir)
        if directory is None:
            return SKIPPED
        return check(directory)

    if not args.version:
        _receipt("refused", "--version is required to assemble "
                            "(or pass --check / --count for the read-only modes)")
        return REFUSED

    target = _fold_target()
    if target is None:
        return REFUSED
    changelog, directory = target
    return assemble(changelog, directory, args.version, args.date,
                    dry_run=args.dry_run, keep=args.keep)

def _exit(code: int) -> int:
    """Deliver whatever is still buffered, and keep *code*.

    CPython flushes stdout again while shutting down. On a closed pipe that
    raises a second time, is reported as `Exception ignored while flushing
    sys.stdout`, and sets the exit status to 120 -- so the number this script
    decided on is replaced by an accident of the plumbing, which is a smaller
    version of the bug the guard in `assemble` exists to close. Pointing the
    descriptor at the null device before shutdown is what stops that.

    **Only the script entry point does this.** `os.dup2` on fd 1 is
    process-wide and permanent, and this module is also imported and called
    in-process -- by its own tests, and by anything that vendors it -- where
    silencing the caller's stdout for the rest of its run is a far worse
    outcome than a wrong exit code. So it lives here, below `main`, and not in
    any function a caller can reach.
    """
    try:
        sys.stdout.flush()
    except Exception:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except Exception:  # pragma: no cover - nothing better is available
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(_exit(main()))
