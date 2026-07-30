"""today-*.md is written by one lock's holder and retired by another's (#225).

`save-session.sh`'s NDC step appends a compressed day summary to
`today-<day>.md` while holding **save.lock** (and, since #223, re-taking it for
the now.md truncate). `run-consolidation.sh` reads that same file, calls Haiku,
and renames it to `.done.md` while holding **consolidation.lock**. Both locks
are sound (#182). Neither says anything about the other.

The consumed-byte count added for the staging rename closes the wide window —
the 180s Haiku call — by keeping whatever was appended past the offset it
recorded. It does not close the retire loop itself: `wc -c`, `head`, `tail` and
`mv` are four separate operations, and an NDC append landing among them lands
in the inode about to become `.done.md`. Nothing globs `.done.md` again and
session start never injects it, so the entry is written to disk and then
unreachable, with no log line. #142's signature, in milliseconds instead of
minutes, one file over.

Reachable because `$NDC_DAY` legitimately names a past day: it comes from
`tmp/now-day`, the #141 marker recording which day now.md's content is from, so
a session that crosses midnight compresses into `today-<yesterday>.md` — the
exact file consolidation considers eligible (it excludes only *today's*). See
`test_ndc_day_boundary.py::test_compression_files_content_under_the_stamped_day`.

The fix gives the file its own lock (`scripts/lib-staging-lock.sh`), held only
by the NDC append and by the retire loop. Neither critical section contains a
model call, and no path holds this lock while taking another — so there is no
lock order to get wrong, which is what keeps a separate now.md lock (PR #173)
compatible rather than deadlock-prone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .test_consolidation_append_race import _make_env, _run
from .test_save_session_gates import _run as _run_save
from .test_ndc_truncate_race import _ndc_env, _wait_for_background_ndc
from .subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_LOCK = REPO_ROOT / "scripts" / "lib-lock.sh"
LIB_STAGING_LOCK = REPO_ROOT / "scripts" / "lib-staging-lock.sh"

ENTRY = "## 23:59 | main\n\n- NDC summary of yesterday\n"

# One-shot sync point on the retire loop's `wc -c` (run-consolidation.sh). The
# real `wc` runs FIRST, so the byte count the loop will act on is already
# taken; only then does the gate open and the stub wait for the competing
# appender. Anything that appender writes therefore lands strictly between the
# byte count and the rename — the window this file is about, which is
# microseconds wide in real time and so not reachable from another process
# without help. `wc -l` (log rotation) passes straight through, and `command -p`
# resolves against the shell's default PATH so the stub cannot recurse.
WC_STUB = """#!/bin/sh
case "$1" in
    -c)
        command -p wc "$@" || exit $?
        if [ -n "$STUB_WC_GATE" ] && [ ! -e "$STUB_WC_GATE" ]; then
            : > "$STUB_WC_GATE"
            _i=0
            while [ ! -e "$STUB_WC_DONE" ] && [ "$_i" -lt "${STUB_WC_TICKS:-40}" ]; do
                sleep 0.05
                _i=$((_i + 1))
            done
        fi
        exit 0
        ;;
esac
command -p wc "$@"
"""

# A competing NDC round: waits for the gate, then appends through the SHIPPED
# protocol — the same two functions save-session.sh calls. Nothing is
# special-cased for the test. With the retire loop holding staging.lock this
# blocks until the loop is finished; without it, it appends immediately and the
# pending rename seals the entry away.
APPENDER = """#!/usr/bin/env bash
set -u
source "$LIB_LOCK"
source "$LIB_STAGING_LOCK"
while [ ! -e "$GATE" ]; do sleep 0.02; done
printf '%s' "$ENTRY_TEXT" > "$ENTRY_FILE"
if staging_lock_acquire "${WAIT:-30}"; then
    staging_append "$TODAY_FILE" "$ENTRY_FILE"
    staging_lock_release
    printf 'appended\n' > "$RESULT_FILE"
else
    printf 'timeout\n' > "$RESULT_FILE"
fi
: > "$DONE_FILE"
"""

# Holds staging.lock from outside and does not let go, so the retire loop has
# to decide what to do when it cannot have it.
HOLDER = """#!/usr/bin/env bash
set -u
source "$LIB_LOCK"
source "$LIB_STAGING_LOCK"
staging_lock_acquire 5 || exit 1
: > "$HELD_FILE"
while [ ! -e "$RELEASE_FILE" ]; do sleep 0.05; done
staging_lock_release
"""


def _install(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _log_text(remember: Path) -> str:
    return "\n".join(f.read_text(encoding="utf-8")
                     for f in (remember / "logs").glob("*.log"))


def _live_staging(remember: Path) -> str:
    return "".join(p.read_text(encoding="utf-8")
                   for p in remember.glob("today-*.md")
                   if not p.name.endswith(".done.md"))


def _retired_staging(remember: Path) -> str:
    return "".join(p.read_text(encoding="utf-8")
                   for p in remember.glob("today-*.done.md"))


def _wait_for(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


class TestConsolidationRetireVersusNdcAppend:
    """The reported loss, as an actual race between two processes."""

    def test_an_ndc_append_racing_the_retire_is_not_sealed_away(self, tmp_path):
        env, project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n",
                           encoding="utf-8")

        gate, done, result_file = (tmp_path / "wc-gate", tmp_path / "appended",
                                   tmp_path / "appender-result")
        bindir = tmp_path / "stub-bin"
        _install(bindir / "wc", WC_STUB)
        env = dict(env)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["STUB_WC_GATE"] = str(gate)
        env["STUB_WC_DONE"] = str(done)

        appender = _install(tmp_path / "appender.sh", APPENDER)
        proc = subprocess.Popen(
            ["bash", str(appender)],
            env={
                **os.environ,
                "REMEMBER_DIR": str(remember),
                "LIB_LOCK": str(LIB_LOCK),
                "LIB_STAGING_LOCK": str(LIB_STAGING_LOCK),
                "GATE": str(gate),
                "DONE_FILE": str(done),
                "RESULT_FILE": str(result_file),
                "TODAY_FILE": str(staging),
                "ENTRY_FILE": str(tmp_path / "ndc-text.md"),
                "ENTRY_TEXT": ENTRY,
            },
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            result = _run(plugin, env)
            assert result.returncode == 0, result.stderr
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()

        assert gate.is_file(), (
            "the sync point never fired — the retire loop was never reached, "
            "so this test proves nothing"
        )
        assert result_file.read_text().strip() == "appended", (
            "the competing NDC round never appended, so the interleave this "
            "test exists for did not happen"
        )
        assert "NDC summary of yesterday" in _live_staging(remember), (
            "an NDC append that landed after consolidation measured the "
            "staging file was sealed inside .done.md — nothing globs those "
            "again and session start never injects them, so the entry is "
            "unreachable memory and nothing was logged (#225)"
        )
        assert "earlier work" in _retired_staging(remember), (
            "the consolidated span must still be retired — a fix that stops "
            "retiring is a no-op with extra steps, and the next run would "
            "re-consolidate work already in recent.md"
        )

    def test_consolidation_refuses_to_touch_staging_without_the_lock(self, tmp_path):
        """Losing the wait must leave staging alone, and say so.

        Since #235 the bail happens earlier than the name used to suggest:
        consolidation now takes its snapshot under staging.lock, so a lost
        wait exits at the *snapshot* acquire and never reaches the retire
        loop. Both are covered by the same assertions — staging untouched,
        recent.md and archive.md untouched, and a log line saying why — but
        the exit under test is the snapshot one, and a reader who assumed
        otherwise would think the retire loop was still being exercised.
        """
        env, project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n",
                           encoding="utf-8")

        held, release = tmp_path / "held", tmp_path / "release"
        holder = _install(tmp_path / "holder.sh", HOLDER)
        proc = subprocess.Popen(
            ["bash", str(holder)],
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
            result = _run(plugin, env)
            assert result.returncode == 0, result.stderr
        finally:
            release.write_text("")
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

        assert not _retired_staging(remember), (
            "staging was retired to .done.md while another process held "
            "staging.lock — that process is entitled to be appending an NDC "
            "summary right now, and the rename would seal it away (#225)"
        )
        assert "earlier work" in _live_staging(remember), (
            "the staging file must be left in place for the next run"
        )
        assert "staging.lock" in _log_text(remember), (
            "a consolidation that consolidated but did not retire leaves a "
            "duplicate for the next run; a duplicate that is never logged is "
            "indistinguishable from a bug"
        )


class TestNdcAppendTakesTheStagingLock:
    """The other half: the append side must respect the same lock."""

    def test_the_append_and_the_truncate_are_both_skipped_without_the_lock(
        self, tmp_path
    ):
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        remember = project / ".remember"
        memory_file = remember / "now.md"

        held, release = tmp_path / "held", tmp_path / "release"
        holder = _install(tmp_path / "holder.sh", HOLDER)
        proc = subprocess.Popen(
            ["bash", str(holder)],
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
            result = _run_save(plugin, env, sid)
            assert result.returncode == 0, subprocess_failure_detail(result, remember)
            remaining = _wait_for_background_ndc(memory_file)
        finally:
            release.write_text("")
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

        assert not list(remember.glob("today-*.md")), (
            "the NDC summary was appended to today-*.md while a consolidation "
            "held staging.lock — that consolidation may be renaming this very "
            "file, which would seal the append inside .done.md (#225)"
        )
        assert "did some work" in remaining, (
            "now.md must keep every byte when the append did not happen — "
            "truncating a span that was never staged loses it outright"
        )
        assert "SKIPPED" in _log_text(remember), (
            "skipping a compression round silently is indistinguishable from "
            "a compression that never fired"
        )
