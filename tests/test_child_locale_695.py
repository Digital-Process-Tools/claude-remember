"""`local LC_ALL=C` does not reach a child process (#695, round-1 audit finding).

#699 put `local LC_ALL=C` in thirteen functions to make their bracket ranges
byte-wise. That works for everything bash matches itself -- `[[ =~ ]]`, `case`,
`${v//[!...]/}` -- because bash reads the variable directly, shell-local or not.

It does NOT follow a child process across `fork`. `local` on a name that was
not already exported leaves it unexported, so a spawned program keeps the
caller's locale. Measured:

    LC_ALL unset in the environment   -> child sees no LC_ALL at all
    LC_ALL exported in the environment -> child sees LC_ALL=C

Two functions in that set spawn one:

    _drive=$(printf '%s' "$_drive" | tr '[:lower:]' '[:upper:]')

-- `_remember_normalize_win_path` (scripts/resolve-paths.sh) and
`_remember_env_cache_normalize_into` (scripts/lib-env-cache.sh). On the host
this whole issue is about -- `LANG=tr_TR.UTF-8` with `LC_ALL` unset, which is
what setting a system language actually produces -- that `tr` still runs under
Turkish rules, where lower-case `i` upper-cases to the dotted `İ`: two bytes,
in a slot that has to hold one ASCII drive letter.

tests/test_id_guards_locale_695.py did not catch it, and the reason is the
point: its harness passes `LC_ALL` in the subprocess environment, so `local
LC_ALL=C` shadowed an already-exported name and the child did see `C`. It
passed for a reason that does not hold on a real host -- this repo's CLAUDE.md
bar ("would this test still pass if the code did nothing?") answered from the
wrong environment.

So there are two legs here, deliberately:

  * the behavioural one, `LANG` set and `LC_ALL` unset, which needs glibc and
    skips elsewhere;
  * a control that runs on EVERY platform by putting a recording `tr` on PATH
    and asserting what locale it was actually called under. That one cannot
    skip, and it is what would have caught this on the machine it was written
    on.
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
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

from ._locale_probe import SKIP_REASON, collation_locale

REPO_ROOT = Path(__file__).resolve().parent.parent

# The two functions that spawn a child while holding the new `local LC_ALL=C`.
# The call shape differs: the `_into` variant takes a destination variable
# name first (it exists to answer without a subshell on the hot path), so the
# value is its $2. Getting that wrong calls the function with nothing to
# normalise and it never reaches the `tr` at all -- which is why the control
# below asserts the shim actually ran before asserting anything about it.
SPAWNING_FUNCTIONS = (
    ("scripts/resolve-paths.sh", "_remember_normalize_win_path", "'i:/x'"),
    ("scripts/lib-env-cache.sh", "_remember_env_cache_normalize_into", "_out 'i:/x'"),
)


def _extract(rel: str, func: str) -> str:
    """Pull one function's definition out of a script. Both of these are
    `unset -f`'d after use, so sourcing the file does not leave them
    callable -- the same route tests/test_save_session_marker_unreadable_625.py
    documents for ts_marker_read."""
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(func)}\(\).*?^\}}", text,
                      re.MULTILINE | re.DOTALL)
    assert match, f"{func}() not found in {rel} -- extraction regex is stale"
    return match.group(0) + "\n"


def _run(script: str, env: dict, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], env=env, check=False,
                          capture_output=True, text=True, timeout=timeout)


class TestTheChildSeesTheCLocale:
    """Runs everywhere. No locale needed: it asks what the child was called
    under, which is the property the fix is actually about."""

    @pytest.mark.parametrize("rel,func,call_args", SPAWNING_FUNCTIONS)
    def test_the_tr_child_runs_under_lc_all_c(self, tmp_path, rel, func, call_args):
        func_file = tmp_path / "func.sh"
        func_file.write_text(_extract(rel, func), encoding="utf-8")

        # A `tr` that records the locale it was handed and then does the ASCII
        # job itself, so the function under test still gets a usable answer.
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        record = tmp_path / "tr-locale.txt"
        shim = shim_dir / "tr"
        shim.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' \"LC_ALL=${{LC_ALL-<unset>}} LANG=${{LANG-<unset>}}\" >> '{record}'\n"
            "exec /usr/bin/tr \"$@\"\n",
            encoding="utf-8")
        shim.chmod(0o755)

        env = {**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH','')}"}
        env.pop("LC_ALL", None)          # the real-host shape: language set via
        env["LANG"] = "tr_TR.UTF-8"      # LANG, with LC_ALL never exported
        result = _run(f"""
            set -u
            source '{func_file}'
            OSTYPE=msys
            _out=""
            {func} {call_args} >/dev/null 2>&1 || true
        """, env)

        assert record.is_file(), (
            f"the recording `tr` was never called by {func}() -- this control "
            f"asserts nothing until it is. bash said: {result.stderr!r}"
        )
        seen = record.read_text(encoding="utf-8").strip().splitlines()
        assert seen and all(line.startswith("LC_ALL=C ") for line in seen), (
            f"{rel}'s {func}() spawned `tr` under {seen!r}. `local LC_ALL=C` "
            f"does not export, so on a host whose language is set through LANG "
            f"alone the child keeps Turkish case rules and `i` upper-cases to "
            f"the dotted `İ` -- two bytes where one ASCII drive letter goes. "
            f"Prefix the command instead: `LC_ALL=C tr ...` (#695)"
        )


class TestTheDriveLetterOnARealTurkishHost:
    """The behavioural half. Needs glibc, so it skips on macOS and Windows --
    which is exactly why the control above exists."""

    def test_lowercase_drive_uppercases_to_ascii_with_lang_only(self, tmp_path):
        found = collation_locale()
        if found is None:
            pytest.skip(SKIP_REASON)
        name, overlay = found

        func_file = tmp_path / "func.sh"
        func_file.write_text(
            _extract("scripts/resolve-paths.sh", "_remember_normalize_win_path"),
            encoding="utf-8")

        env = {**os.environ, **overlay}
        env.pop("LC_ALL", None)
        env["LANG"] = name
        result = _run(f"""
            set -u
            source '{func_file}'
            OSTYPE=msys
            _remember_normalize_win_path 'i:/x'
            echo
        """, env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "I:\\x", (
            f"under LANG={name!r} with LC_ALL unset -- the shape a host gets "
            f"from simply setting its system language -- the drive letter came "
            f"back as {result.stdout.strip()!r}. `local LC_ALL=C` fixed the "
            f"shell's own matching but never reached the `tr` child (#695)"
        )

    def test_positive_control_the_same_call_under_the_c_locale(self, tmp_path):
        """Runs on every platform: if this one fails the harness is broken and
        the skip above is hiding it rather than reporting it."""
        func_file = tmp_path / "func.sh"
        func_file.write_text(
            _extract("scripts/resolve-paths.sh", "_remember_normalize_win_path"),
            encoding="utf-8")
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        result = _run(f"""
            set -u
            source '{func_file}'
            OSTYPE=msys
            _remember_normalize_win_path 'i:/x'
            echo
        """, env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "I:\\x"
