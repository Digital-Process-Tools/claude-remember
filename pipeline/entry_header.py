"""What a memory entry header looks like — defined once (#177).

There were two answers. `save-session.sh` validated `^## (HH:MM|H:MM AM/PM) \\|`
— 12-hour form accepted, trailing pipe required. `consolidate.py` matched
`^## (HH:MM|Week of |YYYY-MM-DD)` — 24-hour only, no pipe.

Normally reconciled, because the header is rewritten to 24h before it is
written. Except when it is not: the re-validation fallback added in #139 keeps
the model's ORIGINAL line when splitting fails, and if that line carries an
AM/PM time, `consolidate.py` does not recognise the entry at all — it reads as
prose rather than as memory.

Nobody had to be wrong for that to happen. The two patterns simply drifted, the
way the slug did in five places (#144/#156/#157/#158/#174) and the position
readers did in five more. So the answer lives here, and the shell asks for it
rather than restating it:

    ENTRY_HEADER_ERE=$("$PYTHON" -m pipeline.entry_header --ere entry)

The patterns are written in POSIX ERE, which `grep -E` and Python's `re` both
accept for constructs this simple — no backreferences, no lazy quantifiers, no
lookaround.
"""

from __future__ import annotations

import re

#: A time of day in either form the pipeline can produce. 24-hour is what the
#: header is rewritten to; the 12-hour form reaches memory only through #139's
#: fallback, which is exactly the case that used to be invisible downstream.
TIME_ERE = r"([0-9]{2}:[0-9]{2}|[0-9]{1,2}:[0-9]{2} (AM|PM))"

#: A session entry: a time, then the identity field behind a pipe.
ENTRY_HEADER_ERE = rf"^## {TIME_ERE} \|"

#: Any header the memory layer recognises — session entries plus the day and
#: week headers consolidation writes.
ANY_HEADER_ERE = rf"^## ({TIME_ERE}|Week of |[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})"

_ENTRY = re.compile(ENTRY_HEADER_ERE)
_ANY = re.compile(ANY_HEADER_ERE, re.MULTILINE)


def is_entry_header(line: str) -> bool:
    """Whether `line` is a session entry header."""
    return _ENTRY.search(line) is not None


def contains_header(text: str) -> bool:
    """Whether `text` contains any recognised header, anywhere."""
    return _ANY.search(text) is not None


def main(argv: list[str]) -> int:
    """``python3 -m pipeline.entry_header --ere {entry|any}``.

    Prints the pattern for the shell to hand to `grep -E`, so there is one
    definition rather than one per language.
    """
    if len(argv) == 3 and argv[1] == "--ere":
        if argv[2] == "entry":
            print(ENTRY_HEADER_ERE)
            return 0
        if argv[2] == "any":
            print(ANY_HEADER_ERE)
            return 0
    print("usage: python3 -m pipeline.entry_header --ere {entry|any}")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
