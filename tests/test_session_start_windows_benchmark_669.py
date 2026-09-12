"""Windows benchmark for session-start-hook.sh -- wall time and spawn count,
measured on the leg that exists for exactly this (#669, part of #660).

Every latency number attached to #660 and its follow-ups (#662-#668) is
REASONED from the reporter's own per-spawn figure, never OBSERVED in CI,
because the one place this repo runs Windows -- the `windows-latest` leg of
`.github/workflows/tests.yml` -- never executed this hook at all: 92 modules
blanket-skip on `win32` (#497), and #661's own two tests are
`skipif(sys.platform == "win32")` too. The leg is green because it exercises
almost nothing about the shell path #660 is about.

`windows-latest` ships Git for Windows, so a real, non-WSL bash is on the box
on every run (`tests/_bash_runner.py`'s `resolve_bash()`, built for exactly
this by #432). This file drives the hook under that bash, for real, and turns
the #662-#667 claims from reasoned into observed for whichever platform ran
it.

── Why this is a new test, not a new CI job ──────────────────────────────────
`.oss.json`'s `test_command` is plain `pytest`, and `.github/workflows/tests.yml`
already runs it across `{ubuntu, macos, windows}-latest` x 4 Python versions,
with Windows Defender already excluded from the checkout and `RUNNER_TEMP`
(see that file's own comment). A new module reaches all twelve legs for free;
a dedicated job would re-pay the checkout/setup/Defender-exclusion cost for
one test and run it on fewer platforms, not more. The "same test on
Linux/macOS... in one log" the issue asks for (step 6) falls out of this for
free too: this file carries no platform skip beyond "no bash found", so every
leg of the existing matrix runs it and the cross-platform ratio is one
`pytest -rs`-summary away, no extra wiring.

── Why spawn count is asserted and wall time is only recorded ───────────────
This repo already made this call twice, for the same reason, before this
issue existed: `scripts/report_test_durations.py` (#510) and
`scripts/report_windows_skip_floor.py` (#497) are both a REPORT, never a GATE,
explicitly because a shared CI runner's own load is not in anyone's diff, and
failing a build on a neighbour's noisy machine only teaches people to re-run
until green. Wall-clock inherits that problem directly -- this repo's own
CLAUDE.md says a green run on one platform is "the weakest evidence
available" about platforms it was not run on, and a shared Windows runner's
wall clock is noisy even about ITSELF, run to run. Spawn count does not have
that problem: it is counted by execution (`tests/spawn_counting.py`, built for
the identical reason in #227/#230), so the same code path produces the same
count on a loaded box and a quiet one. So:

- spawn count is a real, failing budget (generous, see the constants below --
  MEASURE FIRST, then tighten, per the issue's own "Ask");
- wall time is printed and handed to `record_property` so it shows in the job
  log and any consumer reading the JUnit XML, but is only asserted against a
  deliberately huge ceiling meant to catch a hang, not a regression -- the
  actual before/after comparison #662-#667 need is "read last run's number
  off this log, read this run's number off this log", by a human, the same
  way #510's own duration report is read.

── The fixture ────────────────────────────────────────────────────────────────
A representative, already-healthy project: legacy `.remember/` layout,
identity.md plus four other memory files (core-memories.md, now.md,
recent.md, archive.md), `installed_plugins.json` schema version 2 (so the
promo path runs its real code instead of silently no-op'ing on an
unrecognised schema -- #660's own promo-spawn-budget fixture does the same),
and a PREVIOUS session whose `last-save.json` entry genuinely marks it saved
(a line count, not a truthy placeholder -- `session_was_saved`'s own query at
session-start-hook.sh:333 requires an integer; this file used `{"saved":
true}` during manual verification before reading that query and it silently
read as UNSAVED, triggering the background recovery fork every single run and
inflating both numbers with a path this benchmark is not about). That avoids
the recovery fork entirely, so what gets measured is the hook's normal
startup cost on a healthy install, not the strictly larger capture-recovery
path (which has its own dedicated tests in test_session_start_prev_session_270.py).

stdin is closed deliberately, by `subprocess.run`'s own `input=` (it writes
the payload through `communicate()` and closes the pipe), so the hook's
`read -r -t 1` loop -- session-start-hook.sh:92, named explicitly in the
issue's caveats -- reads to EOF rather than blocking the full second. The
open-stdin path is a real, separately-pinned behaviour
(test_session_start_prev_session_270.py's
`test_the_hook_does_not_block_on_an_open_stdin_that_is_never_written` and
`..._terminal_stdin`), not this file's concern.

Two scenarios, run and asserted separately, because the issue's own ask names
both: jq genuinely on PATH, and jq genuinely absent (Git for Windows does not
ship it) so `detect-tools.sh` falls through to `_jq_fallback`'s Python path --
a materially different cost, not a hypothetical one.

── What is OBSERVED here and what is REASONED ────────────────────────────────
The constants below were measured on this agent's own platform (macOS,
system bash 3.2 via `/usr/bin/env bash` resolution) -- OBSERVED there,
REASONED everywhere else. Git Bash is known to pay extra for things this
platform does not (an extra `cygpath` per `resolve-paths.sh` call, per that
script's own comments), so the budgets carry deliberate slack rather than the
tight "+2" margin `test_post_tool_hook_spawns.py` uses for a single-platform
budget -- this file's whole point is to be the first real Windows number, and
a budget with no slack against an unmeasured platform would report its own
first real failure as a regression instead of as what it is: the first look.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

from ._bash_runner import resolve_bash
from .spawn_counting import make_shim_dir, spawns

# #432's own narrowing, not a platform skip: the windows-latest leg genuinely
# has a real, non-WSL bash (Git for Windows), so "no bash found" is the only
# honest reason to skip here -- never a silent pass for the one leg this file
# exists to exercise.
BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations) "
    "-- the #669 Windows/Git-Bash benchmark cannot run without one",
)

SESSION = "ffffffff-0000-4000-8000-000000000669"
PREV_SESSION = "eeeeeeee-0000-4000-8000-000000000669"
PREV_SESSION_LINES = 181  # arbitrary, plausible; only its type (an integer) matters

# Measured (OBSERVED, macOS, this file, this commit) cold/warm spawn counts:
#   jq present:  cold 75, warm 38
#   jq absent:   cold 63, warm 56
# Budgets carry roughly 2x slack over the observed number, not the tight "+2"
# margin a single-platform budget can afford -- see the module docstring.
# MEASURE FIRST on whatever runner reads this, THEN tighten, per the issue's
# own "Ask" item 5 -- these are deliberately generous starting values, not a
# claim about what #662-#667 should leave behind.
JQ_PRESENT_COLD_SPAWN_BUDGET = 160
JQ_PRESENT_WARM_SPAWN_BUDGET = 120
JQ_ABSENT_COLD_SPAWN_BUDGET = 160
JQ_ABSENT_WARM_SPAWN_BUDGET = 140

# A safety net against a genuine hang, not a regression detector -- wall
# clock is asserted nowhere tighter than this; see the module docstring for
# why. Measured locally: ~2.6s cold, ~0.2s warm.
WALL_TIME_CEILING_SECONDS = 45.0


def _store(tmp_path: Path):
    """A healthy, already-populated project: legacy `.remember/` layout."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)

    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": "2", "plugins": {}}), encoding="utf-8"
    )

    bodies = {
        "identity.md": "IDENTITY-BODY-669",
        "core-memories.md": "CORE-BODY-669",
        "now.md": "NOW-BODY-669",
        "recent.md": "RECENT-BODY-669",
        "archive.md": "ARCHIVE-BODY-669",
    }
    for name, body in bodies.items():
        (remember / name).write_text(body + "\n", encoding="utf-8")

    # A genuine previous session, saved: a real transcript on disk, and
    # last-save.json recording it with an INTEGER (a line count), which is
    # what session_was_saved()'s jq query actually checks for -- see the
    # module docstring for the fixture bug this avoids.
    prev_transcript = session_dir / f"{PREV_SESSION}.jsonl"
    prev_transcript.write_text(
        '{"type":"assistant","message":{"content":"x"}}\n' * PREV_SESSION_LINES,
        encoding="utf-8",
    )
    (remember / "tmp" / "last-save.json").write_text(
        json.dumps({"sessions": {PREV_SESSION: PREV_SESSION_LINES}}), encoding="utf-8"
    )

    return home, project, remember


def _path_without_jq(path_value: str) -> str:
    """PATH with every directory that holds a `jq` (or `jq.exe` etc. on
    Windows) removed -- a real "jq absent" PATH, not a mock, so
    detect-tools.sh's own `command -v jq` genuinely fails and falls through
    to `_jq_fallback`. Mirrors the suffix list tests/spawn_counting.py
    documents for the identical native-Windows-naming reason.
    """
    suffixes = [""] if os.name != "nt" else ["", ".exe", ".cmd", ".bat"]
    kept = []
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        has_jq = any((Path(entry) / ("jq" + suf)).is_file() for suf in suffixes)
        if not has_jq:
            kept.append(entry)
    return os.pathsep.join(kept)


def _env(home: Path, project: Path, remember: Path, path_value: str) -> dict:
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "PATH": path_value,
    }
    # Each PATH variant (jq present vs. absent) gets its own detect-tools.sh
    # cache key, so the two scenarios never answer from each other's cache
    # (scripts/detect-tools.sh's own #668 cache keys on the exact PATH
    # string) -- but a stale cache from a PREVIOUS test run on this same
    # PATH would still be read here, which is exactly the warm-path
    # behaviour a real repeated Windows session gets, so it is left on
    # rather than forced off.
    return env


def _payload() -> str:
    return json.dumps({
        "session_id": SESSION,
        "transcript_path": f"/does/not/matter/{SESSION}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })


def _run_once(env: dict, shims: Path, log: Path) -> tuple[subprocess.CompletedProcess, float, list[str]]:
    log.write_text("", encoding="utf-8")
    run_env = {**env, "SPAWN_LOG": str(log), "PATH": f"{shims}{os.pathsep}{env['PATH']}"}
    t0 = time.perf_counter()
    result = subprocess.run(
        [BASH, str(SESSION_START)],
        input=_payload(),
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    elapsed = time.perf_counter() - t0
    return result, elapsed, spawns(log)


def _benchmark(tmp_path: Path, record_property, *, jq_present: bool,
                cold_budget: int, warm_budget: int, label: str):
    home, project, remember = _store(tmp_path)
    base_path = os.environ["PATH"] if jq_present else _path_without_jq(os.environ["PATH"])
    env = _env(home, project, remember, base_path)
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)

    cold, cold_time, cold_spawns = _run_once(env, shims, log)
    assert cold.returncode == 0, (
        f"[{label}] cold run failed: {cold.stderr[-2000:]!r}"
    )
    warm, warm_time, warm_spawns = _run_once(env, shims, log)
    assert warm.returncode == 0, (
        f"[{label}] warm run failed: {warm.stderr[-2000:]!r}"
    )

    print(
        f"#669 benchmark [{label}]: "
        f"cold {cold_time:.3f}s / {len(cold_spawns)} spawns, "
        f"warm {warm_time:.3f}s / {len(warm_spawns)} spawns "
        f"(platform={sys.platform}, bash={BASH})"
    )
    record_property(f"remember_benchmark_{label}_cold_seconds", round(cold_time, 3))
    record_property(f"remember_benchmark_{label}_cold_spawns", len(cold_spawns))
    record_property(f"remember_benchmark_{label}_warm_seconds", round(warm_time, 3))
    record_property(f"remember_benchmark_{label}_warm_spawns", len(warm_spawns))

    assert cold_time < WALL_TIME_CEILING_SECONDS, (
        f"[{label}] cold run took {cold_time:.1f}s -- past the hang-detection "
        f"ceiling of {WALL_TIME_CEILING_SECONDS}s, not merely slow"
    )
    assert warm_time < WALL_TIME_CEILING_SECONDS, (
        f"[{label}] warm run took {warm_time:.1f}s -- past the hang-detection "
        f"ceiling of {WALL_TIME_CEILING_SECONDS}s, not merely slow"
    )
    assert len(cold_spawns) <= cold_budget, (
        f"[{label}] cold run spawned {len(cold_spawns)} processes "
        f"(budget {cold_budget}):\n  " + "\n  ".join(cold_spawns)
    )
    assert len(warm_spawns) <= warm_budget, (
        f"[{label}] warm run spawned {len(warm_spawns)} processes "
        f"(budget {warm_budget}):\n  " + "\n  ".join(warm_spawns)
    )


def test_session_start_hook_benchmark_with_jq_present(tmp_path, record_property):
    """The common case: jq resolvable on PATH, as on macOS/Linux and on a
    Windows box where the user installed it (this repo's own docs/windows.md
    tells Git-Bash users to)."""
    _benchmark(
        tmp_path, record_property, jq_present=True,
        cold_budget=JQ_PRESENT_COLD_SPAWN_BUDGET,
        warm_budget=JQ_PRESENT_WARM_SPAWN_BUDGET,
        label="jq_present",
    )


def test_session_start_hook_benchmark_with_jq_absent(tmp_path, record_property):
    """Git for Windows does not ship jq -- the issue's own stated reason this
    scenario needs its own measurement, not an assumption that the fallback
    costs the same."""
    _benchmark(
        tmp_path, record_property, jq_present=False,
        cold_budget=JQ_ABSENT_COLD_SPAWN_BUDGET,
        warm_budget=JQ_ABSENT_WARM_SPAWN_BUDGET,
        label="jq_absent",
    )
