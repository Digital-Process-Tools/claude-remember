"""#1047: SessionStart's stdin is not bounded, and everything downstream of it is
quadratic in the payload's length.

The hook reads stdin into `HOOK_STDIN` (a line loop, deliberately kept after
#1040 -- the payload is a few hundred bytes: ids, paths, the source) and then
hands that whole string to `_stdin_json_string_into`, whose first real step is

    _sjsi_rest=${_sjsi_raw#*\\"$_sjsi_key\\"}

`${var#*pattern}` with a leading `*` is quadratic in bash -- the pilot re-runs
the pattern matcher at every offset. Measured for #1047 on bash 5.2.15, one
synthetic string of n bytes with `"cwd"` at the end: 0.28 s at 12.5 KB, 0.95 s
at 25 KB, 3.55 s at 50 KB, 15.2 s at 100 KB. `case x in *"cwd"*)` on the same
strings is 2-6 ms, so it is the `#` operator, not pattern matching as such.

Nothing in the hook picks its stdin. The reporter's box had 27 of these hooks
alive for 21 hours each, at 11% CPU, after child `claude` sessions (the harness
runs `CLAUDE_CODE_ENTRYPOINT=sdk-cli`) were handed their own transcript as fd 0:
6.8 MB, `"cwd"` at byte 3,353,224. Four extractions on that is hours, and a
`${var#...}` builtin runs without a signal check, so `SIGTERM` did nothing --
`SIGKILL` was the only way out.

What the tests are evidence of:

* DETERMINISTIC, every platform. The payload the hook publishes to a `hooks.d/`
  listener (`REMEMBER_HOOK_STDIN_FILE`) is the payload it kept, and that is
  what a 64 KB stdin must not be able to make large. Counted by execution, not
  by a clock, so a loaded box and a quiet one agree (#669's own reason for
  preferring a count).
* THE POSITIVE CONTROL, same fixture, same listener: a real SessionStart
  payload must arrive byte-for-byte as it did before, or a bound that crops
  everything would pass the first assertion too.
* A HANG CEILING. A transcript-sized (1 MB) stdin must finish. On macOS and
  Linux the surviving line loop costs about a second per MB (#1040), so the
  ceiling is huge on purpose: it is the bug's shape -- never finishing -- that
  it catches, not a duration. OBSERVED on macOS only; reasoned elsewhere, and
  stated as such rather than claimed.

Every payload here is newline-terminated, the way a host sends one, and the
sizes stay inside what bash 3.2 is observed to deliver: its `read -r -t 1`
returns EMPTY -- not a partial line -- once a payload needs more than its
second to fetch (measured here: a 16 KB single line arrives, a 64 KB one and
any 8-line 131 KB one do not), so a larger fixture would leave the hook with no
payload at all and turn the assertions into a test of that timeout. The 1 MB
case therefore skips on bash 3.x instead of passing vacuously.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash
from .subprocess_helpers import subprocess_failure_detail
from .test_path_resolution import _create_full_plugin_copy, _create_full_project

# A real POSIX bash, never bare "bash": on windows-latest that commonly resolves
# to the WSL launcher rather than Git Bash (#432).
BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash on this host (#432)"
)

SESSION = "1047abcd-0000-4000-8000-000000001047"

# What the hook keeps: one crop per side of the join, so the accumulator is
# bounded by twice the crop whatever the host sends.
CROP = 4096
BOUND = 2 * CROP

# A bound on a hang, not a measurement of anything. The slowest case measured
# here is about 1.5 s (1 MB through the line loop); the bug this catches does
# not finish at all.
CEILING = 20

LISTENER = """#!/bin/bash
# Test stub for hooks.d/before_session_start/ (tests/test_session_start_stdin_bound_1047.py).
# The hook publishes the payload it consumed as a file; the size of that file is
# what these tests assert on.
cp "$REMEMBER_HOOK_STDIN_FILE" "$REMEMBER_TEST_STDIN_COPY"
"""


def _bash_major() -> int:
    out = subprocess.run(
        [BASH, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True
    )
    return int(out.stdout.strip() or 0)


def _run(tmp_path: Path, payload: str) -> bytes:
    plugin = tmp_path / "plugin"
    project = tmp_path / "project"
    home = tmp_path / "home"
    for d in (plugin, project, home):
        d.mkdir()
    _create_full_plugin_copy(str(plugin))
    _create_full_project(str(project))

    # Named `99-record-stdin.sh`, not `50-git-restore.sh`: the hook defers a
    # LONE bundled listener (`& disown`, #660) and deletes the published file on
    # its way out, which a deferred copy would race. A second, unknown listener
    # keeps the dispatch in the foreground (#660's own condition), so the copy
    # below is on disk by the time the hook exits and nothing here has to poll.
    listener = plugin / "hooks.d" / "before_session_start" / "99-record-stdin.sh"
    listener.write_text(LISTENER, encoding="utf-8")
    listener.chmod(listener.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    copy = tmp_path / "stdin-copy"
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "REMEMBER_TEST_STDIN_COPY": str(copy),
    }
    env.pop("REMEMBER_NESTED_SUMMARIZER", None)
    result = subprocess.run(
        [BASH, str(plugin / "scripts" / "session-start-hook.sh")],
        input=payload.encode(), env=env, capture_output=True,
        timeout=CEILING, check=False,
    )
    remember = project / ".remember"
    assert result.returncode == 0, subprocess_failure_detail(result, remember)
    assert copy.is_file(), (
        "the hooks.d listener never recorded the published payload: "
        + subprocess_failure_detail(result, remember)
    )
    return copy.read_bytes()


def _payload() -> str:
    return json.dumps({
        "session_id": SESSION,
        "transcript_path": f"/does/not/matter/{SESSION}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    }) + "\n"


def test_a_64_kb_stdin_is_cropped_before_the_hook_publishes_it(tmp_path):
    """Over the crop, the payload the extractors see must stay bounded.

    Would this test still pass if the code did nothing? No: the hook used to
    keep every byte it read, so the published copy was the payload -- 16,419
    bytes here, against the 8,192 bound. Nothing else in the hook looks at its
    size.

    16 KB and not more: on bash 3.2 a payload past roughly this size is not
    delivered to the loop at all (see the module docstring), which would leave
    the hook with nothing to crop and the test green for the wrong reason.
    """
    payload = '{"session_id":"' + SESSION + '","pad":"' + "x" * 16384 + '","cwd":"/does/not/matter"}' + "\n"
    copy = _run(tmp_path, payload)

    assert len(copy) <= BOUND, (
        f"the hook published a {len(copy)}-byte payload; the bound is {BOUND}"
    )


def test_a_real_payload_arrives_byte_for_byte(tmp_path):
    """The positive control for the crop: a few-hundred-byte payload is untouched.

    Same fixture, same listener, same assertion surface -- only the size
    changes. Byte equality, not a prefix: the crop is only allowed to matter
    past its own bound.
    """
    payload = _payload()
    copy = _run(tmp_path, payload)

    assert len(payload) <= CROP, "fixture is not under the crop"
    # The hook strips line terminators, as it always has (#1040): what arrives
    # is the payload without its trailing newline, and nothing else changes.
    assert copy == payload[:-1].encode(), (
        f"a {len(payload)}-byte payload arrived as {len(copy)} bytes: {copy[:200]!r}"
    )


def test_a_transcript_sized_stdin_does_not_hang_the_hook(tmp_path):
    """The reported shape: a transcript-sized payload, the key at the end of it.

    Before the fix this does not finish in any wall clock a session survives:
    the strip to `"cwd"` alone is (1 MB / 100 KB)^2 * 15.2 s. `CEILING` is the
    assertion -- this test is about hanging, not about speed.

    64 lines of 16 KB rather than one of 1 MB: bash 3.2 cannot deliver a
    line that large (see the module docstring), so this pairs with a skip on
    that bash rather than passing vacuously. 64 lines also exercises the other
    quadratic this fix closes -- the join.
    """
    if _bash_major() < 4:
        pytest.skip("bash 3.2 delivers at most ~16 KB through `read -r -t 1`; see the module docstring")
    line = '"pad":"' + "y" * 16384 + '"'
    payload = '{"type":"queue-operation",' + ",".join([line] * 64) + ',"cwd":"/does/not/matter"}' + "\n"
    copy = _run(tmp_path, payload)

    assert len(copy) <= BOUND, (
        f"the hook published a {len(copy)}-byte payload; the bound is {BOUND}"
    )
