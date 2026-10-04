"""Tell a compiled hook from its source (#900).

The `hook tests (compiled)` CI leg (.github/workflows/tests.yml) runs
`.github/scripts/compile_hooks.py --apply` on a fresh checkout, which
overwrites each of the four hooks.json-registered scripts IN PLACE with its
compiled form: libraries inlined, whole-line comments stripped, unreached
functions dropped. Every behavioural test then exercises the compiled hook,
which is the point of that leg.

A handful of tests are not behavioural: they pin facts about a hook's SOURCE
TEXT -- a comment's wording (#637), how many comment lines keep an em-dash
(#367), that the hook's own code no longer spells a subshell (#511), which
source lines carry a letter range and where (#695). A compiled hook is a
build product, not that source: its comments are gone by design and its
lines carry inlined library code. Those tests skip a compiled hook, saying
so, rather than assert facts about the wrong file -- the plain `pytest` job
runs them against the real source on every OS and interpreter, so nothing
they check goes unchecked.

The compiler writes COMPILED_MARKER as the line right after the shebang;
.github/scripts/compile_hooks.py holds the same string, and
tests/test_compile_hooks_900.py pins that a compiled hook carries it and a
source hook does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

COMPILED_MARKER = "# Compiled by .github/scripts/compile_hooks.py (#900)"


def is_compiled_text(text: str) -> bool:
    """True when TEXT is compiler output: the marker sits on line 2."""
    lines = text.splitlines()[:2]
    return len(lines) == 2 and lines[1].startswith(COMPILED_MARKER)


def skip_if_compiled(path, why: str = "") -> None:
    """Skip the calling test when PATH is a compiled hook, naming why.

    WHY replaces the default reason (source text) for a test whose premise
    is something else a compiled hook does not have -- e.g. that the hook
    loads a library from disk at run time, which a self-contained compiled
    hook by construction does not."""
    path = Path(path)
    if is_compiled_text(path.read_text(encoding="utf-8")):
        pytest.skip(
            f"{path.name} is the compiled build (#900 compiled CI leg), not "
            f"its source -- "
            + (why or "this test pins source text")
            + ", which the plain pytest job checks on every leg"
        )
