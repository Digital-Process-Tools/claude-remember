"""The fork-free REMEMBER_ROOT must answer exactly what `dirname` answered.

`_remember_memory_paths` ran `REMEMBER_ROOT=$(dirname "$REMEMBER_DIR")` on
every session start -- one fork, on the foreground path, to do string
manipulation bash can do for nothing. Replacing it is only safe if the
replacement is `dirname` for every path shape a store can have, and
REMEMBER_ROOT decides where the user-global identity.md is looked for: get it
wrong and the identity tier silently stops resolving.

So this compares the two implementations directly, on the same host, rather
than asserting what one of them "should" return. `dirname` itself is the
oracle -- including for the cases nobody remembers (a trailing slash, a bare
name, the root).
"""

from __future__ import annotations

import subprocess

import pytest

from .test_session_start_windows_benchmark_669 import BASH, REPO_ROOT

pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash on this host (#432)"
)

LIB = (REPO_ROOT / "scripts" / "lib-memory-context.sh").as_posix()

PATHS = [
    "/a/b/.remember",
    "/a/b/.remember/",
    "/a/b/.remember///",
    "/a/.remember",
    "/a",
    "/",
    "relative/.remember",
    "bare",
    "/a/b c/.remember",
    "/a/b'c/.remember",
    "/a/b-with-dash/.remember",
    "/étage/.remember",
]

# The replacement, lifted out of the shipped function so the test runs the
# real code rather than a copy of it.
EXTRACT = r'''
_remember_root_of() {
    REMEMBER_DIR="$1"
    _body=$(sed -n '/Parameter expansion, not a `dirname` fork/,/unset _remember_root_scratch/p' "@LIB@")
    eval "$_body"
    printf '%s' "$REMEMBER_ROOT"
}
_remember_root_of "$1"
'''.replace('@LIB@', LIB)


def _ours(path: str) -> str:
    done = subprocess.run(
        [BASH, "-c", EXTRACT, "bash", path],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _dirname(path: str) -> str:
    done = subprocess.run(
        [BASH, "-c", 'printf "%s" "$(dirname "$1")"', "bash", path],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.parametrize("path", PATHS)
def test_the_fork_free_root_matches_dirname(path):
    assert _ours(path) == _dirname(path), (
        f"{path!r}: fork-free={_ours(path)!r} dirname={_dirname(path)!r}"
    )


def test_the_render_path_no_longer_forks_dirname():
    """Pinned, so a revert to `$(dirname ...)` here is loud."""
    source = (REPO_ROOT / "scripts" / "lib-memory-context.sh").read_text(
        encoding="utf-8")
    body = source.split("_remember_memory_paths()")[1].split("\n}")[0]
    assert "$(dirname" not in body, "REMEMBER_ROOT forks dirname again"
