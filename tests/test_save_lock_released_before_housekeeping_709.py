"""#709: save.lock must be free before the autonomous-log housekeeping
sweep and the SessionStart cache pre-render run, not held through them.

save-session.sh released save.lock only in the EXIT trap. After the NDC
subshell is backgrounded (or the RUN_NDC block is skipped entirely), the
parent kept the lock through the whole housekeeping sweep (one `stat` per
file under logs/autonomous/) and the cache pre-render -- a hold the issue
measured at 231.9s against 1,520 files. Neither NDC_COMMIT_LOCK_TIMEOUT nor
FORCE_LOCK_TIMEOUT (30s each) could outlast that, so every waiting save and
every --force session-end flush timed out.

This test does not need 1,520 files or a slow sweep to prove the ordering:
it shims `stat` -- the one external command the sweep calls per file -- to
record whether save.lock is held (as a directory) at the moment the sweep
reaches it. A single non-empty log file is enough to trigger exactly one
call.
"""

import os
import sys

import pytest

from .subprocess_helpers import subprocess_failure_detail
from .test_save_session_gates import _make_env, _run, _suppress_ndc

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout -- not portable to Windows runners (#79)",
)

STAT_SHIM = """#!/bin/sh
# Records whether save.lock (a directory) exists at the moment the
# housekeeping sweep calls `stat` on a logs/autonomous/*.log file, then
# answers with a real mtime so the sweep's own age arithmetic still works.
if [ -d "$STUB_LOCK_DIR" ]; then
    printf 'held\n' >> "$STUB_STAT_LOG"
else
    printf 'free\n' >> "$STUB_STAT_LOG"
fi
date +%s
"""


def test_lock_is_free_when_housekeeping_stats_a_log_file(tmp_path):
    # exchanges/humans and STUB_HAIKU_SKIP=0 are chosen so the run reaches
    # Step 7 (append + position) and Step 8 (NDC gate) rather than exiting
    # early on either the zero-exchange short-circuit or a model SKIP --
    # housekeeping runs only past that point. NDC itself is suppressed
    # (_suppress_ndc): irrelevant to this ordering question and would only
    # add an unawaited background subshell to the fixture.
    env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=10, humans=5)
    _suppress_ndc(project)
    env = {**env, "STUB_HAIKU_SKIP": "0"}

    autonomous_dir = project / ".remember" / "logs" / "autonomous"
    autonomous_dir.mkdir(parents=True)
    (autonomous_dir / "save-1.log").write_text("[post-tool] save triggered\n")

    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    stat_shim = bin_dir / "stat"
    stat_shim.write_text(STAT_SHIM)
    os.chmod(stat_shim, 0o755)

    stat_log = tmp_path / "stat-calls.log"
    env = {
        **env,
        "PATH": f"{bin_dir}:{env['PATH']}",
        "STUB_LOCK_DIR": str(project / ".remember" / "tmp" / "save.lock"),
        "STUB_STAT_LOG": str(stat_log),
    }

    result = _run(plugin, env, sid)

    assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
    assert stat_log.is_file(), (
        "the shim was never called -- the housekeeping sweep did not reach "
        "the seeded log file, so this test proves nothing"
    )
    calls_seen = stat_log.read_text().splitlines()
    assert calls_seen, "the sweep's stat call produced no recorded line"
    assert "held" not in calls_seen, (
        f"save.lock was still held while housekeeping ran ({calls_seen!r}) -- "
        "the whole point of #709: a sweep over hundreds of aged logs must not "
        "run behind the lock every waiting save and every --force session-end "
        "flush is timing out on"
    )
