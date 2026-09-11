"""SessionStart's promo selection must not fork one `jq` per field, per entry,
per candidate (#660).

#660 reports `session-start-hook.sh` averaging ~12.5s per session start on
Windows/Git Bash across 36 real starts, against 0.5-1.3s for comparable
SessionStart hooks in the same sessions -- and names Windows process-spawn
cost (~50-200ms per subprocess/`jq` invocation under Git Bash, the reporter's
own measurement) as the plausible multiplier for a hook that shells out
repeatedly on the hot (foreground) path.

Walking that path (not assuming the reporter's own hypothesis is the right
lever, per the brief): `config()` was already collapsed to one `jq` process
per session by #232, and the recovery/consolidation forks below are already
backgrounded by #646 -- so they were not the lever either. What remained was
`_remember_compute_promo()`: one `jq -r '.promos | length'`, then FOUR more
`jq` calls per candidate entry (id/text/url/installed_key), and then, for
every candidate that survived those, the exact same `.plugins[$k]` query run
TWICE -- once to capture its value, once again just to inspect its exit
status. Measured on this file before #660 (`spawn_counting`'s PATH shim, one
promos.json candidate resolving on the first entry): 10 `jq` forks for the
promo mechanism alone, inside a hook that otherwise costs 1 (config()'s
one-pass load) + 1 (the final systemMessage JSON). After #660 folds the field
reads into one `@tsv` dump and the installed-plugins probe into one call:
4 total.

**Observed, not reasoned**: this suite runs on the CI/dev host's own `jq` and
counts *executions*, not wall-clock -- the same choice `tests/spawn_counting.py`
made for the post-tool-hook and prompt-hook budgets, because wall clock is
what differs between platforms and spawn count is what causes it. Whether the
per-spawn COST is 50-200ms is Git-Bash-under-Windows-specific and not
something this host can measure; only the count survives the trip -- what
this test pins is the number of forks, and the reasoned (not observed) claim
is that removing 6 of them removes 6 x that unmeasured per-spawn cost,
wherever it runs.
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
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug
from tests.spawn_counting import make_shim_dir, spawns

SESSION = "eeeeeeee-0000-4000-8000-000000000660"

# Measured post-#660 (this file, one not-installed candidate resolving on the
# first promos.json entry): 4 `jq` forks. Pre-#660 the same scenario cost 10.
# The budget is the measured number, not a rounder one, so the next `jq` call
# added to this path trips it as a regression rather than hiding in slack --
# the same discipline test_post_tool_hook_spawns.py documents for its own
# budget.
PROMO_JQ_SPAWN_BUDGET = 4


def _run_with_shim(tmp_path: Path) -> tuple[list[str], subprocess.CompletedProcess]:
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": "2", "plugins": {}}), encoding="utf-8"
    )

    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "SPAWN_LOG": str(log),
        "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
    }
    payload = json.dumps(
        {
            "session_id": SESSION,
            "transcript_path": f"/does/not/matter/{SESSION}.jsonl",
            "hook_event_name": "SessionStart",
            "cwd": "/does/not/matter",
        }
    )
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return spawns(log), result


def test_promo_selection_stays_within_the_jq_spawn_budget(tmp_path):
    """Positive control built in: the promo genuinely fires (a not-installed
    candidate exists), so this counts the SAME run the promo feature is
    supposed to speak on -- a budget measured against a run that emits
    nothing would not be measuring the path #660 is actually about.
    """
    lines, result = _run_with_shim(tmp_path)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed.get("systemMessage"), (
        "positive control failed: no promo fired, so a spawn count of 0 here "
        "would prove nothing about the path this budget is pinning"
    )

    jq_spawns = [line for line in lines if line.startswith("jq ")]
    assert len(jq_spawns) <= PROMO_JQ_SPAWN_BUDGET, (
        f"promo selection spawned {len(jq_spawns)} jq processes "
        f"(budget {PROMO_JQ_SPAWN_BUDGET}):\n" + "\n".join(jq_spawns)
    )
