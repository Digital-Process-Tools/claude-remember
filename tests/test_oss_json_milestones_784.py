"""#784 -- .oss.json must record either real milestones or a declined-note.

A triage sweep found the repo has zero GitHub milestones and .oss.json's
`milestones` key is an empty list, with nothing recording whether that is an
oversight or a deliberate maintainer decision. This asserts the config states
one or the other explicitly: a non-empty `milestones` list, or a
`_milestones_note` key explaining why milestones are declined -- so a future
triage sweep can tell "nobody decided" from "decided not to" without
re-opening the question.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_oss_json_declares_milestones_or_explains_why_not():
    config = json.loads((REPO_ROOT / ".oss.json").read_text(encoding="utf-8"))

    milestones = config.get("milestones")
    note = config.get("_milestones_note")

    has_milestones = isinstance(milestones, list) and len(milestones) > 0
    has_note = isinstance(note, str) and len(note.strip()) > 0

    assert has_milestones or has_note, (
        "'.oss.json' has neither a non-empty 'milestones' list nor a "
        "'_milestones_note' key explaining why milestones are declined -- "
        "a triage sweep will keep re-surfacing this as an open question "
        "(see issue #784)"
    )
