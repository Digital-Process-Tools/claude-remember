"""#902: logs/autonomous/save-*.log must carry the date and a per-process
part, not HHMMSS alone -- a bare HHMMSS collides with a file left over from
an earlier day that is still inside the retention window (the housekeeping
sweep in save-session.sh only reclaims a non-empty log once it is older than
``_AUTONOMOUS_LOG_RETENTION_DAYS``, default 7). A save seeded into that
reused name appends to someone else's old content instead of starting a
fresh file -- the reported corruption started with exactly that collision.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_post_tool_hook_spawns import _env, _project, _reap, _run

# Inherited with the helpers, not re-decided here: test_post_tool_hook_spawns
# opts out of Windows because its `_run` invokes a bare "bash" -- the WSL
# launcher on the Windows runner (#912 CI: rc 1, UTF-16 "... to install.").
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

# save-<8 digit date>-<6 digit time>-<pid digits>.log
_NAME_RE = re.compile(r"^save-\d{8}-\d{6}-\d+\.log$")


def test_must_fire_save_log_name_carries_date_and_pid(tmp_path: Path):
    home, project, remember = _project(tmp_path, jsonl_lines=200)
    result = _run(_env(tmp_path, home, project))
    assert result.returncode == 0, result.stderr[:300]

    autonomous = remember / "logs" / "autonomous"
    names = [p.name for p in autonomous.glob("save-*.log")]
    assert names, "no save-*.log was created at all"
    for name in names:
        assert _NAME_RE.match(name), (
            f"{name} does not carry a date + per-process part -- a bare "
            "HHMMSS name can collide with a file from an earlier day still "
            "inside the retention window (#902)"
        )
    _reap(remember)


def test_must_not_fire_same_second_same_process_name_is_stable(tmp_path: Path):
    """Positive control: the naming scheme is still deterministic enough
    within one process invocation to be matched by the regex above at all --
    without this, a harness that produced no save-*.log file (a broken
    fixture) would make the must-fire test above vacuously pass too."""
    home, project, remember = _project(tmp_path, jsonl_lines=200)
    result = _run(_env(tmp_path, home, project))
    assert result.returncode == 0, result.stderr[:300]
    autonomous = remember / "logs" / "autonomous"
    names = [p.name for p in autonomous.glob("save-*.log")]
    assert len(names) == 1, names
    assert _NAME_RE.match(names[0]), names[0]
    _reap(remember)
