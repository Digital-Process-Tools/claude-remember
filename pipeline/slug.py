"""Claude Code's session-directory slug — the single Python implementation.

`~/.claude/projects/<slug>/` is where Claude Code writes session transcripts,
and the plugin has to compute the same name or it reads an empty directory and
saves nothing, in silence. That has already happened twice (#144, #166), so the
rules are written down here rather than re-derived per call site.

From the shipped CLI bundle:

    var jXA = 200;
    function JXA(A) {
      let q = A.replace(/[^a-zA-Z0-9]/g, "-");
      if (q.length <= jXA) return q;
      let K = 0;
      for (let _ = 0; _ < A.length; _++)
        K = (K << 5) - K + A.charCodeAt(_), K |= 0;
      return `${q.slice(0, jXA)}-${Math.abs(K).toString(36)}`;
    }

Two details do all the damage:

1. **No `/u` flag on that regex**, so it walks UTF-16 CODE UNITS, not
   codepoints. A BMP character (é, 日, プ) costs one dash; anything astral
   (emoji, Extension-B kanji) is a surrogate pair and costs *two*. Python
   strings are codepoint-based, so a plain ``re.sub`` silently disagrees on
   exactly the astral case — which is how the Python copy stayed wrong after
   the bash one was fixed (#174).

2. **Past 200 characters it truncates and appends a hash** of the ORIGINAL
   path (not the slug), `charCodeAt` again, forced to signed 32-bit by `|0`
   every round, then `Math.abs(...).toString(36)`. Nothing here truncated, so
   any project whose slugged path exceeded 200 characters computed a name
   Claude Code never created and never saved anything (#157).
"""

from __future__ import annotations

import os

SLUG_MAX = 200

_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _utf16_code_units(text: str) -> list[int]:
    """The string as UTF-16 code units, which is what `charCodeAt` returns."""
    raw = text.encode("utf-16-le", "surrogatepass")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def _to_base36(value: int) -> str:
    """`Number.prototype.toString(36)` for a non-negative integer."""
    if value == 0:
        return "0"
    out = []
    while value:
        value, digit = divmod(value, 36)
        out.append(_BASE36_DIGITS[digit])
    return "".join(reversed(out))


def path_hash(path: str) -> str:
    """The suffix Claude Code appends to an over-long slug.

    `K = (K << 5) - K + c` is `K * 31 + c`; `|0` after each round is a wrap to
    signed 32-bit. JS computes the multiply as a double first, but the
    intermediate stays under 2**37 and is therefore exact, so plain integer
    arithmetic modulo 2**32 gives the same answer.
    """
    acc = 0
    for unit in _utf16_code_units(path):
        acc = (acc * 31 + unit) & 0xFFFFFFFF
    if acc >= 0x80000000:
        acc -= 0x100000000
    return _to_base36(abs(acc))


def _fold_drive_letter(path: str) -> str:
    """Lower-case a leading Windows drive letter, matching lib-slug.sh (#268).

    scripts/lib-slug.sh folds this unconditionally since #263; this module
    did not fold at all, because it is a transcription of Claude Code's own
    JXA routine, which never sees a raw drive letter — the host normalises
    before calling it. On Windows, CLAUDE_PROJECT_DIR reaches this function
    already normalised to the native Win32 form with an UPPER-case drive
    (resolve-paths.sh's own regex uppercases), so this slugged it literally
    and disagreed with the shell side by one character's case.

    The direction is not a free choice (see #263's PR): Git for Windows
    ships cygpath, so the working majority's on-disk stores are already
    spelled with the lower-case drive; folding the other way would fix the
    minority and rename every other store.

    Anchored to position 0 only, matching lib-slug.sh's `case "$path" in
    ?:*)`: an embedded ``X:`` elsewhere in a path is an ordinary colon, not a
    drive letter, and must not be touched.
    """
    if len(path) >= 2 and path[1] == ":" and "A" <= path[0] <= "Z":
        return path[0].lower() + path[1:]
    return path


def session_dir_slug(path: str) -> str:
    """The directory name Claude Code uses for ``path``."""
    path = _fold_drive_letter(path)
    slug = "".join(
        c if c.isascii() and c.isalnum() else "-" * (2 if ord(c) > 0xFFFF else 1)
        for c in path
    )
    if len(slug) <= SLUG_MAX:
        return slug
    return slug[:SLUG_MAX] + "-" + path_hash(path)


def main(argv: list[str]) -> int:
    """``python3 -m pipeline.slug [--hash] <path>``.

    ``--hash`` prints only the suffix: the shell implementation does its own
    character replacement (it cannot afford a subprocess on a function the
    post-tool hook calls on every tool call) and needs this part alone, and
    only for the rare over-long path.
    """
    args = argv[1:]
    want_hash = False
    if args and args[0] == "--hash":
        want_hash = True
        args = args[1:]
    if len(args) != 1:
        print("usage: python3 -m pipeline.slug [--hash] <path>")
        return 2

    # argv arrives through the filesystem encoding, so bytes that are not valid
    # UTF-8 reach here as surrogate escapes — one per bad byte. Decoding them
    # the way the real decoder does collapses each ill-formed sequence into a
    # single replacement character by the maximal-subpart rule, which is the
    # whole reason the shell delegates a malformed path here at all (#186).
    # Library callers already hold a proper str and are unaffected.
    raw = args[0]
    try:
        raw = os.fsencode(raw).decode("utf-8", "replace")
    except (UnicodeError, ValueError):
        pass

    print(path_hash(raw) if want_hash else session_dir_slug(raw))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
