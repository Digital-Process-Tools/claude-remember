"""Issue 1115: the macOS pytest(3.12) leg hit the "Run tests" step's
25-minute timeout-minutes twice on main (run 42282642194, attempt 1 job
128115350145 and attempt 2 job 128128937578), while the 3.9/3.10/3.11
macOS legs in the same run passed with only ~3 minutes of headroom left
under that same limit (pytest totals 1306.78s-1357.88s). The step-level
cap was sized from Windows-hang risk only (#1014 round 2) and never
accounted for 3.12's own longer wall-clock time, so it needs real
headroom -- and the job-level cap (sized the same way, #1002 round 2)
must grow with it or it becomes the new, tighter bottleneck.

Written before the fix: on the pre-fix workflow, step-level
timeout-minutes is 25 and job-level is 30 for both the plain `pytest` job
and the `hook-tests-compiled` job (which shares the identical values and
explicitly cross-references the same reasoning in its own comments).

Only test_pytest_job_step_timeout_has_real_macos_312_headroom actually
pins a value the pre-fix file cannot satisfy (25 < MIN_STEP_TIMEOUT) --
that is the one assertion this test cannot pass by the code doing
nothing. The other three assertions describe relationships (job cap
above step cap; hook-tests-compiled matching the plain pytest job) that
already held pre-fix, since both jobs' values were already equal and the
old job-step gap was already >= 5 minutes. They are kept anyway as
regression guards: once the headroom fix lands, nothing else stops a
later change from raising one job's timeout and leaving the other
stale, or from shrinking the job/step gap back down -- these pin that
relationship going forward even though they do not, by themselves,
distinguish the pre-fix file from the fixed one.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# A real macOS 3.12 run needs roughly 28 minutes; the old 25-minute step cap
# left no room for that at all, and the old 30-minute job cap left only
# ~2 extra minutes beyond that real 28-minute runtime (30 - 28 = 2) for
# checkout/setup/upload overhead -- not beyond the step cap itself, which
# had a full 5-minute gap (30 - 25 = 5) that was never the tight part.
# Both must grow, and the job cap must stay comfortably above the step cap.
MIN_STEP_TIMEOUT = 35
MIN_JOB_TIMEOUT_OVER_STEP = 5


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _pytest_job(doc: dict) -> dict:
    return doc["jobs"]["pytest"]


def _hook_tests_compiled_job(doc: dict) -> dict:
    return doc["jobs"]["hook-tests-compiled"]


def _run_tests_step(job: dict) -> dict:
    for step in job["steps"]:
        if step.get("name") == "Run tests":
            return step
    raise AssertionError("no 'Run tests' step found in the pytest job")


def _compiled_run_step(job: dict) -> dict:
    for step in job["steps"]:
        if step.get("name") == "Run the full suite against the compiled hooks":
            return step
    raise AssertionError(
        "no 'Run the full suite against the compiled hooks' step found"
    )


def test_pytest_job_step_timeout_has_real_macos_312_headroom():
    doc = _doc()
    job = _pytest_job(doc)
    step = _run_tests_step(job)
    assert step["timeout-minutes"] >= MIN_STEP_TIMEOUT, (
        "the 'Run tests' step timeout must be raised well above the old "
        "25 minutes -- a real macOS 3.12 run needs roughly 28 minutes on "
        "its own (#1115)"
    )


def test_pytest_job_level_timeout_stays_above_the_step_timeout():
    doc = _doc()
    job = _pytest_job(doc)
    step = _run_tests_step(job)
    assert job["timeout-minutes"] >= step["timeout-minutes"] + MIN_JOB_TIMEOUT_OVER_STEP, (
        "the job-level cap must leave real headroom over the step-level "
        "cap for checkout/setup/upload steps, or it becomes the new, "
        "tighter bottleneck instead of the step timeout"
    )


def test_hook_tests_compiled_step_timeout_matches_pytest_jobs_new_value():
    doc = _doc()
    pytest_step = _run_tests_step(_pytest_job(doc))
    compiled_step = _compiled_run_step(_hook_tests_compiled_job(doc))
    assert compiled_step["timeout-minutes"] == pytest_step["timeout-minutes"], (
        "hook-tests-compiled's own comment explicitly claims the identical "
        "reasoning as the plain pytest job's 'Run tests' step -- its "
        "timeout must move in lockstep rather than being raised for one "
        "job and left stale on the other"
    )


def test_hook_tests_compiled_job_level_timeout_matches_pytest_jobs_new_value():
    doc = _doc()
    pytest_job = _pytest_job(doc)
    compiled_job = _hook_tests_compiled_job(doc)
    assert compiled_job["timeout-minutes"] == pytest_job["timeout-minutes"], (
        "hook-tests-compiled's own comment explicitly claims the identical "
        "reasoning as the plain pytest job's own job-level cap -- it must "
        "move in lockstep rather than being raised for one job and left "
        "stale on the other"
    )
