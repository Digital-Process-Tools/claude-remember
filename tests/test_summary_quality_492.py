"""Live summary-quality harness (#492).

#406's plan for the ``codex exec`` provider was two things, not one: the
provider itself, plus a way to judge summary quality -- "a green contract
suite passes while summaries quietly become useless." v0.24.0 shipped only
the provider. ``tests/test_summarizer_routing_460.py`` and
``tests/test_nested_summarizer.py`` pin routing and liveness -- which
provider a call resolves to, how an unavailable/empty/declined spawn is
reported -- entirely with a mocked ``subprocess.run``. None of it looks at
what the summary actually says, and every one of the four prompts under
``prompts/`` was written and tuned against Claude's behaviour before
``codex exec`` started running them against a different model.

This file is the fixture+substring shape the issue names as cheaper than an
LLM-judge pipeline: two small recorded session excerpts
(``tests/fixtures/summary_quality/*.jsonl``) with a short list of facts a
correct ``save-session`` summary must preserve (specific filenames, error
codes, decisions -- exactly what ``prompts/save-session.prompt.txt`` itself
asks the model to keep: "mention files, MR numbers, issue numbers"). Each
test builds the real save-session prompt via ``pipeline.prompts`` from a
fixture transcript, sends it through the live ``call_haiku()`` -- forced
onto the ``codex`` route via ``REMEMBER_SUMMARIZER=codex`` (the same
explicit override ``tests/test_summarizer_routing_460.py`` uses under
mock), unmocked -- and asserts every named fact survives into the summary
as a substring. The route is forced deliberately: ``auto`` only resolves
to ``codex`` when ``REMEMBER_TRANSCRIPT_PATH`` already names a live
Codex-shaped transcript, which is not the ordinary dev-shell or CI-runner
case this harness runs in -- an unforced run would silently judge the
pre-existing ``claude`` route instead of the one this issue is about.

A judge-call shape (an LLM grading another LLM's prose) needs its own
calibration and is a much bigger scope than this issue asks for; substring
presence against hand-picked facts is the cheaper, deterministic check the
issue's own text prefers.

Deliberately NOT run by the default suite -- per CLAUDE.md ("The suite runs
locally on demand, never on push") and the issue's own constraint
("runnable on demand rather than in CI, it needs a live provider and costs
a call"). Skipped unless RUN_LIVE_SUMMARY_JUDGE=1 is set, the same
opt-in-costs-tokens shape scripts/run-tests.sh already uses for its own
live Haiku test (section 8, gated on --live rather than an env var only
because that harness is a shell script with a flag parser and this one is
pytest).

Would this test pass if the code did nothing? No: an empty or SKIP result,
or a summary that drops a fixture's facts, fails it. Proven both ways
against the committed shape (REMEMBER_SUMMARIZER=codex forced, current
fact lists) before this file reached its final commit -- with
RUN_LIVE_SUMMARY_JUDGE=1 set, a version of the auth-csrf-bugfix case
asserting a fact absent from the fixture ("Kubernetes pod eviction", which
that transcript never mentions) failed with a real diff showing the
missing fact against a live codex-routed call; restoring the correct
fact list turned it green against another live codex-routed call, and
the sibling rate-limit-feature case was independently confirmed green the
same way. Not re-run on every edit after that, because each run is a
live, billed API call -- exactly the cost this file exists to keep out of
CI and out of the default local suite.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import extract as _extract
from pipeline.haiku import call_haiku
from pipeline.prompts import build_save_prompt

RUN_LIVE = os.environ.get("RUN_LIVE_SUMMARY_JUDGE") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_LIVE,
    reason="needs a live summarizer provider and costs a real API call -- "
           "opt in with RUN_LIVE_SUMMARY_JUDGE=1 (see CLAUDE.md: the suite "
           "runs locally on demand, never on push; see also "
           "scripts/run-tests.sh's own --live-gated Haiku test)",
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "summary_quality"

# Each fact is chosen because prompts/save-session.prompt.txt explicitly
# rewards keeping it ("Be specific -- mention files, MR numbers, issue
# numbers") and because it is a token a paraphrase is unlikely to drop
# without also dropping the underlying meaning -- a filename, an error
# code, a concrete number, or (for rate-limit-feature) the still-blocked
# status the prompt separately forbids compressing away ("Blocking,
# pending, or 'not yet live' status is a fact, not filler").
CASES = [
    {
        "id": "auth-csrf-bugfix",
        "transcript": FIXTURES_DIR / "auth-csrf-bugfix.jsonl",
        "facts": ["auth.py", "CSRF"],
    },
    {
        "id": "rate-limit-feature",
        "transcript": FIXTURES_DIR / "rate-limit-feature.jsonl",
        "facts": ["middleware.py", "100 req", "Redis"],
    },
]


def _build_prompt_for(transcript_path: Path) -> str:
    """The real save-session prompt for a fixture transcript.

    Reuses the extractor's own message-formatting shape (see
    ``pipeline.extract.extract_session``) rather than hand-writing a
    second copy of it, so the prompt this harness sends is the same shape
    the pipeline actually sends in production. The ``Lines:`` count in
    particular has to come from ``count_lines()`` (the raw physical line
    count, metadata included) rather than ``len(messages)`` (the filtered
    count) -- production's own ``extract_session`` uses the former, and a
    hand-rolled ``len(messages)`` here would quietly diverge from it.
    """
    messages = _extract.extract_messages(
        str(transcript_path), skip_lines=0, envelope="claude-code"
    )
    total_lines = _extract.count_lines(str(transcript_path))
    lines = [f"Session: {transcript_path.stem}", f"Lines: {total_lines}", "=" * 60]
    for role, text in messages:
        lines.append(f"\n[{role}]")
        lines.append(text)
        lines.append("-" * 40)
    extract_text = "\n".join(lines)
    return build_save_prompt(
        time="14:32",
        branch="main",
        last_entry="(no previous entry)",
        extract=extract_text,
    )


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_live_summary_preserves_session_facts(case, monkeypatch):
    # This is the one thing the issue is actually about: without forcing
    # the route, _choose_summarizer_provider()'s "auto" default only ever
    # resolves to "codex" when REMEMBER_TRANSCRIPT_PATH already points at a
    # live Codex-shaped transcript (pipeline/haiku.py's own
    # _choose_summarizer_provider docstring) -- never true in an ordinary
    # dev shell or CI runner, so an unforced run here would silently judge
    # the pre-existing claude route instead of the codex route #492 is
    # about. REMEMBER_SUMMARIZER=codex is the explicit override every
    # provider always honours (see
    # tests/test_summarizer_routing_460.py::test_explicit_codex_override_wins_under_claude_code_host),
    # same as that file's own precedent for forcing this route under test.
    monkeypatch.setenv("REMEMBER_SUMMARIZER", "codex")
    prompt = _build_prompt_for(case["transcript"])
    result = call_haiku(prompt, timeout=60)

    assert not result.is_skip, (
        f"summarizer returned SKIP for a session with real, substantive "
        f"work -- got: {result.text!r}"
    )

    missing = [f for f in case["facts"] if f.lower() not in result.text.lower()]
    assert not missing, (
        f"live summary for {case['id']!r} dropped fact(s) {missing} -- "
        f"the session that produced this fixture is in "
        f"{case['transcript']}, and the full summary returned was:\n"
        f"{result.text}"
    )
