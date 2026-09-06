"""Recompute the live set of win32 blanket-skip test modules (#497).

`grep -rl "pytestmark = pytest.mark.skipif" tests | xargs grep -l "win32"`
-- the command #497's own brief and `scripts/report_windows_skip_floor.py`'s
docstring both cite (92, then 107) -- over-counts. It requires only that both
substrings appear *somewhere* in the file, not that they form one real
`pytestmark` assignment. On this tree it catches a docstring that merely
*quotes* `pytestmark = pytest.mark.skipif(...)` as prose (`test_bash_runner_432.py`,
`test_sanctioned_divergence_state_440.py`) and, more importantly, files that
already carry the `resolve_bash()`-style route -- a local `_find_bash()` whose
own probe order happens to test `sys.platform == "win32"` and whose skip
reason is `"bash not available"` / `"Git Bash not found (...)"`, not a blanket
win32 skip at all (`test_stale_config_sweep_362.py`, `test_tmpdir_cleanup.py`,
`test_trap_quoting_375.py`, `test_log_sh.py`, `test_migration.py`,
`test_security_fixes.py`, plus the four files this issue's own #432 already
converted). Counted on this tree at the base commit for #497 (718a10f): the
grep-shape check finds 107 files; this AST-based check, requiring an actual
module-level `pytestmark = pytest.mark.skipif(...)` assignment (a bare call,
or one arm of a list/tuple of marks -- pytest ORs a list, so any one arm
mentioning `win32` is a real blanket skip, e.g. `test_slug_vectors_294.py`)
whose test expression mentions `win32`, finds 95. An earlier version of this
scanner only matched the bare-call form and undercounted at 94, missing the
list-form file above -- caught by review, not by the drift-detection test
below, since that test recomputes its own baseline from this same function
and so cannot see a module class the function itself never looked for.

Used by both `docs/windows-skip-triage.md`'s own generation and by
`tests/test_windows_skip_triage_497.py`, which recomputes this set fresh on
every run and diffs it against the file the doc lists -- so the doc cannot
silently drift out of sync with the tree the way the issue itself describes
happening to the grep-based count.
"""

from __future__ import annotations

import ast
import glob
import os
import re

# Every row in docs/windows-skip-triage.md's table opens with a path cell in
# backticks, followed by a reason cell and a verdict cell -- the same three
# leading columns regardless of how many trailing columns (e.g. "basis")
# follow. `tests/test_windows_skip_triage_497.py` (which only needs the path
# column, to diff the row set against the live tree) and
# `tests/test_windows_skip_triage_prose_totals_595.py` (which also needs the
# verdict column, to check the doc's prose totals against the table's own
# counts) both parse this same table. A second, independently-written copy of
# this regex in the newer file was flagged in #595's own self-review: nothing
# ties two separate patterns together, so a future change to the table's
# column layout could satisfy one guard's row model while silently breaking
# the other's. Parsing through this one shared function instead means both
# guards see the same rows or fail the same way.
_TABLE_ROW_RE = re.compile(r"^\|\s*`(tests/[^`]+\.py)`\s*\|([^|]*)\|([^|]*)\|", re.MULTILINE)


def parse_doc_table_rows(doc_text: str) -> list[tuple[str, str, str]]:
    """Return (path, reason, verdict) for every row in the doc's table,
    trimmed of surrounding whitespace. `doc_text` is the full text of
    docs/windows-skip-triage.md (or a fixture built to look like it)."""
    return [
        (path, reason.strip(), verdict.strip())
        for path, reason, verdict in _TABLE_ROW_RE.findall(doc_text)
    ]


def find_blanket_skip_modules(tests_dir: str) -> dict[str, str | None]:
    """Return {relative POSIX path: skip reason} for every test module under
    `tests_dir` that carries a module-level
    `pytestmark = pytest.mark.skipif(<expr mentioning win32>, reason=...)`.

    `tests_dir` is the tests directory itself (e.g. `.../tests`); returned
    paths are relative to its parent, POSIX-separated, so they match the
    literal path strings a human (or this repo's own doc) would write.
    """
    root = os.path.dirname(os.path.abspath(tests_dir))
    found: dict[str, str | None] = {}

    for path in sorted(glob.glob(os.path.join(tests_dir, "**", "*.py"), recursive=True)):
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src, filename=path)
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "pytestmark" not in targets:
                continue

            # `pytestmark` is either one bare `pytest.mark.skipif(...)` call,
            # or a list/tuple of marks (pytest ORs them -- any one firing
            # skips the whole module), e.g. `pytestmark = [mark.skipif(...),
            # mark.skipif(...)]`. Check every candidate call either way.
            if isinstance(node.value, ast.Call):
                candidates = [node.value]
            elif isinstance(node.value, (ast.List, ast.Tuple)):
                candidates = [elt for elt in node.value.elts if isinstance(elt, ast.Call)]
            else:
                continue

            for call in candidates:
                func = call.func
                is_skipif = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "skipif"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "mark"
                )
                if not is_skipif:
                    continue
                segment = ast.get_source_segment(src, call) or ""
                if "win32" not in segment:
                    continue

                reason = None
                for kw in call.keywords:
                    if kw.arg == "reason":
                        try:
                            reason = ast.literal_eval(kw.value)
                        except ValueError:
                            reason = ast.get_source_segment(src, kw.value)

                rel = os.path.relpath(path, root).replace(os.sep, "/")
                found[rel] = reason

    return found


if __name__ == "__main__":
    # A reason string here can carry a printable non-ASCII character (an em
    # dash is routine in this tree's skip reasons). `repr()` leaves such
    # characters literal, and a default Windows console codepage (e.g.
    # cp1252) raises UnicodeEncodeError writing them to stdout -- killing
    # this script mid-report, after the scan work it is reporting already
    # ran. `ascii()` instead of `!r` guarantees pure-ASCII output (escaping
    # non-ASCII to backslash-u-XXXX) so the print itself can never fail on
    # codepage grounds, on any platform.
    here = os.path.dirname(os.path.abspath(__file__))
    tests_dir = os.path.join(os.path.dirname(here), "tests")
    modules = find_blanket_skip_modules(tests_dir)
    print(f"{len(modules)} blanket win32-skip modules found under {tests_dir}")
    for path in sorted(modules):
        print(f"  {path}: {modules[path]!a}")
