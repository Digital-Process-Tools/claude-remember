"""#692: the Defender-exclusion step in tests.yml claimed a scanning tax that
measurement showed was not being charged on the current windows-latest image
(a state read there returned RealTimeProtectionEnabled: False, ExclusionPath
already covering the whole drive, before the step ran). The step itself was
harmless; the comment asserting it removed a tax was not, and fed a wrong
inference during #660 about why the Windows legs looked fast.

Runner images change, so the fix does not delete the step -- it makes the
step report Defender's own state before adding its exclusions, so the log
says whether the exclusion is doing anything on the image of the day,
instead of asserting a load-bearing effect nothing in the log can check.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "tests.yml"


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def test_defender_step_reports_its_own_state_before_excluding() -> None:
    text = _workflow_text()
    assert "Get-MpComputerStatus" in text, (
        "the step must query Defender's real-time-protection state, not just "
        "assert one in a comment (#692)"
    )
    assert "RealTimeProtectionEnabled" in text
    assert "ExclusionPath" in text

    # Reporting state and then mutating it in the same step only proves anything
    # if the read happens first -- a state-report that ran after the exclusions
    # were added would print this step's own change, not the runner's own state.
    report_at = text.index("RealTimeProtectionEnabled")
    exclude_at = text.index('Add-MpPreference -ExclusionPath "${{ github.workspace }}"')
    assert report_at < exclude_at, (
        "the state report must be printed before the exclusions are added, or "
        "the printed ExclusionPath includes this step's own additions (#692)"
    )


def test_defender_comment_no_longer_claims_a_tax_it_cannot_show_is_charged() -> None:
    text = _workflow_text().lower()
    # A substring match on one exact phrasing only catches that phrasing coming
    # back verbatim -- check a spread of the ways the same unchecked claim could
    # be restated, not just the one wording #692 happened to use.
    banned = (
        "removes that tax",
        "removes the tax",
        "removes the scanning tax",
        "eliminates that tax",
        "eliminates the scanning cost",
        "removes the scanning cost",
    )
    found = [phrase for phrase in banned if phrase in text]
    assert not found, (
        "the old comment asserted the exclusion removes a scanning cost with "
        "nothing in the log to show it was ever charged (#692) -- it, or a "
        f"rewording of it, must not come back: {found}"
    )


def test_the_other_windows_only_step_is_still_present() -> None:
    """Positive control: a workflow-text assertion that finds nothing to check
    against would also pass the two tests above vacuously if the file were
    empty, unreadable, or the wrong file entirely."""
    text = _workflow_text()
    assert "Install Windows tzdata" in text
