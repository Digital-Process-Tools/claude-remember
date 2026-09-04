"""Tests for #527: the backgrounded save-*.log deletes itself.

post-tool-hook.sh forks save-session.sh with its stdout+stderr redirected
into a fresh, empty logs/autonomous/save-HHMMSS.log (scripts/post-tool-hook.sh)
before the flush even starts. save-session.sh's own housekeeping (unconditional
on every flush since #498, scripts/save-session.sh) then sweeps that same
directory for empty logs with `[ ! -s "$f" ] -> rm -f "$f"` -- and on an
ordinary successful flush nothing ever writes a byte into the save-*.log
(save-session.sh logs to its own daily narrative file, not to
stdout/stderr), so the housekeeping it runs from *inside the very process
writing that log* matches it and deletes it out from under the still-open
redirect. Same shape as #483, which session-end-hook.sh already defends
against by seeding a header before its own subshell opens the file
(scripts/session-end-hook.sh:296-311).

Two things are tested, deliberately not one:

  1. That post-tool-hook.sh's OWN redirect can never hand save-session.sh an
     empty file (the actual code that changed for this fix) --
     TestSaveLogIsSeededBeforeTheForkBackgrounds, driven through the real
     hook, no stub needed since it only inspects the file the instant the
     hook returns.

  2. That save-session.sh's real housekeeping sweep (unchanged by this fix)
     genuinely leaves a seeded, non-empty save-*.log alone while still
     reclaiming a genuinely stale, empty one -- TestSeededSaveLogSurvivesHousekeeping,
     driven directly against save-session.sh with the stub pipeline shell
     from test_save_session_gates.py, because an unstubbed run SKIPs and
     exits (save-session.sh:668) before ever reaching housekeeping
     (save-session.sh:1080) -- exercising the mechanism the fix relies on
     without depending on post-tool-hook.sh's own fork completing in time.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_post_tool_hook_spawns import _project, _env, _run, _reap  # noqa: E402
from test_save_session_gates import _make_env, _run as _run_save_session  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)


class TestSaveLogIsSeededBeforeTheForkBackgrounds:
    """post-tool-hook.sh's own redirect, exercised end to end."""

    def test_must_fire_the_save_log_is_never_created_empty(self, tmp_path):
        """The guarantee this fix must hold, tested directly: a freshly
        created save-*.log must already be non-empty the instant
        post-tool-hook.sh returns -- BEFORE the backgrounded save-session.sh
        has necessarily run at all. The seed write has to happen in the
        PARENT (post-tool-hook.sh), synchronously, ahead of the `nohup ...
        &` that backgrounds the child -- not race the child to write it.
        `-s` (non-empty) is exactly what save-session.sh's own housekeeping
        checks; a file this hook itself seeds can never look abandoned to
        that sweep."""
        home, project, remember = _project(tmp_path, jsonl_lines=200)
        result = _run(_env(tmp_path, home, project))
        assert result.returncode == 0, result.stderr[:300]

        autonomous = remember / "logs" / "autonomous"
        save_logs = list(autonomous.glob("save-*.log"))
        assert save_logs, "no save-*.log was created at all"
        for p in save_logs:
            assert p.stat().st_size > 0, (
                f"{p} was created empty -- a still-open, empty save-*.log "
                "matches save-session.sh's own housekeeping "
                "`[ ! -s ] -> rm -f` sweep and can be unlinked while its "
                "writer still holds the fd (#527)"
            )
        _reap(remember)


class TestSeededSaveLogSurvivesHousekeeping:
    """save-session.sh's real, unchanged housekeeping sweep, driven directly
    (bypassing post-tool-hook.sh's fork, which SKIPs and exits before
    reaching housekeeping unless a real/stubbed summarizer response is
    provided). Reproduces the exact `save-*.log` glob and `[ ! -s ] -> rm -f`
    logic this fix relies on staying correct for."""

    def test_must_fire_a_seeded_save_log_survives_the_real_sweep(self, tmp_path):
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=3)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"

        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        seeded = autonomous / "save-235900.log"
        seeded.write_text("18:59:00 [post-tool] save triggered\n", encoding="utf-8")
        assert seeded.stat().st_size > 0

        result = _run_save_session(plugin, env, sid)

        assert result.returncode == 0, result.stderr[:400]
        assert seeded.exists(), (
            "a seeded, non-empty save-*.log was deleted by save-session.sh's "
            "own housekeeping sweep despite being non-empty -- the sweep's "
            "`[ ! -s ]` check is no longer the guarantee this fix relies on "
            "(#527)\n" + str(list(autonomous.iterdir()))
        )

    def test_must_fire_a_genuinely_stale_empty_save_log_is_still_swept(self, tmp_path):
        """Positive control: a stale, truly-abandoned empty save-*.log in
        the same directory must still be deleted by the SAME run. Proves
        the housekeeping ran at all -- without this, a harness where the
        sweep never fires would make the test above pass vacuously."""
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=4, humans=3)
        env["STUB_HAIKU_TEXT"] = "## 18:30 | main\n\n- did some work\n"

        autonomous = project / ".remember" / "logs" / "autonomous"
        autonomous.mkdir(parents=True, exist_ok=True)
        stale = autonomous / "save-000000.log"
        stale.write_text("")
        assert stale.stat().st_size == 0

        result = _run_save_session(plugin, env, sid)

        assert result.returncode == 0, result.stderr[:400]
        assert not stale.exists(), (
            "a genuinely stale, empty log from an earlier run must still "
            "be swept -- if it survives, the housekeeping did not run and "
            "the test above proves nothing"
        )
