"""#318 — the marker scan must read the CLI's error, not the conversation.

`_isolation_may_be_the_cause` decides whether to retry the nested call
**without** `--setting-sources ''`, i.e. with the user's hooks live. That makes
the marker tuple a trust boundary, not a convenience list, and it was matched by
a substring scan over the whole of stdout.

What the audit hypothesised — that model-authored text reaches that haystack on
a failing call — is **not** reachable through `error_max_turns`: measured against
a real CLI, that subtype exits 1 and carries no `result` key at all, only a
CLI-authored `errors` list. But "the one route I probed is closed" is not the
same as "the field is safe to scan", and the CLI v2 array format puts assistant
message content in the same stdout blob as the terminal result record.

So the scan is narrowed to the fields the CLI itself authors. The #316 auth
outage still has to be recognised — that is the regression this must not cause.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import _isolation_may_be_the_cause


def test_the_316_auth_failure_is_still_recognised():
    stdout = json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "Failed to authenticate. API Error: 401 Invalid bearer token",
    })
    assert _isolation_may_be_the_cause(stdout, "") is True


def test_an_auth_failure_in_the_errors_list_is_recognised():
    stdout = json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "errors": ["Invalid API key · Please run /login"],
    })
    assert _isolation_may_be_the_cause(stdout, "") is True


def test_model_text_quoting_a_marker_does_not_drop_hook_isolation():
    """The conversation being summarized must not decide this.

    A session that merely *discusses* an auth failure — a dev debugging their
    login, or anyone reading #316 — puts the marker text into assistant content.
    The actual failure here is a network error, which must not be retried
    un-isolated.
    """
    stdout = json.dumps([
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "Yesterday you hit 'not logged in' and an "
                                "invalid api key error while wiring Bedrock.",
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["connect ECONNREFUSED 127.0.0.1:443"],
        },
    ])
    assert _isolation_may_be_the_cause(stdout, "") is False


def test_max_turns_is_not_an_auth_failure():
    """Measured against the real CLI: exits 1, no result key, CLI-authored errors."""
    stdout = json.dumps({
        "type": "result",
        "subtype": "error_max_turns",
        "is_error": True,
        "errors": ["Reached maximum number of turns (1)"],
    })
    assert _isolation_may_be_the_cause(stdout, "") is False


def test_non_json_stdout_still_falls_back_to_a_raw_scan():
    """An older CLI, or a crash before JSON is emitted, must not go unrecognised."""
    assert _isolation_may_be_the_cause("Not logged in. Please run /login", "") is True


def test_stderr_is_still_scanned():
    assert _isolation_may_be_the_cause("", "API Error: 401 Invalid bearer token") is True


def test_the_unknown_option_rule_is_unchanged():
    assert _isolation_may_be_the_cause(
        "", "error: unknown option '--setting-sources'"
    ) is True
    assert _isolation_may_be_the_cause(
        "", "error: unknown option '--some-other-flag'"
    ) is False


@pytest.mark.parametrize(
    "detail",
    [
        "API Error: 429 rate_limit_error",
        "API Error: 529 overloaded_error",
        "Credit balance is too low",
    ],
)
def test_a_failure_that_already_cost_money_is_never_retried(detail):
    stdout = json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": detail,
    })
    assert _isolation_may_be_the_cause(stdout, "") is False
