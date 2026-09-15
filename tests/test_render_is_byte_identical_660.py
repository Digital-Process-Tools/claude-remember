"""The memory render must stay byte-identical now that `cat` is gone (#660).

Five `cat` forks per start was the largest single fork group left on the
foreground path, and bash can read a file without forking at all. The risk in
that swap is not speed, it is bytes: the replacement anyone reaches for first,
`printf '%s\n' "$(<file)"`, is WRONG. Command substitution strips every
trailing newline and the printf puts exactly one back, so a memory file ending
in two newlines -- or in none -- renders differently from what is on disk.
Silently, in the model's injected context.

So this file characterises `_remember_emit_file` against file shapes that
differ only in their trailing bytes, and it carries a demonstration that these
assertions FAIL against the naive replacement. A refactor guarded by a test
that would pass either way is not guarded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .test_session_start_windows_benchmark_669 import BASH, REPO_ROOT

pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash on this host (#432)"
)

LIB = REPO_ROOT / "scripts" / "lib-memory-context.sh"

# Every shape whose trailing bytes a careless read would change.
SHAPES = {
    "no-trailing-newline.md": "alpha",
    "one-trailing-newline.md": "alpha\n",
    "two-trailing-newlines.md": "alpha\n\n",
    "blank-lines-inside.md": "alpha\n\n\nbeta\n",
    "trailing-spaces.md": "alpha   \n",
    "unicode.md": "éà中文 \U0001f600\n",
}

# Both branches of the size switch, exercised by name: under the threshold
# the shell reads the file itself, over it the function falls back to `cat`.
# A test that only ever drove one of them would leave the other unproven --
# and the `cat` branch exists BECAUSE an unconditional read made a 4MB file
# take 24 seconds on windows-latest.
SHIPPED_SMALL = f'source "{LIB.as_posix()}"; _remember_emit_file "$MFILE" 10'
SHIPPED_LARGE = f'source "{LIB.as_posix()}"; _remember_emit_file "$MFILE" 999999'
SHIPPED_NO_SIZE = f'source "{LIB.as_posix()}"; _remember_emit_file "$MFILE"'
SHIPPED = SHIPPED_SMALL
NAIVE = 'printf \'%s\\n\' "$(<"$MFILE")"'
CAT = 'cat "$MFILE"'


def _emit(tmp_path: Path, name: str, body: str, snippet: str) -> bytes:
    """Run one snippet over one file and return exactly what it wrote."""
    target = tmp_path / name
    # newline="" so the fixture's own trailing bytes survive on Windows --
    # universal-newline translation would rewrite the very thing under test.
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    done = subprocess.run(
        [BASH, "-c", f'MFILE="{target.as_posix()}"\n{snippet}\n'],
        capture_output=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout


def test_the_render_delegates_to_the_size_aware_emitter():
    """The point of the change, pinned so a revert is loud."""
    source = LIB.read_text(encoding="utf-8")
    body = source.split("_remember_render_memory_section()")[1]
    assert '_remember_emit_file "$MFILE" "$MFILE_BYTES"' in body, (
        "the render must hand the size over -- without it the emitter cannot "
        "choose, and falls back to cat for every file"
    )
    assert 'cat "$MFILE"' not in body, "the render forks `cat` directly again"


@pytest.mark.parametrize("name,body", sorted(SHAPES.items()))
def test_the_large_file_branch_is_byte_identical_too(tmp_path, name, body):
    """Over the threshold the emitter uses `cat`; same bytes, by definition,
    but pinned because the branch is chosen by a number that can be edited."""
    assert _emit(tmp_path, name, body, SHIPPED_LARGE) == body.encode("utf-8")


@pytest.mark.parametrize("name,body", sorted(SHAPES.items()))
def test_a_missing_size_still_renders_the_file(tmp_path, name, body):
    """A caller that forgets the second argument must not render nothing.

    The size decides the branch, so an absent or non-numeric one has to fall
    somewhere deliberate rather than into an arithmetic error that prints an
    empty section and looks like an empty memory file.
    """
    assert _emit(tmp_path, name, body, SHIPPED_NO_SIZE) == body.encode("utf-8")


@pytest.mark.parametrize("name,body", sorted(SHAPES.items()))
def test_the_render_reproduces_the_file_byte_for_byte(tmp_path, name, body):
    """Whatever the file holds is what reaches the model."""
    produced = _emit(tmp_path, name, body, SHIPPED)
    assert produced == body.encode("utf-8"), (
        f"{name}: the render changed the bytes\n"
        f"  on disk:  {body.encode('utf-8')!r}\n"
        f"  rendered: {produced!r}"
    )


@pytest.mark.parametrize("name,body", sorted(SHAPES.items()))
def test_the_render_still_matches_cat(tmp_path, name, body):
    """`cat` is the behaviour being preserved, so `cat` is the control."""
    produced = _emit(tmp_path, name, body, SHIPPED)
    catted = _emit(tmp_path, name, body, CAT)
    assert produced == catted, (
        f"{name}: the render and cat disagree\n"
        f"  cat:      {catted!r}\n"
        f"  rendered: {produced!r}"
    )


def test_these_shapes_catch_the_naive_replacement(tmp_path):
    """This file would be decoration if it passed against the obvious bug."""
    wrong = [
        name for name, body in sorted(SHAPES.items())
        if _emit(tmp_path, name, body, NAIVE) != body.encode("utf-8")
    ]
    assert wrong, (
        "the naive replacement produced correct bytes for every shape here, "
        "so these shapes cannot tell the two implementations apart"
    )
