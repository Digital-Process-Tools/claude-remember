"""#1047 round 2: the line-accumulation join in session-start-hook.sh's own
stdin read loop (`HOOK_STDIN="$HOOK_STDIN$_line"`) is a SECOND quadratic,
independent of the one `_stdin_json_string_into` already bounds -- this one
tracks the number of LINES accumulated, not the length of any one line.
Measured by the reporter: 3.3 MB over 2,500 lines, 68.8s on bash 3.2 and
23.6s on bash 5.3 just to finish the read loop, before extraction ever runs.
The existing targeted tests for #1047 (tests/test_stdin_extract_bound_1047.py)
only call `_stdin_json_string_into` directly -- pulled verbatim out of the
real hook, but never through the hook's own read loop -- so they structurally
cannot see this second quadratic at all.

Fixed by capping what is actually KEPT in HOOK_STDIN at a 32 KiB ceiling
DURING accumulation (stop appending once the cap is reached, never build the
full multi-megabyte string and truncate it afterwards), matching the cap
the before_session_start/after_session_start listener-publish block (#266)
already uses for its own byte-for-byte promise. Adopts PR #1049's own
approach (external contributor CNSeniorious000/Muspi Merol) of capping
during the read loop itself rather than only at the point of extraction;
credited via a Co-authored-by trailer on the commit that made this change.

This file drives the REAL session-start-hook.sh end to end (subprocess,
real stdin, real read loop) -- not the extracted function alone -- because
that is the one thing the existing #1047 tests cannot exercise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

from ._bash_runner import resolve_bash

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash found (see tests/_bash_runner.py)",
)

SESSION_ID = "aaaaaaaa-0000-4000-8000-0000000001047"


def _env(home: Path, project: Path, remember: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_DEFER": "0",
    }


def _store(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    return home, project, remember


def _transcript_shaped_payload(total_bytes: int, lines: int) -> str:
    """A SessionStart payload shaped like a host piping a whole transcript in
    as stdin (legal, not something the hook can refuse) -- the issue's own
    repro shape (#1047), scaled to >= total_bytes across `lines` lines, with
    the real fields (session_id, source, cwd) near the front where a
    legitimate payload always puts them (same assumption the existing #1047
    tests already rely on)."""
    prefix = json.dumps({
        "session_id": SESSION_ID,
        "transcript_path": f"/does/not/matter/{SESSION_ID}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })
    budget = max(total_bytes - len(prefix), lines)
    per_line = budget // lines
    newline = chr(10)
    filler_line = ("x" * per_line) + newline
    body = filler_line * lines
    return prefix + body


def test_a_1mb_transcript_sized_stdin_does_not_hang_the_real_hook(tmp_path):
    """Must-fire case: red before the fix (the reporter's own numbers --
    23.6s on bash 5.3 for 3.3 MB/2,500 lines, 68.8s on bash 3.2), green
    after it (accumulation stops at the 32 KiB cap, so a 1 MB payload costs
    the same as a 32 KiB one). The ceiling here is generous -- a bound on a
    hang, not a measurement of anything -- so it would fail red regardless
    of which bash this runs under."""
    home, project, remember = _store(tmp_path)
    # 2,500 lines: the reporter's own measured shape (3.3 MB/2,500 lines,
    # 68.8s on bash 3.2, 23.6s on bash 5.3) -- the quadratic cost tracks the
    # NUMBER of concatenations in the read loop, not merely the total byte
    # count, so a 1 MB payload spread thin across many lines is what
    # actually exercises it; a 1 MB payload in a handful of huge lines does
    # not (confirmed empirically: 256 lines finished in under 3s even
    # against the unfixed loop).
    payload = _transcript_shaped_payload(1024 * 1024, lines=2500)
    assert len(payload.encode("utf-8")) >= 1024 * 1024, (
        "fixture bug: payload must be >= 1 MB to exercise the issue's own shape"
    )

    start = time.monotonic()
    result = subprocess.run(
        [BASH, str(SESSION_START)],
        input=payload,
        env=_env(home, project, remember),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, (
        f"hook exited {result.returncode}, stderr={result.stderr!r}"
    )
    # 5s: tight enough that the unfixed loop measurably approaches it on
    # this machine (8.51s observed locally, bash 5.3/macOS, pre-fix,
    # confirmed red before this test was adjusted) while the fixed loop
    # finishes in a small fraction of a second regardless of payload size
    # -- not the reporter's own 20s+ numbers (a different bash/OS), but
    # still a real red/green distinction on this machine, not merely a
    # generous ceiling neither side could ever hit.
    assert elapsed < 5.0, (
        f"a 1 MB transcript-shaped stdin took {elapsed:.2f}s to finish -- "
        f"the read-loop's own quadratic accumulation is back if this scales "
        f"with payload size"
    )


def test_hook_returns_quickly_when_a_writer_holds_stdin_open_past_the_cap(tmp_path):
    """#1047 round 5 (final review): the bulk drain added after the read
    loop's own cap trips (`cat <&0 >/dev/null`) used to run SYNCHRONOUSLY,
    with no time bound of its own -- the per-line `read -t 1` loop above is
    bounded, but that drain was not, so a host that writes an over-cap
    payload and then holds its write end of stdin open (never sends EOF)
    made the hook block for as long as the writer kept stdin open, however
    long that is. Reproduced here with a real pipe: the feeder writes an
    over-cap payload, then sleeps past this test's own patience window
    before closing its write end -- a host that is merely slow to finish,
    not one that never finishes at all. Red before the fix: the hook
    returned only once the feeder closed its end, i.e. in lockstep with the
    sleep. Green after it: the drain is backgrounded, so the hook returns
    as soon as the read loop's own cap trips, long before the feeder closes
    anything."""
    home, project, remember = _store(tmp_path)
    # Over the 32 KiB cap so the read loop's own truncation -- and therefore
    # the drain this test targets -- actually fires.
    payload = _transcript_shaped_payload(40000, lines=50).encode("utf-8")

    read_fd, write_fd = os.pipe()
    _feeder_done = threading.Event()

    def _feed() -> None:
        try:
            os.write(write_fd, payload)
            # Longer than this test's own elapsed-time assertion below, so a
            # hook that (bug) waits for EOF cannot pass by accident -- it
            # would have to wait out this whole sleep first. Held 3x longer
            # than the bound so widening the bound for a slow runner cannot
            # quietly reach the hold and stop catching the bug.
            time.sleep(15)
        finally:
            os.close(write_fd)
            _feeder_done.set()

    feeder = threading.Thread(target=_feed)
    feeder.start()
    try:
        start = time.monotonic()
        result = subprocess.run(
            [BASH, str(SESSION_START)],
            stdin=read_fd,
            env=_env(home, project, remember),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        elapsed = time.monotonic() - start
    finally:
        os.close(read_fd)
        feeder.join(timeout=20)

    assert result.returncode == 0, (
        f"hook exited {result.returncode}, stderr={result.stderr!r}"
    )
    # 5s: a third of the feeder's 15s hold, and well over what the fixed,
    # backgrounded drain actually costs (bounded by the read loop's own
    # `read -t 1` cadence, not by the writer at all). Matches the other
    # must-fire test in this file (test_a_1mb_transcript_sized_stdin_...):
    # a tight ceiling nearer the writer's own hold measurably flaked on
    # windows-latest CI (3.16s observed against a 3.0s bound) from process
    # spawn overhead alone, with the fix correctly in place.
    assert elapsed < 5.0, (
        f"hook took {elapsed:.2f}s to return while a writer held stdin open "
        f"for 15s past the cap -- the post-cap drain is blocking on the "
        f"writer again instead of running detached"
    )
    assert _feeder_done.wait(timeout=10), "feeder thread never finished"


def _extract_read_loop() -> str:
    """Pulls the real read-loop block (HOOK_STDIN accumulation -- the exact
    code the must-fire test above drives through the real hook) out of
    session-start-hook.sh verbatim, between its own stable markers -- same
    extraction technique test_stdin_extract_bound_1047.py already uses for
    `_stdin_json_string_into`, applied here to a top-level block rather than
    a named function (resolve-paths.sh unconditionally overrides
    REMEMBER_HOOKS_DIR/PIPELINE_DIR from CLAUDE_PLUGIN_ROOT, which makes
    observing HOOK_STDIN through the full hooks.d publish path require
    either mutating the real shipped hooks.d or a much larger fake-plugin-
    root fixture; extracting the loop itself is the narrowest real-code
    check available for the byte-for-byte half of the claim)."""
    source = SESSION_START.read_text(encoding="utf-8")
    start_marker = "_REMEMBER_HOOK_STDIN_CAP=32768"
    # The block now contains THREE `fi` lines (the LC_ALL save-check, the
    # main read loop, and the LC_ALL restore if/else) -- ending at the
    # first one would truncate the extraction before the read loop even
    # runs. The restore's final `unset` line is the real, stable end of
    # the whole block.
    end_marker = "unset _hook_stdin_lc_all_was_set _hook_stdin_prior_lc_all"
    start = source.index(start_marker)
    end = source.index(end_marker, start) + len(end_marker)
    return source[start:end]


_LOOP_DRIVER = """
%s
printf '%%s' "$HOOK_STDIN"
"""


def test_the_cap_is_a_byte_cap_not_a_character_cap_under_utf8():
    """Self-review finding (Explore + oss:auditor pass on this diff): the
    read loop's own `${#HOOK_STDIN}`/`${HOOK_STDIN:0:N}` count and slice by
    CHARACTER under any UTF-8 locale unless LC_ALL=C is forced for that
    scope -- the exact defect class `_stdin_json_string_into`'s own
    comment already names, reintroduced here because this block is
    top-level code and cannot use that function's `local LC_ALL=C` fix.
    A payload built entirely from a 4-byte UTF-8 character (so "character
    count" and "byte count" diverge by 4x) must still be capped at
    _REMEMBER_HOOK_STDIN_CAP BYTES, not characters -- confirmed by running
    under a UTF-8 locale explicitly (not relying on whatever the test
    runner's own ambient locale happens to be, which is exactly how this
    class of bug hides)."""
    four_byte_char = "\U0001F600"  # U+1F600, encodes to 4 bytes in UTF-8
    payload = four_byte_char * 20000  # >> 32768 bytes, well under as chars
    script = _LOOP_DRIVER % _extract_read_loop()
    env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}
    # text=True with no explicit encoding uses the PLATFORM's preferred
    # encoding to decode/encode -- cp1252 on Windows, which cannot
    # represent this payload's own 4-byte UTF-8 character at all and
    # raises UnicodeEncodeError on `input=` before the subprocess even
    # starts. The payload is genuinely UTF-8 (that is the whole point of
    # this test), so the encoding must be pinned explicitly rather than
    # left to whatever codepage the host happens to default to.
    result = subprocess.run(
        [BASH, "-c", script],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out_bytes = len(result.stdout.encode("utf-8"))
    assert out_bytes <= 32768, (
        f"expected the loop's own output capped to <= 32768 BYTES, got "
        f"{out_bytes} bytes ({len(result.stdout)} characters) -- the cap "
        f"is counting characters, not bytes, under a UTF-8 locale"
    )


def test_a_small_payload_survives_the_cap_byte_for_byte():
    """Positive control, paired with the must-fire case above: an ordinary
    small payload (well under the 32 KiB cap) must come out of the
    accumulation loop byte-for-byte -- the cap must be a no-op for every
    real payload, not merely avoid hanging on an adversarial one."""
    small_payload = "just an ordinary short payload, nothing adversarial"
    script = _LOOP_DRIVER % _extract_read_loop()
    result = subprocess.run(
        [BASH, "-c", script],
        input=small_payload,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == small_payload, (
        f"expected the small payload back byte-for-byte, got {result.stdout!r}"
    )
