"""#870, claim 3: the un-isolated-retry warning always blames the rejected
--setting-sources flag, even when the retry then fails with the SAME
authentication error -- proving isolation was never the cause. The daily log
excerpt in the issue showed this exact pair, repeated on every save for ten
days: "this CLI rejected --setting-sources (Failed to authenticate: ...)"
followed immediately by the retry failing with the identical auth error,
and nothing ever said isolation was innocent or pointed at the real fix
(REMEMBER_OAUTH_TOKEN / `claude setup-token`, #129/#131).

A must-fire / must-not-fire pair: the retry failing with the SAME auth
marker must print the correction; the retry failing with an unrelated
reason (so the correction would be a non-sequitur) must not.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import _HOOK_ISOLATION_FLAG, call_haiku


AUTH_ERROR = json.dumps({
    "type": "result", "subtype": "error_during_execution", "is_error": True,
    "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
})

RATE_LIMIT_ERROR = json.dumps({
    "type": "result", "subtype": "error_during_execution", "is_error": True,
    "result": "API Error: 429 rate_limit_error",
})


@patch("pipeline.haiku.subprocess.run")
def test_an_auth_only_failure_is_not_blamed_on_the_rejected_flag(mock_run):
    """Self-review regression: _isolation_may_be_the_cause() also returns True
    on a pure auth-marker match with no "unknown option" text anywhere -- the
    flag was never rejected in that case, so the first warning must not claim
    it was. Positive control for the "unknown option" wording is the next
    test, so a fix that deleted the flag-rejection wording outright would
    fail that one instead of passing both for the wrong reason."""
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout=AUTH_ERROR, stderr=""),
        MagicMock(returncode=1, stdout=RATE_LIMIT_ERROR, stderr=""),
    ]

    with patch("pipeline.haiku._warn") as mock_warn:
        try:
            call_haiku("p")
            assert False, "should raise -- both attempts failed"
        except RuntimeError:
            pass

    warnings = " ".join(str(c.args[0]) for c in mock_warn.call_args_list)
    assert "rejected" not in warnings, (
        "a pure auth failure, with no 'unknown option' text, was blamed on "
        f"a rejected flag that was never rejected:\n{warnings}"
    )
    assert "failed authentication" in warnings, (
        f"the real cause (auth) was not named:\n{warnings}"
    )


@patch("pipeline.haiku.subprocess.run")
def test_an_unknown_option_failure_still_blames_the_flag(mock_run):
    """Positive control for the test above: when the CLI's own failure text
    DOES say "unknown option" naming this flag, the original wording is
    correct and must still be used."""
    unknown_option_error = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": f"error: unknown option '{_HOOK_ISOLATION_FLAG}'",
    })
    ok_response = json.dumps({"result": "## 10:00 | did stuff", "input_tokens": 10, "output_tokens": 5})
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout=unknown_option_error, stderr=""),
        MagicMock(returncode=0, stdout=ok_response, stderr=""),
    ]

    with patch("pipeline.haiku._warn") as mock_warn:
        call_haiku("p")

    warnings = " ".join(str(c.args[0]) for c in mock_warn.call_args_list)
    assert "rejected" in warnings, (
        f"a genuine 'unknown option' failure stopped being named as a "
        f"rejected flag:\n{warnings}"
    )


@patch("pipeline.haiku.subprocess.run")
def test_retry_failing_with_the_same_auth_error_names_isolation_as_innocent(mock_run):
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout=AUTH_ERROR, stderr=""),
        MagicMock(returncode=1, stdout=AUTH_ERROR, stderr=""),
    ]

    with patch("pipeline.haiku._warn") as mock_warn:
        try:
            call_haiku("p")
            assert False, "should raise -- both attempts failed"
        except RuntimeError:
            pass

    warnings = " ".join(str(c.args[0]) for c in mock_warn.call_args_list)
    assert "isolation was not the cause" in warnings, (
        "a retry failing with the identical auth error must say isolation "
        "was not the cause:\n" + warnings
    )
    assert "REMEMBER_OAUTH_TOKEN" in warnings, (
        "the correction must point at the documented remedy:\n" + warnings
    )
    assert mock_run.call_count == 2, "the retry must actually have run"


@patch("pipeline.haiku.subprocess.run")
def test_retry_succeeding_prints_no_correction(mock_run):
    """Positive control: when the un-isolated retry SUCCEEDS, there is
    nothing to correct -- the first warning was right that dropping
    isolation fixed it. Pins that the new check does not fire unconditionally
    after every retry."""
    ok_response = json.dumps({"result": "## 10:00 | did stuff", "input_tokens": 10, "output_tokens": 5})
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout=AUTH_ERROR, stderr=""),
        MagicMock(returncode=0, stdout=ok_response, stderr=""),
    ]

    with patch("pipeline.haiku._warn") as mock_warn:
        call_haiku("p")

    warnings = " ".join(str(c.args[0]) for c in mock_warn.call_args_list)
    assert "isolation was not the cause" not in warnings, (
        "a successful retry must not print the correction:\n" + warnings
    )


@patch("pipeline.haiku.subprocess.run")
def test_retry_failing_for_an_unrelated_reason_prints_no_correction(mock_run):
    """Negative control: the retry fails, but NOT with an auth marker -- the
    correction ("isolation was not the cause, it's your login") would be
    unsupported for a rate limit, so it must not print."""
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout=AUTH_ERROR, stderr=""),
        MagicMock(returncode=1, stdout=RATE_LIMIT_ERROR, stderr=""),
    ]

    with patch("pipeline.haiku._warn") as mock_warn:
        try:
            call_haiku("p")
            assert False, "should raise -- both attempts failed"
        except RuntimeError:
            pass

    warnings = " ".join(str(c.args[0]) for c in mock_warn.call_args_list)
    assert "isolation was not the cause" not in warnings, (
        "a retry failing for an unrelated (non-auth) reason must not claim "
        "isolation was not the cause:\n" + warnings
    )
