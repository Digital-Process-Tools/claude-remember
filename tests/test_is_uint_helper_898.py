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
import subprocess
from pathlib import Path

import pytest

from tests._bash_runner import decode_bash_output, resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"

INPUTS = ["", "0", "00", "007", "123", "12a", "a1", " 1", "1 ", "-1", "+1",
          "1.5", "a\nb", "1\n", "\n", "*", "?", "[", "x*y", "é", "١",
          "99999999999999999999"]

BASHES = [b for b in {resolve_bash(), "/bin/bash" if Path("/bin/bash").exists() else None} if b]


def _helper() -> str:
    m = re.search(r"^_remember_is_uint\(\) \{.*?^\}$|^_remember_is_uint\(\) \{[^\n]*\}$",
                  LOG_SH.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    assert m, "_remember_is_uint not defined in scripts/log.sh"
    return m.group(0)


def _run(bash: str, body: str, x: str) -> str:
    out = subprocess.run([bash, "-c", body, "_", x], capture_output=True, check=False, timeout=30)
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
