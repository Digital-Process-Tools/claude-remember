"""#920 self-review finding: lib-memory-dir.sh and post-tool-hook.sh each
define their own, textually-identical copy of `_remember_dir_is_unsafe()` --
necessarily duplicated because post-tool-hook.sh's fast path never sources
lib-memory-dir.sh (it replays a cached REMEMBER_DIR instead), so the function
has to exist there standalone. The two definitions share one name and, within
the SAME process on the slow path, the second one silently overwrites the
first rather than one calling the other -- harmless only as long as both
bodies stay byte-identical. This pins that invariant so a future edit to one
copy (e.g. widening the UNC case further) that forgets the other fails loudly
here instead of silently diverging with no test to catch it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_remember_dir_guard_920 import HOOK, LOG_SH, _extract_function

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_MEMORY_DIR = REPO_ROOT / "scripts" / "lib-memory-dir.sh"


def test_must_fire_the_two_unsafe_dir_predicates_stay_in_sync():
    lib_body = _extract_function(LIB_MEMORY_DIR, "_remember_dir_is_unsafe")
    hook_body = _extract_function(HOOK, "_remember_dir_is_unsafe")
    assert lib_body == hook_body, (
        "lib-memory-dir.sh's and post-tool-hook.sh's own copies of "
        "_remember_dir_is_unsafe have diverged -- on post-tool-hook.sh's "
        "slow path the SECOND definition silently wins the name collision, "
        "so an edit to only one copy can look applied everywhere while "
        "actually never reaching the process that sources both.\n"
        f"lib-memory-dir.sh:\n{lib_body}\npost-tool-hook.sh:\n{hook_body}"
    )


def test_must_not_fire_log_sh_predicate_has_its_own_distinct_name():
    """Positive control: log.sh's twin is a DIFFERENT function name
    (_remember_log_dir_unsafe) by design -- asserting sameness against the
    wrong pair would be a test that could never fail for the right reason."""
    log_body = _extract_function(LOG_SH, "_remember_log_dir_unsafe")
    hook_body = _extract_function(HOOK, "_remember_dir_is_unsafe")
    # Same LOGIC (both carry the identical UNC/absolute/newline check), just
    # under the other function's own name -- so a body-for-body string
    # compare would legitimately fail; only the final `[[ ]]` guard line
    # matters here, and it should still match.
    assert log_body.splitlines()[-2] == hook_body.splitlines()[-2]
