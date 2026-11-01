"""#1083, case 6: a Windows path case that is NOT skipif(win32).

Scheduler review on #1083's own PR, finding 3: the class exercising this
case previously lived inside tests/test_session_store_pointer_1083.py,
whose module-level `pytestmark = pytest.mark.skipif(sys.platform ==
"win32", ...)` pytest applies to EVERY item in that module -- including a
class defined inside it, regardless of the class's own docstring claiming
otherwise. A class's own `pytestmark` attribute only ADDS markers; it
cannot remove or override one the module already declared. The only way to
make this case actually run on windows-latest is a module with no such
skip at all, which is this file.

The pointer-path logic lib-session-store.sh's own shape predicate implements
is pure path handling, not filesystem access -- a drive letter and a UNC
path are bash STRING matching, identical on every platform the shell itself
runs on. Observed on macOS/Linux; REASONED (not observed) for the actual
Windows runner, consistent with this repo's own convention for the sibling
ancestor-walk termination logic (see write-handoff.sh's own #1058/PR #1081
comment on the UNC root case).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SESSION_STORE = REPO_ROOT / "scripts" / "lib-session-store.sh"


def _check(value: str) -> str:
    script = (
        "set -u\n"
        f'source "{LIB_SESSION_STORE}"\n'
        '_remember_session_store_shape_ok "$1" && echo ACCEPTED || echo REJECTED\n'
    )
    result = subprocess.run(
        ["bash", "-c", script, "shape-check", value],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip()


def test_a_drive_letter_path_is_accepted():
    assert _check("C:/Users/dev/project/.remember") == "ACCEPTED"


def test_a_drive_letter_path_with_backslashes_is_accepted():
    assert _check("C:\\Users\\dev\\project\\.remember") == "ACCEPTED"


def test_a_unc_path_is_accepted():
    assert _check("\\\\server\\share\\.remember") == "ACCEPTED"


def test_a_bare_backslash_pair_with_nothing_after_is_rejected():
    """Exactly two backslashes and no third character at all -- not a
    genuine UNC share, and must not be accepted as one."""
    assert _check("\\\\") == "REJECTED"


def test_a_three_backslash_prefixed_value_is_rejected():
    """#920 scheduler review round 2, item 6's own counterexample: a third
    character that is ITSELF a backslash, with nothing real after it. The
    old, looser UNC arm accepted any third character including another
    backslash; the stricter one this file relies on requires the third
    character to NOT be a backslash."""
    assert _check("\\\\\\x") == "REJECTED"


def test_a_relative_path_is_rejected():
    assert _check("relative/path/.remember") == "REJECTED"


def test_a_value_with_an_embedded_newline_is_rejected():
    assert _check("/tmp/proj/.remember\nmalicious") == "REJECTED"


def test_an_empty_value_is_rejected():
    assert _check("") == "REJECTED"
