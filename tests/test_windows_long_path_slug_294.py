"""The drive-letter fold must survive `cygpath -w`'s long-path prefix (#294).

`session_dir_slug` converts to the native Win32 form with `cygpath -w` before
slugging, because Claude Code names `~/.claude/projects/<slug>/` from the
Windows path and a hook is handed the MSYS one. Past MAX_PATH (260 chars),
`cygpath -w` returns that path with a `\\\\?\\` prefix — the Win32 long-path
escape. The fold that follows is `case "$path" in ?:*)`, which `\\\\?\\C:\\…`
cannot match, so on exactly the paths the 200-char truncation already exists to
protect:

* the drive letter stops folding — #263's condition, a slug that cannot match
  its own tracked spelling, reintroduced for a subset of paths after #267 fixed
  it for the rest; and
* the slug gains four leading dashes from a prefix Claude Code never sees, so
  it names a directory that does not exist — the #157/#166 silent miss.

**The prefix is ours, not Claude Code's.** Claude Code slugs whatever `cwd`
it holds; nothing in this plugin's chain hands it a `\\\\?\\` form. So the fix
strips only what `cygpath` itself added, inside the `cygpath` branch, and a
path that merely *looks* like a long-path prefix without cygpath in the picture
is left exactly as it arrived — pinned below, because that is the difference
between a fix and a store rename.

The reporter's evidence is that real `cygpath -w` emits the prefix past
MAX_PATH; that could not be executed here (no Windows, no cygpath) and the stub
below models it rather than proving it. The fix does not depend on the claim:
if the prefix never appears the strip never fires, and the invariant test says
so directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.slug import session_dir_slug as py_slug  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess assertions — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SLUG_SH = REPO_ROOT / "scripts" / "lib-slug.sh"

# MAX_PATH is 260 *including* the terminating NUL, so 260 characters is already
# over. The exact threshold does not matter here — only that a long path gets
# the prefix and a short one does not.
MAX_PATH = 260

# A stand-in for MSYS/Cygwin `cygpath`, now shared: `tests/cygpath_stub.py`.
# It was a literal here and a narrower literal in
# tests/test_windows_drive_slug_263.py, and the conformance vectors (#294)
# needed a third — which is the same drift those vectors exist to delete. The
# behaviour this file depends on is unchanged and documented at the definition:
# past MAX_PATH the `-w` result carries the long-path prefix, and
# CYGPATH_STUB_EMPTY=1 makes it exit 0 printing nothing.
from .cygpath_stub import CYGPATH_STUB  # noqa: E402


def _bash_slug(path: str, *, tmp_path: Path, cygpath: bool = True, empty: bool = False) -> str:
    """`session_dir_slug` as a hook runs it, with the stub on PATH or not."""
    env = dict(os.environ)
    env["PIPELINE_DIR"] = str(REPO_ROOT)
    if cygpath:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "cygpath"
        stub.write_text(CYGPATH_STUB, encoding="utf-8")
        stub.chmod(0o755)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        if empty:
            env["CYGPATH_STUB_EMPTY"] = "1"
    elif shutil.which("cygpath") is not None:
        pytest.skip("a real cygpath is on PATH; the no-cygpath half needs it absent")

    script = f'source "{LIB_SLUG_SH}"\nsession_dir_slug "$1"\n'
    result = subprocess.run(
        ["bash", "-c", script, "bash", path],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


# The reporter's shape: a project deep enough that the native Win32 form crosses
# MAX_PATH. 3 + 1 + 130 + 1 + 130 = 265 characters.
LONG_TAIL = "a" * 130 + "\\" + "b" * 130
WIN_LONG = "C:\\dev\\" + LONG_TAIL
MSYS_LONG = "/c/dev/" + LONG_TAIL.replace("\\", "/")

UNC_LONG = "\\\\server\\share\\" + LONG_TAIL
MSYS_UNC_LONG = "//server/share/" + LONG_TAIL.replace("\\", "/")


def test_the_fixture_is_actually_a_long_path():
    """If this stops crossing MAX_PATH the rest of the file tests nothing."""
    assert len(WIN_LONG) >= MAX_PATH
    assert len(UNC_LONG) >= MAX_PATH


@pytest.mark.parametrize("shape", [MSYS_LONG, WIN_LONG], ids=["msys", "win32"])
def test_a_long_path_slugs_to_the_directory_claude_code_actually_created(shape, tmp_path):
    """The whole defect, stated against the only oracle that matters.

    Claude Code slugs the ordinary Win32 path — it has no `\\\\?\\` prefix to
    slug. So whatever shape the hook is handed, the slug must equal the slug of
    that ordinary path. Anything else reads an empty directory and saves
    nothing, in silence.
    """
    assert _bash_slug(shape, tmp_path=tmp_path) == py_slug(WIN_LONG)


def test_a_long_path_still_folds_the_drive_letter(tmp_path):
    """#263's condition, which this reintroduced past MAX_PATH.

    Stated separately from the oracle above because the two halves fail
    independently: a fix that strips the prefix but folds nothing passes
    neither, and one that folds but leaves the prefix would still miss.
    """
    slug = _bash_slug(MSYS_LONG, tmp_path=tmp_path)
    assert slug.startswith("c-"), slug[:12]
    assert not slug.startswith("-"), slug[:12]


def test_a_long_unc_path_slugs_like_the_short_unc_path_it_is(tmp_path):
    """`\\\\?\\UNC\\server\\share` IS `\\\\server\\share`, spelled for Win32.

    One directory, one slug — the same argument as #263. There is no drive
    letter to fold here, so the leading `\\\\` must survive as the two dashes
    Claude Code writes, rather than being eaten with the prefix.
    """
    slug = _bash_slug(MSYS_UNC_LONG, tmp_path=tmp_path)
    assert slug == py_slug(UNC_LONG)
    assert slug.startswith("--server-share-"), slug[:24]


# ── The invariant: nothing that is not long-path-prefixed may move ────────────

# Every shape #263 pinned, plus POSIX. These slugs name stores that exist on
# disk today; a change to any of them is a rename, not a fix.
UNCHANGED = [
    ("/c/Users/ricky/dev/jqit/project-b", "c--Users-ricky-dev-jqit-project-b"),
    ("c:/Users/ricky/dev/jqit/project-b", "c--Users-ricky-dev-jqit-project-b"),
    ("C:/Users/ricky/dev/jqit/project-b", "c--Users-ricky-dev-jqit-project-b"),
    ("C:\\Users\\ricky\\dev\\jqit\\project-b", "c--Users-ricky-dev-jqit-project-b"),
    ("//server/share/project", "--server-share-project"),
    ("/Users/f/Documents/dvsi", "-Users-f-Documents-dvsi"),
]


@pytest.mark.parametrize("path,expected", UNCHANGED, ids=[p for p, _ in UNCHANGED])
def test_no_short_path_slug_moves(path, expected, tmp_path):
    """The store-rename guard.

    `session_dir_slug` runs on every single tool call and names everyone's
    memory store. #263's fix had to choose lowercase over uppercase precisely
    because the other direction would have renamed every working install. The
    same constraint applies here: a path with no long-path prefix must slug
    exactly as it did before.
    """
    assert _bash_slug(path, tmp_path=tmp_path) == expected


def test_a_path_that_merely_looks_prefixed_is_untouched_without_cygpath(tmp_path):
    """The strip is confined to what cygpath produced, and this says why.

    `\\` is an ordinary filename byte on Linux, so a directory literally named
    `\\\\?\\C:` can exist there — and Claude Code would slug it literally,
    prefix and all. Stripping unconditionally would compute a name for that
    project that Claude Code never created. Absurd path, real invariant: it is
    what keeps this a Windows-only change.
    """
    weird = "/tmp/\\\\?\\C:/x"
    assert _bash_slug(weird, tmp_path=tmp_path, cygpath=False) == py_slug(weird)


def test_an_empty_cygpath_result_does_not_empty_the_slug(tmp_path):
    """Found while reading the line, not reported (#294).

    `winpath=$(cygpath -w "$path") || winpath="$path"` guards a non-zero exit
    and nothing else. A `cygpath` that exits 0 and prints nothing replaced the
    path with the empty string, and an empty slug is not a miss — it resolves
    to `~/.claude/projects/` **itself**, a directory that exists and holds
    every project's transcripts. So the missing-session-directory warning never
    fires and the pipeline reads someone else's session. `claude_projects_dir`,
    a hundred lines above in the same file, already has exactly this guard
    (`&& [ -n "$_converted" ]`); this function did not.

    The fallback is the untouched input, which on Git Bash still slugs to a
    directory Claude Code did not create — reconstructing the Win32 form
    without cygpath is `resolve-paths.sh`'s job, not this one's. That miss is
    loud (no such directory, warning fires); the empty slug was silent and
    pointed somewhere real. This pins the difference, not a cure.
    """
    slug = _bash_slug("/c/dev/project", tmp_path=tmp_path, empty=True)
    assert slug, "an empty slug resolves to the projects root, not to nothing"
    assert slug == py_slug("/c/dev/project")
