"""#504 -- user-prompt-hook.sh's fast path keys the env cache off the RAW
CLAUDE_PROJECT_DIR (read before resolve-paths.sh has normalized it),
session-start-hook.sh's publish keys off the NORMALIZED CLAUDE_PROJECT_DIR
(resolve-paths.sh has already run). On a platform where normalization
actually rewrites the string -- Windows Git Bash, forward-slash drive form
-> backslash drive form -- the two hooks derive different cache keys for
the same project, so the fast path never hits what the slow path wrote.

Forces OSTYPE=msys so the real normalizing branch runs regardless of host
(same technique as tests/test_windows_native_hook_cwd_448.py and
tests/test_env_cache_hook_cwd_key_469.py).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_ENV_CACHE = REPO_ROOT / "scripts" / "lib-env-cache.sh"

# bash on PATH commonly resolves to the WSL launcher on windows-latest
# runners rather than Git Bash -- resolve_bash() (tests/_bash_runner.py,
# #432) probes for the real Git-for-Windows bash instead of trusting the
# literal string "bash", the same convention every other subprocess-bash
# test in this suite already follows. A bare "bash" hits the WSL stub,
# which -- with no distro installed -- prints a UTF-16 nag to stdout/stderr
# and exits nonzero, distinct from anything this file is testing.
BASH = resolve_bash()
_needs_bash = pytest.mark.skipif(BASH is None, reason="no usable bash found")

_SCRIPT = r"""
source "{lib}"
export CLAUDE_PROJECT_DIR="{project_dir}"
_remember_env_cache_path
echo "KEY=$_REMEMBER_ENV_CACHE_KEY"
echo "FILE=$_REMEMBER_ENV_CACHE_FILE"
"""


def _run(project_dir: str, tmp_home: Path):
    script = _SCRIPT.format(lib=LIB_ENV_CACHE, project_dir=project_dir)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env.update({"HOME": str(tmp_home), "TMPDIR": str(tmp_home / "tmp"), "OSTYPE": "msys"})
    result = subprocess.run(
        [BASH, "-c", script], env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    out = {}
    for line in result.stdout.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


@_needs_bash
def test_raw_and_normalized_windows_forms_key_the_same_cache_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "tmp").mkdir()

    # The RAW form: what CLAUDE_PROJECT_DIR looks like as the host sets it,
    # BEFORE resolve-paths.sh's Windows normalization has run -- this is what
    # user-prompt-hook.sh's fast-path _remember_env_cache_load sees.
    raw = _run("/c/Users/dev/project", home)
    # The NORMALIZED form: what resolve-paths.sh re-exports CLAUDE_PROJECT_DIR
    # as, after _remember_normalize_win_path has rewritten it -- this is what
    # session-start-hook.sh's _remember_env_cache_publish sees.
    normalized = _run("C:\\Users\\dev\\project", home)

    assert raw["FILE"] == normalized["FILE"], (
        "the raw and normalized spellings of the SAME project must key the "
        "same cache file, or the fast path can never hit what the slow path "
        "published: raw=" + repr(raw) + " normalized=" + repr(normalized)
    )


@_needs_bash
def test_a_genuinely_different_project_still_keys_differently(tmp_path):
    """Positive control: this must not degenerate into every project hashing
    to the same file."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "tmp").mkdir()

    a = _run("/c/Users/dev/project-a", home)
    b = _run("/c/Users/dev/project-b", home)
    assert a["FILE"] != b["FILE"], repr(a) + " " + repr(b)


@_needs_bash
def test_non_windows_ostype_is_unaffected(tmp_path):
    """Off the msys/cygwin branch (an ordinary macOS/Linux OSTYPE), the key
    must still be the raw string verbatim -- no normalization should ever
    fire there, and forward-slash paths are not Windows drive paths."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "tmp").mkdir()
    script = _SCRIPT.format(lib=LIB_ENV_CACHE, project_dir="/home/dev/project")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env.update({"HOME": str(home), "TMPDIR": str(home / "tmp"), "OSTYPE": "darwin23"})
    result = subprocess.run([BASH, "-c", script], env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert "KEY=/home/dev/project" in result.stdout, result.stdout
