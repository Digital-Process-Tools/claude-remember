"""#320 — the recognised field set is an assumption, and it must say when it fails.

`_failure_haystack` (#318) narrowed the marker scan to the fields the CLI
authors — `error` / `result` / `message`, the `errors` list, stderr — and falls
back to the raw stdout scan only `if not authored`, i.e. only when *every*
recognised field is empty.

So a terminal record that carries an auth failure in some **new** diagnostic
field while a recognised field holds something benign is scanned, found clean,
and reported as "not an isolation problem". The un-isolated retry never runs,
capture fails permanently, and nothing says why — #316's shape, reached through
the field set instead of through the spelling list.

The fix is not to widen the haystack. Scanning the raw blob whenever the
authored text holds no marker hands the decision back to the conversation for
every non-auth failure, which is the whole of what #318 closed. Three states
instead of two:

  * a marker in a field the CLI authors      → retry un-isolated (unchanged)
  * no marker anywhere                        → no retry, silently (unchanged)
  * a marker in the terminal record but
    outside the fields we scan                → no retry, and SAY SO

The third is new. The decision stays exactly as narrow as #318 made it; what
stops is the silence.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import haiku
from pipeline.haiku import _isolation_may_be_the_cause


@pytest.fixture
def warnings(monkeypatch):
    recorded: list[str] = []
    monkeypatch.setattr(haiku, "_warn", recorded.append)
    return recorded


UNRECOGNISED_FIELD_AUTH_FAILURE = json.dumps({
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    # Recognised, non-empty, and benign — this is what keeps `authored` truthy
    # and the raw-scan fallback unreachable.
    "result": "Session ended before a reply was produced.",
    # Not in the recognised set. The whole point of the issue.
    # One marker, not two, so the assertion on *which* one is reported is not a
    # statement about the tuple's iteration order.
    "diagnostic": "Failed to authenticate: no credentials could be resolved.",
})


def test_a_marker_outside_the_scanned_fields_does_not_silently_read_as_clean(warnings):
    """The one thing that must not happen: nothing at all."""
    assert _isolation_may_be_the_cause(UNRECOGNISED_FIELD_AUTH_FAILURE, "") is False
    assert warnings, (
        "an auth marker sat in the terminal record and the scan reported "
        "nothing — the #316 outage with no trace"
    )


def test_the_report_names_the_marker_and_the_fields_that_were_scanned(warnings):
    _isolation_may_be_the_cause(UNRECOGNISED_FIELD_AUTH_FAILURE, "")
    message = "\n".join(warnings).lower()
    assert "failed to authenticate" in message, "the marker it found is not named"
    assert "diagnostic" in message, "the field it found it in is not named"
    for scanned in ("error", "result", "message", "errors"):
        assert scanned in message, f"the scanned set does not name {scanned!r}"


def test_reporting_it_does_not_retry_un_isolated(warnings):
    """The report is a log line, not a decision.

    Widening the haystack here would let any failing call whose recognised
    fields hold no marker be scanned in full again — including the assistant
    content #318 removed from this decision.
    """
    conversation_quoting_a_marker = json.dumps([
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "you hit 'not logged in'"}]},
        },
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["connect ECONNREFUSED 127.0.0.1:443"],
        },
    ])
    assert _isolation_may_be_the_cause(conversation_quoting_a_marker, "") is False
    assert warnings == [], (
        "the conversation is not the terminal record, and flagging it would "
        "make the report fire on every ordinary network failure"
    )


def test_an_ordinary_failure_reports_nothing(warnings):
    stdout = json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "API Error: 429 rate_limit_error",
        "duration_ms": 812,
    })
    assert _isolation_may_be_the_cause(stdout, "") is False
    assert warnings == []


def test_a_recognised_auth_failure_reports_nothing_and_still_retries(warnings):
    stdout = json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "Failed to authenticate. API Error: 401 Invalid bearer token",
        "diagnostic": "Failed to authenticate. API Error: 401 Invalid bearer token",
    })
    assert _isolation_may_be_the_cause(stdout, "") is True
    assert warnings == [], "nothing was missed — the marker was in a scanned field"


def test_a_flag_rejection_outside_the_scanned_fields_is_reported_too(warnings):
    """Same blindness, other branch.

    `unknown option` decides the same retry. A CLI that reports a rejected flag
    in a new field while `result` holds something benign takes the same silent
    path.
    """
    stdout = json.dumps({
        "type": "result",
        "is_error": True,
        "result": "the nested call did not start",
        "cli_error": "error: unknown option '--setting-sources'",
    })
    assert _isolation_may_be_the_cause(stdout, "") is False
    assert warnings, "a rejected isolation flag went unreported"


def test_the_recognised_field_set_is_pinned():
    """A schema change breaks a test rather than a user's capture (#320, option 1).

    These names are an assumption about the terminal-record schema, not a fact.
    Adding one is correct; adding one without noticing that #316's recovery
    depends on the set is not.
    """
    assert haiku._SCANNED_FAILURE_FIELDS == ("error", "result", "message")
    assert haiku._SCANNED_FAILURE_LIST_FIELDS == ("errors",)


def test_the_318_narrowing_is_unchanged():
    """Regression guard: none of the above may widen the haystack."""
    conversation_only = json.dumps([
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "invalid api key"}]},
        },
        {"type": "result", "is_error": True, "errors": ["ECONNREFUSED"]},
    ])
    assert _isolation_may_be_the_cause(conversation_only, "") is False
    assert _isolation_may_be_the_cause("Not logged in. Please run /login", "") is True
