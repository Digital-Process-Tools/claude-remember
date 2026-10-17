r"""#1036: the transcript scan in post-tool-hook.sh must not hold the transcript
in a shell variable.

`_pt_scan_lines` used to read everything after its resume offset with
`_chunk=$(tail -c +OFFSET file; printf X)`, then cut the trailing partial line
off with two `##`/`%` parameter expansions and counted newlines with a `wc -l`
herestring of the result. At offset 0 -- no sidecar yet: the first call of
every new session, and of every existing session after an upgrade -- that is
the WHOLE transcript inside bash. @KRR-66 measured it on the Windows (MSYS2)
bash 5.2.37 with real transcripts (long lines): 5 MB 1.5 s, 20 MB 13.5 s,
50 MB 92 s. A 247 MB and a 1.33 GB transcript hit the host's 600 s
PostToolUse timeout; the hook was killed before it wrote the sidecar, so EVERY
later call started at offset 0 again and stalled 600 s, forever.

What the cases below are evidence of:

* The timing case runs the real hook cold on a 27 MB transcript made of
  multi-MB lines, and compares it with the same hook on a 10-line transcript
  run the same way, so the hook's own fixed cost (and a slow Windows runner)
  cancels out. The bound is far above a linear scan and below what the
  in-variable scan cost on every shell it was measured on.
* The correctness cases pin the sidecar's (OFFSET, LINES) pair against Python's
  own count of the same bytes: a file ending in a newline, one ending in a
  partial line, a single multi-MB line, a multi-MB partial line (longer than
  any bounded tail the scan may read), a file with no newline at all, and a
  warm resume after an append. The resume case is paired with a
  "must take the warm path" control (a deliberately inflated LINES in the
  sidecar must survive), and the invalidation cases with their "must recount"
  twin, so neither can pass because one path silently stands in for the other.

All of it runs on Windows too, through resolve_bash(), with the harness of
test_post_tool_backslash_transcript_basename_1034.py.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from .test_post_tool_backslash_transcript_basename_1034 import (
    BASH,
    LINE,
    SESSION_ID,
    _fixture,
    _fwd,
    _run,
)

pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash found (see tests/_bash_runner.py)",
)

NL = b"\n"
MB = 1024 * 1024
ROW = LINE.encode("utf-8")


def _sidecar(remember: Path) -> Path:
    return remember / "tmp" / ("transcript-scan." + SESSION_ID)


def _scan(remember: Path):
    offset, lines = _sidecar(remember).read_text(encoding="utf-8").split()
    return int(offset), int(lines)


def _truth(data: bytes):
    """Python's own answer: OFFSET just past the last newline, LINES = the
    number of newlines. A trailing partial line counts toward neither."""
    last = data.rfind(NL)
    return (last + 1 if last >= 0 else 0), data.count(NL)


def _long_line(size: int, tag: str = "a") -> bytes:
    """One JSONL record of about `size` bytes, newline-terminated -- the shape
    of a big tool result in a real transcript."""
    head = ('{"type":"user","message":{"content":"' + tag).encode("utf-8")
    tail = b'"}}\n'
    return head + b"x" * max(0, size - len(head) - len(tail)) + tail


def _call(tmp_path, home, project, transcript):
    return _run(tmp_path, home, project, _fwd(transcript))


def _cold(tmp_path, data: bytes):
    home, project, remember, transcript = _fixture(tmp_path)
    transcript.write_bytes(data)
    _call(tmp_path, home, project, transcript)
    return home, project, remember, transcript


# --- correctness against Python ground truth ---------------------------------

@pytest.mark.parametrize("data", [
    pytest.param(ROW * 7, id="trailing-newline"),
    pytest.param(ROW * 5 + b'{"type":"assistant","mess', id="partial-trailing-line"),
    pytest.param(b'{"type":"assistant"' + b"y" * 5000, id="no-newline-at-all"),
    pytest.param(_long_line(3 * MB), id="huge-single-line"),
    pytest.param(ROW * 2 + _long_line(3 * MB)[:-1], id="huge-partial-line"),
    pytest.param(ROW * 3 + _long_line(2 * MB) + ROW * 4, id="lines-around-a-huge-one"),
])
def test_cold_scan_matches_python_ground_truth(tmp_path, data):
    _home, _project, remember, _transcript = _cold(tmp_path, data)
    assert _scan(remember) == _truth(data)


def test_warm_resume_after_append_matches_ground_truth(tmp_path):
    first = ROW * 4 + b'{"type":"assistant","mess'
    home, project, remember, transcript = _cold(tmp_path, first)
    assert _scan(remember) == _truth(first)
    # Complete the partial line, add a multi-MB record and a fresh partial.
    second = first + b'age":{}}\n' + _long_line(2 * MB) + ROW * 3 + b'{"par'
    transcript.write_bytes(second)
    _call(tmp_path, home, project, transcript)
    assert _scan(remember) == _truth(second)
    # And once more, ending on a newline this time.
    third = second + b'tial":1}\n' + ROW
    transcript.write_bytes(third)
    _call(tmp_path, home, project, transcript)
    assert _scan(remember) == _truth(third)
    # And a call with nothing new at all keeps the pair as it is.
    _call(tmp_path, home, project, transcript)
    assert _scan(remember) == _truth(third)


def test_warm_resume_really_resumes_from_the_sidecar(tmp_path):
    """MUST TAKE THE WARM PATH: a valid OFFSET with a LINES deliberately 1000
    too high must be carried forward, not recounted -- proof the warm-resume
    case above exercises the resume, not a full rescan that happens to agree."""
    data = ROW * 6
    home, project, remember, transcript = _cold(tmp_path, data)
    offset, lines = _scan(remember)
    _sidecar(remember).write_text(f"{offset} {lines + 1000}\n", encoding="utf-8")
    more = data + ROW * 2 + _long_line(MB) + b'{"par'
    transcript.write_bytes(more)
    _call(tmp_path, home, project, transcript)
    want_offset, want_lines = _truth(more)
    assert _scan(remember) == (want_offset, want_lines + 1000)


def test_a_sidecar_that_no_longer_points_at_a_newline_forces_a_recount(tmp_path):
    """MUST RECOUNT, the twin of the case above: the byte at the stored OFFSET
    is checked to be the newline it claims to follow. A rewritten file fails
    that check, and the inflated LINES must be discarded by a full recount."""
    data = ROW * 6
    home, project, remember, transcript = _cold(tmp_path, data)
    offset, lines = _scan(remember)
    _sidecar(remember).write_text(f"{offset} {lines + 1000}\n", encoding="utf-8")
    rewritten = b"z" * offset + ROW * 3
    transcript.write_bytes(rewritten)
    _call(tmp_path, home, project, transcript)
    assert _scan(remember) == _truth(rewritten)


def test_a_sidecar_past_the_end_of_a_shrunk_file_forces_a_recount(tmp_path):
    data = ROW * 6
    home, project, remember, transcript = _cold(tmp_path, data)
    offset, lines = _scan(remember)
    _sidecar(remember).write_text(f"{offset} {lines + 1000}\n", encoding="utf-8")
    shrunk = ROW * 2
    transcript.write_bytes(shrunk)
    _call(tmp_path, home, project, transcript)
    assert _scan(remember) == _truth(shrunk)


# --- cost: the cold scan must not hold the transcript in bash ----------------

def _timed_cold(tmp_path, data: bytes) -> float:
    tmp_path.mkdir(parents=True)
    home, project, remember, transcript = _fixture(tmp_path)
    transcript.write_bytes(data)
    start = time.monotonic()
    _call(tmp_path, home, project, transcript)
    elapsed = time.monotonic() - start
    assert _scan(remember) == _truth(data)
    return elapsed


BIG_LINES = 6
BIG_LINE_MB = 4
# Seconds over the 10-line baseline. Measured on macOS arm64 (this file, cold,
# 24 MB of 4 MB lines ending on a newline): the in-variable scan cost 1.48 s
# extra on bash 5.3.15 and 1.71 s on /bin/bash 3.2.57; this fix 0.02-0.04 s on
# both. With a 3 MB partial record at the end, the old code did not finish in
# 120 s (the `%` strip of the partial line); this fix 0.04 s. Windows gets more
# room for its noisier process start-up -- the reporter measured the old code
# at 13.5 s for 20 MB there, so 5 s still separates the two.
BOUND_S = 5.0 if sys.platform == "win32" else 1.0


@pytest.mark.parametrize("partial_mb", [
    pytest.param(0, id="ends-on-a-newline"),
    pytest.param(3, id="ends-mid-record"),
])
def test_cold_scan_of_a_large_long_line_transcript_is_bounded(tmp_path, partial_mb):
    """THE #1036 CASE: 6 lines of 4 MB (24 MB), optionally followed by a 3 MB
    record still being written, scanned cold, i.e. with no sidecar. The hook's own fixed cost is
    measured on a 10-line transcript in the same test and subtracted, so a
    slow runner does not move the verdict.

    A linear scan of 27 MB with C tools costs well under a second on any
    runner. The in-variable scan this replaces cost more than this bound on
    every shell it was measured on (numbers in the PR), and the reporter's
    Windows numbers (20 MB 13.5 s, 50 MB 92 s) put it far past it there.
    """
    baseline = min(_timed_cold(tmp_path / f"small{i}", ROW * 10) for i in range(2))
    big = b"".join(_long_line(BIG_LINE_MB * MB, tag=str(i)) for i in range(BIG_LINES))
    if partial_mb:
        big += _long_line(partial_mb * MB)[:-1]
    elapsed = _timed_cold(tmp_path / "big", big)
    extra = elapsed - baseline
    print(f"#1036 cold scan: {len(big)} bytes, {elapsed:.3f}s, baseline {baseline:.3f}s")
    assert extra < BOUND_S, (
        f"cold scan of a {len(big) // MB} MB transcript of {BIG_LINE_MB} MB lines "
        f"cost {extra:.2f}s over the hook's own {baseline:.2f}s baseline -- the "
        "scan is holding the transcript in a shell variable again (#1036)"
    )
