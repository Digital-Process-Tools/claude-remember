"""Tests for #487: logs/autonomous/session-end-*.log is never reclaimed.

#483 seeded `$_END_LOG` with a header line before the flush subshell ever
opens it, so save-session.sh's own housekeeping

    find "${REMEMBER_DIR}/logs/autonomous" -name "*.log" -empty -delete

no longer matches the log its own parent shell is writing into -- the
correct fix for the bug #483 was filed about. But that `-empty -delete` was
this directory's ONLY retention mechanism: there is no mtime sweep, no
count cap, no rotation. Before #483's fix, every ordinary session-end-*.log
was reclaimed on the next flush because it stayed empty; after it, every
one of them is non-empty by construction and nothing ever removed it. One
file per session, forever.

The fix adds a second, age-keyed sweep over the same "*.log" glob (so it
covers save-*.log and session-end-*.log alike -- both file classes this
directory ever holds), independent of emptiness. Emptiness was always a
proxy for staleness, and it is the proxy that produced #483 in the first
place.

Positive control lives in the same fixture, per this repo's own testing
rule: a run's OWN freshly-written log (mtime "now") must survive its own
housekeeping, exactly as test_session_end_log_swept_483.py already pins for
the emptiness sweep -- an assertion that only checked "the old file is
gone" would also pass if the housekeeping deleted the whole directory.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from subprocess_helpers import subprocess_failure_detail
from test_session_end_hook_345 import _make_env, _run_hook, _wire_hook

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)


class TestAgedAutonomousLogsAreReclaimed:
    def test_must_fire_an_old_nonempty_session_end_log_is_swept(self, tmp_path):
        """The defect: a non-empty session-end-*.log, backdated well past
        the default retention window, must be reclaimed by an ordinary
        flush's own housekeeping -- not just the still-empty ones.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        stale = autonomous / "session-end-000000-11111.log"
        stale.write_text(
            "12:00:00 [session-end] flush started\n"
            "12:00:01 save-session.sh output from a run long finished\n"
        )
        eight_days_ago = time.time() - (8 * 24 * 3600)
        os.utime(stale, (eight_days_ago, eight_days_ago))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert not stale.exists(), (
            "a non-empty session-end log, 8 days old, survived an ordinary "
            "flush's own housekeeping -- #487's retention gap: emptiness "
            "was the only thing ever reclaimed here, and this file was "
            "never empty\n" + str(list(autonomous.iterdir()))
        )

    def test_must_fire_a_fresh_nonempty_log_survives_the_same_sweep(self, tmp_path):
        """Positive control, same fixture shape as the test above but with
        the stale log backdated only 1 day (inside the default 7-day
        window) -- must survive. Without this, a housekeeping change that
        deleted every "*.log" regardless of age would also pass the test
        above.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        recent = autonomous / "session-end-000000-22222.log"
        recent.write_text("12:00:00 [session-end] flush started\n")
        one_day_ago = time.time() - (1 * 24 * 3600)
        os.utime(recent, (one_day_ago, one_day_ago))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert recent.exists(), (
            "a 1-day-old, non-empty session-end log was reclaimed well "
            "inside the default 7-day retention window -- the sweep is not "
            "keyed to the configured age at all\n" + str(list(autonomous.iterdir()))
        )

    def test_must_fire_retention_window_is_configurable(self, tmp_path):
        """`thresholds.autonomous_log_retention_days` must actually gate the
        sweep -- without this, the config read could be dead code that
        always falls through to the hardcoded default.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        cfg_layer = plugin / "config.json"
        import json as _json
        cfg = _json.loads(cfg_layer.read_text())
        cfg.setdefault("thresholds", {})["autonomous_log_retention_days"] = 1
        cfg_layer.write_text(_json.dumps(cfg))
        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        two_days_old = autonomous / "session-end-000000-33333.log"
        two_days_old.write_text("12:00:00 [session-end] flush started\n")
        two_days_ago = time.time() - (2 * 24 * 3600)
        os.utime(two_days_old, (two_days_ago, two_days_ago))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert not two_days_old.exists(), (
            "thresholds.autonomous_log_retention_days=1 did not shrink the "
            "retention window -- a 2-day-old log survived a sweep "
            "configured to reclaim anything over 1 day old\n"
            + str(list(autonomous.iterdir()))
        )
