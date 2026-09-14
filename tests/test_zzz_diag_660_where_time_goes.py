"""THROWAWAY diagnostic for #660 -- delete this file with its branch.

Not a regression test. It asserts nothing about speed and must never be
merged: its whole job is to make ONE measurement visible in a CI log on the
platform that has the problem, and then be thrown away.

The question it answers: on Windows/Git Bash, WHERE does session-start-hook.sh
actually spend its wall clock? The reporter of #660 measured ~40 subprocess
calls on 0.32.0 and ~90ms per fork on their host -- 3.6s of accounted time
against 38.5s observed, so roughly nine tenths of it is unexplained by the
model everyone has been reasoning from (this repo's own latency numbers
included).

Method: run the real hook under `bash -x` with `PS4='+$EPOCHREALTIME '`, so
every traced command carries a timestamp written by the shell itself, with no
fork of its own. The gap between one timestamp and the next is the cost of the
command on the earlier line. Aggregate those gaps per command, print the
slowest, and fail on purpose so pytest puts the table in the job log (a
passing test prints nothing without -s, and this exists only to print).

Scaled on the one axis the existing benchmark fixture cannot show:
`previous_transcript()` runs `ls -t "$SESSIONS_DIR"/*.jsonl` and stats/sorts
EVERY past transcript to read one line off the top. The benchmark fixture has
one. A heavy user has thousands. If that is where the time is, the curve says
so; if it is flat, the suspicion is dead and the trace still says what is
actually slow.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import pytest

from .test_session_start_windows_benchmark_669 import (
    BASH,
    SESSION_START,
    _env,
    _payload,
    _slug,
    _store,
    _write_no_crlf,
)

pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash on this host (#432)"
)

# How many past transcripts to litter SESSIONS_DIR with, per scenario.
TRANSCRIPT_COUNTS = (1, 500, 2000)

# Roughly the reporter's store: ~480KB across the six memory files.
MEMORY_FILE_BYTES = 80_000


def _fatten(remember: Path) -> None:
    """Grow the fixture's memory files to a reporter-representative size."""
    filler = ("- a remembered line about as long as a real one\n" * 4000)[
        :MEMORY_FILE_BYTES
    ]
    for name in (
        "identity.md",
        "core-memories.md",
        "now.md",
        "recent.md",
        "archive.md",
    ):
        _write_no_crlf(remember / name, filler)


def _litter_transcripts(home: Path, project: Path, count: int) -> None:
    """Put `count` past session transcripts where previous_transcript() looks."""
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True, exist_ok=True)
    body = '{"type":"assistant","message":{"content":"x"}}\n' * 20
    for i in range(count):
        _write_no_crlf(session_dir / f"diag-{i:05d}.jsonl", body)


def _trace(env: dict, trace_file: Path) -> tuple[float, str]:
    """Run the hook once under `bash -x`, timestamps via PS4."""
    run_env = {
        **env,
        # A decimal COMMA is what $EPOCHREALTIME gives under a fr_FR locale,
        # and float() will not parse it. Force the C locale for the traced
        # shell so the trace parses wherever this runs.
        "LC_ALL": "C",
        "PS4": "+$EPOCHREALTIME ",
    }
    # BASH_XTRACEFD, not plain `bash -x`: bootstrap-dirs.sh:364 runs
    # `exec 2>> "$REMEMBER_DIR/logs/hook-errors.log"`, so a trace on fd 2 is
    # hijacked into the hook's own error log part-way through the run and the
    # capture silently stops -- measured here first as "wall 0.71s, traced
    # span 0.07s". A dedicated fd survives that redirect.
    wrapper = trace_file.parent / "trace-wrapper.sh"
    _write_no_crlf(
        wrapper,
        "\n".join([
            f'exec 9>"{trace_file.as_posix()}"',
            "export BASH_XTRACEFD=9",
            "set -x",
            f'. "{SESSION_START.as_posix()}"',
            "",
        ]),
    )
    t0 = time.perf_counter()
    subprocess.run(
        [BASH, wrapper.as_posix()],
        input=_payload(),
        env=run_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=600,
        check=False,
    )
    elapsed = time.perf_counter() - t0
    return elapsed, trace_file.read_text(encoding="utf-8", errors="replace")


def _attribute(trace: str) -> tuple[list[tuple[float, int, str]], float]:
    """Charge the gap between consecutive timestamps to the earlier command."""
    stamps: list[tuple[float, str]] = []
    for line in trace.splitlines():
        if not line.startswith("+"):
            continue
        body = line.lstrip("+")
        ts, _, cmd = body.partition(" ")
        try:
            stamps.append((float(ts), cmd.strip()))
        except ValueError:
            continue

    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for (t_now, cmd), (t_next, _) in zip(stamps, stamps[1:]):
        gap = t_next - t_now
        if gap < 0:
            continue
        key = cmd[:120]
        totals[key] += gap
        counts[key] += 1

    rows = sorted(
        ((secs, counts[cmd], cmd) for cmd, secs in totals.items()), reverse=True
    )
    traced = stamps[-1][0] - stamps[0][0] if len(stamps) > 1 else 0.0
    return rows, traced


def _render(label, wall, traced, lines, rows) -> str:
    out = [
        f"### {label}",
        f"wall {wall:.2f}s | traced span {traced:.2f}s | {lines} traced lines",
        "",
        "| secs | calls | command |",
        "|---|---|---|",
    ]
    for secs, calls, cmd in rows[:25]:
        out.append(f"| {secs:.3f} | {calls} | `{cmd}` |")
    return "\n".join(out)


@pytest.mark.parametrize("count", TRANSCRIPT_COUNTS)
def test_diag_where_does_session_start_spend_its_time(tmp_path, count):
    home, project, remember = _store(tmp_path)
    _fatten(remember)
    _litter_transcripts(home, project, count)
    env = _env(home, project, remember, os.environ["PATH"])

    # Cold (nothing cached) then warm (every #668/#684 cache filled): a real
    # repeated session start is the warm one, and the caches are exactly what
    # the fork model assumes are working.
    cold_wall, cold_trace = _trace(env, tmp_path / "trace-cold.txt")
    cold_rows, cold_traced = _attribute(cold_trace)
    warm_wall, warm_trace = _trace(env, tmp_path / "trace-warm.txt")
    warm_rows, warm_traced = _attribute(warm_trace)

    report = "\n\n".join([
        f"## #660 diagnostic -- {count} past transcripts, "
        f"{MEMORY_FILE_BYTES * 5 // 1000}KB store, {sys.platform}",
        _render("cold", cold_wall, cold_traced, len(cold_trace.splitlines()), cold_rows),
        _render("warm", warm_wall, warm_traced, len(warm_trace.splitlines()), warm_rows),
    ])

    # Deliberate failure: this test exists to print. See the module docstring.
    pytest.fail(report, pytrace=False)
