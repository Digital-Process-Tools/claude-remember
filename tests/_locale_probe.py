"""Find a locale that actually reproduces #695 on THIS machine.

Shared by the behavioural halves of #695 (tests/test_safe_eval_locale_695.py,
tests/test_id_guards_locale_695.py). Kept out of both so the rule stays in one
place: a locale is accepted by OBSERVED behaviour, never by its name. macOS
ships a `tr_TR.UTF-8` that does not collate ranges at all, so a name-only
choice would run the assertions under a locale where the bug cannot occur and
report a pass that means nothing.
"""

from __future__ import annotations

import functools
import os
import subprocess
import tempfile
from pathlib import Path

SKIP_REASON = (
    "no locale on this runner makes bash refuse 'I' against [A-Z], and none "
    "could be compiled with localedef -- #695 is a glibc collation behaviour "
    "and macOS's libc does not reproduce it under tr_TR or anything else, so "
    "this leg would assert nothing about the bug"
)


def _reproduces(name: str, env_extra: dict) -> bool:
    try:
        probe = subprocess.run(
            ["bash", "-c", '[[ "I" =~ ^[A-Z]+$ ]]'],
            env={**os.environ, **env_extra, "LC_ALL": name, "LANG": name},
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode != 0


@functools.lru_cache(maxsize=1)
def collation_locale() -> "tuple[str, dict] | None":
    """`(name, env_overlay)` for a locale that reproduces #695 here, or None.

    An already-installed locale first (tr_TR/az_AZ preferred -- the documented
    cases -- then anything else, since the property under test is the
    collation and not the language). Failing that, one compiled on the spot
    with `localedef` into a scratch directory and reached through `LOCPATH`,
    which needs no root: GitHub's Linux runners ship C.UTF-8 and en_US.UTF-8
    and nothing else, so without this step every caller would skip on the
    exact platform where the bug lives.
    """
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True,
                             timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    names = [line.strip() for line in out.splitlines() if line.strip()]
    preferred = [n for n in names if n.lower().startswith(("tr_tr", "az_az"))]
    for name in preferred + [n for n in names if n not in preferred]:
        if _reproduces(name, {}):
            return (name, {})

    locpath = Path(tempfile.mkdtemp(prefix="remember-695-locale-"))
    for source, name in (("tr_TR", "tr_TR.UTF-8"), ("az_AZ", "az_AZ.UTF-8")):
        target = locpath / name
        try:
            built = subprocess.run(
                ["localedef", "-i", source, "-f", "UTF-8", str(target)],
                capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            break
        if built.returncode != 0 or not target.exists():
            continue
        overlay = {"LOCPATH": str(locpath)}
        if _reproduces(name, overlay):
            return (name, overlay)
    return None
