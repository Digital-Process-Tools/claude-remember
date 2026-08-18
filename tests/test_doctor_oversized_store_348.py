"""doctor.sh must have something to say about an over-cap store (#348).

The session-start notice shipped in #347 tells the user to run
``/remember:doctor`` when a memory file is too large to inject. Today that
command says nothing at all about the condition -- it prints one line summing
every memory file's bytes and moves on. A remedy that points at a diagnostic
which is silent about the thing it was pointed at is worse than no pointer: the
user follows it, reads a clean report, and concludes the notice was noise.

So this is the other half of a notice that already ships, not new scope.

Both directions are pinned, in the same fixture shape. A check that fired on
every store would satisfy the over-cap cases and quietly tell every healthy
user their memory is broken, which is the more expensive failure of the two.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

sys.path.insert(0, str(REPO_ROOT))

from pipeline.slug import session_dir_slug as _slug  # noqa: E402

CAP = 600000  # thresholds.consolidate_max_bytes, the shipped default


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    return home, project, remember


def _run(home: Path, project: Path, remember: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DOCTOR)],
        env={
            **os.environ,
            "HOME": str(home),
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "REMEMBER_DIR": str(remember),
            "_LIB_MEMORY_DIR_LOADED": "1",
        },
        capture_output=True, text=True, timeout=180,
    )


def _fill(path: Path, size: int) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Memory\n")
        f.write("x" * size)


def test_an_over_cap_recent_is_named_by_doctor(tmp_path):
    """The state the #346 reporter is in. The report has to say so."""
    home, project, remember = _project(tmp_path)
    _fill(remember / "recent.md", CAP + 1000)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "consolidation" in result.stdout.lower(), (
        "doctor says nothing about consolidation being blocked by the store's "
        "size -- the /remember:doctor pointer in the session-start notice "
        "lands on silence:\n" + result.stdout
    )
    assert "recent.md" in result.stdout, (
        "the oversized file is not named, so the user cannot act on it:\n"
        + result.stdout
    )


def test_doctor_reports_the_size_and_the_cap_it_is_measured_against(tmp_path):
    """A byte count with no threshold beside it is not a diagnosis.

    "recent.md is 601009 bytes" is only actionable next to the number that
    makes it a problem.
    """
    home, project, remember = _project(tmp_path)
    _fill(remember / "recent.md", CAP + 1000)

    result = _run(home, project, remember)

    assert str(CAP) in result.stdout, (
        "the cap the store is measured against is not in the report:\n"
        + result.stdout
    )


def test_doctor_says_the_store_will_recover_itself(tmp_path):
    """The remedy changed with this issue and the report must not be stale.

    Before #348 the only cure was ``mv recent.md && touch``, which discards
    every byte. Now the next consolidation rotates it. A diagnostic still
    telling people to delete their memory would be worse than one saying
    nothing.
    """
    home, project, remember = _project(tmp_path)
    _fill(remember / "recent.md", CAP + 1000)

    result = _run(home, project, remember)

    assert "rotate" in result.stdout.lower(), (
        "the report does not tell the user the store recovers on its own:\n"
        + result.stdout
    )


def test_a_healthy_store_is_not_reported_as_over_cap(tmp_path):
    """Positive control's negative half, same fixture.

    Without this, a check that printed the warning unconditionally would pass
    every test above and tell every healthy user their memory is broken.
    """
    home, project, remember = _project(tmp_path)
    _fill(remember / "recent.md", 4000)
    _fill(remember / "archive.md", 20000)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "too large to consolidate" not in result.stdout.lower(), (
        "a healthy store was reported as over the consolidation cap:\n"
        + result.stdout
    )


def _verdict(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("VERDICT:"):
            return line
    raise AssertionError("no VERDICT line in output:\n" + stdout)


def test_a_self_healing_store_does_not_claim_a_problem_in_the_verdict(tmp_path):
    """recent.md is over the cap and the next round rotates it.

    Capture is unaffected and no human has to do anything, so this must not
    reach the verdict line -- the same trade the log-rotation and
    case-divergence checks already make. Overstating it devalues the one line
    commands/doctor.md tells the operator to trust without scrolling.
    """
    home, project, remember = _project(tmp_path)
    _fill(remember / "recent.md", CAP + 1000)
    (remember / "tmp" / "capture-alive").write_text("sess-1")
    (remember / "tmp" / "last-save.json").write_text(
        '{"session": "sess-1", "line": 500}', encoding="utf-8")

    result = _run(home, project, remember)

    assert "capture is working" in _verdict(result.stdout), (
        "an over-cap store that repairs itself on the next round was reported "
        "as a broken install:\n" + result.stdout
    )


def test_a_store_that_cannot_heal_itself_reaches_the_verdict(tmp_path):
    """Past-day staging alone over the cap: no rotation available changes it.

    Nothing in the pipeline will clear this, and the user reading the report
    was sent here by a notice that promised an answer. A "capture is working"
    verdict above a store that has not consolidated in months is the report
    contradicting itself -- and the verdict is the half people read.

    Paired with the test above, which is the same fixture with the bytes in a
    different file.
    """
    home, project, remember = _project(tmp_path)
    _fill(remember / "today-2020-01-01.md", CAP + 1000)
    (remember / "tmp" / "capture-alive").write_text("sess-1")
    (remember / "tmp" / "last-save.json").write_text(
        '{"session": "sess-1", "line": 500}', encoding="utf-8")

    result = _run(home, project, remember)

    assert "cannot heal itself" in result.stdout, (
        "the one store shape rotation cannot fix is not distinguished from the "
        "one it can:\n" + result.stdout
    )
    assert "problem" in _verdict(result.stdout), (
        "a store that has stopped consolidating for good was signed off as "
        "healthy in the verdict:\n" + result.stdout
    )


def test_todays_staging_is_not_counted_against_the_cap(tmp_path):
    """Consolidation excludes today's file, so the diagnostic must too.

    Counting it would invent an alarm on a store the pipeline is perfectly
    happy with -- the false-positive direction, and the expensive one for a
    command whose whole job is telling people whether to worry.
    """
    import subprocess as _sp

    home, project, remember = _project(tmp_path)
    # Ask the same `date` doctor.sh asks, rather than Python's clock: on the
    # far side of local midnight those two disagree, and a test that disagreed
    # with the script under test would fail for the wrong reason.
    today = _sp.run(["date", "+%Y-%m-%d"], capture_output=True, text=True,
                    check=True).stdout.strip()
    _fill(remember / f"today-{today}.md", CAP + 1000)

    result = _run(home, project, remember)

    assert "too large to consolidate" not in result.stdout.lower(), (
        "today's staging file was counted against the cap, but consolidation "
        "never sends it:\n" + result.stdout
    )


def test_the_cap_is_measured_on_the_sum_not_on_one_file(tmp_path):
    """The pipeline caps staging + recent.md + archive.md together.

    A doctor that only ever looked at one file would call a store healthy that
    consolidation is skipping on every round -- the false-clean direction.
    """
    home, project, remember = _project(tmp_path)
    _fill(remember / "recent.md", int(CAP * 0.6))
    _fill(remember / "archive.md", int(CAP * 0.6))

    result = _run(home, project, remember)

    assert "consolidation" in result.stdout.lower(), (
        "no file is individually over the cap but their sum is, and the round "
        "skips on exactly that sum:\n" + result.stdout
    )
