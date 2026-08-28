"""doctor.sh's SessionEnd verdict no longer outranks a PostToolUse arm that has
already named a more specific cause, on an aged store (#404).

#392/#400 fixed the fresh-install half of this displacement (a quiet
transcript predating the store's own baseline no longer counts as evidence).
They did not move the VERDICT ladder itself: `_SESSION_END_STATE = "not-fired"`
still sat above the PostToolUse-exiting-early arm, so on an AGED store --
baseline genuinely in the past, transcripts genuinely quiet after it -- the
displacement survived: "SessionEnd has never fired" won over the actionable,
more specific "PostToolUse is wired and running, but has not serviced a
session -- it is exiting early" diagnosis already printed higher in the same
report.

The fix moves that one arm below the two PostToolUse arms that already name a
cause (never fired at all; fired but never completed a save) -- ladder rule at
scripts/doctor.sh:751, "specific causes before the general one". It leaves
SessionEnd's own priority over "capture is working" and over "PostToolUse
never fired at all" untouched: those are pinned by
test_doctor_session_end_370.py and are not this issue's scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.test_doctor_session_end_370 import (
    _backdate,
    _install_store,
    _project,
    _run,
    _verdict,
)


def test_post_tool_use_exiting_early_outranks_session_end_on_an_aged_store(tmp_path):
    """The specific cause (PostToolUse wired and exiting early) must win.

    Aged store: the install marker is genuinely two hours old. A prior
    session's transcript has gone quiet for over 15 minutes since then, which
    is what makes `_SESSION_END_STATE` become "not-fired" at all. PostToolUse
    HAS run here (`tmp/post-tool-ran` exists) but has never once produced a
    live marker (`tmp/capture-alive` absent) or a completed save
    (`tmp/last-save.json` absent) -- the exact "wired and running, but exiting
    early" shape scripts/doctor.sh:274-278 already reports, above the VERDICT
    line, as the actionable cause.

    Before the fix, the VERDICT line named SessionEnd instead, even though
    SessionEnd's own silence here is fully explained by PostToolUse never
    having reached a save in the first place.
    """
    home, project, remember, session_dir = _project(tmp_path)
    _install_store(remember, 7200)
    stale = session_dir / "aaaa-quiet-session.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    _backdate(stale, 3600)

    (remember / "tmp" / "post-tool-ran").write_text("1", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd has never fired" in result.stdout, (
        "an aged store with a quiet post-baseline transcript did not reach "
        "_SESSION_END_STATE=not-fired -- fixture is not exercising the case "
        "this test means to pin:\n" + result.stdout
    )
    verdict = _verdict(result.stdout)
    assert verdict.startswith(
        "VERDICT: problem — PostToolUse has fired but no save has completed yet"
    ), (
        "SessionEnd's general verdict outranked the more specific "
        "PostToolUse-exiting-early cause already printed above it:\n"
        + result.stdout
    )
    assert "SessionEnd" not in verdict, (
        "the VERDICT line named SessionEnd instead of the specific, "
        "actionable PostToolUse cause:\n" + result.stdout
    )


def test_positive_control_session_end_verdict_still_reachable_with_no_post_tool_arm(
    tmp_path,
):
    """Positive control: with no PostToolUse arm fired at all (never wired),
    the SessionEnd verdict must still be reachable -- otherwise a fix that
    simply deleted or permanently masked the SessionEnd arm would also
    satisfy the test above.

    Same aged-store, quiet-transcript shape, but with nothing at all written
    under tmp/ -- PostToolUse has never fired for this project, which is the
    general case the ladder's own rule (scripts/doctor.sh:751) ranks BELOW
    SessionEnd's own silent failure (see
    test_doctor_session_end_370.test_prior_sessions_ended_after_install_fails_and_reaches_verdict,
    unchanged by this fix).
    """
    home, project, remember, session_dir = _project(tmp_path)
    _install_store(remember, 7200)
    stale = session_dir / "aaaa-quiet-session.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    _backdate(stale, 3600)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd has never fired" in result.stdout
    verdict = _verdict(result.stdout)
    assert verdict.startswith("VERDICT: problem — SessionEnd"), (
        "with no PostToolUse arm fired at all, SessionEnd's own verdict was "
        "not reachable:\n" + result.stdout
    )
