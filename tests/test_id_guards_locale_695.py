"""The two `[[ =~ ]]` guards #695's matrix showed actually break (#695).

#698's diagnostic run measured which bracket-range shapes move with the
locale on glibc. The answer was narrower than expected and is what this module
is scoped to: only `[[ =~ ]]` collates. Under a `localedef`-built `tr_TR.UTF-8`
on ubuntu-latest / bash 5.2,

    [[ "I"  =~ ^[A-Z]+$      ]]   C: match     tr_TR: NO MATCH
    [[ "i"  =~ ^[a-z]+$      ]]   C: match     tr_TR: NO MATCH
    [[ "ID" =~ ^[A-Za-z0-9_]+$ ]] C: match     tr_TR: NO MATCH
    [[ "é"  =~ ^[A-Za-z]+$   ]]   C: NO match  tr_TR: MATCH

while `case` patterns and `${v//[!...]/}` did not move at all, in either
direction. So two live sites are in scope here, both reached through `=~`:

  * `_remember_cfg_flatten_cache_valid_line` (scripts/log.sh) validates each
    line of the config-data cache against `^_RCFG_[A-Za-z0-9_]+=`. A config
    key carrying an `I` makes the line read as malformed, the cache is
    refused, and every hook falls back to re-reading and re-flattening the
    config -- permanently, silently, on that host. Not a wrong answer: a
    performance floor that nothing reports, which is the same shape of
    invisible loss #695 itself was.

  * `_remember_normalize_win_path` (scripts/resolve-paths.sh, mirrored in
    scripts/lib-env-cache.sh) recognises a Windows drive with
    `^([a-zA-Z]):[/\\]`. Drive `I:` stops being recognised, and the path is
    handed back unnormalised. Its `tr '[:lower:]' '[:upper:]'` is the second
    half of the same defect: under Turkish rules an `i:` drive upper-cases to
    a dotted `İ`, a multi-byte character in a place that must hold one ASCII
    letter.

The fix in both is `local LC_ALL=C`, scoped to the function.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="glibc locale collation under a bash subprocess -- the behaviour "
           "under test does not exist on Windows runners (#695)",
)

from ._locale_probe import SKIP_REASON, collation_locale

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT = REPO_ROOT / "scripts" / "detect-tools.sh"
LIBDIR = REPO_ROOT / "scripts" / "lib-memory-dir.sh"
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
RESOLVE = REPO_ROOT / "scripts" / "resolve-paths.sh"


def _bash(script: str, env_extra: dict, tmp_path: Path) -> str:
    env = {**os.environ, **env_extra}
    result = subprocess.run(
        ["bash", "-c", f"""
        set -u
        export PIPELINE_DIR={REPO_ROOT}
        export PROJECT_DIR={tmp_path}
        # resolve-paths.sh reads this at source time under `set -u`; without it
        # the whole harness exits 127 before the function under test is ever
        # called, and four legs below report a failure that is about this
        # script rather than about the locale.
        export CLAUDE_PROJECT_DIR={tmp_path}
        {script}
        """], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, (
        f"harness failed (exit {result.returncode}):\n{result.stderr}")
    return result.stdout.strip()


def _cache_line_verdict(line: str, env_extra: dict, tmp_path: Path) -> str:
    return _bash(f"""
        source {DETECT} >/dev/null 2>&1
        source {LIBDIR} >/dev/null 2>&1
        source {LOG_SH} >/dev/null 2>&1
        if _remember_cfg_flatten_cache_valid_line '{line}'; then
            echo VALID
        else
            echo REJECTED
        fi
    """, env_extra, tmp_path)


def _extract_normalize_win_path() -> str:
    """Pull `_remember_normalize_win_path()`'s definition out of
    resolve-paths.sh.

    Sourcing the script does not leave the function callable: it is used
    during resolution and then removed with `unset -f` (resolve-paths.sh:340),
    deliberately, so nothing downstream can reach it. Extracting the
    definition is the same route tests/test_save_session_marker_unreadable_625.py
    already takes for ts_marker_read, and for the reason that module records:
    a plain multiline regex, never a `sed`-into-`source <(...)` pipeline, which
    was observed failing on macOS CI only."""
    text = (REPO_ROOT / "scripts" / "resolve-paths.sh").read_text(encoding="utf-8")
    match = re.search(r"^_remember_normalize_win_path\(\).*?^\}",
                      text, re.MULTILINE | re.DOTALL)
    assert match, (
        "_remember_normalize_win_path() not found in scripts/resolve-paths.sh "
        "-- the extraction regex is stale"
    )
    return match.group(0) + "\n"


def _normalized(path: str, env_extra: dict, tmp_path: Path) -> str:
    func_file = tmp_path / "normalize.sh"
    func_file.write_text(_extract_normalize_win_path(), encoding="utf-8")
    return _bash(f"""
        source '{func_file}'
        OSTYPE=msys
        _remember_normalize_win_path '{path}'
        echo
    """, env_extra, tmp_path)


def _locale_env():
    found = collation_locale()
    if found is None:
        pytest.skip(SKIP_REASON)
    name, overlay = found
    return name, {**overlay, "LC_ALL": name, "LANG": name}


class TestConfigCacheLineUnderTurkishCollation:

    def test_a_key_containing_i_is_still_a_valid_cache_line(self, tmp_path):
        name, env = _locale_env()
        assert _cache_line_verdict("_RCFG_ID=x", env, tmp_path) == "VALID", (
            f"the config-data cache rejected `_RCFG_ID=x` under {name!r}: "
            f"`^_RCFG_[A-Za-z0-9_]+=` is matched by that locale's collation, "
            f"which does not place `I` inside A-Z. Every hook then falls back "
            f"to re-reading and re-flattening the config, forever, with "
            f"nothing said (#695)"
        )

    def test_positive_control_a_key_without_i_is_valid(self, tmp_path):
        """If this one failed too, the harness never reached the function and
        the assertion above would be about nothing."""
        _name, env = _locale_env()
        assert _cache_line_verdict("_RCFG_MODEL=x", env, tmp_path) == "VALID"

    def test_the_validator_still_rejects_a_non_cache_line(self, tmp_path):
        """Must-not-fire half: the C locale must not be bought by accepting
        anything that is not an `_RCFG_` assignment."""
        _name, env = _locale_env()
        assert _cache_line_verdict("rm -rf /", env, tmp_path) == "REJECTED"
        assert _cache_line_verdict("_OTHER_KEY=x", env, tmp_path) == "REJECTED"


class TestWindowsDriveUnderTurkishCollation:

    def test_drive_i_is_still_recognised(self, tmp_path):
        name, env = _locale_env()
        assert _normalized("I:/x", env, tmp_path) == "I:\\x", (
            f"drive `I:` was not recognised under {name!r} -- "
            f"`^([a-zA-Z]):[/\\]` is collated there, so the path is handed "
            f"back unnormalised and every later comparison against it is "
            f"against a different spelling (#695)"
        )

    def test_lowercase_drive_i_uppercases_to_ascii_not_dotted_capital(self, tmp_path):
        """The second half of the same defect. `tr '[:lower:]' '[:upper:]'`
        under Turkish rules maps `i` to the dotted `İ` -- two bytes, and not
        a drive letter any Windows API will accept."""
        name, env = _locale_env()
        got = _normalized("i:/x", env, tmp_path)
        assert got == "I:\\x", (
            f"lower-case drive `i:` became {got!r} under {name!r} rather "
            f"than the ASCII `I:\\x` -- Turkish case rules reached a place "
            f"that must hold one ASCII letter (#695)"
        )

    def test_positive_control_a_drive_without_i(self, tmp_path):
        _name, env = _locale_env()
        assert _normalized("c:/x", env, tmp_path) == "C:\\x"

    def test_a_non_drive_path_is_returned_untouched(self, tmp_path):
        """Must-not-fire half: forcing the C locale must not make the
        normaliser start claiming paths that are not drive paths."""
        _name, env = _locale_env()
        assert _normalized("/home/x", env, tmp_path) == "/home/x"


class TestTheHarnessItselfRuns:
    """Every leg above skips on a runner that cannot reproduce #695 -- which
    is most of them, and all of macOS. A harness that had simply stopped
    working would skip identically and say nothing: that is exactly what
    happened on this branch's first CI run, where four legs reported a
    failure that was `exit 127` from sourcing, not the locale.

    These two run under the C locale on every platform, so a broken harness
    is a loud failure everywhere rather than a silent skip.
    """

    def test_the_cache_line_validator_is_reachable(self, tmp_path):
        assert _cache_line_verdict("_RCFG_ID=x", {"LC_ALL": "C"}, tmp_path) == "VALID"

    def test_the_win_path_normaliser_is_reachable(self, tmp_path):
        assert _normalized("I:/x", {"LC_ALL": "C"}, tmp_path) == "I:\\x"
