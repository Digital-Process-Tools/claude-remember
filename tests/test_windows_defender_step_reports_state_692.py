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


def test_defender_comment_no_longer_claims_a_tax_it_cannot_show_is_charged() -> None:
    text = _workflow_text()
    assert "removes that tax" not in text, (
        "the old comment asserted the exclusion removes a scanning cost with "
        "nothing in the log to show it was ever charged (#692) -- it must not "
        "come back"
    )


def test_the_other_windows_only_step_is_still_present() -> None:
    """Positive control: a workflow-text assertion that finds nothing to check
    against would also pass the two tests above vacuously if the file were
    empty, unreadable, or the wrong file entirely."""
    text = _workflow_text()
    assert "Install Windows tzdata" in text
