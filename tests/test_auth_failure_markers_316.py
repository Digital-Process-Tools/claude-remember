"""#316 — the isolation fallback must recognise the Bedrock-proxy 401.

`_child_env()` strips every `CLAUDE_CODE_*` var and `--setting-sources ''`
stops `~/.claude/settings.json`'s `env` block from restoring them, so a
Bedrock/proxy install loses `CLAUDE_CODE_USE_BEDROCK` in the child and the
nested CLI talks to the real API with a proxy token. The API answers

    Failed to authenticate. API Error: 401 Invalid bearer token

which is exactly the outage `_isolation_may_be_the_cause` exists to recover
from — a different spelling of it was simply never in the marker tuple, so
capture failed 100% of the time on those installs with no retry.

The negative cases are the point of the tuple being narrow: a rate limit or an
overload already cost money, and retrying them would double a spend while
hiding the cause (#129/#190).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import _isolation_may_be_the_cause


BEDROCK_401 = (
    '{"type":"result","subtype":"error_during_execution","is_error":true,'
    '"result":"Failed to authenticate. API Error: 401 Invalid bearer token"}'
)


def test_bedrock_proxy_401_is_recognised_as_an_isolation_failure():
    assert _isolation_may_be_the_cause(BEDROCK_401, "") is True


def test_bedrock_proxy_401_is_recognised_on_stderr_too():
    assert _isolation_may_be_the_cause("", "API Error: 401 Invalid bearer token") is True


@pytest.mark.parametrize(
    "text",
    [
        "Not logged in",
        "Please run /login",
        "Invalid API key · Fix external API key",
        '{"error":{"type":"authentication_error"}}',
    ],
)
def test_the_auth_failures_that_already_worked_still_do(text):
    assert _isolation_may_be_the_cause(text, "") is True


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 429 rate_limit_error",
        "API Error: 529 overloaded_error",
        "API Error: 500 internal server error",
        "Credit balance is too low",
        "",
    ],
)
def test_a_failure_that_already_cost_money_is_never_retried(text):
    assert _isolation_may_be_the_cause(text, "") is False
