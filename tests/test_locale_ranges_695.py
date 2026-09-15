"""Every ASCII letter range in a shipped shell script must match byte-wise (#695).

`[A-Z]`, `[a-z]` and `[A-Za-z0-9._-]` are POSIX bracket RANGES, and a range is
matched by the locale's collation order rather than by byte value.

MEASURED, not assumed -- the matrix is from a deliberate diagnostic run on
ubuntu-latest (glibc, bash 5.2) under a `localedef`-built `tr_TR.UTF-8`, PR
#698:

    [[ =~ ]]  [A-Z] vs I .............. C: match   tr_TR: NO MATCH
    [[ =~ ]]  [a-z] vs i .............. C: match   tr_TR: NO MATCH
    [[ =~ ]]  [a-zA-Z] vs I and i ..... C: match   tr_TR: NO MATCH
    [[ =~ ]]  [A-Za-z0-9_] vs "ID" .... C: match   tr_TR: NO MATCH
    [[ =~ ]]  [A-Za-z] vs "é" ......... C: NO match  tr_TR: MATCH
    case      [A-Za-z]:/* vs I:/x ..... identical in both
    case      *[!A-Za-z0-9._-]* ....... identical in both, for "SESS-I" and "é"
    ${v//[!a-zA-Z0-9]/-} .............. identical in both

So both failure directions are real -- a range that loses `I`, and the same
range gaining `é` -- but only through `[[ =~ ]]`, which glibc's regcomp
matches by collation. bash's own glob matcher, which backs `case` patterns and
`${v//[!...]/}`, compares bytes and was observed not to move at all.

The scanner still reports every letter range, `case` and substitution
included, and those sites sit in ALLOWLIST with that measurement as their
reason. Two reasons not to simply stop looking at them: the measurement is one
libc and one bash, and POSIX permits a `case` range to collate; and a site that
is invisible cannot be re-checked when either changes.

This is not a theoretical hazard -- it is #695, where `[A-Z]` stopped
matching `I` under Turkish collation, the extract bridge silently dropped
`EXTRACT_FILE`, and no save ever completed on any host whose language was
Turkish. The class cuts the other way too: under a widening locale `[A-Za-z]`
matches `é`, so a guard whose entire job is an ASCII allowlist waves through
exactly what it exists to reject -- #539 fixed one instance of that.

Three sites in this tree already carried the fix before #695 -- `config()`,
`_remember_cfg_flatten_cache_valid_value`, `lib-slug.sh`'s `LC_ALL=C sed` --
each found the hard way, separately, one bug report at a time. This module is
the general form: a letter range is allowed only where the C locale is in
force, so the next one is a finding at authoring time rather than a report
from a user whose only unusual property is their language.

Runs on every platform: it reads source, so unlike the behavioural half
(tests/test_safe_eval_locale_695.py, which needs glibc) it cannot skip.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every shipped shell script. tests/ is excluded: a test's own ranges never
# run on a user's machine, and the suite is not what #695 broke.
SHELL_DIRS = ("scripts", "hooks.d", "hooks")

# A LETTER range, in one of the three spellings a guard is ever written in:
# `A-Z`, `a-z`, `A-z`. Three exclusions, each deliberate:
#
#   * Arbitrary endpoints -- `[agy-stop-hook]` inside an echo string,
#     `[ -f "$D/.install-marker" ]` -- are not ranges anybody wrote as a
#     range. Matching them buries the real findings under literal text, and
#     an allowlist that long stops being read.
#   * `$'\200-\277'`-style byte ranges (lib-slug.sh builds UTF-8
#     continuation classes that way) are byte ranges by construction.
#   * `[0-9]`, which this repo uses ~40 times for "is this an integer".
#     REASONED, not measured: POSIX requires the digits to have consecutive
#     collating values in every locale, so a digit range cannot lose a
#     member the way `[A-Z]` loses `I`. The #695 diagnostic matrix did not
#     probe digits, so this rests on the standard rather than on an
#     observation, and is written down rather than assumed.
_RANGE = re.compile(r"\[!?\^?[^]\[]*(?:A-Z|a-z|A-z)")

_LOCAL_LC = re.compile(r"^\s*local\s+LC_ALL=C\s*(#.*)?$", re.MULTILINE)
_INLINE_LC = re.compile(r"\bLC_ALL=C\b")


def _shell_files() -> list[Path]:
    files: list[Path] = []
    for d in SHELL_DIRS:
        root = REPO_ROOT / d
        if root.is_dir():
            files.extend(sorted(p for p in root.rglob("*.sh") if p.is_file()))
    return files


def _strip_comment(line: str) -> str:
    """Drop a whole-line comment. Deliberately crude: a `#` mid-line may be
    inside a string or a pattern, so only a line that STARTS as a comment is
    removed. A missed comment is a false finding the allowlist can carry; a
    wrongly-stripped line would hide a real one."""
    return "" if line.lstrip().startswith("#") else line


def _function_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, own_body) per function opened at column 0.

    `own_body` excludes any nested function's lines, so a declaration inside
    an inner function is never credited to the outer one.

    Nesting is not hypothetical here: #696 defined `capture_was_seen() {` at
    column 0 inside `_remember_deferred_phase() {`. A parser that closed at the
    first column-0 `}` computed the outer span as ending at the INNER
    function's brace and handed the inner's `local LC_ALL=C` to 148 outer
    lines that have none -- reporting them protected when they are not. A
    stack, so an opener seen while already inside a span nests rather than
    being mistaken for the continuation of one.
    """
    lines = text.splitlines()
    opener = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{\s*$")
    stack: list[int] = []
    spans: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if opener.match(line):
            stack.append(i)
        elif line == "}" and stack:
            spans.append((stack.pop(), i))

    out = []
    for start, end in spans:
        inner = {n for s2, e2 in spans
                 if (s2, e2) != (start, end) and start <= s2 and e2 <= end
                 for n in range(s2, e2 + 1)}
        own = "\n".join(line for n, line in enumerate(lines[start:end + 1],
                                                      start=start)
                        if n not in inner)
        out.append((start, end, own))
    return out


def _is_protected(text: str, line_no: int, line: str) -> bool:
    """Is this line's range matched under the C locale?

    The INNERMOST function containing the line decides: a declaration in an
    enclosing function does govern a nested one (the nested body runs with the
    outer's `local` still in scope), but never the other way round."""
    if _INLINE_LC.search(line):
        return True
    containing = [(start, end, body) for start, end, body in _function_spans(text)
                  if start <= line_no <= end]
    if not containing:
        return False
    containing.sort(key=lambda s: s[1] - s[0])
    for _start, _end, body in containing:
        if _LOCAL_LC.search(body):
            return True
    return False


# `case` patterns compare bytes -- measured, see the module docstring. Left
# where they are rather than restructured into a shared helper: these guards
# have to run at the point of entry, and three of the four hooks holding one
# have sourced no shared library by then, so reaching a helper would mean
# moving the guard, and the guard's position is the guarantee.
_CASE = (
    "a `case` pattern. Measured under tr_TR on glibc / bash 5.2: `case` "
    "ranges compare bytes and do not move with the locale in either "
    "direction (#698's matrix). Only `[[ =~ ]]` collates."
)

# Sites that are correct as they stand. A bare path is not accepted: every
# exemption is argued once, in writing, where the next reader can weigh it.
ALLOWLIST: dict[tuple[str, int], str] = {
    ("scripts/log.sh", 149):
        "inside _REMEMBER_CFG_FLATTEN_JQ, a jq program string. jq matches "
        "with Oniguruma, which is not driven by the shell's LC_COLLATE.",
    ("scripts/log.sh", 175):
        "inside the embedded Python fallback. Python's `re` over `str` "
        "matches [A-Za-z0-9_] as ASCII regardless of locale.",
    ("scripts/log.sh", 178):
        "the same Python fallback's own message text, not a pattern.",
    ("scripts/lib-slug.sh", 71):
        "a member of the _REMEMBER_SLUG_SED array, only ever invoked as "
        "`LC_ALL=C sed` (lib-slug.sh:373). The locale is forced at the call "
        "site, which this scanner cannot see from the definition; "
        "lib-slug.sh:131-150 documents the whole decision.",

    # --- `case` patterns: measured not to collate (see the module docstring
    # for the matrix and the run it came from). Listed rather than excluded
    # from the scan, so that one libc's behaviour stays visible and can be
    # re-checked rather than quietly assumed forever.
    ("scripts/agy-stop-hook.sh", 158): _CASE,
    ("scripts/post-tool-hook.sh", 424): _CASE,
    ("scripts/post-tool-hook.sh", 616): _CASE,
    ("scripts/post-tool-hook.sh", 658): _CASE,
    ("scripts/post-tool-hook.sh", 712): _CASE,
    ("scripts/session-end-hook.sh", 193): _CASE,
    ("scripts/session-end-hook.sh", 201): _CASE,
    ("scripts/session-start-hook.sh", 257): _CASE,
    ("scripts/lib-slug.sh", 406): _CASE,
}


def _findings() -> list[str]:
    out = []
    for path in _shell_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for i, raw in enumerate(text.splitlines()):
            line = _strip_comment(raw)
            if not _RANGE.search(line):
                continue
            if _is_protected(text, i, line):
                continue
            if (rel, i + 1) in ALLOWLIST:
                continue
            out.append(f"{rel}:{i + 1}: {raw.strip()}")
    return out


def test_no_unguarded_letter_range_in_shipped_shell():
    findings = _findings()
    assert not findings, (
        "bracket ranges matched under the caller's locale rather than "
        "byte-wise (#695). Each is a guard or an allowlist whose meaning "
        "changes with the user's language: under Turkish collation `[A-Z]` "
        "does not match `I`, and under a widening locale `[A-Za-z]` matches "
        "`é`. Put `local LC_ALL=C` in the enclosing function, prefix the "
        "command with `LC_ALL=C`, or add the line to ALLOWLIST with the "
        "reason it is safe:\n  " + "\n  ".join(findings)
    )


def test_the_scanner_sees_through_a_nested_function():
    """MUST-FIRE control for the shape that actually occurs in this tree.

    `_function_spans` used to close a function at the first `}` at column 0,
    on the stated assumption that no function is defined inside another. #696
    broke that assumption without anyone noticing: `capture_was_seen() {` is
    defined at column 0 INSIDE `_remember_deferred_phase() {`, so the outer
    span was computed as ending at the inner function's brace, and the inner
    function's `local LC_ALL=C` was credited to 148 lines of the outer one
    that has no such declaration. Every range in those lines would have been
    reported protected while it was not -- a scanner that is the whole
    standing defence for a class that already shipped a total-failure bug.

    Neither of the two controls below could see it: both use flat fixtures.
    This one is the nested shape, both halves -- the outer lines must NOT
    inherit the inner's declaration, and the inner's own lines must still be
    recognised."""
    nested = (
        'outer() {\n'                                   # 0
        '    case "$1" in *[!A-Za-z0-9._-]*) : ;; esac\n'  # 1  unprotected
        'inner() {\n'                                   # 2
        '    local LC_ALL=C\n'                           # 3
        '    case "$2" in *[!A-Za-z0-9._-]*) : ;; esac\n'  # 4  protected
        '}\n'                                           # 5
        '    case "$3" in *[!A-Za-z0-9._-]*) : ;; esac\n'  # 6  unprotected
        '}\n'                                           # 7
    )
    lines = nested.splitlines()

    assert not _is_protected(nested, 1, lines[1]), (
        "a line in the outer function, before the nested one even opens, was "
        "reported protected -- the nested function's `local LC_ALL=C` is "
        "being credited to lines it does not govern"
    )
    assert _is_protected(nested, 4, lines[4]), (
        "the nested function's own line was not recognised as protected -- "
        "fixing the over-reporting must not break the ordinary case"
    )
    assert not _is_protected(nested, 6, lines[6]), (
        "a line in the outer function AFTER the nested one closed was "
        "reported protected -- the inner declaration does not reach here "
        "either"
    )


def test_the_scanner_actually_fires():
    """MUST-FIRE control. A negative assertion passes just as well when the
    scanner finds nothing because it is broken -- a stale function-span
    regex, a path list pointing at an empty directory. Feed it the real
    defect's shape and require it to be seen; feed it the fixed shape and
    require it not to be."""
    unguarded = 'f() {\n    case "$1" in *[!A-Za-z0-9._-]*) return 1 ;; esac\n}\n'
    guarded = ('f() {\n    local LC_ALL=C\n'
               '    case "$1" in *[!A-Za-z0-9._-]*) return 1 ;; esac\n}\n')

    assert _RANGE.search(unguarded), "the range regex no longer sees a plain range"
    assert not _is_protected(unguarded, 1, unguarded.splitlines()[1]), (
        "the scanner called an unguarded function protected -- every "
        "finding above would be dropped silently"
    )
    assert _is_protected(guarded, 2, guarded.splitlines()[2]), (
        "the scanner does not recognise `local LC_ALL=C` -- it would report "
        "every already-fixed site, and the allowlist would grow to hide them"
    )


def test_the_scanner_reads_a_non_empty_tree():
    """Second must-fire control: the file list. A rename of scripts/ would
    empty SHELL_DIRS and make the assertion above pass over nothing."""
    files = _shell_files()
    assert len(files) > 10, f"only {len(files)} shell files found -- SHELL_DIRS is stale"
    assert any(f.name == "log.sh" for f in files)


def test_every_allowlist_entry_still_points_at_a_range():
    """An allowlist entry whose line has moved silently exempts whatever is
    at that line NOW -- and stops exempting the thing it was written for,
    which then reappears as a finding nobody can explain."""
    for (rel, line_no), reason in ALLOWLIST.items():
        path = REPO_ROOT / rel
        assert path.is_file(), f"allowlisted file is gone: {rel}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert line_no <= len(lines), f"{rel}:{line_no} is past end of file"
        assert _RANGE.search(lines[line_no - 1]), (
            f"{rel}:{line_no} no longer holds a letter range -- the "
            f"exemption ({reason[:40]}...) has drifted off its line"
        )
