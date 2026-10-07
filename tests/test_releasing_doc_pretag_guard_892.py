"""#892: #856's check_version_order.py refusal only runs for a tag cut from a
commit whose own copy of release-branch.yml already has that step -- an ordinary
`push: tags:` trigger runs the workflow file as it exists at the tagged commit, not
on `main`. #856 first shipped in v0.39.0, so docs/releasing.md's worked example
(pushing a patch tag for an older line directly) would run that line's own
pre-#856 copy of the workflow, which has no check_version_order.py step at all --
the refusal never fires, and a silent version drop is the only sign anything
happened. The doc must say so, not just describe the guard as if it always runs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "releasing.md"


def _guard_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    marker = "refuses to push a tree whose `plugin.json` version"
    assert marker in text, f"{DOC} has no {marker!r} text (#856)"
    start = text.index(marker)
    # Up to the next top-level or second-level heading, or end of file.
    rest = text[start:]
    nxt = re.search(r"\n#{1,2} ", rest)
    return rest[: nxt.start()] if nxt else rest


def test_doc_warns_pretag_push_skips_the_guard_before_v0_39_0():
    """The caveat: a tag pushed directly for a line before v0.39.0 (where #856
    landed) runs that line's own pre-#856 workflow file, which has no guard --
    publish it via workflow_dispatch instead."""
    section = _guard_section()
    assert "workflow_dispatch" in section, section
    assert "v0.39.0" in section, section
    assert "892" in section, section


def test_doc_still_describes_the_normal_regression_override():
    """Positive control: a rewrite that added the pre-v0.39.0 caveat while
    dropping the existing allow_version_regression override description would
    pass the test above while making the doc worse, not better."""
    section = _guard_section()
    assert "allow_version_regression" in section, section
    assert "0.36.2" in section and "0.37.0" in section, section
