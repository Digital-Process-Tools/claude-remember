"""A digits-only cooldown marker AHEAD of now sticks the throttle ON (#326).

#322/#324/#327 taught the four cooldown gates to reject a marker that is not a
number. None of them bounds the number. `case "$X" in ''|*[!0-9]*) X=0 ;; esac`
accepts `99999999999999999999` and `9223372036854775807` as readily as it
accepts a real epoch, and both produce a NEGATIVE `ELAPSED`:

    $ M=9223372036854775807; echo $(( $(date +%s) - 10#$M ))
    -9223372035068589843

`[ "$ELAPSED" -lt "$SAVE_COOLDOWN" ]` is then true and the gate takes its
`exit 0` — which sits ABOVE the line that rewrites the marker. So the self-heal
every one of those four fixes rests on is unreachable on exactly the path that
needs it, and the throttle stays engaged until the wall clock catches up.

That is a different class from #322. A syntax-error marker self-heals and leaves
one line in the log; this one is PERMANENT and MUTE. Class `fails-to-preserve`:
the data is still on the machine, what stops is the preservation.

How a marker gets ahead of now without anybody corrupting anything: an NTP step
backwards, a VM snapshot restore, a container clock jump, a store on a share
with a skewed clock. Not a timezone or DST change — epoch seconds do not move
for those, which is why proceeding here costs at most one extra save rather than
mis-throttling a laptop that suspended over a TZ boundary.

WHAT IS PINNED, AND WHY IT IS BEHAVIOUR RATHER THAN A DIAGNOSTIC
================================================================
The issue is explicit that a diagnostic alone is the wrong pin here: falling
back to 0 makes a corrupt marker read as "very old", so corrupt and
clean-but-ancient reach the same decision. A future-dated marker is different —
it reaches the OPPOSITE decision from every other input — so the observable is
the one that matters: does the second run still do the work.

Each gate is pinned twice, and both halves have to hold:

  * the work happens (a save extracts, NDC compresses, the backup commits), and
  * the marker is left holding a sane value, so the run after this one is
    throttled normally instead of skipping again.

The diagnostic is pinned SEPARATELY, in its own tests, so a reworded message
cannot take the behaviour assertions down with it — and so that a silent clamp
cannot pass. A silent clamp trades a mute stuck throttle for a mute wrong value,
which is the same defect one layer along.

CONTROLS
========
`test_a_marker_written_now_still_throttles` and its NDC/backup siblings are
green against the UNFIXED scripts on purpose. Without them a "fix" that simply
retires the cooldown would satisfy everything above.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from subprocess_helpers import subprocess_failure_detail
from test_save_session_gates import _make_env, _run
from test_save_session_marker_arithmetic_322 import _set_cooldowns

# Three ways to land ahead of `now`, kept as three because they fail for two
# different reasons and a fix could plausibly catch one and miss the others.
#
#   3600                  a plain clock step backwards. Still a valid epoch.
#   9223372036854775807   INT64_MAX. Digits only, so the case guard passes it.
#   99999999999999999999  wider than int64; `10#` wraps it. Measured negative
#                         on both bash 3.2.57 and 5.2.37 (the issue's own
#                         transcript) — asserted below rather than assumed,
#                         because a wrap that landed positive would make this
#                         parameter vacuous.
OVERFLOW_MARKERS = ["9223372036854775807", "99999999999999999999"]


def _future(seconds: int) -> str:
    return str(int(time.time()) + seconds)


def _elapsed_is_negative(marker: str) -> bool:
    """Does this marker actually reach the gate as a negative ELAPSED?

    A parameter that cannot fail is not a test. Computed the way bash computes
    it, in Python's unbounded ints wrapped to int64, so an overflow value that
    happened to land positive is skipped loudly instead of passing silently.
    """
    value = int(marker)
    wrapped = ((value + 2 ** 63) % 2 ** 64) - 2 ** 63
    return int(time.time()) - wrapped < 0


@pytest.mark.parametrize("marker_value", OVERFLOW_MARKERS)
def test_the_overflow_parameters_really_do_go_negative(marker_value):
    assert _elapsed_is_negative(marker_value), (
        f"{marker_value!r} does not produce a negative ELAPSED on this "
        "platform, so every case parametrized on it below is vacuous"
    )


def _hook_errors(project: Path) -> str:
    log = project / ".remember" / "logs" / "hook-errors.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""


# ═════════════════════════════════════════════════════════════════════════════
# save-session.sh — the save cooldown gate
# ═════════════════════════════════════════════════════════════════════════════
#
# Named rather than numbered, here and below. A line number in a comment is
# stale the next time anyone edits above it — and three of the four in the first
# draft of this file were already wrong by the time it was committed. This PR
# exists because a comment outlived the truth; it should not add more of them.

@pytest.mark.parametrize("marker_value", [None, *OVERFLOW_MARKERS])
def test_a_future_dated_save_marker_does_not_stick_the_throttle(tmp_path, marker_value):
    """The headline. Nothing is ever saved again while the marker is ahead."""
    stamp = _future(3600) if marker_value is None else marker_value
    env, project, plugin, calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    _set_cooldowns(env, plugin, save_seconds=120, ndc_seconds=999999)
    (project / ".remember" / "tmp" / "last-save-ts").write_text(stamp)

    proc = _run(plugin, env, session_id)

    assert proc.returncode == 0, subprocess_failure_detail(
        proc, project / ".remember")
    ran = calls.read_text() if calls.is_file() else ""
    assert "extract" in ran, (
        f"a cooldown marker holding {stamp!r} — ahead of now, and all digits so "
        "the guard accepts it — made ELAPSED negative, which reads as 'inside "
        "the window' and takes the exit 0. That exit sits above the line that "
        "rewrites the marker, so no save will ever run again until the wall "
        f"clock passes the marker. calls: {ran!r}"
    )


def test_a_future_dated_save_marker_is_reset_to_a_usable_value(tmp_path):
    """Self-heal, the other half. Proceeding once is not enough if the marker
    is left ahead: the NEXT run reads the same value and skips again."""
    env, project, plugin, _calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    _set_cooldowns(env, plugin, save_seconds=120, ndc_seconds=999999)
    marker = project / ".remember" / "tmp" / "last-save-ts"
    marker.write_text(_future(3600))

    _run(plugin, env, session_id)

    after = marker.read_text(encoding="utf-8").strip()
    assert after.isdigit(), f"marker is not a number after the run: {after!r}"
    assert int(after) <= int(time.time()) + 2, (
        f"the marker is still ahead of now ({after}) — the run got through but "
        "left the trap armed for the next one"
    )


def test_a_future_dated_save_marker_says_so(tmp_path):
    """A silent clamp trades a mute stuck throttle for a mute wrong value.

    hook-errors.log, not just the daily log: /remember:doctor reports 'Recent
    errors' out of it (#252/#260/#266), and a clock that stepped backwards far
    enough to disable saving is exactly what a user pastes that file to find.
    """
    env, project, plugin, _calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    _set_cooldowns(env, plugin, save_seconds=120, ndc_seconds=999999)
    (project / ".remember" / "tmp" / "last-save-ts").write_text(_future(3600))

    _run(plugin, env, session_id)

    errors = _hook_errors(project)
    assert "ahead of now" in errors, (
        "the marker was ahead of now and nothing said so. The value was "
        "rejected and replaced silently, which is the same shape as the bug: a "
        f"decision nobody can see.\n--- hook-errors.log ---\n{errors.strip()}"
    )


def test_a_marker_written_now_still_throttles(tmp_path):
    """CONTROL — green before the fix too. The range check must not become a
    way of turning the cooldown off for everybody."""
    env, project, plugin, calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    _set_cooldowns(env, plugin, save_seconds=3600, ndc_seconds=999999)
    (project / ".remember" / "tmp" / "last-save-ts").write_text(str(int(time.time())))

    proc = _run(plugin, env, session_id)

    assert proc.returncode == 0, subprocess_failure_detail(
        proc, project / ".remember")
    ran = calls.read_text() if calls.is_file() else ""
    assert "extract" not in ran, (
        f"a marker written seconds ago no longer engages the cooldown: {ran!r}"
    )
    assert "ahead of now" not in _hook_errors(project), (
        "a perfectly good marker was reported as being in the future"
    )


# ═════════════════════════════════════════════════════════════════════════════
# save-session.sh — the NDC compression gate
# ═════════════════════════════════════════════════════════════════════════════

def test_a_future_dated_ndc_marker_does_not_stop_compression(tmp_path):
    """Same shape, different consequence: now.md is never compressed again and
    grows without bound, which is the one file every later read walks."""
    env, project, plugin, _calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    # Without a haiku body the run exits at Step 6 and never reaches Step 8 —
    # the gate under test. #322's NDC case documents the same trap.
    env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- an entry\n"
    _set_cooldowns(env, plugin, save_seconds=0, ndc_seconds=3600)
    marker = project / ".remember" / "tmp" / "last-ndc.ts"
    marker.write_text(_future(7200))

    proc = _run(plugin, env, session_id)

    assert proc.returncode == 0, subprocess_failure_detail(
        proc, project / ".remember")
    after = marker.read_text(encoding="utf-8").strip()
    assert after.isdigit() and int(after) <= int(time.time()) + 2, (
        f"the NDC marker is still ahead of now ({after!r}) — the gate took the "
        "RUN_NDC=false branch, and the marker is only rewritten inside the "
        "branch it did not take, so compression never runs again"
    )


def test_a_future_dated_ndc_marker_says_so(tmp_path):
    env, project, plugin, _calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- an entry\n"
    _set_cooldowns(env, plugin, save_seconds=0, ndc_seconds=3600)
    (project / ".remember" / "tmp" / "last-ndc.ts").write_text(_future(7200))

    _run(plugin, env, session_id)

    errors = _hook_errors(project)
    assert "ahead of now" in errors, (
        "the NDC marker was ahead of now and nothing said so\n"
        f"--- hook-errors.log ---\n{errors.strip()}"
    )


def test_a_recent_ndc_marker_still_suppresses_compression(tmp_path):
    """CONTROL — green before the fix."""
    env, project, plugin, _calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5)
    env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- an entry\n"
    _set_cooldowns(env, plugin, save_seconds=0, ndc_seconds=3600)
    marker = project / ".remember" / "tmp" / "last-ndc.ts"
    written = str(int(time.time()))
    marker.write_text(written)

    _run(plugin, env, session_id)

    assert marker.read_text(encoding="utf-8").strip() == written, (
        "a marker written seconds ago no longer suppresses NDC — the gate has "
        "been disabled rather than bounded"
    )

# ═════════════════════════════════════════════════════════════════════════════
# scripts/post-tool-hook.sh — the fork throttle that reads the same marker
# ═════════════════════════════════════════════════════════════════════════════
#
# This one is deliberately NOT symmetrical with the other three, and the
# asymmetry is the design:
#
#   * it does not rewrite the marker. `tmp/last-save-ts` belongs to
#     save-session.sh, which stamps it on the path this hook unblocks. A second
#     writer on a per-tool-call path is a race for no gain.
#   * it emits no diagnostic. save-session.sh emits exactly one when it heals
#     the marker; emitting one here as well would put a line in
#     hook-errors.log on EVERY tool call for as long as the clock is behind.
#   * it therefore costs zero extra subprocess spawns and zero extra file
#     reads (#299/#330) — the range test reuses the value already computed.
#
# What it must do is decline to claim a cooldown it cannot substantiate, so the
# fork happens and save-session.sh gets the chance to heal.

from test_post_tool_cooldown import _reap, _run_post_tool


def test_the_fork_throttle_does_not_stick_on_a_future_marker(tmp_path):
    proc, remember = _run_post_tool(tmp_path, cooldown_ts=int(time.time()) + 3600)
    _reap(remember)

    assert proc.returncode == 0
    assert (remember / "tmp" / "save-session.pid").exists(), (
        "the hook read a marker an hour ahead of now, computed a negative "
        "ELAPSED, and took that for 'inside the cooldown'. No save is forked, "
        "so save-session.sh never runs, so the marker it would have healed "
        "stays ahead — the two throttles hold each other shut"
    )


def test_the_fork_throttle_still_suppresses_on_a_fresh_marker(tmp_path):
    """CONTROL — green before the fix (this is #125's own test, restated)."""
    proc, remember = _run_post_tool(tmp_path, cooldown_ts=int(time.time()))
    _reap(remember)

    assert proc.returncode == 0
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "the range check disabled the fork throttle instead of bounding it"
    )


# ═════════════════════════════════════════════════════════════════════════════
# hooks.d/after_save/50-git-backup.sh — #258's outage, through an accepted value
# ═════════════════════════════════════════════════════════════════════════════

from tests.test_git_backup_hook import hook_state
from tests.test_git_backup_silent_stops_257 import (
    _backup_config,
    _log_or_empty,
    _run_backup,
    _slug_is_committed,
    _store,
    _wait_until,
)


def test_a_future_dated_backup_marker_does_not_stop_the_backup(tmp_path):
    """#258's original outage, reached through a value the guard ACCEPTS.

    `exit 0` at the cooldown sits above every path that stamps the marker
    (`_gb_stamp_cooldown` and both its call sites), so once it is ahead of now
    the store is never committed again — the git history simply stops, and the
    only trace is the absence of commits.
    """
    home, remember, _remote, slug_dir, project = _store(tmp_path)
    marker = hook_state(remember, ".last-git-backup-ts", create_dir=True)
    marker.write_text(str(int(time.time()) + 3600), encoding="utf-8")

    proc = _run_backup(slug_dir, project, home, _backup_config(tmp_path))

    assert proc.returncode == 0, proc.stderr.strip()
    assert _wait_until(lambda: _slug_is_committed(remember)), (
        "a cooldown marker an hour ahead of now stopped the git backup, and "
        "the marker is only rewritten below the exit it took — so it stays "
        "ahead and the store is never backed up again"
    )


def test_a_future_dated_backup_marker_says_so(tmp_path):
    home, remember, _remote, slug_dir, project = _store(tmp_path)
    hook_state(remember, ".last-git-backup-ts", create_dir=True).write_text(
        str(int(time.time()) + 3600), encoding="utf-8")

    _run_backup(slug_dir, project, home, _backup_config(tmp_path))

    assert _wait_until(lambda: "ahead of now" in _log_or_empty(slug_dir)), (
        "the backup cooldown marker was ahead of now and nothing said so\n"
        f"--- log ---{_log_or_empty(slug_dir)}"
    )


def test_a_fresh_backup_marker_still_suppresses_the_backup(tmp_path):
    """CONTROL — green before the fix."""
    home, remember, _remote, slug_dir, project = _store(tmp_path)
    hook_state(remember, ".last-git-backup-ts", create_dir=True).write_text(
        str(int(time.time())), encoding="utf-8")

    _run_backup(slug_dir, project, home, _backup_config(tmp_path))

    assert not _slug_is_committed(remember), (
        "a marker written seconds ago no longer suppresses the backup — the "
        "cooldown has been disabled rather than bounded"
    )
