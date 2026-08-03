"""The `now.md` entry must land in one atomic step, because its readers cannot lock (#247).

`save-session.sh` appended an entry in **two** operations:

    echo "" >> "$MEMORY_FILE" 2>/dev/null || { log "write" "ERROR: ..."; exit 1; }   # 435
    cat "$HAIKU_TEXT_FILE" >> "$MEMORY_FILE"                                          # 436

Both run under `save.lock`, which is correct and which excludes other *writers*.
It excludes no reader, and the reader that matters cannot take it:
`session-start-hook.sh` sources exactly `resolve-paths.sh`, `detect-tools.sh`,
`bootstrap-dirs.sh`, `log.sh` and `lib-env-cache.sh` — never `lib-lock.sh` — so
`lock_acquire` is not merely uncalled on that path, it is *undefined in that
process*. Line 385 is a bare `cat "$MFILE"` that injects `now.md` straight into
the new session's context.

Between the two appends there is a whole `fork`+`exec` of `cat`, during which
`now.md` ends in a separator whose entry has not arrived; inside the second
append there is one visible state per write chunk. A session start landing in
either window is handed a truncated final entry with nothing to tell it so.

**Scope, honestly.** This is a torn entry *boundary*, not loss: every byte
reaches disk and the next read sees the file whole. It is not #242's severity
and this test suite does not claim it.

**Why not a reader lock.** #227 measured the hook path at a p50 of 8.7s on
Windows, #230 cut the per-tool-call prefix, and #204 is an external report of
this plugin blocking a user's prompts. Putting session start behind a lock held
by a process that calls Haiku is a worse trade than the tear. The writer carries
the atomicity instead — sibling temp, then `rename(2)`, the pattern this repo
already uses in `lib-env-cache.sh:170-175` ("Rename, so no reader ever parses a
partial file"), `post-tool-hook.sh:260-262`, the NDC commit (#245) and the
consolidation staging tail (#249). This was the fifth call site, and it had not
adopted it.

A rename is not a cheaper way of saying "one `cat` instead of two". A single
`cat` of separator-plus-entry narrows the window to the chunk boundaries inside
one `write(2)` loop; a rename removes it. The reader either opens the old inode
and reads it whole, or opens the new one and reads it whole. There is no third
state to observe, at any entry size, on any filesystem.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .test_save_session_gates import _make_env, _run, _saved_position, _suppress_ndc
from .test_ndc_commit_atomicity import _with_mv_stub, _commit_calls, _logs
from .subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

PRE = "## 09:00 | main\n\n- an entry that was already in memory\n"

# Large enough that the second append is several `write(2)` chunks, so a reader
# has many observable intermediate sizes and not only the one the process gap
# between `echo` and `cat` creates. This size is load-bearing: shrink it and a
# fix that merely collapsed the two appends into one `cat` would start passing.
#
# It reaches the stub through a FILE, never through the environment. Linux caps
# each single string in argv/envp at MAX_ARG_STRLEN (32 pages = 128KB) whatever
# ARG_MAX allows, so passing this as STUB_HAIKU_TEXT killed `bash` with
# `OSError: [Errno 7] Argument list too long` on all four ubuntu legs while
# macOS (no per-string cap) and Windows stayed green. That is precisely the
# limit #107 moved the real summarizer prompt onto stdin to avoid
# (pipeline/haiku.py:515) — this fixture reproduced, from the other side, the
# hazard the production path is built around.
BODY_LINE = "- a line of the entry being written right now\n"
ENTRY = "## 11:30 | main\n\n" + BODY_LINE * 6000

# A reader that hammers `stat` on now.md and records every distinct size it
# sees. Size is the right probe for an append: the file grows monotonically from
# the old content to the new, so *any* size strictly between the two is a state
# in which a reader would have got a truncated final entry. `stat` is a single
# syscall, so this polls fast enough to catch the fork+exec gap between the two
# appends with certainty rather than with luck — that gap is milliseconds and
# holds a file that ends in a bare separator.
READER = r"""
import json, os, sys, time

path, out, ready, stop = sys.argv[1:5]
seen = {}

def observe():
    try:
        size = os.stat(path).st_size
    except OSError:
        return
    if size in seen:
        return
    tail = b""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - 80))
            tail = f.read()
    except OSError:
        pass
    seen[size] = tail.decode("utf-8", "replace")

open(ready, "w").close()
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    observe()
    if os.path.exists(stop):
        observe()
        break

with open(out, "w") as f:
    json.dump({str(k): v for k, v in seen.items()}, f)
"""


def _append_env(tmp_path):
    """Harness env that writes one real entry and does not run compression."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    env["STUB_HAIKU_SKIP"] = "0"
    entry_file = tmp_path / "entry.md"
    entry_file.write_text(ENTRY, encoding="utf-8")
    env["STUB_HAIKU_TEXT_FILE"] = str(entry_file)
    memory_file = project / ".remember" / "now.md"
    memory_file.write_text(PRE, encoding="utf-8")
    # NDC would replace now.md in a disowned subshell moments after the append,
    # which is a second writer this suite is not about — every assertion below
    # would then be racing it.
    _suppress_ndc(project)
    return env, project, plugin, calls, sid, memory_file


class TestNowMdAppendIsAtomic:

    def test_a_reader_polling_across_the_append_never_sees_a_torn_file(self, tmp_path):
        """The defect itself: a lock-less reader must never observe a partial entry.

        This is the assertion that would still fail if the fix only *narrowed*
        the window — collapsing the two appends into one `cat` leaves the chunk
        boundaries of a single write loop observable, and `ENTRY` is sized to
        make those boundaries real.

        The reader runs for the whole save and records every distinct file size.
        Two things are then required of it, and the second is what stops this
        test from passing vacuously: every size observed must be either the old
        file or the new one, **and** both of those must actually appear — a
        reader that started late, died early, or never looked proves nothing and
        must fail rather than pass quietly.
        """
        env, project, plugin, calls, sid, memory_file = _append_env(tmp_path)

        reader_py = tmp_path / "reader.py"
        reader_py.write_text(READER, encoding="utf-8")
        out = tmp_path / "observations.json"
        ready = tmp_path / "reader-ready"
        stop = tmp_path / "reader-stop"

        reader = subprocess.Popen(
            [sys.executable, str(reader_py), str(memory_file), str(out),
             str(ready), str(stop)],
        )
        try:
            deadline = time.monotonic() + 30
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "the reader subprocess never started"

            result = _run(plugin, env, sid)
            assert result.returncode == 0, subprocess_failure_detail(
                result, project / ".remember"
            )
        finally:
            stop.write_text("")
            reader.wait(timeout=30)

        observed = {int(k): v for k, v in json.loads(out.read_text()).items()}

        old_size = len(PRE.encode())
        # The separator is one byte; the entry's header line is rewritten to the
        # current time by the #139 normalizer, which is length-preserving.
        new_size = old_size + 1 + len(ENTRY.encode())
        assert memory_file.stat().st_size == new_size, (
            "the final now.md is not old + separator + entry, so the sizes this "
            "test reasons about are not the ones the code produces — fix the "
            "test before trusting its verdict. "
            f"expected={new_size} actual={memory_file.stat().st_size}"
        )

        assert old_size in observed and new_size in observed, (
            "the reader did not observe both sides of the transition, so it "
            "cannot have been looking while the entry landed and this test "
            "proves nothing. "
            f"old={old_size} new={new_size} observed={sorted(observed)}"
        )

        torn = {size: tail for size, tail in observed.items()
                if size not in (old_size, new_size)}
        assert not torn, (
            "a reader observed now.md part-way through the append. Each of these "
            "sizes is a state a session start could have injected into a new "
            "session's context: a file ending in a separator with no entry, or "
            "an entry cut mid-line, with nothing to tell the session it was "
            "reading a fragment. session-start-hook.sh cannot take save.lock — "
            "it never sources lib-lock.sh — so the write has to be atomic on "
            "the writer's side: sibling temp, then rename. "
            f"old={old_size} new={new_size} torn={ {k: v[-60:] for k, v in sorted(torn.items())} }"
        )

    def test_the_entry_is_committed_by_rename_not_written_in_place(self, tmp_path):
        """The structural form of the same claim, with no timing in it at all.

        Any write that keeps now.md's inode is a write a concurrent `open` can
        land inside. A rename installs a wholly-built file under the path in one
        step: a reader holds either the old inode or the new one, and both are
        complete. That property is what makes the polling test above impossible
        to fail, so it is pinned directly — a future refactor back to `>>` would
        turn that test flaky before it turned it red, and would fail this one
        immediately.
        """
        env, project, plugin, calls, sid, memory_file = _append_env(tmp_path)
        before = memory_file.stat()

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(
            result, project / ".remember"
        )

        after = memory_file.stat()
        assert memory_file.read_text(encoding="utf-8").endswith(BODY_LINE), (
            "the entry did not reach now.md at all"
        )
        assert memory_file.read_text(encoding="utf-8").startswith(PRE), (
            "the previous content of now.md was not preserved — an append that "
            "loses the file it appends to is worse than the tear this fixes"
        )
        assert (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev), (
            "now.md still has the inode it had before the entry was written, so "
            "the entry was written into the live file rather than renamed over "
            "it. Every byte of that write is observable by session-start-hook's "
            f"bare `cat`, which cannot lock. ino {before.st_ino} -> {after.st_ino}"
        )

    def test_the_commit_temp_is_a_sibling_of_now_md(self, tmp_path):
        """#242's lesson, applied here before it can be relearned.

        A rename is only `rename(2)` within one filesystem. A temp under
        `$TMPDIR` makes the commit a copy-then-unlink wherever `$TMPDIR` is
        elsewhere — tmpfs `/tmp` on Fedora/Arch/RHEL, any devcontainer, WSL with
        the project under `/mnt/c`, external `data_dir` mode — which would trade
        a torn *read* for a destroyed *file*. That is a strictly worse bug than
        the one being fixed, and it is exactly what shipped in #142.
        """
        env, project, plugin, calls, sid, memory_file = _append_env(tmp_path)
        day_file = project / ".remember" / "tmp" / "now-day"
        env = _with_mv_stub(env, tmp_path, "passthrough", memory_file, day_file,
                              src_match="*.append-*")

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(
            result, project / ".remember"
        )

        commits = _commit_calls(tmp_path)
        assert commits, (
            "no rename targeted now.md, so the entry was not committed by "
            "rename at all — see test_the_entry_is_committed_by_rename"
        )
        src, dst = commits[-1]
        assert os.path.dirname(src) == os.path.dirname(dst), (
            "the append temp is not a sibling of now.md, so the commit is a "
            "copy-then-unlink and not a rename. Across filesystems that "
            "destroys now.md on a partial failure instead of leaving it old or "
            f"new. src={src!r} dst={dst!r}"
        )

    def test_a_failed_commit_leaves_now_md_and_the_position_untouched(self, tmp_path):
        """A commit that does not land must not be reported as one.

        The position is saved immediately after the append and is what tells the
        next run the span is done. Advancing it for an entry that never reached
        now.md drops that span from memory permanently and silently — #243's
        shape, one file over. On failure the old content must still be there,
        the cursor must not move, and the log must say so.
        """
        env, project, plugin, calls, sid, memory_file = _append_env(tmp_path)
        day_file = project / ".remember" / "tmp" / "now-day"
        env = _with_mv_stub(env, tmp_path, "fail", memory_file, day_file,
                              src_match="*.append-*")

        result = _run(plugin, env, sid)

        assert memory_file.read_text(encoding="utf-8") == PRE, (
            "now.md is not the content it held before a commit that failed"
        )
        assert _saved_position(project) is None, (
            "the position advanced past a span whose entry never reached "
            "now.md. That span is now invisible to every future extract: the "
            "summary is gone and nothing logged a loss"
        )
        assert "ERROR" in _logs(project), (
            "a failed write to now.md was not logged — silent is the one thing "
            "this file has been hardened against three times (#142, #225, #235)"
        )
        assert result.returncode != 0, (
            "the run reported success for a save that did not save"
        )

    def test_a_failed_commit_leaves_no_stray_temp(self, tmp_path):
        """The cost of a sibling temp is a stray in the user's memory directory.

        Inert there — `.remember/.gitignore` is `*`, doctor.sh globs `now.md`
        exactly, and the name deliberately does not end in `.md` so neither
        `today-*.md` nor session-start injection can see it — but one per
        failure, forever, is still a leak.
        """
        env, project, plugin, calls, sid, memory_file = _append_env(tmp_path)
        day_file = project / ".remember" / "tmp" / "now-day"
        env = _with_mv_stub(env, tmp_path, "fail", memory_file, day_file,
                              src_match="*.append-*")

        _run(plugin, env, sid)

        beside = sorted(p.name for p in (project / ".remember").glob("now.md.*"))
        in_tmpdir = sorted(p.name for p in Path(env["TMPDIR"]).glob("remember-*"))
        assert not beside and not in_tmpdir, (
            f"a failed commit orphaned its temp. beside={beside} tmpdir={in_tmpdir}"
        )


class TestTheReaderStillCannotLock:
    """The premise, pinned so a later change cannot quietly invert it.

    This passes before the fix as well as after — it is not a regression test
    for the defect, it is a guard on the *reason* the fix lives on the writer's
    side. If someone ever answers a torn read by making session start acquire
    `save.lock`, this fails and says why that is the worse trade.
    """

    def test_session_start_hook_takes_no_lock_another_script_can_hold(self):
        """Originally "does not source lib-lock.sh at all", which #297 had to
        narrow: session start now rewrites the store-root session index under
        `sessions.lock`, a lock no other script takes and which is held across
        one awk and one mv.

        The danger this guards is unchanged, and it is about WHICH lock rather
        than about locking. If session start takes `save.lock`, a SessionStart
        hook blocks the user's first prompt behind a lock held for the whole of
        a save — including its summarize Haiku call. #227 measured that path at
        a p50 of 8.7s already, #230 exists to cut its prefix, and #204 is a
        user reporting this plugin blocking them. The writer carries now.md's
        atomicity instead (#247); the reader stays lean.

        So the assertion is the exhaustive one: session start may name exactly
        one lock, and it is its own.
        """
        hook = (Path(__file__).resolve().parent.parent
                / "scripts" / "session-start-hook.sh").read_text()
        named = set(re.findall(r"[A-Za-z0-9_.-]+\.lock", hook))
        assert named <= {"sessions.lock"}, (
            f"session-start-hook.sh names {sorted(named - {'sessions.lock'})}. "
            "A SessionStart hook that waits on a lock another script holds "
            "blocks the user's first prompt for as long as that script runs — "
            "for save.lock, the whole of a save including its Haiku call."
        )

    def test_the_lock_session_start_does_take_is_bounded(self):
        """And bounded by a constant small enough to read, because the failure
        this cannot have is a session start that waits. The index is a way to
        find the per-project record, not a second source of truth, so a session
        that cannot take the lock writes no row and the next one repairs it."""
        hook = (Path(__file__).resolve().parent.parent
                / "scripts" / "session-start-hook.sh").read_text()
        assert 'lock_acquire "$_lock" "$SLUG_INDEX_LOCK_TIMEOUT"' in hook
        timeout = re.search(r"^SLUG_INDEX_LOCK_TIMEOUT=(\d+)$", hook, re.M)
        assert timeout and int(timeout.group(1)) <= 5, (
            "the session-index lock timeout is unbounded or large; session "
            "start is a path the user waits on"
        )

    def test_session_start_hook_reads_now_md_unsynchronised(self):
        hook = (Path(__file__).resolve().parent.parent
                / "scripts" / "session-start-hook.sh").read_text()
        assert 'cat "$MFILE"' in hook, (
            "the memory injection loop no longer reads now.md with a bare cat. "
            "That is fine, but #247's fix is justified by this being an "
            "unsynchronised read — re-check the reasoning before deleting it."
        )
