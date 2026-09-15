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

# Round 1/2 scaled transcript COUNT and found it nearly flat (0.41s of a 3.2s
# run at 2000). Round 3 scales SIZE instead, on the three axes a long-lived
# real store grows along and the fixture never had:
#
#   store_kb      -- the six memory files. archive.md alone is megabytes on a
#                    store that has been consolidating for months.
#   transcript_mb -- the PREVIOUS session's .jsonl. The capture-gap check runs
#                    `grep -q '"tool_use"'` over it (session-start-hook.sh:999),
#                    which reads the whole file when the pattern is absent.
#   slices        -- rotated archive-*.md / recent-*.md. The memory render
#                    globs, sorts and lists every one of them on a cache miss.
#
# name, transcripts, store_kb, transcript_mb, rotated slices, staging files
SCENARIOS = (
    ("baseline", 50, 400, 0.01, 0, 0),
    ("year-old-store", 50, 6_000, 20.0, 60, 3),
    ("abandoned-store", 50, 20_000, 100.0, 250, 10),
)


LINE = "- a remembered line about as long as a real one in a real store\n"


def _filler(nbytes: int) -> str:
    return (LINE * (nbytes // len(LINE) + 1))[:nbytes]


def _fatten(remember: Path, store_kb: int, slices: int, staging: int) -> None:
    """Grow the store along the three axes a long-lived one grows along."""
    each = (store_kb * 1000) // 5
    for name in (
        "identity.md",
        "core-memories.md",
        "now.md",
        "recent.md",
        "archive.md",
    ):
        _write_no_crlf(remember / name, _filler(each))

    # Rotated slices: globbed, sorted and listed by the memory render.
    for i in range(slices):
        day = f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
        _write_no_crlf(remember / f"archive-{day}.md", _filler(20_000))
        _write_no_crlf(remember / f"recent-{day}.md", _filler(20_000))

    # Past-day staging files: what the consolidation trigger counts.
    for i in range(staging):
        _write_no_crlf(remember / f"today-2026-08-{i + 1:02d}.md", _filler(30_000))


def _litter_transcripts(home: Path, project: Path, count: int,
                        previous_mb: float) -> None:
    """`count` past transcripts, the newest one `previous_mb` megabytes.

    Size matters on the newest one specifically: it is what
    previous_transcript() returns, and the capture-gap check greps it
    (session-start-hook.sh:999) -- `grep -q` reads to EOF when the pattern is
    absent, and this fixture's lines never contain "tool_use".
    """
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True, exist_ok=True)
    small = '{"type":"assistant","message":{"content":"x"}}\n' * 20
    for i in range(count):
        _write_no_crlf(session_dir / f"diag-{i:05d}.jsonl", small)

    big = session_dir / f"diag-{count:05d}.jsonl"
    chunk = '{"type":"assistant","message":{"content":"xxxxxxxxxxxxxxxx"}}\n' * 1000
    with open(big, "w", encoding="utf-8", newline="") as fh:
        for _ in range(max(1, int(previous_mb * 1_000_000 / len(chunk)))):
            fh.write(chunk)


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


@pytest.mark.parametrize(
    "name,count,store_kb,previous_mb,slices,staging",
    SCENARIOS,
    ids=[s[0] for s in SCENARIOS],
)
def test_diag_where_does_session_start_spend_its_time(
    tmp_path, name, count, store_kb, previous_mb, slices, staging
):
    home, project, remember = _store(tmp_path)
    _fatten(remember, store_kb, slices, staging)
    _litter_transcripts(home, project, count, previous_mb)
    env = _env(home, project, remember, os.environ["PATH"])

    # Cold (nothing cached) then warm (every #668/#684 cache filled): a real
    # repeated session start is the warm one, and the caches are exactly what
    # the fork model assumes are working.
    cold_wall, cold_trace = _trace(env, tmp_path / "trace-cold.txt")
    cold_rows, cold_traced = _attribute(cold_trace)
    warm_wall, warm_trace = _trace(env, tmp_path / "trace-warm.txt")
    warm_rows, warm_traced = _attribute(warm_trace)

    report = "\n\n".join([
        f"## #660 diagnostic [{name}] -- {store_kb}KB store, "
        f"{previous_mb}MB previous transcript, {count} transcripts, "
        f"{slices} rotated slices, {staging} staging files, {sys.platform}",
        _render("cold", cold_wall, cold_traced, len(cold_trace.splitlines()), cold_rows),
        _render("warm", warm_wall, warm_traced, len(warm_trace.splitlines()), warm_rows),
    ])

    # Deliberate failure: this test exists to print. See the module docstring.
    pytest.fail(report, pytrace=False)
