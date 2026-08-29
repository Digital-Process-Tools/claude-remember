"""Regression tests for #373: with handoff_mode: "per_session",
remember.delivered.<session_id> files accumulate forever in
$REMEMBER_DIR/tmp -- nothing ever pruned them, the same directory (and
class of leak) #362 already fixed once, under a different filename.

The chosen coupling is deliberately NOT the paired remember.<session_id>.md
handoff slot -- that survives forever on purpose (#221), so tying the
record's life to it would reproduce unbounded growth under a new name. It is
coupled instead to the one fact that actually answers "is this session
over": whether Claude Code's own transcript for that session id still
exists under $SESSIONS_DIR (the same directory session-start-hook.sh already
reads via `previous_transcript`).

Four states, tested here as four cases over one fixture shape (a fourth,
#393, joined the original three below):
  - the session's transcript is gone AND its record is old enough to be
    outside the #393 startup grace window -> its delivery record is pruned
    (the "must fire" case)
  - the session's transcript is gone but its record is still inside that
    grace window -> its delivery record survives (#393's own "must not
    fire" case -- a session still starting up is indistinguishable from a
    dead one on the transcript check alone)
  - the session's transcript still exists -> its delivery record survives
    (the paired "must not fire" case -- the positive control that a broken
    or over-eager sweep would fail)
  - $SESSIONS_DIR itself cannot be listed at all -> nothing is pruned,
    because "could not tell whether the session is over" must never render
    as either "pruned" or "confirmed still active"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX session-start hook -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START_SCRIPT = REPO_ROOT / "scripts" / "session-start-hook.sh"

from pipeline.slug import session_dir_slug as _slug

# Mirrors the grace window scripts/session-start-hook.sh's #393 fix uses to
# arbitrate the sweep -- see the comment beside GRACE_MIN there for why this
# duration (reasoned, not measured against real Claude Code timing).
_GRACE_MIN = 5


def _age_record(record: Path, minutes: float) -> None:
    """Backdate a record's mtime so the #393 grace-window check reads it as
    old enough that "no transcript yet" can only mean "session is over" --
    never a session still inside its own startup window."""
    import time
    old = time.time() - (minutes * 60)
    os.utime(record, (old, old))


def _sandbox(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)

    cfg = {"features": {"recovery": False}, "handoff_mode": "per_session", "data_dir": ".remember"}
    (home / ".remember" / "config.json").write_text(json.dumps(cfg))

    slug = _slug(str(project))
    sessions_dir = home / ".claude" / "projects" / slug
    sessions_dir.mkdir(parents=True)

    remember_dir = project / ".remember"
    remember_dir.mkdir(parents=True, exist_ok=True)
    return project, home, remember_dir, sessions_dir


def _set_handoff_mode(home: Path, mode: str) -> None:
    cfg_path = home / ".remember" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["handoff_mode"] = mode
    cfg_path.write_text(json.dumps(cfg))


def _payload(session_id: str) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })


def _session_start(project: Path, home: Path, session_id: str | None) -> str:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
    }
    kwargs = {"env": env, "capture_output": True, "text": True, "timeout": 60}
    if session_id is not None:
        kwargs["input"] = _payload(session_id)
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    result = subprocess.run(
        ["bash", str(SESSION_START_SCRIPT)], check=False, **kwargs
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr[:500]}"
    return result.stdout


def _seed_delivery_record(remember_dir: Path, session_id: str) -> Path:
    """A per-session handoff plus one delivered-once run against it -- the
    ordinary way a remember.delivered.<id> record comes to exist."""
    (remember_dir / f"remember.{session_id}.md").write_text(
        f"Handoff for {session_id}.\n"
    )
    record = remember_dir / "tmp" / f"remember.delivered.{session_id}"
    return record


class TestStaleDeliveryRecordsArePruned:

    def test_record_for_a_session_with_no_transcript_is_pruned(self, tmp_path):
        """MUST FIRE: session AAA delivered once and left a record behind;
        its transcript is gone (never existed, or was cleaned up elsewhere),
        and the record is old enough that #393's grace window no longer
        applies. The next session start must remove AAA's stale record."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)

        out_a = _session_start(project, home, "sess-aaa")
        assert "Handoff for" not in out_a  # nothing written yet on first run

        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        # Deliver it once for real, so the record this test prunes is the
        # genuine artifact the hook itself writes, not a hand-built stand-in.
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"
        # Past the #393 grace window -- old enough that "no transcript" can
        # only mean the session is over, never that it is still starting up.
        _age_record(record_a, _GRACE_MIN + 1)

        # sess-aaa's transcript never existed under sessions_dir -- simulates
        # a session that is over and gone. A different, unrelated session
        # (BBB) starts next.
        _session_start(project, home, "sess-bbb")

        assert not record_a.exists(), (
            "a delivery record for a session with no transcript on disk, "
            "old enough to be outside the startup grace window, survived "
            "a later session start -- this is the #373 leak"
        )

    def test_fresh_record_with_no_transcript_yet_survives(self, tmp_path):
        """MUST NOT FIRE (#393 positive control): session AAA's record was
        JUST written and its transcript does not exist yet -- indistinguish-
        able, on the sweep's only signal, from a session that is over. A
        different session (BBB) starts inside that same window. AAA's
        record must survive: at source=startup Claude Code creates a
        session's own transcript only AFTER this hook has already run, so a
        record this young is not evidence the session is gone."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"
        # record_a's mtime is left exactly as the hook just wrote it --
        # inside the grace window, deliberately not backdated.

        # sess-aaa's transcript still does not exist -- the exact #393 shape:
        # absent transcript, freshly-written record. A different session
        # (BBB) starts inside the window.
        _session_start(project, home, "sess-bbb")

        assert record_a.exists(), (
            "a delivery record younger than the #393 startup grace window "
            "was pruned on the strength of an absent transcript alone -- "
            "a live session starting up is indistinguishable from a dead "
            "one on that signal, so this is the #393 race"
        )

    def test_record_for_a_session_whose_transcript_still_exists_survives(self, tmp_path):
        """MUST NOT FIRE (positive control): same shape, except sess-aaa's
        transcript is still present under $SESSIONS_DIR when session BBB
        starts. Its delivery record must survive -- an over-eager sweep
        that ignores the transcript check would fail this."""
        project, home, remember_dir, sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"

        # sess-aaa's transcript IS present -- the session could still resume.
        (sessions_dir / "sess-aaa.jsonl").write_text('{"type":"user"}\n')

        _session_start(project, home, "sess-bbb")

        assert record_a.exists(), (
            "a delivery record for a session whose transcript still exists "
            "was pruned -- the sweep must not delete state for a session "
            "that could still be active"
        )

    def test_could_not_tell_leaves_records_untouched(self, tmp_path):
        """Third state: $SESSIONS_DIR itself is gone (unreadable), so there
        is no way to tell whether sess-aaa is over. The record must survive
        -- could-not-tell must never render as "pruned"."""
        project, home, remember_dir, sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"

        # Remove the whole sessions directory -- "cannot tell", not "empty".
        import shutil
        shutil.rmtree(sessions_dir)

        _session_start(project, home, "sess-bbb")

        assert record_a.exists(), (
            "a delivery record was pruned even though $SESSIONS_DIR could "
            "not be read at all -- could-not-tell must not act like pruned"
        )

    def test_own_record_is_never_pruned_by_the_sweep(self, tmp_path):
        """The running session's own record (just written this same
        invocation) must never be swept as though it belonged to some other,
        absent session."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")

        assert record_a.exists(), (
            "a session's own just-written delivery record was removed by "
            "its own invocation's sweep"
        )

    def test_records_left_over_after_switching_back_to_single_mode_are_still_pruned(self, tmp_path):
        """The sweep must not be gated on THIS session's own handoff_mode.
        A record left behind during an earlier per_session period does not
        stop existing just because the user later switched handoff_mode
        back to "single" -- a sweep gated on the current mode would leak
        exactly those records forever, reproducing #373 under a config
        toggle instead of fixing it."""
        project, home, remember_dir, sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"
        # Past the #393 grace window -- an old per_session-era leftover, not
        # a session still inside its own startup.
        _age_record(record_a, _GRACE_MIN + 1)

        # sess-aaa's transcript is gone, and the user has switched back to
        # single mode before the next session starts.
        _set_handoff_mode(home, "single")
        (remember_dir / "remember.md").write_text("Shared note.\n")

        _session_start(project, home, "sess-bbb")

        assert not record_a.exists(), (
            "a stale per-session delivery record survived a session start "
            "under handoff_mode: \"single\" -- the sweep must not be gated "
            "on the CURRENT session's own mode, only on whether the "
            "session in question is confirmed over"
        )
        assert sessions_dir.is_dir()  # sanity: the fixture itself is sound

def _session_start_with_env(project, home, session_id, extra_env: dict) -> str:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
        **extra_env,
    }
    result = subprocess.run(
        ["bash", str(SESSION_START_SCRIPT)],
        input=_payload(session_id),
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr[:500]}"
    return result.stdout


def _fake_date_bin(tmp_path: Path, behavior: str) -> Path:
    """A `date` that fails (or answers non-numeric garbage) for `+%s` only,
    and defers to the real `date` for every other call site this hook makes
    (TODAY, FIRST_DELIVERED, ...) -- #402 is specifically about the clock
    read at the GRACE_MIN arbitration, not about breaking the whole hook.

    Putting a fake `date` on PATH is exactly the seam
    tests/test_prompt_hook_spawns.py's
    test_no_test_fakes_the_clock_on_path_without_disabling_the_builtin guards:
    on bash >= 4.2 lib-clock.sh's `_remember_date` can answer from bash's own
    `printf '%(FMT)T'` builtin, which is not on PATH, so a fake `date` is
    silently ignored for any format that goes through it. `+%s` is not one of
    those formats -- `_remember_date_builtin_ok` in lib-clock.sh refuses `%s`
    unconditionally, so `_remember_date +%s` always execs the real `date`
    binary regardless of bash version or REMEMBER_NO_PRINTF_T (confirmed by
    reading _remember_date_builtin_ok's case pattern, and by forcing
    _REMEMBER_PRINTF_T=1 locally and observing the fake still gets called for
    `+%s` while a non-`%s` format attempts the builtin instead). Callers below
    still pass REMEMBER_NO_PRINTF_T=1 anyway, so this fake does not depend on
    that %s-specific carve-out remaining true forever.
    """
    import shutil
    real_date = shutil.which("date")
    assert real_date, "no real `date` on PATH -- cannot build the fake"
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "date"
    if behavior == "fail":
        body = f'case "$1" in\n  +%s) exit 1 ;;\n  *) exec "{real_date}" "$@" ;;\nesac\n'
    elif behavior == "garbage":
        body = f'case "$1" in\n  +%s) echo "not-a-number"; exit 0 ;;\n  *) exec "{real_date}" "$@" ;;\nesac\n'
    else:
        raise ValueError(behavior)
    fake.write_text("#!/bin/sh\n" + body)
    fake.chmod(0o755)
    return bindir


class TestUnreadableClockDuringPruneSweep:
    """#402: the #393 grace window degrades could-not-tell to prune when the
    clock read fails, the opposite of what the code two lines above it does
    for an unreadable record mtime. Case numbering matches the #402 audit:

      1. live record, healthy clock    : KEPT    (existing #393 control)
      2. old record,  healthy clock    : PRUNED  (existing #373 control)
      3. live record, clock UNREADABLE : must be KEPT -- the defect
      4. live record, clock non-numeric: must be KEPT -- the defect
    """

    def test_live_record_survives_when_clock_read_fails(self, tmp_path):
        """MUST NOT FIRE (#402 case 3): the record is fresh -- well inside
        the #393 grace window -- and its transcript is absent, exactly the
        #393 shape. If the clock used to arbitrate the window cannot be
        read, that is could-not-tell, not confirmation the record is old:
        it must survive, the same way an unreadable mtime already does two
        lines above in the source."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)
        bindir = _fake_date_bin(tmp_path, "fail")
        # REMEMBER_NO_PRINTF_T=1 forces lib-clock.sh's `date` fallback path
        # even though `+%s` already always takes it (see _fake_date_bin's
        # docstring) -- belt and suspenders, so this test does not depend on
        # that %s carve-out surviving a future lib-clock.sh change.
        extra_env = {"PATH": f"{bindir}:{os.environ['PATH']}", "REMEMBER_NO_PRINTF_T": "1"}

        _session_start_with_env(project, home, "sess-aaa", extra_env)
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start_with_env(project, home, "sess-aaa", extra_env)
        assert record_a.exists(), "setup did not produce a delivery record"
        # record_a is left exactly as freshly written -- inside the grace
        # window on any clock that CAN be read.

        _session_start_with_env(project, home, "sess-bbb", extra_env)

        assert record_a.exists(), (
            "a live delivery record was pruned because the clock used to "
            "arbitrate the #393 grace window could not be read -- "
            "could-not-tell must never render as pruned"
        )

    def test_live_record_survives_when_clock_output_is_non_numeric(self, tmp_path):
        """MUST NOT FIRE (#402 case 4): same shape as above, but the clock
        command exits 0 and prints something that is not a number instead
        of failing outright."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)
        bindir = _fake_date_bin(tmp_path, "garbage")
        # See the "fail" case above for why REMEMBER_NO_PRINTF_T=1 is set here
        # even though `+%s` already always takes the `date` fallback path.
        extra_env = {"PATH": f"{bindir}:{os.environ['PATH']}", "REMEMBER_NO_PRINTF_T": "1"}

        _session_start_with_env(project, home, "sess-aaa", extra_env)
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start_with_env(project, home, "sess-aaa", extra_env)
        assert record_a.exists(), "setup did not produce a delivery record"

        _session_start_with_env(project, home, "sess-bbb", extra_env)

        assert record_a.exists(), (
            "a live delivery record was pruned because the clock printed "
            "non-numeric output -- could-not-tell must never render as "
            "pruned"
        )

    def test_old_record_is_still_pruned_when_clock_is_healthy(self, tmp_path):
        """MUST FIRE (#402 case 2, the paired must-prune control run
        alongside cases 3/4 above in this same fixture): with a healthy
        clock and no fake `date` on PATH, an old record outside the grace
        window is still pruned. Without this control, a harness broken in
        a way that always "keeps" would pass cases 3/4 for the wrong
        reason -- this is what tells a real fix from a silently-broken
        sweep."""
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"
        _age_record(record_a, _GRACE_MIN + 1)

        _session_start(project, home, "sess-bbb")

        assert not record_a.exists(), (
            "an old delivery record with a healthy clock survived a later "
            "session start -- the pairing control for #402 failed, "
            "independent of the clock-unreadable fix"
        )



def _fake_stat_bin(tmp_path: Path, target_path: Path) -> Path:
    """A `stat` that answers non-numeric garbage for a `%Y`/`%m` mtime read
    of exactly `target_path`, and defers to the real `stat` for every other
    call -- including this hook's own GNU-then-BSD fallback chain hitting
    the SAME path with the flag the platform does not support, and every
    OTHER file this hook or a library it sources reads the mtime/uid of
    (lib-lock.sh, log.sh)."""
    import shutil
    real_stat = shutil.which("stat")
    assert real_stat, "no real `stat` on PATH -- cannot build the fake"
    bindir = tmp_path / "fakestatbin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "stat"
    script_lines = [
        "#!/bin/sh",
        'if [ "$3" = "' + str(target_path) + '" ]; then',
        '    case "$1$2" in',
        '        "-c%Y"|"-f%m")',
        '            echo "not-a-number"',
        "            exit 0",
        "            ;;",
        "    esac",
        "fi",
        'exec "' + real_stat + '" "$@"',
        "",
    ]
    fake.write_text("\n".join(script_lines))
    fake.chmod(0o755)
    return bindir


class TestUnreadableMtimeGarbageDuringPruneSweep:
    """Sibling to TestUnreadableClockDuringPruneSweep, found during #402's
    own self-review: `_remember_stale_mtime` had the identical gap
    `_remember_now` did -- a non-empty, non-numeric `stat` read was coerced
    to 0 rather than treated as could-not-tell, so it could reach the
    -gt 0 gate as a "confirmed" zero-age record and be pruned on a healthy
    clock. Fixed alongside #402 in the same commit; this is that fix's own
    must-not-prune case, with #402's own case-2 control (a healthy stat and
    clock still prunes an old record) already covering the must-fire side
    in the class above."""

    def test_live_record_survives_when_stat_output_is_non_numeric(self, tmp_path):
        project, home, remember_dir, _sessions_dir = _sandbox(tmp_path)

        _session_start(project, home, "sess-aaa")
        record_a = _seed_delivery_record(remember_dir, "sess-aaa")
        _session_start(project, home, "sess-aaa")
        assert record_a.exists(), "setup did not produce a delivery record"

        bindir = _fake_stat_bin(tmp_path, record_a)
        extra_env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
        _session_start_with_env(project, home, "sess-bbb", extra_env)

        assert record_a.exists(), (
            "a live delivery record was pruned because `stat` printed "
            "non-numeric output for its mtime -- could-not-tell must "
            "never render as pruned"
        )
