"""`cmd_consolidate`'s READ of today-*.md must happen under staging.lock (#235).

#234 gave `today-*.md` its own lock and held it around the two operations that
WRITE the file: the NDC append (`staging_append`) and consolidation's retire
loop. It deliberately left consolidation's *read* outside, because closing it
naively means holding the lock across the Haiku call — the shape that produced
#142 and the reason `save.lock` was rejected as the staging lock in the first
place.

WHAT THE UNLOCKED READ ACTUALLY COSTS

The issue predicted a summary split down the middle: part consumed into
recent.md, the remainder kept as the tail. Measured, that is not the reachable
failure. `staging_append` writes the summary with a single `cat`, and a
concurrent reader on a regular file never observes a partial `write()` — 97827
reads against a 3000-record appender produced 0 torn reads.

What IS reachable, on 93% of those reads, is the *other* intermediate state.
`staging_append` is two operations, which is exactly why #234 put it in a
critical section::

    [ -s "$_today" ] && echo "" >> "$_today"     # 1. separator
    cat "$_text" >> "$_today"                    # 2. the summary

A reader landing between them consumes the separator and not the entry it
separates. So the consolidated span retired into `.done.md` ends with a blank
line whose summary is somewhere else, and the summary is re-consolidated on the
next round under a later timestamp — attributed to the day it was re-consumed
rather than the day it was written. Smaller than the issue's framing, same
class of harm, and it is a half-applied append being read as a whole one.

The fix takes the bytes under the lock and calls the model outside it — #224's
shape, one file over. In this architecture that is the same change as the
issue's "snapshot" option: the lock lives in bash (`lib-lock.sh`) and the read
lives in the same Python process as the Haiku call, so ending the critical
section before the model call requires the read to end at a process boundary,
and the bytes to be handed across it on disk. That handover directory IS the
snapshot; `run-consolidation.sh` owns its lifetime.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .test_consolidation_append_race import _real_pipeline_env, _run_real
from .test_staging_lock import HOLDER, _install, _log_text, _wait_for

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_LOCK = REPO_ROOT / "scripts" / "lib-lock.sh"
LIB_STAGING_LOCK = REPO_ROOT / "scripts" / "lib-staging-lock.sh"

ORIGINAL = "# Day\n\n## 10:00 | main\n\n- earlier work\n"
ENTRY = "## 23:59 | main\n\n- landed during a mid-append read\n"

# One-shot sync point INSIDE `staging_append`, between its two writes. The
# separator has already been appended when this fires, so the file is in the
# half-applied state for as long as the gate is open. Only the NDC text file is
# gated — every other `cat` in the scripts passes straight through, and
# `command -p` resolves against the shell's default PATH so the stub cannot
# recurse. Same technique, and the same justification, as the `wc` stub in
# test_staging_lock.py: the window is microseconds wide in real time and is not
# reachable from another process without help.
CAT_STUB = """#!/bin/sh
if [ -n "$STUB_CAT_TARGET" ] && [ "$1" = "$STUB_CAT_TARGET" ]; then
    : > "$STUB_CAT_GATE"
    _i=0
    while [ ! -e "$STUB_CAT_DONE" ] && [ "$_i" -lt "${STUB_CAT_TICKS:-100}" ]; do
        sleep 0.05
        _i=$((_i + 1))
    done
fi
command -p cat "$@"
"""

# Holds the consolidation side back until the appender is provably mid-append,
# so the read cannot simply win the race and prove nothing. Gates only the
# pipeline invocations; `python3 -V` (detect-tools.sh) and any other use pass
# through untouched.
PYTHON_STUB = """#!/bin/sh
case "$*" in
    *pipeline.shell*)
        _i=0
        while [ ! -e "$STUB_PY_GATE" ] && [ "$_i" -lt 200 ]; do
            sleep 0.05
            _i=$((_i + 1))
        done
        "$REAL_PYTHON" "$@"
        _rc=$?
        : > "$STUB_CAT_DONE"
        exit $_rc
        ;;
esac
exec "$REAL_PYTHON" "$@"
"""

# A competing NDC round, through the SHIPPED protocol — the same two functions
# save-session.sh calls. Nothing is special-cased for the test; the interleave
# comes from the `cat` stub sitting inside `staging_append`.
APPENDER = """#!/usr/bin/env bash
set -u
source "$LIB_LOCK"
source "$LIB_STAGING_LOCK"
printf '%s' "$ENTRY_TEXT" > "$ENTRY_FILE"
if staging_lock_acquire 30; then
    : > "$HELD_FILE"
    staging_append "$TODAY_FILE" "$ENTRY_FILE"
    staging_lock_release
    printf 'appended\n' > "$RESULT_FILE"
else
    printf 'timeout\n' > "$RESULT_FILE"
fi
"""


def _staging_text(remember: Path, done: bool) -> str:
    pattern = "today-*.done.md" if done else "today-*.md"
    return "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(remember.glob(pattern))
        if p.name.endswith(".done.md") == done
    )


class TestConsolidationReadVersusNdcAppend:
    """The read must observe whole appends or none — never a half-applied one."""

    def test_a_half_applied_append_is_not_consumed_as_a_whole_one(self, tmp_path):
        env, project, remember = _real_pipeline_env(tmp_path)
        staging = remember / "today-2020-01-01.md"
        staging.write_text(ORIGINAL, encoding="utf-8")

        bindir = tmp_path / "stub-bin"
        _install(bindir / "cat", CAT_STUB)
        _install(bindir / "python3", PYTHON_STUB)

        gate = tmp_path / "mid-append"
        cat_done = tmp_path / "cat-done"
        held = tmp_path / "lock-held"
        result_file = tmp_path / "appender-result"
        entry_file = tmp_path / "ndc-text.md"

        env = dict(env)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["REAL_PYTHON"] = sys.executable
        env["STUB_PY_GATE"] = str(gate)
        env["STUB_CAT_DONE"] = str(cat_done)
        env["REMEMBER_STAGING_LOCK_TIMEOUT"] = "30"

        proc = subprocess.Popen(
            ["bash", str(_install(tmp_path / "appender.sh", APPENDER))],
            env={
                **os.environ,
                "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                "REMEMBER_DIR": str(remember),
                "LIB_LOCK": str(LIB_LOCK),
                "LIB_STAGING_LOCK": str(LIB_STAGING_LOCK),
                "TODAY_FILE": str(staging),
                "ENTRY_FILE": str(entry_file),
                "ENTRY_TEXT": ENTRY,
                "HELD_FILE": str(held),
                "RESULT_FILE": str(result_file),
                "STUB_CAT_TARGET": str(entry_file),
                "STUB_CAT_GATE": str(gate),
                "STUB_CAT_DONE": str(cat_done),
            },
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert _wait_for(held), "the competing NDC round never took staging.lock"
            assert _wait_for(gate), (
                "the append never reached its second write, so the half-applied "
                "state this test is about never existed"
            )
            result = _run_real(env)
            assert result.returncode == 0, result.stderr
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()

        assert result_file.read_text().strip() == "appended", (
            "the competing NDC round never finished appending, so the "
            "interleave this test exists for did not happen"
        )

        retired = _staging_text(remember, done=True)
        assert retired, f"nothing was retired: {result.stderr}"
        assert "earlier work" in retired, "the pre-existing span must still be retired"
        assert "landed during a mid-append read" in retired, (
            "consolidation read today-*.md while an NDC append was half applied: "
            "it consumed the separator `staging_append` writes before the summary "
            "and stopped there. The blank line is retired into .done.md as if it "
            "were the end of the day, and the summary it separates is "
            "re-consolidated on a later round under a later timestamp — "
            "attributed to the day it was re-consumed, not the day it was "
            "written, with nothing to say so. The read has to happen under "
            "staging.lock, with the model call outside it (#235). "
            f"retired={retired!r} live={_staging_text(remember, done=False)!r}"
        )


class TestConsolidationRefusesToReadWithoutTheLock:
    """Losing the wait must consolidate nothing, and say so."""

    def test_nothing_is_read_consolidated_or_retired_without_the_lock(self, tmp_path):
        env, project, remember = _real_pipeline_env(tmp_path)
        staging = remember / "today-2020-01-01.md"
        staging.write_text(ORIGINAL, encoding="utf-8")
        recent = remember / "recent.md"
        recent.write_text("# Recent\n\nuntouched\n", encoding="utf-8")

        held, release = tmp_path / "held", tmp_path / "release"
        proc = subprocess.Popen(
            ["bash", str(_install(tmp_path / "holder.sh", HOLDER))],
            env={
                **os.environ,
                "REMEMBER_DIR": str(remember),
                "LIB_LOCK": str(LIB_LOCK),
                "LIB_STAGING_LOCK": str(LIB_STAGING_LOCK),
                "HELD_FILE": str(held),
                "RELEASE_FILE": str(release),
            },
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert _wait_for(held), "the holder never took staging.lock"
            env = dict(env)
            env["REMEMBER_STAGING_LOCK_TIMEOUT"] = "1"
            result = _run_real(env)
            assert result.returncode == 0, result.stderr
        finally:
            release.write_text("")
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

        assert not list(remember.glob("today-*.done.md")), (
            "staging was retired while another process held staging.lock"
        )
        assert "earlier work" in staging.read_text(encoding="utf-8"), (
            "the staging file must be left in place for the next run"
        )
        assert "untouched" in recent.read_text(encoding="utf-8"), (
            "recent.md was rewritten from a span read without the lock — the "
            "holder is entitled to be half way through an NDC append right now"
        )
        assert "staging.lock" in _log_text(remember), (
            "a consolidation that did nothing must say why; a silent no-op is "
            "indistinguishable from a consolidation that never fired"
        )
