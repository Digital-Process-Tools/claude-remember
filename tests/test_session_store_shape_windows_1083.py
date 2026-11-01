"""#1083, case 6: a Windows path case that is NOT skipif(win32).

Scheduler review on #1083's own PR, finding 3: the class exercising this
case previously lived inside tests/test_session_store_pointer_1083.py,
whose module-level `pytestmark = pytest.mark.skipif(sys.platform ==
"win32", ...)` pytest applies to EVERY item in that module -- including a
class defined inside it, regardless of the class's own docstring claiming
otherwise. A class's own `pytestmark` attribute only ADDS markers; it
cannot remove or override one the module already declared. This file has
no such skip, so it runs on every platform including windows-latest.

First CI round on windows-latest found a SECOND bug this move exposed:
the literal string "bash" on PATH there commonly resolves to the WSL
launcher (which then prints "Windows Subsystem for Linux has no installed
distributions" to stdout and exits nonzero), not Git Bash -- exactly the
hazard tests/_bash_runner.py's own module docstring documents and that
test_hooks_json.py, test_hook_cwd_leak_417.py and others already route
around via resolve_bash()/find_git_bash(). This file now uses that same
helper instead of a raw ["bash", "-c", ...] call, and passes the value
under test on stdin (octal-escaped, via bash_octal/printf '%b') rather
than as an argv entry -- argv is NOT trusted on Windows either: Git Bash
rebuilds it from the raw Windows command line itself, so an unquoted
backslash or an embedded newline in an argv string does not survive
intact (see run_bash_file's own docstring).

The pointer-path logic lib-session-store.sh's own shape predicate implements
is pure path handling, not filesystem access -- a drive letter and a UNC
path are bash STRING matching, identical on every platform the shell itself
runs on, once that shell is genuinely bash and the input reaches it intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._bash_runner import (
    bash_octal,
    decode_bash_output,
    resolve_bash,
    run_bash_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SESSION_STORE = REPO_ROOT / "scripts" / "lib-session-store.sh"

BASH = resolve_bash()

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (not Git Bash on Windows, no bash on PATH elsewhere)",
)


def _check(value: str) -> str:
    script = (
        "set -u\n"
        "IFS= read -r enc\n"
        'val=""\n'
        '[ -z "$enc" ] || printf -v val "%b" "$enc"\n'
        f'source "{LIB_SESSION_STORE.as_posix()}"\n'
        '_remember_session_store_shape_ok "$val" && echo ACCEPTED || echo REJECTED\n'
    )
    stdin = (bash_octal(value) + "\n").encode("ascii")
    result = run_bash_file(BASH, script, stdin=stdin)
    return decode_bash_output(result.stdout).strip()


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
