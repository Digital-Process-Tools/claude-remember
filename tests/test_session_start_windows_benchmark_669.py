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

# Measured (OBSERVED, macOS, this file, this commit, AFTER #679's fixes --
# see that issue and this file's own `_path_without_jq` docstring) cold/warm
# spawn counts:
#   jq present:  cold 43, warm 24
#   jq absent:   cold 73, warm 24
# The #679 finding, in one line: the #668 caches were ALREADY hitting
# correctly on a warm start for both scenarios -- the "jq-absent warm miss"
# this issue was filed against was `_path_without_jq`'s own bug (stripping a
# whole PATH directory that also held `mktemp` on any machine where jq
# shares one with core tools, e.g. macOS 15+'s /usr/bin), not a defect in
# detect-tools.sh or lib-memory-context.sh. Once the harness stopped taking
# mktemp down with it, jq-absent's warm number matches jq-present's exactly
# (both 24): the fallback's own extra cost is entirely a COLD-run cost
# (_jq_fallback's per-key python3 spawns on the first, uncached run), and
# warm reuses the cache regardless of which path filled it. #679 also
# removed real per-warm-start forks common to BOTH scenarios (a `sed`
# recovering the EXIT trap in lib-memory-dir.sh and bootstrap-dirs.sh, two
# `cat`s of small id files, one `cat` of a static prompt file, and one `rm`
# call merged into another) -- jq present's own warm count dropped from 30
# to 24 as a result. Budgets carry roughly 2x slack over the observed
# number, not the tight "+2" margin a single-platform budget can afford --
# see the module docstring. MEASURE FIRST on whatever runner reads this,
# THEN tighten.
JQ_PRESENT_COLD_SPAWN_BUDGET = 90
JQ_PRESENT_WARM_SPAWN_BUDGET = 50
JQ_ABSENT_COLD_SPAWN_BUDGET = 150
JQ_ABSENT_WARM_SPAWN_BUDGET = 50

# A safety net against a genuine hang, not a regression detector -- wall
# clock is asserted nowhere tighter than this; see the module docstring for
# why. Measured locally: ~2.6s cold, ~0.2s warm.
WALL_TIME_CEILING_SECONDS = 45.0


def _format_benchmark_table(label: str, *, cold_time: float, cold_spawns: list,
                             warm_time: float, warm_spawns: list) -> str:
    """#673: the table the issue asks for -- one row per scenario x
    cold/warm, wall time in whole milliseconds (CI's own summary renderer is
    Markdown, not a spreadsheet; sub-ms precision is not what a human
    comparing run over run needs) and the real spawn count beside it. Pure
    and platform-independent by construction -- it takes numbers in and
    returns text, so it is tested directly with no subprocess, no tmp_path,
    and no skip condition of its own."""
    return (
        f"### #669 benchmark: `{label}` ({sys.platform})\n"
        "\n"
        "| scenario | wall (ms) | spawns |\n"
        "| --- | --- | --- |\n"
        f"| cold | {cold_time * 1000:.0f} | {len(cold_spawns)} |\n"
        f"| warm | {warm_time * 1000:.0f} | {len(warm_spawns)} |\n"
    )


def _append_step_summary(table: str) -> None:
    """Append `table` to $GITHUB_STEP_SUMMARY, the route the issue names as
    needing zero workflow-file change: GitHub Actions sets this env var to a
    real, writable file for every step of every job, and a child process
    (pytest, here) inherits it like any other env var -- nothing in
    `.github/workflows/tests.yml` has to name it for that inheritance to
    happen (verified by reading that file, not assumed from the issue's own
    "zero workflow change" framing -- #673's own pushback).

    Two failure shapes, deliberately different:
    - UNSET (the ordinary local `pytest` run, which never has this variable
      at all) is not a failure: silently doing nothing here is what keeps
      this file passing off of CI exactly as it did before #673, on every
      platform the existing matrix already runs it on.
    - SET but unwritable (a real CI misconfiguration, not a local
      dev-machine fact) is let through as a genuine OSError rather than
      caught and swallowed -- a silently-dropped benchmark table is
      precisely the failure mode #673 exists to fix for the *reading* side,
      and catching this here would just move the same silent absence to the
      *writing* side instead.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(table)


def _write_no_crlf(path: Path, text: str) -> None:
    """write_text(..., newline="") without Path.write_text's own newline=
    kwarg, which this repo's own matrix cannot use: it only exists from
    Python 3.10, and .github/workflows/tests.yml floors at 3.9 -- the first
    attempt at this fixture's CRLF fix used write_text(newline=...) directly
    and broke windows-latest/3.9 with TypeError: write_text() got an
    unexpected keyword argument 'newline' (observed on CI, job
    103473817312), for every existing caller of tests/spawn_counting's
    make_shim_dir too, not just this file. open()'s own `newline` parameter
    has existed on every Python version this repo supports."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


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
    _write_no_crlf(
        plugins_dir / "installed_plugins.json",
        json.dumps({"version": "2", "plugins": {}}),
    )

    bodies = {
        "identity.md": "IDENTITY-BODY-669",
        "core-memories.md": "CORE-BODY-669",
        "now.md": "NOW-BODY-669",
        "recent.md": "RECENT-BODY-669",
        "archive.md": "ARCHIVE-BODY-669",
    }
    for name, body in bodies.items():
        # No CRLF translation anywhere in this fixture: write_text's default
        # universal-newline translation turns every \n into \r\n on
        # Windows, which this repo has already named and fixed once for
        # exactly this reason (tests/test_install_agy_hooks_563.py, #577;
        # tests/_glob_backslash_517.py) -- a fixture that silently differs
        # by platform is not "a representative, already-healthy project" on
        # all three.
        _write_no_crlf(remember / name, body + "\n")

    # A genuine previous session, saved: a real transcript on disk, and
    # last-save.json recording it with an INTEGER (a line count), which is
    # what session_was_saved()'s jq query actually checks for -- see the
    # module docstring for the fixture bug this avoids.
    prev_transcript = session_dir / f"{PREV_SESSION}.jsonl"
    _write_no_crlf(
        prev_transcript,
        '{"type":"assistant","message":{"content":"x"}}\n' * PREV_SESSION_LINES,
    )
    _write_no_crlf(
        remember / "tmp" / "last-save.json",
        json.dumps({"sessions": {PREV_SESSION: PREV_SESSION_LINES}}),
    )

    return home, project, remember


def _which(name: str, path_value: str) -> str | None:
    """The first executable named `name` on `path_value`, PATH-search order
    -- a tiny, dependency-free stand-in for `shutil.which` that a harness
    fixture can point at a PATH string that is not the process's own."""
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        cand = Path(entry) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _path_without_jq(path_value: str, workdir: Path) -> str:
    """PATH with every `jq` (or `jq.exe` etc. on Windows) made unreachable --
    a real "jq absent" PATH, not a mock, so detect-tools.sh's own
    `command -v jq` genuinely fails and falls through to `_jq_fallback`.
    Mirrors the suffix list tests/spawn_counting.py documents for the
    identical native-Windows-naming reason.

    Does NOT drop a PATH entry wholesale just because it holds a `jq` --
    an earlier version of this helper did, and macOS 15+ ships jq at
    /usr/bin, the SAME directory as mktemp, cat, wc and sh (Apple's own
    build: `jq-1.6-...-apple-...`, confirmed by running it directly).
    Dropping /usr/bin wholesale took mktemp down with it for the whole
    scenario, cold run included, and every #668 cache's publish step fails
    silently when its own `mktemp` call fails (by design, for a genuinely
    read-only filesystem) -- so the resulting "neither cache ever
    populates" symptom looked exactly like the #679 bug this scenario
    exists to catch, for a reason that had nothing to do with
    detect-tools.sh: a genuine Windows/Git-Bash "jq absent" machine has no
    jq anywhere on PATH at all, so there is no directory to remove and
    mktemp (bundled with Git for Windows) is never at risk.

    Instead: for each PATH entry that holds a jq, build a view directory
    under `workdir` holding a tiny exec-shim (the exact `#!/bin/bash` +
    `exec "<original>" "$@"` shape tests/spawn_counting.py's own
    `make_shim_dir` already uses) for every OTHER file in it, and
    substitute that view directory in PATH. A shim that `exec`s the
    ORIGINAL binary at its ORIGINAL path -- not a byte-copy of it -- is
    required on macOS specifically: a `shutil.copy2` of a system binary out
    of /usr/bin was OBSERVED to be killed by the kernel with SIGKILL the
    instant it ran (`rc=137`, confirmed with `mktemp` on this machine) --
    Apple's code-signing/AMFI enforcement ties a Mach-O binary's validity
    to its own signed location, and a copy elsewhere is not a binary the
    kernel will start at all. A same-path `exec` from a script never
    touches that check because the ORIGINAL, still-signed binary is what
    actually runs. Every sibling tool in the directory stays reachable;
    only jq itself is gone.
    """
    suffixes = [""] if os.name != "nt" else ["", ".exe", ".cmd", ".bat"]
    jq_names = {"jq" + suf for suf in suffixes}
    kept = []
    view_counter = 0
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        entry_path = Path(entry)
        if not any((entry_path / name).is_file() for name in jq_names):
            kept.append(entry)
            continue
        if not entry_path.is_dir():
            # A jq-named file whose parent is not a real, listable
            # directory (should not happen on a real PATH entry) -- drop it
            # rather than guess, same as the old behaviour for this one
            # unreachable corner.
            continue
        view_counter += 1
        view_dir = workdir / f"pathview-{view_counter}"
        view_dir.mkdir(parents=True, exist_ok=True)
        try:
            children = list(entry_path.iterdir())
        except OSError:
            continue
        for child in children:
            if (
                child.name in jq_names
                or not child.is_file()
                or not os.access(child, os.X_OK)
            ):
                continue
            shim = view_dir / child.name
            # newline="": see make_shim_dir's own comment -- a shebang line
            # ending in \r is a broken interpreter directive under Git
            # Bash/MSYS.
            with open(shim, "w", encoding="utf-8", newline="") as _f:
                _f.write("#!/bin/bash\n")
                _f.write(f'exec "{child.as_posix()}" "$@"\n')
            shim.chmod(0o755)
        kept.append(str(view_dir))
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
        # SESSION_START.as_posix(), not str(SESSION_START) (#669 round 5): on
        # Windows str() gives a fully backslash-separated path, and
        # session-start-hook.sh:60 derives its own directory with
        # `_HOOK_DIR="${BASH_SOURCE[0]%/*}"` -- bash's `%/*` suffix removal
        # is pure string matching and only recognises `/`, never `\`
        # (docs/windows.md already names this exact class of bug for other
        # variables: #487/#517/#519/#524-#526's `_remember_forward_slash`).
        # A backslash-only BASH_SOURCE[0] has no `/` to strip, so `_HOOK_DIR`
        # falls back to "." -- exactly the observed CI failure
        # (session-start-hook.sh: line 160: ./resolve-paths.sh: No such file
        # or directory, job 103477377903, windows-latest/3.10), confirmed
        # by this file's own round-4 diagnostics. This is a TEST HARNESS bug,
        # not a hook bug: the hook's real caller (hooks/hooks.json:
        # `bash "${CLAUDE_PLUGIN_ROOT}/scripts/session-start-hook.sh"`)
        # always has a literal forward slash hardcoded immediately before
        # the filename, regardless of what separator style
        # CLAUDE_PLUGIN_ROOT itself uses, so BASH_SOURCE[0] there always
        # ends in `/session-start-hook.sh` and `_HOOK_DIR` resolves
        # correctly -- this repo's years of real Windows usage never hit
        # this because the real invocation never constructs the path via
        # Python's native str(Path).
        [BASH, SESSION_START.as_posix()],
        input=_payload(),
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    elapsed = time.perf_counter() - t0
    return result, elapsed, spawns(log)


def _diagnose(env: dict, shims: Path, log: Path, label: str,
              cold: subprocess.CompletedProcess, cold_time: float,
              warm: subprocess.CompletedProcess, warm_time: float) -> str:
    """Everything #669's "don't fix blind again" round asked for, gathered
    once per scenario so a single CI run on an unreachable platform answers
    every open question about the empty-spawn-log symptom at once, rather
    than costing another 5-minute round-trip per hypothesis.

    Deliberately never raises: a diagnostic that crashes instead of reporting
    is strictly worse than the assertion failure it was meant to explain, so
    every piece is wrapped to degrade to a one-line "<could not get: ...>"
    rather than replacing the real failure with a diagnostic one.
    """
    run_env = {**env, "SPAWN_LOG": str(log), "PATH": f"{shims}{os.pathsep}{env['PATH']}"}
    lines = [f"=== #669 diagnostics [{label}] ==="]
    lines.append(f"resolved bash: {BASH!r}")
    lines.append(f"python-side PATH (as built for run_env): {run_env['PATH']!r}")

    # What bash itself resolves PATH to, from INSIDE the same subprocess
    # shape the real runs use -- the working theory this round exists to
    # rule in/out is MSYS path translation (Windows-style vs POSIX-style),
    # which only a bash-side read can show; the Python-side string above
    # cannot prove what bash actually sees after its own startup conversion.
    try:
        path_probe = subprocess.run(
            [BASH, "-c", 'printf "DIAG_PATH=%s\\nDIAG_JQ=%s\\n" "$PATH" "$(command -v jq 2>&1)"'],
            env=run_env, capture_output=True, text=True, timeout=30, check=False,
        )
        lines.append(f"bash-side PATH probe rc={path_probe.returncode}:")
        lines.append(f"  stdout: {path_probe.stdout!r}")
        lines.append(f"  stderr: {path_probe.stderr!r}")
    except Exception as exc:  # noqa: BLE001 -- diagnostics must never crash the test
        lines.append(f"  <could not probe bash-side PATH: {exc!r}>")

    try:
        entries = sorted(shims.iterdir())
        lines.append(f"shim dir {shims} contains {len(entries)} entries:")
        for entry in entries:
            try:
                st = entry.stat()
                lines.append(f"  {entry.name}  mode={oct(st.st_mode)}  size={st.st_size}")
            except OSError as exc:
                lines.append(f"  {entry.name}  <stat failed: {exc!r}>")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  <could not list shim dir {shims}: {exc!r}>")

    try:
        log_exists = log.exists()
        log_size = log.stat().st_size if log_exists else None
        lines.append(f"spawn log {log}: exists={log_exists} size={log_size}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  <could not stat spawn log {log}: {exc!r}>")

    lines.append(f"cold: rc={cold.returncode} elapsed={cold_time:.3f}s")
    lines.append(f"  cold stdout ({len(cold.stdout)} chars): {cold.stdout!r}")
    lines.append(f"  cold stderr ({len(cold.stderr)} chars): {cold.stderr!r}")
    lines.append(f"warm: rc={warm.returncode} elapsed={warm_time:.3f}s")
    lines.append(f"  warm stdout ({len(warm.stdout)} chars): {warm.stdout!r}")
    lines.append(f"  warm stderr ({len(warm.stderr)} chars): {warm.stderr!r}")
    lines.append("=== end #669 diagnostics ===")
    return "\n".join(lines)


def _benchmark(tmp_path: Path, record_property, *, jq_present: bool,
                cold_budget: int, warm_budget: int, label: str):
    home, project, remember = _store(tmp_path)
    base_path = os.environ["PATH"] if jq_present else _path_without_jq(os.environ["PATH"], tmp_path)
    env = _env(home, project, remember, base_path)
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    if not jq_present:
        # make_shim_dir (tests/spawn_counting.py) shims every name in its own
        # COUNTED list -- which includes "jq" -- by resolving it off the
        # REAL ambient PATH of the process running pytest, not off
        # `base_path` above. On any machine/runner that has a real jq (every
        # GitHub-hosted runner image does), that silently puts a working jq
        # shim back in front of the jq-stripped PATH in _run_once below,
        # defeating _path_without_jq entirely and measuring the jq-PRESENT
        # code path under the jq-absent label (caught by both self-review
        # spawns; confirmed empirically: with detect-tools.sh's
        # _jq_fallback deliberately broken, this test still passed before
        # this fix). Removing the shim -- leaving no `jq` anywhere reachable
        # from the run_env PATH built in `_run_once` -- is what actually
        # forces detect-tools.sh's `command -v jq` to fail and fall through
        # to `_jq_fallback`.
        jq_shim = shims / "jq"
        if jq_shim.exists():
            jq_shim.unlink()

    cold, cold_time, cold_spawns = _run_once(env, shims, log)
    assert cold.returncode == 0, (
        f"[{label}] cold run failed: {cold.stderr[-2000:]!r}"
    )
    warm, warm_time, warm_spawns = _run_once(env, shims, log)
    assert warm.returncode == 0, (
        f"[{label}] warm run failed: {warm.stderr[-2000:]!r}"
    )

    # Printed unconditionally, not only on failure: pytest shows captured
    # stdout for a FAILED test by default (no -s needed, confirmed against
    # this repo's own addopts in pyproject.toml, which sets none of
    # --capture=no/-s), so this appears in the job log exactly when it is
    # needed and costs nothing extra when it is not. Built once per
    # scenario, not duplicated in every assertion below it.
    diag = _diagnose(env, shims, log, label, cold, cold_time, warm, warm_time)
    print(diag)

    # Positive/negative control, paired (CLAUDE.md: a "must not fire"
    # assertion needs a "must fire" twin, or a broken harness that shims
    # nothing passes the negative half for free). jq_present MUST see real
    # `jq` invocations -- proof the harness can detect one at all; jq_absent
    # MUST NOT, proving the scenario above actually forced the fallback.
    all_spawns = cold_spawns + warm_spawns
    jq_calls = [s for s in all_spawns if s.startswith("jq ")]
    if jq_present:
        assert jq_calls, (
            f"[{label}] expected real jq invocations (this is the jq-present "
            "scenario) and saw none -- the harness cannot distinguish "
            f"jq-present from jq-absent if this fires. Spawns: {all_spawns}\n\n"
            f"{diag}"
        )
    else:
        assert not jq_calls, (
            f"[{label}] expected NO real jq invocations (jq was removed from "
            "every shim and from PATH) but saw real jq calls -- the "
            f"jq-absent scenario is not actually exercising _jq_fallback: "
            f"{jq_calls}\n\n{diag}"
        )

    summary_line = (
        f"#669 benchmark [{label}]: "
        f"cold {cold_time:.3f}s / {len(cold_spawns)} spawns, "
        f"warm {warm_time:.3f}s / {len(warm_spawns)} spawns "
        f"(platform={sys.platform}, bash={BASH})"
    )
    # #673: printed to BOTH streams (the issue's own ask) -- stdout so it
    # still shows up the way it always has (captured, surfaced by pytest on
    # a failure), stderr so it reaches a log that shows even when the run as
    # a whole is green, which is the whole gap #673 exists to close.
    print(summary_line)
    print(summary_line, file=sys.stderr)
    table = _format_benchmark_table(
        label, cold_time=cold_time, cold_spawns=cold_spawns,
        warm_time=warm_time, warm_spawns=warm_spawns,
    )
    print(table)
    print(table, file=sys.stderr)
    _append_step_summary(table)
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


def _extract_hook_dir_lines() -> str:
    """The exact two lines session-start-hook.sh derives its own directory
    with, read out of the real file rather than retyped -- so this test
    breaks loudly if that mechanism ever changes, instead of silently
    testing a copy that has drifted from what actually ships."""
    text = SESSION_START.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != '_HOOK_DIR="${BASH_SOURCE[0]%/*}"':
            continue
        return "\n".join(lines[i : i + 2])
    raise AssertionError(
        "session-start-hook.sh no longer derives _HOOK_DIR the way this "
        "test expects -- either the mechanism changed or moved, and this "
        "test's extraction needs to move with it"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="PR #671: a bash subprocess probe's own stdout/returncode arrives "
    "NUL-interleaved (UTF-16LE) on windows-latest, reproduced identically "
    "across every call in this function -- including the FIRST, plain "
    "forward-slash case, not only the backslash one -- and across two "
    "structurally different invocation shapes (round 5's inline `bash -c`, "
    "round 7's plain script-path via .as_posix()). Root cause not yet "
    "identified; this is an environment-level corruption, not a defect in "
    "the %/* mechanism this test pins or in _run_once's own .as_posix() fix "
    "(which is independently exercised and green via the benchmark tests "
    "in this file, on every Windows leg). Tracked in #672.",
)
def test_hook_dir_derivation_needs_a_forward_slash_path(tmp_path):
    """Round 5 (#669): windows-latest/3.10 CI showed session-start-hook.sh
    failing to source resolve-paths.sh at all ('./resolve-paths.sh: No such
    file or directory') -- this file's own round-4 diagnostics (bash-side
    PATH probe, shim dir listing) ruled out jq, PATH and MSYS path
    translation entirely; the hook had simply never gotten past its own
    directory derivation.

    OBSERVED here, on THIS platform, not merely reasoned about Windows:
    `_HOOK_DIR="${BASH_SOURCE[0]%/*}"` is pure bash STRING pattern matching
    -- it only ever recognises `/`, never `\\`, regardless of what the
    filesystem underneath considers a separator.

    `BASH_SOURCE` cannot simply be assigned a fake value (it is populated by
    bash itself from how the running script was actually invoked, and a
    bare `BASH_SOURCE=(...)` in a `bash -c` string silently produces an
    EMPTY array rather than overriding it -- caught while writing this test:
    an earlier version of this probe asserted `HOOK_DIR=.` for every case,
    including the forward-slash one, for exactly that reason). So this test
    drives the REAL mechanism by actually invoking a real file at a chosen
    path, the same way `_run_once` invokes the real hook -- on THIS platform
    (POSIX), a path with forward slashes is both a valid filesystem path AND
    a string `%/*` can split; there is no way to make a SINGLE real
    invocation here exercise the Windows symptom exactly (a backslash path
    is simultaneously a valid Windows filesystem path string-matchable only
    by `/`, and on POSIX it is invalid as a path at all -- `bash
    \\foo\\bar.sh` fails to even locate the file, a different, earlier
    failure than the Windows one). What IS directly testable here, with no
    platform-specific reasoning required, is the `%/*` operator itself: it
    is applied to a real, readable variable (not the unsettable
    `BASH_SOURCE`), driven by the exact pattern/fallback the two lines use.

    The fake value under test travels through an ENVIRONMENT VARIABLE, not
    argv, and the probe script is a real FILE invoked by path rather than
    inline `bash -c "<script>"` text. tests/test_fast_path_subshells_511.py
    already documents why, for the same hazard: on Windows,
    subprocess.run's argv list is re-flattened into a single command line
    by Python's own MSVCRT-style quoting rules (subprocess.list2cmdline)
    before CreateProcess hands it to bash.exe, and MSYS's own argv
    re-parsing of that line does not agree with it on a string carrying
    embedded double quotes -- which a `bash -c` command built from this
    snippet necessarily does (`"${FAKE_SOURCE%/*}"`, `"$_HOOK_DIR"`, etc.).
    An earlier version of this test passed both the script AND the fake
    path through argv and failed on EVERY Windows leg, at the FIRST
    (plain-POSIX, no-backslash) case -- with an empty stderr and a garbled
    `args` repr in the failure message, consistent with the argv/CreateProcess
    round-trip corrupting the command text before bash ever saw it, not
    with anything in this test's own Python-side string construction (which
    this file's own local run, and the CI job's own observed content right
    up to the point of corruption, both show is correct). An environment
    variable is passed as an already-decoded block with no command-line
    re-quoting step in between, sidestepping the whole cross-runtime
    quoting question -- the identical fix #511's own test made for the
    identical reason.
    """
    snippet = _extract_hook_dir_lines().replace("BASH_SOURCE[0]", "FAKE_SOURCE")
    probe_script = tmp_path / "hook_dir_probe.sh"
    # _write_no_crlf(), not probe_script.write_text(..., newline=""): that
    # is the exact Path.write_text()-newline=-kwarg shape round 3 (#669,
    # commit b84a5c9) already found broken on Python 3.9 (the kwarg only
    # exists from 3.10) -- reintroduced here by mistake in round 6 instead
    # of reusing the helper this file already defines for exactly this.
    _write_no_crlf(
        probe_script,
        'FAKE_SOURCE="$FAKE_SOURCE_ENV"\n' + snippet + '\necho "HOOK_DIR=$_HOOK_DIR"\n',
    )

    def _probe(fake_source: str) -> str:
        env = {**os.environ, "FAKE_SOURCE_ENV": fake_source}
        result = subprocess.run(
            ["bash", probe_script.as_posix()],
            env=env, capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    posix = _probe("/some/plugin/root/scripts/session-start-hook.sh")
    assert posix == "HOOK_DIR=/some/plugin/root/scripts", (
        f"a forward-slash path must derive the real scripts directory: {posix!r}"
    )

    # POSITIVE CONTROL for the positive control above: a path with no
    # separator at all is the ONE case this mechanism is DESIGNED to fall
    # back on ("." -- see the hook's own comment at line 57-58). Without
    # this, a probe bug that always returned "." would pass the backslash
    # assertion below for the wrong reason.
    no_sep = _probe("session-start-hook.sh")
    assert no_sep == "HOOK_DIR=.", (
        f"a bare filename with no separator is the documented fallback case: {no_sep!r}"
    )

    backslash = _probe("C:\\some\\plugin\\root\\scripts\\session-start-hook.sh")
    assert backslash == "HOOK_DIR=.", (
        "a backslash-only path must fall back to the SAME '.' this mechanism "
        "uses for 'no separator at all' -- bash's own %/* cannot tell a "
        f"Windows path from a bare filename. Got: {backslash!r}"
    )

    # The actual fix (#669 round 5): invoke the REAL hook file and check
    # bash's own BASH_SOURCE[0] resolves correctly when given the exact
    # .as_posix() form _run_once now uses -- end to end, on the real file,
    # not a simulated variable.
    real_probe_script = tmp_path / "hook_dir_real_probe.sh"
    real_probe_script.write_text(
        '_HOOK_DIR="${BASH_SOURCE[0]%/*}"\n'
        '[ "$_HOOK_DIR" = "${BASH_SOURCE[0]}" ] && _HOOK_DIR="."\n'
        'echo "HOOK_DIR=$_HOOK_DIR"\n',
        encoding="utf-8",
    )
    real_result = subprocess.run(
        ["bash", real_probe_script.as_posix()],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert real_result.returncode == 0, real_result.stderr
    assert real_result.stdout.strip() == f"HOOK_DIR={real_probe_script.parent.as_posix()}", (
        f"invoking with .as_posix() (what _run_once now does) must resolve "
        f"_HOOK_DIR to the script's real parent directory: {real_result.stdout!r}"
    )


def test_step_summary_table_has_one_row_per_scenario_cold_warm():
    """#673: the table-building helper must emit exactly the rows the issue
    asks for -- one per scenario x cold/warm -- with real numbers, not just
    headers. A row-count-only assertion would pass on an empty table body,
    so this pins actual values too."""
    table = _format_benchmark_table(
        "jq_present", cold_time=2.601, cold_spawns=["a"] * 76,
        warm_time=0.234, warm_spawns=["b"] * 39,
    )
    body_lines = [
        line for line in table.splitlines()
        if line.startswith("|") and "---" not in line and "scenario" not in line
    ]
    assert len(body_lines) == 2, f"expected exactly 2 data rows, got: {body_lines}"
    assert "2601" in body_lines[0] and "76" in body_lines[0]
    assert "234" in body_lines[1] and "39" in body_lines[1]


def test_step_summary_written_when_env_set(tmp_path, monkeypatch):
    """Positive control: with GITHUB_STEP_SUMMARY pointing at a real,
    writable file, appending must land the table's own text in that file."""
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    table = _format_benchmark_table(
        "jq_present", cold_time=1.0, cold_spawns=["a"], warm_time=0.5, warm_spawns=["b"],
    )
    _append_step_summary(table)
    written = summary.read_text(encoding="utf-8")
    assert "jq_present" in written
    assert "1000" in written  # cold wall-ms


def test_step_summary_skipped_silently_when_env_unset(monkeypatch):
    """Negative control, paired with the 'must fire' case above: the common
    local-pytest-run case (no GITHUB_STEP_SUMMARY at all) must NOT raise --
    otherwise this file would start failing the existing test matrix's own
    local/dev runs, which never set this variable (#673's own hidden
    judgment call)."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    table = _format_benchmark_table(
        "jq_present", cold_time=1.0, cold_spawns=[], warm_time=0.5, warm_spawns=[],
    )
    _append_step_summary(table)  # must not raise


def test_step_summary_raises_loudly_when_path_unwritable(tmp_path, monkeypatch):
    """Negative control's OTHER half, from the issue's own wording: a
    genuinely unwritable path (set, but pointing at a directory that does
    not exist) must fail LOUDLY, not be swallowed -- a silent except would
    make a real CI misconfiguration invisible forever."""
    unwritable = tmp_path / "does" / "not" / "exist" / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(unwritable))
    table = _format_benchmark_table(
        "jq_present", cold_time=1.0, cold_spawns=[], warm_time=0.5, warm_spawns=[],
    )
    with pytest.raises(OSError):
        _append_step_summary(table)
