"""#691: `previous_transcript()` (and its no-CURRENT_SESSION_ID sibling at
scripts/session-start-hook.sh's second `PREV_JSONL=` call site) sorted every
`.jsonl` in SESSIONS_DIR with `ls -t` on every session start, only to read
one line and throw the sort of everything else away. Per the issue's own
measured table (windows-latest, the #660 diagnostic): 0.104s at 500
transcripts, 0.41s at 2000 -- the only cost in the hook that grows with how
long a project has been used.

Fixed by a single pass over the glob using bash's own `-nt` test operator (a
builtin, not a fork) to track the newest (and, for the no-id fallback,
second-newest) transcript by mtime -- no `ls`, no `sort`, no per-file `stat`
fork, regardless of how many transcripts exist.

This extracts the real functions out of scripts/session-start-hook.sh (the
same approach test_dirname_without_a_fork_660.py uses for
_remember_memory_paths) rather than running the whole hook, so the fixture
can cheaply reach a "realistic count" (the issue's own words, since the
existing benchmark fixtures never populate more than one transcript) and
exercise both call sites -- with and without CURRENT_SESSION_ID -- directly.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash
from .spawn_counting import make_shim_dir, spawns

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash on this host (#432)"
)

# Per the issue's own table this is where the cost first becomes measurable
# at all -- the existing benchmark fixtures never reach past 1.
REALISTIC_COUNT = 500


def _function_body(name: str) -> str:
    """Pull `name`'s definition verbatim out of the real shipped file, the
    same way test_dirname_without_a_fork_660.py's EXTRACT does -- so this
    test runs the actual code, not a hand-copied stand-in that can drift
    from it. Neither function this issue touches uses a nested `{ }` brace
    group, so the first `\n}\n` after the opening line is genuinely the
    function's own close."""
    source = SESSION_START.read_text(encoding="utf-8")
    start_marker = f"\n{name}() {{\n"
    start = source.index(start_marker)
    if start == -1:
        raise AssertionError(f"{name}() not found in {SESSION_START}")
    end = source.index("\n}\n", start) + len("\n}")
    return source[start + 1 : end]


def _run(script: str, args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-c", script, "bash", *args],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def _populate(dir_: Path, count: int, exclude_index: int | None = None) -> list[Path]:
    """`count` distinct-mtime .jsonl files, oldest to newest by index, so
    "the file at index i" and "the file with the i-th oldest mtime" are the
    same claim. One second apart -- some filesystems (notably HFS+/exFAT,
    and any tmpfs mounted with `relatime` truncation) round mtimes to whole
    seconds, and a sub-second spacing would make ties, not signal, the
    dominant case at REALISTIC_COUNT."""
    now = int(time.time()) - count - 10
    paths = []
    for i in range(count):
        p = dir_ / f"session-{i:05d}.jsonl"
        p.write_text("{}\n")
        os.utime(p, (now + i, now + i))
        paths.append(p)
    return paths


PREVIOUS_TRANSCRIPT_SCRIPT = r"""
%s
CURRENT_SESSION_ID="$2"
previous_transcript "$1"
"""

SECOND_NEWEST_SCRIPT = r"""
%s
_second_newest_jsonl "$1"
printf '%%s' "$_TWO_NEWEST_JSONL_SECOND"
"""


def _env_with_shims(shims: Path, log: Path) -> dict:
    return {
        **os.environ,
        "SPAWN_LOG": str(log),
        "PATH": f"{shims}{os.pathsep}{os.environ.get('PATH', '')}",
    }


# A single ASCII backslash byte, spelled this way rather than as a Python
# escape sequence inside a docstring below -- see the comment beside it.
_BACKSLASH = chr(92)


def _norm(path) -> str:
    """Normalize a path for cross-platform string comparison.

    bash's own glob always joins with a literal forward slash, regardless
    of what separator the OS uses or what characters the directory argument
    itself contains (OBSERVED, bash 3.2: a directory variable containing a
    Windows-style separator byte is passed through into the glob result
    verbatim -- bash never renormalizes it). On Windows, `tmp_path`
    fixtures are `WindowsPath`s whose `str()` uses that separator
    throughout, so the shell side's output (dir-as-passed + a literal
    forward slash + the filename) and the Python side's `str(expected_path)`
    (using that separator throughout) can both be correct paths to the same
    file and still fail a raw string `==`. Folding every occurrence of that
    one byte to a forward slash on both sides sidesteps the exact place bash
    and pathlib disagree, without needing a real Windows host to catch it
    (self-review finding, oss:auditor)."""
    return str(path).replace(_BACKSLASH, "/")


def test_previous_transcript_picks_the_newest_excluding_current_at_scale(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    files = _populate(sessions, REALISTIC_COUNT)
    current = files[-1]  # newest -- excluding it must fall back to the next one
    expected = files[-2]

    body = _function_body("previous_transcript")
    script = PREVIOUS_TRANSCRIPT_SCRIPT % body
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    env = _env_with_shims(shims, log)

    result = _run(script, [str(sessions), current.stem], env)
    assert result.returncode == 0, result.stderr
    assert _norm(result.stdout.strip()) == _norm(expected), (
        f"expected the second-newest transcript {expected}, "
        f"got {result.stdout.strip()!r}: {result.stderr}"
    )

    seen = spawns(log)
    assert not seen, (
        f"previous_transcript() at {REALISTIC_COUNT} transcripts must not "
        f"spawn ANY external process (ls/stat/sort/...) -- the whole point "
        f"of #691 is that the cost stops scaling with directory size. "
        f"Spawned: {seen}"
    )


def test_previous_transcript_excludes_current_by_id_not_position(tmp_path):
    """Positive control for the exclusion itself: with the CURRENT id set to
    something in the MIDDLE of the mtime ordering (not the newest), the
    absolute newest file must still win -- proving the loop is not silently
    just returning "the newest" regardless of the exclude argument."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    files = _populate(sessions, 20)
    middle = files[10]
    newest = files[-1]

    body = _function_body("previous_transcript")
    script = PREVIOUS_TRANSCRIPT_SCRIPT % body
    env = {**os.environ}

    result = _run(script, [str(sessions), middle.stem], env)
    assert result.returncode == 0, result.stderr
    assert _norm(result.stdout.strip()) == _norm(newest)


def test_previous_transcript_returns_nothing_when_only_file_is_current(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    files = _populate(sessions, 1)

    body = _function_body("previous_transcript")
    script = PREVIOUS_TRANSCRIPT_SCRIPT % body
    env = {**os.environ}

    result = _run(script, [str(sessions), files[0].stem], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_second_newest_fallback_matches_ls_t_tail_head_semantics_at_scale(tmp_path):
    """The no-CURRENT_SESSION_ID sibling (session-start-hook.sh's second
    `PREV_JSONL=` call site) used `ls -t ... | tail -n +2 | head -1` --
    "skip the newest, take the next one", positionally, with no id
    filtering at all. Pinning that at REALISTIC_COUNT is what the issue's
    "sibling instance" note is about: a fix limited to previous_transcript()
    alone leaves this exact scaling cost in place one function away."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    files = _populate(sessions, REALISTIC_COUNT)
    expected = files[-2]  # second-newest by mtime

    body = _function_body("_second_newest_jsonl")
    script = SECOND_NEWEST_SCRIPT % body
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    env = _env_with_shims(shims, log)

    result = _run(script, [str(sessions)], env)
    assert result.returncode == 0, result.stderr
    assert _norm(result.stdout.strip()) == _norm(expected), (
        f"expected the second-newest transcript {expected}, "
        f"got {result.stdout.strip()!r}: {result.stderr}"
    )

    seen = spawns(log)
    assert not seen, (
        f"_second_newest_jsonl() at {REALISTIC_COUNT} transcripts must not "
        f"spawn ANY external process. Spawned: {seen}"
    )


def test_second_newest_fallback_empty_with_fewer_than_two_transcripts(tmp_path):
    """Negative control, paired with the must-fire case above: fewer than
    two transcripts means there IS no second-newest, and this must say so
    (empty) rather than returning the only file that exists -- which would
    silently misname the current session's own transcript as "previous"."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _populate(sessions, 1)

    body = _function_body("_second_newest_jsonl")
    script = SECOND_NEWEST_SCRIPT % body
    env = {**os.environ}

    result = _run(script, [str(sessions)], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
