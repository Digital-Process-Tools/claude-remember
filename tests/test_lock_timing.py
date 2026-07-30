"""The `save.lock` hold-duration recorder (#226).

#226 asks whether the NDC commit's 30s `save.lock` timeout is doing what its
reasoning claims. The issue itself says what would settle it: instrument
`lock_acquire` / `lock_release` and record the held-duration distribution across
a normal working day. #234 answered the same question for the *staging* lock
with real numbers (~30ms per append, ~200ms for a 3-file retire loop) and set
that timeout to 10s from them. `save.lock` is still unmeasured, and it is the
one that matters: `save-session.sh` holds it across its own summarize Haiku
call, so anything queued behind it waits behind a model call.

This file pins the instrument, not a number. A number cannot be produced here —
a synthetic run would yield a figure that looks measured and is not, which is
the exact defect #226 complains about, reproduced one layer up.

Four things are asserted, and each one fails if the recorder does nothing:

* a real hold of a known duration is recorded with a *plausible* duration, and
  two holds of different lengths are distinguishable — a recorder that writes a
  constant, or writes the wrong unit, goes red here;
* a lost wait is recorded as a `timeout` carrying the wait it actually spent —
  the event #226 exists to ask about is the one that must not be invisible;
* with the recorder off, no file is created **and** strictly fewer processes are
  spawned than with it on. `save-session.sh` runs on a `PostToolUse` hook, where
  #227/#230 measured the spawn prefix and #204 reported the plugin blocking a
  user's prompts. An instrument that makes that path slower is self-defeating,
  so "off costs nothing" is a pinned property rather than a claim;
* the cap discloses. An unbounded log in the memory directory is a file that
  grows forever on someone's machine, and a silently rolled one loses exactly
  the tail of the distribution a timeout is set from. So recording stops, says
  so in the file, and keeps every record it already had.

Resolution is disclosed by the file itself, because it is not the same
everywhere: `EPOCHREALTIME` (bash >= 5) is spawn-free microseconds, GNU
`date +%s%N` is milliseconds for one spawn, and everything else — notably
macOS's `/bin/bash` 3.2 with BSD `date` — is whole seconds. The assertions below
are written to hold at the coarsest of the three, which is why the long hold is
seconds rather than milliseconds.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.spawn_counting import make_shim_dir, spawns as _spawn_lines

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_LOCK = REPO_ROOT / "scripts" / "lib-lock.sh"
REPORT = REPO_ROOT / "scripts" / "lock-timing-report.sh"

# Long enough to be unambiguous at ONE-SECOND resolution: a 2.5s hold records as
# 2000 or 3000 there, and an immediate release as 0 or 1000, so the difference is
# >= 1000 whatever the boundary alignment. At ms/us resolution the same numbers
# are simply tighter.
LONG_HOLD = "2.5"

HARNESS = """#!/usr/bin/env bash
set -u
source "$LIB_LOCK"
lock_acquire "$LOCK_DIR" 0 || exit 1
lock_release "$LOCK_DIR"
lock_acquire "$LOCK_DIR" 0 || exit 1
sleep "$LONG_HOLD"
lock_release "$LOCK_DIR"
"""

# N short lock uses back to back — the cap test's traffic generator.
REPEAT_HARNESS = """#!/usr/bin/env bash
set -u
source "$LIB_LOCK"
_i=0
while [ "$_i" -lt "$REPEATS" ]; do
    lock_acquire "$LOCK_DIR" 0 || exit 1
    lock_release "$LOCK_DIR"
    _i=$((_i + 1))
done
"""

HOLDER = """#!/usr/bin/env bash
set -u
source "$LIB_LOCK"
lock_acquire "$LOCK_DIR" 0 || exit 1
: > "$HELD_MARKER"
_i=0
while [ ! -e "$RELEASE_MARKER" ] && [ "$_i" -lt 400 ]; do
    sleep 0.05
    _i=$((_i + 1))
done
lock_release "$LOCK_DIR"
"""

CONTENDER = """#!/usr/bin/env bash
set -u
source "$LIB_LOCK"
if lock_acquire "$LOCK_DIR" "$WAIT"; then
    echo ACQUIRED
    lock_release "$LOCK_DIR"
else
    echo TIMEOUT
fi
"""


class Record:
    """One row of the timing file, parsed."""

    __slots__ = ("ts_ms", "lock", "event", "outcome", "wait_ms", "held_ms",
                 "precision", "pid", "raw")

    def __init__(self, line: str):
        self.raw = line
        parts = line.split("\t")
        assert len(parts) == 8, f"expected 8 tab-separated columns, got {parts!r}"
        (ts, lock, event, outcome, wait, held, precision, pid) = parts
        self.ts_ms = int(ts)
        self.lock = lock
        self.event = event
        self.outcome = outcome
        self.wait_ms = int(wait)
        self.held_ms = None if held == "-" else int(held)
        self.precision = precision
        self.pid = pid

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Record({self.raw!r})"


def _write_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _records(timing_file: Path) -> list[Record]:
    if not timing_file.exists():
        return []
    return [
        Record(line)
        for line in timing_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _env(tmp_path: Path, timing_file: Path | None, **extra) -> dict:
    env = dict(os.environ)
    env.pop("REMEMBER_LOCK_TIMING", None)
    env.pop("REMEMBER_LOCK_TIMING_FILE", None)
    env.pop("REMEMBER_LOCK_TIMING_MAX", None)
    env.update(
        LIB_LOCK=str(LIB_LOCK),
        LOCK_DIR=str(tmp_path / "lock" / "save.lock"),
        LONG_HOLD=LONG_HOLD,
    )
    if timing_file is not None:
        env["REMEMBER_LOCK_TIMING"] = "1"
        env["REMEMBER_LOCK_TIMING_FILE"] = str(timing_file)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run(script: Path, env: dict, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=timeout
    )


def test_a_real_hold_is_recorded_with_a_plausible_duration(tmp_path):
    """Two holds, one immediate and one of `LONG_HOLD` seconds, and the file has
    to tell them apart.

    A recorder that writes nothing fails on the file. One that writes a constant
    fails on the difference. One that writes seconds into a millisecond column,
    or nanoseconds, fails on the bounds.
    """
    timing = tmp_path / "lock-timing.tsv"
    script = _write_script(tmp_path / "harness.sh", HARNESS)

    result = _run(script, _env(tmp_path, timing))
    assert result.returncode == 0, result.stderr

    assert timing.exists(), (
        f"REMEMBER_LOCK_TIMING=1 but nothing was written to {timing} — a "
        "measurement that silently did not happen reads identically to one that "
        "showed nothing"
    )

    rows = _records(timing)
    releases = [r for r in rows if r.event == "release"]
    assert len(releases) == 2, f"expected two completed lock uses, got {rows!r}"

    short, long = releases
    assert short.lock == "save.lock" and long.lock == "save.lock"
    assert short.outcome == "ok" and long.outcome == "ok"
    assert long.precision in {"us", "ms", "s"}, long.precision

    assert short.held_ms is not None and long.held_ms is not None
    assert 0 <= short.held_ms <= 1000, (
        f"an immediate release recorded {short.held_ms}ms — at one-second "
        "resolution this is 0 or 1000, never more"
    )
    assert 1000 <= long.held_ms <= 15000, (
        f"a {LONG_HOLD}s hold recorded {long.held_ms}ms — not a plausible "
        "millisecond duration for that sleep at any of the three resolutions"
    )
    assert long.held_ms - short.held_ms >= 1000, (
        f"a {LONG_HOLD}s hold ({long.held_ms}ms) and an immediate one "
        f"({short.held_ms}ms) are not distinguishable — the recorder is not "
        "measuring anything"
    )


def test_a_lost_wait_is_recorded_as_a_timeout_carrying_the_wait_it_spent(tmp_path):
    """The event #226 is about — losing the wait — must not be invisible.

    A holder keeps `save.lock`; a contender waits its whole timeout and gives up.
    The give-up is what a too-small timeout looks like in production, so it is
    the row a maintainer needs in order to see the timeout being reached at all.
    """
    timing = tmp_path / "lock-timing.tsv"
    held = tmp_path / "held"
    release = tmp_path / "release"

    holder_script = _write_script(tmp_path / "holder.sh", HOLDER)
    contender_script = _write_script(tmp_path / "contender.sh", CONTENDER)

    holder_env = _env(tmp_path, timing, HELD_MARKER=held, RELEASE_MARKER=release)
    holder = subprocess.Popen(["bash", str(holder_script)], env=holder_env)
    try:
        deadline = time.time() + 20
        while not held.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert held.exists(), "the holder never took the lock"

        contender_env = _env(tmp_path, timing, WAIT=2)
        contender = _run(contender_script, contender_env)
        assert contender.stdout.strip() == "TIMEOUT", contender.stdout
    finally:
        release.write_text("", encoding="utf-8")
        holder.wait(timeout=30)

    rows = _records(timing)
    timeouts = [r for r in rows if r.event == "acquire" and r.outcome == "timeout"]
    assert len(timeouts) == 1, f"the lost wait was not recorded: {rows!r}"

    lost = timeouts[0]
    assert lost.lock == "save.lock"
    assert lost.held_ms is None, "a wait that never acquired holds nothing"
    assert 700 <= lost.wait_ms <= 12000, (
        f"a 2s wait that ran to its deadline recorded {lost.wait_ms}ms — "
        "lock_acquire cannot return before the deadline second has passed, so "
        "anything under ~1s means the wait is not being measured"
    )

    assert not [
        r for r in rows if r.event == "release" and r.pid == lost.pid
    ], "a process that never acquired the lock recorded a hold"


def test_with_the_recorder_off_nothing_is_written_and_nothing_extra_is_spawned(tmp_path):
    """The disabled path is the one every save pays, on every machine, forever.

    Counted by execution rather than by wall clock, the way #227/#230 counted the
    hook prefix: spawn count is what causes the wall clock difference, and it is
    the same number on every platform. `off` must be strictly cheaper than `on` —
    if someone makes the recorder always-on, these two become equal and this
    goes red.
    """
    off_log = tmp_path / "spawns-off.log"
    on_log = tmp_path / "spawns-on.log"
    shims = make_shim_dir(tmp_path, off_log)
    on_log.write_text("", encoding="utf-8")

    script = _write_script(tmp_path / "harness.sh", HARNESS)
    timing = tmp_path / "lock-timing.tsv"

    off_env = _env(tmp_path, None)
    off_env["PATH"] = f"{shims}{os.pathsep}{off_env['PATH']}"
    off_env["SPAWN_LOG"] = str(off_log)
    off_env["LOCK_DIR"] = str(tmp_path / "lock-off" / "save.lock")
    assert _run(script, off_env).returncode == 0

    assert not timing.exists(), "the recorder wrote a file while disabled"
    assert not list(tmp_path.glob("**/*lock-timing*")), (
        "the recorder left a file somewhere while disabled: "
        f"{list(tmp_path.glob('**/*lock-timing*'))}"
    )

    on_env = _env(tmp_path, timing)
    on_env["PATH"] = f"{shims}{os.pathsep}{on_env['PATH']}"
    on_env["SPAWN_LOG"] = str(on_log)
    on_env["LOCK_DIR"] = str(tmp_path / "lock-on" / "save.lock")
    assert _run(script, on_env).returncode == 0
    assert timing.exists()

    off_spawns = _spawn_lines(off_log)
    on_spawns = _spawn_lines(on_log)
    assert len(off_spawns) < len(on_spawns), (
        f"the disabled path spawned {len(off_spawns)} processes and the enabled "
        f"path {len(on_spawns)} — the recorder is not actually gated, so every "
        "save on every machine pays for it"
    )


def test_the_cap_stops_and_discloses_rather_than_rolling(tmp_path):
    """A bounded file that rolls silently loses the tail of the distribution —
    which is the part a timeout is set from. So it stops, keeps everything it
    already recorded, and says in the file itself that it stopped."""
    timing = tmp_path / "lock-timing.tsv"
    script = _write_script(tmp_path / "repeat.sh", REPEAT_HARNESS)

    env = _env(tmp_path, timing, REPEATS=8)
    env["REMEMBER_LOCK_TIMING_MAX"] = "4"
    assert _run(script, env).returncode == 0

    text = timing.read_text(encoding="utf-8")
    rows = _records(timing)
    assert 0 < len(rows) < 8, (
        f"the cap did not bound the file: {len(rows)} records for 8 lock uses"
    )
    assert len(timing.read_text(encoding="utf-8").splitlines()) <= 6, text

    assert "# CAPPED" in text, (
        "the file hit its cap with nothing in it saying so — a reader would take "
        f"a truncated distribution for a complete one:\n{text}"
    )
    assert rows[0].event == "release" and rows[0].outcome == "ok", (
        "the earliest record is gone — the cap rolled instead of stopping"
    )


def test_the_report_turns_the_file_into_a_distribution(tmp_path):
    """A distribution nobody can read is not an instrument."""
    timing = tmp_path / "lock-timing.tsv"
    script = _write_script(tmp_path / "harness.sh", HARNESS)
    assert _run(script, _env(tmp_path, timing)).returncode == 0

    report = subprocess.run(
        ["bash", str(REPORT), str(timing)],
        capture_output=True, text=True, timeout=60,
    )
    assert report.returncode == 0, report.stderr
    out = report.stdout
    assert "ok" in out.splitlines()[0], out
    assert "save.lock" in out, out
    assert "p50" in out and "max" in out, out

    rows = _records(timing)
    longest = max(r.held_ms for r in rows if r.held_ms is not None)
    assert str(longest) in out, (
        f"the report does not surface the longest hold ({longest}ms), which is "
        f"the number a timeout is set from:\n{out}"
    )


def test_the_report_says_skipped_rather_than_reporting_its_own_silence(tmp_path):
    """`skipped` is not a pass. An empty report that reads like a clean one is
    the same defect as a measurement that silently did not happen."""
    missing = tmp_path / "never-recorded.tsv"
    report = subprocess.run(
        ["bash", str(REPORT), str(missing)],
        capture_output=True, text=True, timeout=60,
    )
    assert report.returncode == 2, (
        f"a missing file exited {report.returncode} — indistinguishable from a "
        "clean run"
    )
    out = report.stdout + report.stderr
    assert "skipped" in out, out
    assert "REMEMBER_LOCK_TIMING" in out, (
        f"the skip does not say how to make it not skip:\n{out}"
    )


# No machine reaches more than one clock tier — bash 5 always picks `us`, this
# repo's macOS runner (/bin/bash 3.2 + BSD date) always picks `s`, and `ms` is
# picked by neither. So the conversions are driven directly, with the reading
# passed as an argument.
#
# The first version of this injected the reading by assigning EPOCHREALTIME and
# calling the clock. That is green on macOS and red on every ubuntu leg, and the
# CI output said why without ambiguity: the three parametrised cases returned
# three DIFFERENT well-formed epoch-millisecond values, 20-50ms apart and ~20
# minutes after the literals — a live clock reading, correctly converted, three
# times. On bash >= 5 EPOCHREALTIME is generated on every reference, so the
# assignment never reached the code under test; on bash 3.2 it is an ordinary
# variable and it did. The conversion was never the thing that was wrong.
#
# The lesson is the one that matters here: an injection mechanism that silently
# no-ops leaves a test that passes while asserting nothing, on exactly the
# platform whose tier it claims to cover. Passing the reading in cannot no-op.
CONVERT_PROBE = """#!/usr/bin/env bash
set -u
REMEMBER_LOCK_TIMING=1
source "$LIB_LOCK"
"$FUNC" "$READING"
printf '%s\\n' "$_LOCK_TIMING_NOW"
"""


@pytest.mark.parametrize(
    "func,reading,expected",
    [
        ("_lock_timing_us_to_ms", "1785436469.123456", 1785436469123),
        # A comma decimal separator is what LC_NUMERIC=de_DE gives, and epoch
        # milliseconds computed from `1785436469` alone would be off by 0.9s.
        ("_lock_timing_us_to_ms", "1785436469,987654", 1785436469987),
        # Truncation, never rounding: a hold must not be able to come back
        # longer than it was.
        ("_lock_timing_us_to_ms", "1785436469.000999", 1785436469000),
        ("_lock_timing_us_to_ms", "1785436469", 1785436469000),
        # A clock that cannot be read must not take locking down with it, and
        # must not produce a plausible number either.
        ("_lock_timing_us_to_ms", "not-a-time", 0),
        # The `ms` tier no runner in this repo's matrix ever executes live.
        ("_lock_timing_ns_to_ms", "1785436469123456789", 1785436469123),
        # What BSD `date +%s%N` prints. `_lock_timing_has_ns_date` rejects it
        # before the tier is chosen, so this pins the second line of defence.
        ("_lock_timing_ns_to_ms", "1785436469N", 0),
        ("_lock_timing_s_to_ms", "1785436469", 1785436469000),
        ("_lock_timing_s_to_ms", "", 0),
    ],
)
def test_each_clock_tier_converts_a_reading_to_epoch_milliseconds(
    tmp_path, func, reading, expected
):
    script = _write_script(tmp_path / "clock.sh", CONVERT_PROBE)
    env = _env(tmp_path, None, FUNC=func, READING=reading)
    result = _run(script, env)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == expected, result.stdout
