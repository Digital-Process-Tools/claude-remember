"""DIAGNOSTIC, NOT A REGRESSION TEST. Fails on purpose to print its table.

#695 proved ONE shape diverges under glibc Turkish collation: `[A-Z]` does
not match `I`. Every other bracket range in scripts/ uses a different
spelling -- `[a-zA-Z]`, `[A-Za-z0-9._-]`, negated `[!A-Za-z0-9._-]`, and the
`${v//[!a-zA-Z0-9]/-}` substitution form -- and whether THOSE exclude `i`/`I`
is a separate question that decides how many sites actually need fixing.

It cannot be answered on macOS: probed across C, en_US.UTF-8 and tr_TR.UTF-8
there, all six shapes behave identically, because darwin's libc does not do
collation ranges the way glibc does. So this module exists to get the matrix
off a Linux runner, once. Delete it with the branch.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="glibc locale collation under a bash subprocess -- diagnostic only (#695)",
)

# Each row: a label, and a bash snippet that prints MATCH or NOMATCH.
SHAPES = [
    ("[A-Z] =~ I", '[[ "I" =~ ^[A-Z]+$ ]] && echo MATCH || echo NOMATCH'),
    ("[a-z] =~ i", '[[ "i" =~ ^[a-z]+$ ]] && echo MATCH || echo NOMATCH'),
    ("[a-zA-Z] =~ I", '[[ "I" =~ ^[a-zA-Z]+$ ]] && echo MATCH || echo NOMATCH'),
    ("[a-zA-Z] =~ i", '[[ "i" =~ ^[a-zA-Z]+$ ]] && echo MATCH || echo NOMATCH'),
    ("[A-Za-z] =~ I", '[[ "I" =~ ^[A-Za-z]+$ ]] && echo MATCH || echo NOMATCH'),
    ("[A-Za-z0-9_] =~ ID", '[[ "ID" =~ ^[A-Za-z0-9_]+$ ]] && echo MATCH || echo NOMATCH'),
    ("[A-Za-z] =~ accented", '[[ "é" =~ ^[A-Za-z]+$ ]] && echo MATCH || echo NOMATCH'),
    ("case [A-Za-z]:/* vs I:/x",
     'case "I:/x" in [A-Za-z]:/*) echo MATCH ;; *) echo NOMATCH ;; esac'),
    ("case *[!A-Za-z0-9._-]* vs SESS-I",
     'case "SESS-I" in *[!A-Za-z0-9._-]*) echo NOMATCH ;; *) echo MATCH ;; esac'),
    ("case *[!A-Za-z0-9._-]* vs accented",
     'case "café" in *[!A-Za-z0-9._-]*) echo NOMATCH ;; *) echo MATCH ;; esac'),
    ("subst ${v//[!a-zA-Z0-9]/-} on /a/IDX/b",
     'v="/a/IDX/b"; echo "${v//[!a-zA-Z0-9]/-}"'),
    ("subst ${v//[!A-Za-z0-9]/_} on a-i-b",
     'v="a-i-b"; echo "${v//[!A-Za-z0-9]/_}"'),
]


def _build_locale() -> "tuple[str, dict] | None":
    """tr_TR.UTF-8 if installed, else compiled with localedef into a scratch
    LOCPATH (no root needed). Accepted only once bash is watched refusing
    `I` against `[A-Z]` under it -- the one divergence #695 already
    established, used here as the probe's own liveness check."""
    def works(name, overlay):
        p = subprocess.run(["bash", "-c", '[[ "I" =~ ^[A-Z]+$ ]]'],
                           env={**os.environ, **overlay,
                                "LC_ALL": name, "LANG": name},
                           capture_output=True, check=False, timeout=10)
        return p.returncode != 0

    installed = subprocess.run(["locale", "-a"], capture_output=True,
                               text=True, check=False, timeout=10).stdout
    for name in (n.strip() for n in installed.splitlines()):
        if name.lower().startswith(("tr_tr", "az_az")) and works(name, {}):
            return (name, {})
    locpath = Path(tempfile.mkdtemp(prefix="diag695-"))
    target = locpath / "tr_TR.UTF-8"
    built = subprocess.run(
        ["localedef", "-i", "tr_TR", "-f", "UTF-8", str(target)],
        capture_output=True, text=True, check=False, timeout=60)
    overlay = {"LOCPATH": str(locpath)}
    if target.exists() and works("tr_TR.UTF-8", overlay):
        return ("tr_TR.UTF-8", overlay)
    return None


def _run(snippet: str, name: str, overlay: dict) -> str:
    p = subprocess.run(["bash", "-c", snippet],
                       env={**os.environ, **overlay, "LC_ALL": name,
                            "LANG": name},
                       capture_output=True, text=True, check=False, timeout=15)
    return (p.stdout.strip() or f"<empty, exit {p.returncode}>")


def test_print_the_range_matrix():
    found = _build_locale()
    if found is None:
        pytest.fail(
            "DIAG: no tr_TR locale installed AND localedef could not build "
            "one -- that is itself the finding: this runner cannot reproduce "
            "#695, and tests/test_safe_eval_locale_695.py's collation legs "
            "are skipping here rather than asserting"
        )
    name, overlay = found
    rows = []
    for label, snippet in SHAPES:
        c = _run(snippet, "C", {})
        tr = _run(snippet, name, overlay)
        flag = "  <-- DIVERGES" if c != tr else ""
        rows.append(f"  {label:<40} C={c:<12} {name}={tr:<12}{flag}")
    pytest.fail("DIAG #695 range matrix (deliberate failure):\n" + "\n".join(rows))
