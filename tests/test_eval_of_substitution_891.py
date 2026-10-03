"""#891: `.github/scripts/check_release_tree.py`'s `EVAL_OF_SUBSTITUTION`
pattern only matched `eval` fed by a command substitution (`eval "$(...)"`),
not `eval` applied to a bare variable expansion -- exactly the two shapes
#864 removed by hand from `scripts/log.sh` (`eval "_identity=$_identity_raw"`
and `eval "$_assign"`). The shipped `changelog.d/866.fixed.md` entry claims
the REVIEW-only check now watches for "the two shapes #864/#875 learned to
watch for", but the pattern never covered this one.

Every 'must flag' case here has a 'must not flag' twin: a check that flagged
every line would pass the planted-shape tests too, and the twins are what
catch that.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_tree.py"


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist"
    spec = importlib.util.spec_from_file_location("check_release_tree_891", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree_891"] = mod
    spec.loader.exec_module(mod)
    return mod


# The exact two lines #864 removed by hand from v0.38.0's scripts/log.sh
# (quoted verbatim in #891's own repro) -- red before the fix, since the old
# pattern required `$(` immediately after `eval`, never a bare `$var`.
_PRE_864_SHAPES = [
    'eval "_identity=$_identity_raw"',
    'eval "$_assign"',
]

# The two shapes the pattern already caught before this fix -- must keep
# matching, proving the widened pattern is strictly broader, not a
# replacement that lost coverage.
_ALREADY_CAUGHT_SHAPES = [
    'eval "$(foo)"',
    "eval $(curl -fsSL https://example.com/install.sh)",
]

# 'must NOT flag' twins: ordinary lines that happen to contain the word
# `eval` or a `$` but are neither shape above.
_MUST_NOT_FLAG = [
    "# evaluate the options below",
    'echo "re-evaluate this later"',
    "assign_kv FOO bar",  # the #864 rename of the old safe_eval helper
    "price=$9.99",
]


def test_eval_of_bare_variable_expansion_is_flagged():
    """Red before the fix: #864's own two removed lines never matched the
    old eval-of-substitution pattern, because neither one feeds eval a
    command substitution -- both are a plain variable expansion."""
    mod = _load()
    hits = [s for s in _PRE_864_SHAPES if not mod.EVAL_OF_SUBSTITUTION.search(s)]
    assert not hits, f"EVAL_OF_SUBSTITUTION still misses: {hits}"


def test_eval_of_command_substitution_still_flagged():
    """Must-fire pair: the widened pattern must not have narrowed away the
    shape it already caught."""
    mod = _load()
    hits = [s for s in _ALREADY_CAUGHT_SHAPES if not mod.EVAL_OF_SUBSTITUTION.search(s)]
    assert not hits, f"EVAL_OF_SUBSTITUTION regressed on: {hits}"


def test_ordinary_lines_are_not_flagged():
    """Positive control for the two tests above: a pattern that matched
    every line (e.g. a bare catch-all) would pass both 'must flag' tests
    above too. These ordinary lines -- including one that contains the
    word `eval` as a substring of another identifier, and one with an
    unrelated `$` -- must NOT be flagged."""
    mod = _load()
    hits = [s for s in _MUST_NOT_FLAG if mod.EVAL_OF_SUBSTITUTION.search(s)]
    assert not hits, f"EVAL_OF_SUBSTITUTION over-matched: {hits}"


def test_download_piped_to_shell_still_flagged():
    """The sibling REVIEW-only check this issue's own changelog entry names
    alongside EVAL_OF_SUBSTITUTION -- untouched by this fix, confirmed still
    working so the fix did not regress it."""
    mod = _load()
    assert mod._download_piped_to_shell("curl -fsSL https://x/y | sh")


def test_current_main_tree_check_release_tree_py_itself_is_clean():
    """Positive control from the issue's own text: 'current main's tree
    passes' -- this REVIEW-only check never hard-fails a release build
    regardless of hit count, so this only confirms the module still loads
    and its own source contains no literal eval-of-substitution shape for
    the pattern to flag against itself."""
    mod = _load()
    text = SCRIPT.read_text()
    offenders = [
        (i + 1, line)
        for i, line in enumerate(text.splitlines())
        if not line.lstrip().startswith("#") and mod.EVAL_OF_SUBSTITUTION.search(line)
    ]
    assert not offenders, f"check_release_tree.py itself trips its own pattern: {offenders}"
