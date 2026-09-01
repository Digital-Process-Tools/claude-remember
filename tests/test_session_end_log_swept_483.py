"""Tests for #483: the session-end flush log deletes itself.

session-end-hook.sh backgrounds `save-session.sh --force`, redirecting its
stdout+stderr into a fresh, empty `logs/autonomous/session-end-HHMMSS.log`
before the flush even starts (scripts/session-end-hook.sh). save-session.sh's
own NDC step then sweeps that same directory for stale logs with
`find ... -name "*.log" -empty -delete` (scripts/save-session.sh) -- and on
an ordinary successful flush nothing ever writes a byte into the session-end
log (save-session.sh logs to its own daily narrative file, not to
stdout/stderr), so the housekeeping it runs from *inside the very process
writing that log* matches it and deletes it out from under the still-running
redirect. Two symptoms follow: the WARNING a later failure would emit names
a path that is already gone, and a healthy run leaves nothing on disk to
confirm it ran.

Positive control lives in the same fixture, per this repo's own testing
rule: a stale, genuinely-empty log left behind by an earlier run must still
be swept. An assertion that only checks "the log survived" would also pass
if the housekeeping simply never ran at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from subprocess_helpers import subprocess_failure_detail
from test_session_end_hook_345 import _make_env, _wire_hook, _run_hook

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)


class TestSessionEndLogSurvivesItsOwnHousekeeping:
    def test_must_fire_own_log_survives_a_successful_flush(self, tmp_path):
        """The log the CURRENT flush is writing into must not be swept by
        that same flush's own empty-log housekeeping.

        STUB_HAIKU_TEXT (not SKIP) is required to reach the NDC step at all
        -- a SKIP exits save-session.sh before the housekeeping line ever
        runs, which would make this test pass for the wrong reason (nothing
        executed the sweep in the first place).
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        autonomous = project / ".remember" / "logs" / "autonomous"
        session_logs = list(autonomous.glob("session-end-*.log"))
        assert session_logs, (
            "the session-end flush's own log is gone -- the housekeeping "
            "sweep that runs inside this same flush (save-session.sh's "
            "`find ... -empty -delete`) matched the still-empty log this "
            "very process redirects into and deleted it out from under "
            "itself (#483)\n" + str(list(autonomous.iterdir()))
        )

    def test_must_fire_a_genuinely_stale_empty_log_is_still_swept(self, tmp_path):
        """Positive control: a stale, truly-abandoned empty log in the same
        directory must still be deleted. Proves the housekeeping ran at all
        -- without this, a harness where the sweep never fires would make
        the test above pass vacuously.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"
        _wire_hook(plugin)
        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        stale = autonomous / "session-end-000000.log"
        stale.write_text("")
        assert stale.stat().st_size == 0

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert not stale.exists(), (
            "a genuinely stale, empty log from an earlier run must still be "
            "swept -- if it survives, the housekeeping did not run and the "
            "paired test above proves nothing"
        )
