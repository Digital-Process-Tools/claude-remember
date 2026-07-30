"""The NDC commit must be an atomic swap, and must not lie when it is not (#242, #243).

`save-session.sh` ends its compression round by replacing `now.md` with the tail
that was appended after the snapshot offset:

    NDC_TAIL=$(mktemp "${TMPDIR:-/tmp}"/remember-ndc-tail-XXXXXX)   # 602
    if tail -c +$(( NDC_SRC_BYTES + 1 )) "$MEMORY_FILE" > "$NDC_TAIL"; then
        NDC_KEPT=$(wc -c < "$NDC_TAIL" | tr -d ' ')                 # 604
        mv "$NDC_TAIL" "$MEMORY_FILE"                               # 605

#142's design argument for why that commit is safe is one sentence: "mv-over is
atomic on the same filesystem", and its own suggested fix wrote `$MEMORY_FILE.tmp`
— beside the target, where that argument holds. The shipped code put the temp in
`$TMPDIR` and kept the argument.

**#242** — across filesystems `mv` is not `rename(2)`, it is copy-then-unlink, so
`now.md` is destroyed and rewritten in place rather than swapped. Reproduced
first-hand against a real second filesystem (a 20MB HFS+ RAM disk holding
REMEMBER_DIR, `$TMPDIR` on the main volume) with a genuine ENOSPC partway
through the copy. BSD `mv` — macOS — does not leave a prefix: it unlinks the
partial destination, so `now.md` was **gone entirely**, 1MB of memory with it.
GNU `mv` leaves the partial prefix instead. Neither is "the old content or the
new content".

**#243** — the `mv` result is never read, and the block runs under `set +e`, so
the code proceeds to the day marker regardless. `NDC_KEPT` was computed at 604
from the *temp file*, which still exists, so the failure arm is never even
plausible to the code: in the same reproduction it stamped `tmp/now-day` forward
from 2026-07-29 to 2026-07-30 and logged `kept 5400000b appended during
compression` — for a `now.md` that no longer existed. Memory destroyed, log
reads clean.

The two are one defect at the commit point and are fixed together: the temp
moves beside `now.md` so the `mv` is a real rename, and the rename's result
gates the day marker. A useful consequence of the first is that ENOSPC now
fails at the `tail` step instead, which already has a correct handler (#173) —
`now.md` untouched, marker untouched, logged.

The repo already models this exact pattern twice, so nothing here is invented:
`lib-env-cache.sh:155` (`_t="${_REMEMBER_ENV_CACHE_FILE}.$$"`, sibling temp,
`mv -f ... || rm -f`, commented "Rename, so no reader ever parses a partial
file") and `post-tool-hook.sh:260-262` (sibling temp, checked `mv`, cleanup on
failure).
"""

import os
import sys
from pathlib import Path

import pytest

from .test_save_session_gates import _make_env, _run  # shared harness
from .test_ndc_truncate_race import _ndc_env, _wait_for_background_ndc
from .subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

# Intercepts ONLY the NDC commit — the one `mv` whose destination is now.md.
# Everything else passes through to the real binary, which matters more here than
# it looks: lib-lock.sh's take-over and adoption paths are built out of `mv`, and
# a stub that swallowed those would break locking rather than test the commit.
#
# `command -p` resolves against the shell's default PATH, not the one this stub
# directory was prepended to, so it cannot recurse into itself. It is deliberately
# not prefixed with `exec` — Debian's dash (/bin/sh on every Ubuntu runner)
# implements `exec` as a PATH search for an executable with no builtin fallback,
# which dies with exit 127 before any mv runs. See test_ndc_tail_failure_no_truncate.
#
# Before intervening it snapshots the three things the assertions need, taken
# synchronously inside the call so they are race-free ground truth: what now.md
# held the instant before the commit, what the tail held, and what the day marker
# read. Reading any of those from the test process is not trustworthy — the NDC
# subshell is backgrounded and disowned.
#
# STUB_MV_MODE:
#   xdev  — model a cross-filesystem mv when src and dst are in different
#           directories: truncate the destination, write a prefix, fail. That is
#           GNU coreutils' observable outcome; BSD additionally unlinks the
#           partial destination (verified on macOS against a real RAM disk).
#           Modelling the *partial* is the stricter case, and it is the one the
#           "wholly old or wholly new" assertion exists to catch.
#           Same directory => real rename, which cannot fail this way at all.
#   fail  — fail cleanly without touching the destination (ENOSPC on a full
#           volume, EPERM, an unlink race). Used for the ordering assertions.
#
# STUB_MV_SRC_MATCH narrows the interception to a source-name glob, because the
# destination alone stopped identifying the NDC commit: since #247 the ordinary
# entry append also commits by renaming a sibling temp over now.md, so a stub
# keyed only on the destination fails that append instead and the run never
# reaches compression at all. The two temps are named apart on purpose —
# now.md.ndc-* and now.md.append-* — and each suite intercepts only its own.
MV_STUB = r"""#!/bin/sh
_last=""
_prev=""
for _a in "$@"; do _prev="$_last"; _last="$_a"; done

_matched=0
if [ -n "$STUB_MV_TARGET" ] && [ "$_last" = "$STUB_MV_TARGET" ]; then
    case "$_prev" in
        ${STUB_MV_SRC_MATCH:-*}) _matched=1 ;;
    esac
fi

if [ "$_matched" = 1 ]; then
    printf '%s\t%s\n' "$_prev" "$_last" >> "$STUB_MV_LOG"
    [ -n "$STUB_MV_SNAP_NOW" ]  && cp "$_last" "$STUB_MV_SNAP_NOW"  2>/dev/null
    [ -n "$STUB_MV_SNAP_TAIL" ] && cp "$_prev" "$STUB_MV_SNAP_TAIL" 2>/dev/null
    [ -n "$STUB_MV_SNAP_DAY" ]  && cp "$STUB_MV_DAY_FILE" "$STUB_MV_SNAP_DAY" 2>/dev/null

    case "$STUB_MV_MODE" in
        fail)
            exit 1
            ;;
        xdev)
            if [ "$(dirname "$_prev")" != "$(dirname "$_last")" ]; then
                : > "$_last"
                head -c 32 "$_prev" >> "$_last" 2>/dev/null
                exit 1
            fi
            ;;
    esac
fi
command -p mv "$@"
"""


def _with_mv_stub(env, tmp_path, mode, memory_file, day_file, src_match="*.ndc-*"):
    bindir = tmp_path / "mv-stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "mv"
    stub.write_text(MV_STUB, encoding="utf-8")
    stub.chmod(0o755)

    # Isolated TMPDIR so "did the commit orphan its temp" is answerable at all —
    # the shared system temp is full of other runs' files.
    isolated_tmp = tmp_path / "isolated-tmp"
    isolated_tmp.mkdir(exist_ok=True)

    env = dict(env)
    env["TMPDIR"] = str(isolated_tmp)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_MV_MODE"] = mode
    env["STUB_MV_TARGET"] = str(memory_file)
    env["STUB_MV_SRC_MATCH"] = src_match
    env["STUB_MV_LOG"] = str(tmp_path / "mv-calls.tsv")
    env["STUB_MV_SNAP_NOW"] = str(tmp_path / "snap-now.md")
    env["STUB_MV_SNAP_TAIL"] = str(tmp_path / "snap-tail.md")
    env["STUB_MV_SNAP_DAY"] = str(tmp_path / "snap-day")
    env["STUB_MV_DAY_FILE"] = str(day_file)
    return env


def _commit_calls(tmp_path):
    log = tmp_path / "mv-calls.tsv"
    if not log.is_file():
        return []
    return [
        line.split("\t")
        for line in log.read_text().splitlines()
        if "\t" in line
    ]


def _logs(project):
    files = list((project / ".remember" / "logs").glob("*.log"))
    return "\n".join(f.read_text() for f in files)


class TestNdcCommitAtomicity:

    def test_commit_temp_is_a_sibling_of_now_md(self, tmp_path):
        """#242: the temp must live beside now.md so the mv is a real rename.

        The cheap, fully portable form of the atomicity claim — and the one that
        holds on runners where a second filesystem cannot be mounted. Renaming
        within a directory is `rename(2)`; anything else is a copy, and a copy
        has a window in which now.md is neither its old self nor its new one.
        """
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"
        day_file = project / ".remember" / "tmp" / "now-day"
        env = _with_mv_stub(env, tmp_path, "passthrough", memory_file, day_file)

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_background_ndc(memory_file)

        commits = _commit_calls(tmp_path)
        assert commits, (
            "the NDC commit never ran — no mv targeted now.md, so this test "
            "proves nothing about where the temp lives"
        )
        src, dst = commits[-1]
        assert os.path.dirname(src) == os.path.dirname(dst), (
            "the NDC commit temp must be a sibling of now.md so the mv is a "
            "rename(2) and not a copy-then-unlink. #142's safety argument is "
            "'mv-over is atomic on the same filesystem' and its own suggested "
            "fix wrote \"$MEMORY_FILE.tmp\"; a temp under $TMPDIR makes that "
            "argument false wherever $TMPDIR is a different filesystem — tmpfs "
            "/tmp on Fedora/Arch/RHEL, any devcontainer, WSL with the project "
            f"under /mnt/c, or external data_dir mode. src={src!r} dst={dst!r}"
        )

    def test_now_md_is_never_partial_when_the_commit_fails(self, tmp_path):
        """#242: a failed commit must leave now.md wholly old or wholly new.

        With the temp under $TMPDIR the commit is a copy, and this stub models
        what a copy does when it fails partway. With the temp beside now.md the
        same stub takes its same-directory branch and performs a real rename,
        which has no partial state to observe.
        """
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"
        day_file = project / ".remember" / "tmp" / "now-day"
        env = _with_mv_stub(env, tmp_path, "xdev", memory_file, day_file)
        # Force a non-empty tail. Without this NDC_KEPT is 0 — the common case —
        # the commit installs an empty file, and "a partial prefix of the tail"
        # is byte-identical to "the whole tail": the corruption this test exists
        # to catch would be invisible. The #142 window is exactly how a real
        # now.md comes to have a tail worth keeping.
        env["STUB_APPEND_DURING_NDC"] = (
            "\n## 11:30 | main\n\n" + "- an entry written while NDC was compressing\n" * 200
        )

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_background_ndc(memory_file)

        snap_now = tmp_path / "snap-now.md"
        snap_tail = tmp_path / "snap-tail.md"
        assert snap_now.is_file(), (
            "the mv stub never saw a commit targeting now.md — the run did not "
            "reach the NDC commit, so this test proves nothing"
        )

        old = snap_now.read_bytes()
        new = snap_tail.read_bytes() if snap_tail.is_file() else None

        assert memory_file.exists(), (
            "now.md does not exist after a failed commit. A cross-filesystem mv "
            "opens the destination with O_TRUNC and copies; BSD mv then unlinks "
            "the partial destination outright. Verified first-hand on macOS "
            "against a real second filesystem: a 1MB now.md was gone entirely "
            "and the run logged 'kept 5400000b appended during compression'"
        )
        after = memory_file.read_bytes()
        assert after in (old, new), (
            "now.md is neither the content it held before the commit nor the "
            "content the commit was installing — it is a fragment of the copy "
            "that failed partway. This is the memory file; a prefix of a tail "
            "is a user losing part of their session permanently, with no way "
            "to know it happened. The commit must be an atomic swap, which "
            "means the temp must be a sibling of the target. "
            f"len(before)={len(old)} len(intended)={len(new) if new is not None else '?'} "
            f"len(after)={len(after)} after={after[:120]!r}"
        )

    def test_day_marker_is_untouched_when_the_commit_fails(self, tmp_path):
        """#243, asserted separately from byte integrity: the marker must not
        move before the bytes have.

        When the commit fails, now.md still holds every byte it held — including
        the span already appended to today-*.md. The #141 day marker describes
        that content and is still correct. Rewriting or deleting it is the actual
        damage: with NDC_KEPT == 0 (the common case) the shipped code runs
        `rm -f "$NOW_DAY_FILE"`, so the next round re-stamps content that is
        still entirely in now.md with whatever day it happens to run on, and a
        session that crossed midnight files yesterday's entries under today.

        This is deliberately a separate test from the byte-integrity one: a fix
        that gets the bytes right and the ordering wrong must still fail.
        """
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"
        day_file = project / ".remember" / "tmp" / "now-day"
        env = _with_mv_stub(env, tmp_path, "fail", memory_file, day_file)

        # The marker must already name a day that is NOT today, or this test
        # cannot tell "left alone" from "stamped forward": save-session.sh writes
        # the marker only when now.md is empty (line 432), so the harness would
        # otherwise leave it holding today's date — the same value a buggy
        # failure arm would write, and the assertion would pass against the bug.
        # A past day is also the real #141 case: now.md holding yesterday's
        # entries is exactly why this marker exists.
        memory_file.write_text("## 09:00 | main\n\n- yesterday's work\n")
        day_file.write_text("2020-01-01\n")

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_background_ndc(memory_file)

        snap_day = tmp_path / "snap-day"
        assert (tmp_path / "snap-now.md").is_file(), (
            "the mv stub never saw a commit targeting now.md — the run did not "
            "reach the NDC commit, so this test proves nothing"
        )

        before = snap_day.read_bytes() if snap_day.is_file() else b""
        after = day_file.read_bytes() if day_file.exists() else b""
        assert after == before == b"2020-01-01\n", (
            "the #141 day marker changed even though the commit failed and "
            "now.md still holds the content the old marker described. The "
            "marker must not move before the bytes have — deleting it "
            "(NDC_KEPT == 0) or stamping it forward (NDC_KEPT > 0) both misfile "
            "memory that never moved. "
            f"before={before!r} after={after!r}"
        )

    def test_failed_commit_is_logged(self, tmp_path):
        """A silent failure here is worse than the failure.

        The whole cost of #243 is that the log reads clean: `kept ${NDC_KEPT}b
        appended during compression` is emitted from a byte count taken off the
        temp file *before* the mv, so it is printed whether or not the commit
        landed. Trading the failed commit for a quiet one — suppressing it, or
        defaulting to 'assume it worked' — converts "your memory was truncated"
        into "your memory silently isn't what you think", which is the one class
        this repo has hardened this file against three times (#142, #225, #235).
        """
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"
        day_file = project / ".remember" / "tmp" / "now-day"
        env = _with_mv_stub(env, tmp_path, "fail", memory_file, day_file)

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_background_ndc(memory_file)

        log_text = _logs(project)
        assert "commit failed" in log_text, (
            "expected an error log naming the failed commit; a failed truncate "
            f"that logs nothing is indistinguishable from a clean run. got: {log_text!r}"
        )
        # The success line must not be emitted for a commit that did not happen.
        assert "appended during compression" not in log_text, (
            "the run logged the success line for a commit that failed — this is "
            "the exact 'memory misfiled, log says fine' signature of #243"
        )

    def test_failed_commit_leaves_no_stray_temp(self, tmp_path):
        """The temp must be cleaned up on the failure arm.

        `test_tmpdir_cleanup.py` already pins 'no remember-* may survive in
        TMPDIR' for the merged-config file; the commit temp is subject to the
        same invariant. Moving it beside now.md changes where a stray would
        land, not whether one is acceptable — a stray in the user's memory
        directory is the cost of the sibling temp, and it is this branch that
        pays it off.
        """
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"
        day_file = project / ".remember" / "tmp" / "now-day"
        env = _with_mv_stub(env, tmp_path, "fail", memory_file, day_file)

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        _wait_for_background_ndc(memory_file)

        beside = sorted(p.name for p in (project / ".remember").glob("now.md.*"))
        in_tmpdir = sorted(
            p.name for p in Path(env["TMPDIR"]).glob("remember-ndc-tail-*")
        )
        assert not beside and not in_tmpdir, (
            "a failed commit orphaned its temp. Today it leaks into $TMPDIR — "
            "verified first-hand in the RAM-disk reproduction, where the failed "
            "cross-device mv left remember-ndc-tail-NGFUrW behind. After the fix "
            "a stray would land beside now.md instead; it is inert there "
            "(.remember/.gitignore is '*', and nothing globs now.md.*), but "
            "either way it accumulates one file per failure forever. "
            f"beside={beside} in_tmpdir={in_tmpdir}"
        )
