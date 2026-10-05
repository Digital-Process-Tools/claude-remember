"""#898: `_remember_is_uint` (scripts/log.sh) replaces the spelled-out
"empty, or carries a non-digit" guard at the session-start sites.

The guard it replaces, and must agree with on every input:

    if [ -z "$x" ] || [ "${x#*[!0-9]}" != "$x" ]; then BAD; fi

Run in every bash found (PATH, plus macOS's /bin/bash 3.2 when present), on
the helper's own text read out of log.sh, so the test cannot drift from what
ships. Positive controls: both verdicts occur, and the helper is found.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from tests._bash_runner import (
    bash_octal,
    decode_bash_output,
    resolve_bash,
    run_bash_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"

INPUTS = ["", "0", "00", "007", "123", "12a", "a1", " 1", "1 ", "-1", "+1",
          "1.5", "a\nb", "1\n", "\n", "*", "?", "[", "x*y", "é", "١",
          "99999999999999999999"]

BASHES = [b for b in {resolve_bash(), "/bin/bash" if Path("/bin/bash").exists() else None} if b]


def _helper() -> str:
    # The one-line form first: tried the other way round, the multi-line
    # alternative matched too, from the one-liner down to the next `}` line
    # of log.sh -- 209 lines of config reads and log() under test, not the helper.
    m = re.search(r"^_remember_is_uint\(\) \{[^\n]*\}$|^_remember_is_uint\(\) \{\n.*?^\}$",
                  LOG_SH.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    assert m, "_remember_is_uint not defined in scripts/log.sh"
    return m.group(0)


def _run(bash: str, body: str, x: str) -> str:
    # X on stdin, the body from a file: Git Bash glob-expands and splits its
    # own argv (see run_bash_file), so `*` or a newline never arrive as passed.
    # bash 3.2's `printf -v arg %b ""` unsets arg: skipped for the empty input.
    script = ('IFS= read -r enc; arg=""; [ -z "$enc" ] || printf -v arg "%b" "$enc"; '
              'set -- "$arg"\n' + body)
    out = run_bash_file(bash, script, stdin=(bash_octal(x) + "\n").encode("ascii"), timeout=30)
    return decode_bash_output(out.stdout).strip()


OLD = 'x=$1; if [ -z "$x" ] || [ "${x#*[!0-9]}" != "$x" ]; then echo bad; else echo ok; fi'


@pytest.mark.skipif(not BASHES, reason="no bash")
@pytest.mark.parametrize("bash", sorted(BASHES))
def test_helper_agrees_with_the_guard_it_replaces(bash):
    new = _helper() + '\nif _remember_is_uint "$1"; then echo ok; else echo bad; fi'
    seen = set()
    for x in INPUTS:
        old_v, new_v = _run(bash, OLD, x), _run(bash, new, x)
        assert old_v == new_v, (bash, x, old_v, new_v)
        seen.add(new_v)
    # Positive control: the comparison saw both answers, not one echoed twice.
    assert seen == {"ok", "bad"}, seen


def test_bash_is_present_where_it_should_be():
    assert BASHES or shutil.which("bash") is None


@pytest.mark.skipif(not BASHES, reason="no bash")
@pytest.mark.parametrize("bash", sorted(BASHES))
def test_the_text_under_test_is_the_helper_alone(bash):
    """What the comparison above runs is the function definition and nothing
    after it: defining it prints nothing and leaves exactly one function.
    The first extraction ran 209 lines of log.sh along with it."""
    out = run_bash_file(bash, _helper() + '\ndeclare -F | while read -r _ _ f; do echo "$f"; done')
    assert decode_bash_output(out.stderr) == ""
    assert decode_bash_output(out.stdout).split() == ["_remember_is_uint"]
