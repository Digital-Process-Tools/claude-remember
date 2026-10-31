"""#920 self-review finding, round 2: lib-memory-dir.sh and post-tool-hook.sh
each need their own copy of the unsafe-dir predicate (post-tool-hook.sh's fast
path never sources lib-memory-dir.sh -- it replays a cached REMEMBER_DIR
instead -- so the check has to exist there standalone). A first version of
this fix gave both copies the SAME name, `_remember_dir_is_unsafe`, reasoning
that -- being byte-identical -- a silent name collision on post-tool-hook.sh's
slow path (where lib-memory-dir.sh IS also sourced) was harmless. CI's
`tests/test_compile_hooks_900.py` disagreed: the #900 compiled-hook build
inlines every sourced file's top level into one script, and two top-level
functions sharing one name there is a build failure
(`duplicate top-level function name '_remember_dir_is_unsafe'`), not a
runtime-harmless shadow. Fixed by renaming lib-memory-dir.sh's copy to
`_remember_lmd_dir_is_unsafe`. This file pins the weaker invariant that
survives the rename: the two functions' own LOGIC (everything but the
`name() {` line) must stay identical, so an edit to the absolute/UNC/newline
check in one that forgets the other still fails loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_remember_dir_guard_920 import HOOK, LOG_SH, _extract_function

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_MEMORY_DIR = REPO_ROOT / "scripts" / "lib-memory-dir.sh"


def _body_without_signature(text: str) -> str:
    """Drop the `name() {` first line and the closing `}` last line -- what
    is left is the part that must agree between differently-named twins."""
    lines = text.splitlines()
    return "\n".join(lines[1:-1])


def test_must_fire_the_three_unsafe_dir_predicates_stay_in_sync():
    lib_logic = _body_without_signature(
        _extract_function(LIB_MEMORY_DIR, "_remember_lmd_dir_is_unsafe")
    )
    hook_logic = _body_without_signature(
        _extract_function(HOOK, "_remember_dir_is_unsafe")
    )
    log_logic = _body_without_signature(
        _extract_function(LOG_SH, "_remember_log_dir_unsafe")
    )
    assert lib_logic == hook_logic == log_logic, (
        "the three REMEMBER_DIR-unsafe predicates (lib-memory-dir.sh's "
        "_remember_lmd_dir_is_unsafe, post-tool-hook.sh's own "
        "_remember_dir_is_unsafe, log.sh's _remember_log_dir_unsafe) have "
        "diverged in logic -- each is a SEPARATE, deliberately distinctly-"
        "named copy (so the #900 compiled-hook build does not see a "
        "duplicate top-level function name when it inlines them together), "
        "and nothing but this test keeps an edit to one from silently "
        "stopping short of the others.\n"
        f"lib-memory-dir.sh:\n{lib_logic}\n"
        f"post-tool-hook.sh:\n{hook_logic}\n"
        f"log.sh:\n{log_logic}"
    )


def test_must_not_fire_an_unrelated_function_is_not_mistaken_for_a_match():
    """Positive control: comparing a predicate against something that is NOT
    one of its twins must fail, or the equality check above proves nothing
    about precision."""
    lib_logic = _body_without_signature(
        _extract_function(LIB_MEMORY_DIR, "_remember_lmd_dir_is_unsafe")
    )
    unrelated_logic = _body_without_signature(
        _extract_function(LIB_MEMORY_DIR, "_classify_project_cfg_haiku_trust")
    )
    assert lib_logic != unrelated_logic
