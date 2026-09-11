"""detect-tools.sh's FATAL must say what it saw, not only what it concluded (#650).

A Windows reporter logged 1,650 consecutive `post-tool-hook.sh` failures over a
month, every one of them:

    FATAL: No working Python found. Tried: python3, python, py -3, py.

while `python -V` and `py -3 -V` both worked in the Git Bash the hooks are
launched from, and a second, independently written probe in another plugin
failed identically in the same window. Then it self-resolved. Nobody can say
why, because the message names the candidates and nothing else: not the PATH
the probe searched, not which candidates `command -v` found, not what the ones
it found exited with. The reporter's own request was exactly that -- with those
in the log, "a report like this one would be answerable from the log alone
rather than needing a reconstruction after the fact".

So: the FATAL line stays (it is what people grep for), and it is followed by the
PATH that was searched and one line per candidate saying `not on PATH` or the
exit status of its `-V` probe. The fixture puts one candidate on PATH that
exits 49 -- the Microsoft Store stub's own status, the case the reporter hit for
`python3` -- and nothing else, so the per-candidate lines have something
specific to say. The positive control sources the same file with a working
interpreter on PATH and asserts it says nothing at all: a probe that narrates
on success would put four lines on every hook's stderr, which the hot path
redirects into hook-errors.log.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT = REPO_ROOT / "scripts" / "detect-tools.sh"

BASH = resolve_bash()
_needs_bash = pytest.mark.skipif(BASH is None, reason="no POSIX bash available")


def _source(env):
    return subprocess.run(
        [BASH, "-c", f'source "{DETECT.as_posix()}"; echo "PYTHON=$PYTHON"'],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@_needs_bash
def test_fatal_reports_the_path_and_each_candidates_probe_result(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # A `python3` that is on PATH but does not run: exit 49, the Microsoft
    # Store placeholder's status, which the reporter observed verbatim.
    stub = bindir / "python3"
    stub.write_text("#!/bin/sh\nexit 49\n", encoding="utf-8")
    stub.chmod(0o755)
    # `command -v` and the stub's own `#!/bin/sh` need sh; nothing else.
    sh_dir = str(Path(subprocess.check_output([BASH, "-c", "command -v sh"], text=True).strip()).parent)
    env = {"PATH": f"{bindir}{os.pathsep}{sh_dir}", "HOME": str(tmp_path)}

    result = _source(env)

    assert result.returncode != 0, "no usable python on PATH must still be fatal"
    err = result.stderr
    assert "FATAL: No working Python found" in err, err
    # Git Bash reports PATH in msys spelling (`/c/Users/...`), so a verbatim
    # `str(bindir)` (drive letter, backslashes) never matches there -- the
    # first CI run of this file failed on windows-latest for exactly that,
    # with the PATH line present and correct. Match the unique directory
    # names instead, on a separator-normalised copy.
    _err_fwd = err.replace(chr(92), "/")
    assert f"{tmp_path.name}/bin" in _err_fwd, (
        "the FATAL does not show the PATH it searched, so a reader cannot tell "
        "whether the interpreter was missing from PATH or present and broken:\n"
        + err
    )
    # The candidate's OWN line must carry the status -- not a legend
    # elsewhere in the message that happens to mention 49. The first draft
    # of this file matched the legend and passed against a line that said
    # `exit 0`.
    python3_lines = [line for line in err.splitlines() if line.strip().startswith("python3:")]
    assert python3_lines and "exit 49" in python3_lines[0], (
        "the FATAL does not say what the candidate it FOUND on PATH exited "
        "with -- exit 49 is the Store-stub signature and the one fact that "
        "separates 'not installed' from 'installed and shadowed':\n" + err
    )
    for absent in ("python:", "py:"):
        assert absent in err, (
            f"no per-candidate line for {absent!r} -- every candidate should "
            "say whether it was on PATH at all:\n" + err
        )


@_needs_bash
def test_a_working_interpreter_produces_no_diagnostics(tmp_path):
    """Positive control: the extra lines are for the FATAL path only."""
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _source(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("PYTHON=") and result.stdout.strip() != "PYTHON=", result.stdout
    assert result.stderr == "", (
        "detect-tools.sh wrote to stderr on a successful detection; on the "
        "post-tool hot path that lands in hook-errors.log on every tool "
        "call:\n" + result.stderr
    )
