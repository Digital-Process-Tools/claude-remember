"""The NDC partial-truncate commit must run under the save lock (#223).

#142 replaced NDC's blind `: > now.md` with a partial truncate: keep everything
past the byte offset the file had when the compression started. That closed the
180-second window in which a newer save's entry could be erased.

It did not close the one @Jutiphan named as defect (b) in #173. The commit —
`tail -c +N` into a temp file, then `mv` it over now.md — runs inside the
subshell backgrounded at the end of `save-session.sh`, and by then the parent
has released the save lock and exited. So:

1. `tail -c +N` reads to EOF into the temp file.
2. Another save acquires the lock and appends to now.md. It is entitled to —
   nothing is held here.
3. `mv` replaces now.md with a copy that predates step 2.

The step-2 entry is gone, its position already advanced, and nothing is logged:
the #142 signature again, in a window of milliseconds instead of minutes,
against the permanent memory record.

The fix re-acquires the lock in the subshell around the whole commit, and
re-reads the file's size under it before trusting the pre-Haiku offset. Losing
the re-acquire leaves now.md untouched, which lands in the same safe state as
the tail-failure branch: a possibly duplicated summary in today-*.md, no data
loss.

#173's own fix used `flock`, which does not exist on macOS; the `mkdir`-based
primitive in lib-lock.sh (#182) is what makes this portable.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .test_save_session_gates import _run  # shared harness
from .test_ndc_truncate_race import _ndc_env, _wait_for_background_ndc
from .subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

APPENDED = "\n## 11:45 | main\n\n- appended between the tail and the mv\n"

# Widens the tail→mv window, which is microseconds wide in real time and
# therefore not reachable from another process without help. Only the exact
# invocation NDC uses (`tail -c +N MEMORY_FILE`) is delayed; every other tail
# in the script runs untouched.
#
# The real tail runs FIRST, so its output (the bytes `mv` will install) is
# already captured before the gate opens. Anything the competing appender
# writes after that gate is, by construction, something the pending `mv` would
# erase. `command -p` resolves against the shell's default PATH, so the stub
# cannot recurse into itself; it is not prefixed with `exec` for the dash
# reason documented in test_ndc_tail_failure_no_truncate.py.
SLOW_TAIL_STUB = """#!/bin/sh
case "$1" in
    -c)
        command -p tail "$@" || exit $?
        : > "$STUB_TAIL_GATE"
        sleep "${STUB_TAIL_DELAY:-3}"
        exit 0
        ;;
esac
command -p tail "$@"
"""

# A competing save: waits for the gate, takes the save lock the way every save
# takes it, appends one entry, releases. Nothing here is special-cased for the
# test — this is exactly what a concurrent save-session.sh is entitled to do
# while an NDC compression is committing.
APPENDER = """#!/usr/bin/env bash
set -u
source "$LIB_LOCK"
while [ ! -e "$GATE" ]; do sleep 0.02; done
if lock_acquire "$LOCK_DIR" 30; then
    printf '%s' "$ENTRY_TEXT" >> "$MEMORY_FILE"
    printf 'acquired\n' > "$RESULT_FILE"
    lock_release "$LOCK_DIR" || true
else
    printf 'timeout\n' > "$RESULT_FILE"
fi
"""


def _install(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _log_text(project: Path) -> str:
    return "\n".join(
        f.read_text() for f in (project / ".remember" / "logs").glob("*.log")
    )


class TestNdcCommitLock:

    def test_entry_appended_between_tail_and_mv_survives(self, tmp_path):
        """(b) itself: a real interleave, with real data loss when unlocked."""
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"
        gate = tmp_path / "tail-gate"
        result_file = tmp_path / "appender-result"

        bindir = tmp_path / "stub-bin"
        _install(bindir / "tail", SLOW_TAIL_STUB)
        env = dict(env)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["STUB_TAIL_GATE"] = str(gate)
        env["STUB_TAIL_DELAY"] = "3"

        appender = _install(tmp_path / "appender.sh", APPENDER)
        proc = subprocess.Popen(
            ["bash", str(appender)],
            env={
                **os.environ,
                "LIB_LOCK": str(plugin / "scripts" / "lib-lock.sh"),
                "LOCK_DIR": str(project / ".remember" / "tmp" / "save.lock"),
                "GATE": str(gate),
                "MEMORY_FILE": str(memory_file),
                "ENTRY_TEXT": APPENDED,
                "RESULT_FILE": str(result_file),
            },
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            result = _run(plugin, env, sid)
            assert result.returncode == 0, subprocess_failure_detail(
                result, project / ".remember"
            )
            proc.wait(timeout=90)
        finally:
            if proc.poll() is None:
                proc.kill()

        assert gate.is_file(), (
            "the slow-tail stub never ran — the NDC commit was never reached, "
            "so this test proves nothing"
        )
        assert result_file.read_text().strip() == "acquired", (
            "the competing save never got the lock, so it never appended — "
            "the interleave this test exists for did not happen"
        )

        remaining = _wait_for_background_ndc(memory_file)
        assert "appended between the tail and the mv" in remaining, (
            "an entry appended by a lock-holding save while the NDC commit was "
            "in flight was erased by the `mv`. Its position is already "
            "advanced, so it is unrecoverable, and nothing was logged (#223)"
        )
        assert "did some work" not in remaining, (
            "the commit must still drop the compressed span — a fix that "
            "simply stops committing is a no-op with extra steps"
        )

    def test_commit_skips_while_another_save_holds_the_lock(self, tmp_path):
        """Losing the re-acquire must leave now.md whole, and say so."""
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"
        lock_dir = project / ".remember" / "tmp" / "save.lock"

        env = dict(env)
        env["STUB_HOLD_LOCK_DURING_NDC"] = str(lock_dir)
        env["STUB_HOLD_LOCK_PID"] = str(os.getpid())
        env["REMEMBER_NDC_COMMIT_LOCK_TIMEOUT"] = "1"

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(
            result, project / ".remember"
        )

        remaining = _wait_for_background_ndc(memory_file)
        assert lock_dir.is_dir(), "the stub never took the lock — nothing was contended"
        assert "did some work" in remaining, (
            "the NDC commit rewrote now.md while another live process held the "
            "save lock — that process is entitled to be appending right now, "
            "and the `mv` would erase whatever it wrote (#223)"
        )
        assert "SKIPPED commit" in _log_text(project), (
            "skipping leaves a duplicate span in today-*.md; a duplicate that "
            "is never logged is indistinguishable from a bug"
        )

    def test_commit_skips_when_now_md_shrank_below_the_snapshot(self, tmp_path):
        """A stale offset must not be acted on: tailing past EOF yields nothing."""
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"

        env = dict(env)
        # Shorter than the now.md the parent snapshotted (36 bytes in this
        # harness), which is the whole point: the snapshot offset now points
        # past this file's end.
        env["STUB_REPLACE_DURING_NDC"] = "- post-rotation\n"

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(
            result, project / ".remember"
        )

        remaining = _wait_for_background_ndc(memory_file)
        assert "post-rotation" in remaining, (
            "now.md shrank below the pre-Haiku snapshot offset, so `tail -c +N` "
            "read past its end and produced nothing — and the `mv` installed "
            "that emptiness over content the offset never described (#223)"
        )
        assert "SKIPPED commit" in _log_text(project), (
            "acting on a stale offset must be refused out loud, not silently"
        )
