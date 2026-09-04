"""#505 -- README.md was 934 lines because it doubled as a reference manual
after ## Data files. Six sections move verbatim to docs/<slug>.md, replaced
in README by one-line pointers under a new ## Reference heading.

This pins three things:

1. README.md no longer carries the six section headings as top-level
   sections -- a stranger reaching ## Architecture no longer scrolls past
   522 lines of reference material to get there.
2. Each moved section's text landed in its new docs/ file with a distinctive
   verbatim sentence intact, and -- the stronger check -- every one of that
   section's own INTERIOR headings (###/####) from before the move is still
   present as a heading in the new file. The interior-heading check is what
   actually backs "verbatim": an earlier version of this test only spot-checked
   two of the six files' opening lines, which passed cleanly even after a
   real regression dropped an entire subsection heading
   (`### Measuring the warm path`) from docs/running-tests.md during the
   move, silently reattaching its body under the wrong subsection. That
   regression is why this file now asserts interior headings for all six,
   not two.
3. No README-internal anchor link points at a heading that is no longer in
   README.md (a same-file pointer left dangling by the move).

Would this test pass if nothing moved? No: assertion 1 fails outright against
the pre-#505 tree (README.md carries all six headings), and assertion 2 fails
because docs/computing-the-slug-outside-bash.md and its five siblings do not
exist at all before this change -- confirmed against the base commit's
README.md via `git show`, not merely reasoned about. And the interior-heading
check specifically fails against the one-line regression described above,
confirmed by reverting `docs/running-tests.md`'s heading fix locally and
re-running this file, which turns `test_each_moved_docs_interior_headings_survived_verbatim`
red.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# heading text -> (docs slug, a distinctive verbatim sentence pulled from the
# section as it stood in README.md before the move)
MOVED_SECTIONS = {
    "Computing the slug outside bash": (
        "computing-the-slug-outside-bash.md",
        (
            "`~/.claude/projects/<slug>/` is where Claude Code writes session "
            "transcripts, and `<slug>` is a pure function of the project path."
        ),
    ),
    "Reading the transcript path the host hands us": (
        "reading-the-transcript-path.md",
        (
            "`SessionStart` and `SessionEnd` carry `transcript_path` on their "
            "stdin payload, and since "
            "[#407](https://github.com/Digital-Process-Tools/claude-remember/issues/407) "
            "the pipeline reads it instead of reconstructing it."
        ),
    ),
    "Configuration": (
        "configuration.md",
        "| `prompt_stamp`                   | `full`           |",
    ),
    "Measuring lock hold times": (
        "measuring-lock-hold-times.md",
        (
            "The NDC commit waits up to `REMEMBER_NDC_COMMIT_LOCK_TIMEOUT` "
            "(default 30s) for `save.lock`."
        ),
    ),
    "External storage mode": (
        "external-storage-mode.md",
        (
            "**External storage mode** relocates `REMEMBER_DIR` to a path "
            "outside the project, one subdirectory per project identified by "
            "a slug."
        ),
    ),
    "Running tests": (
        "running-tests.md",
        "Integration tests (includes shell scripts and prompt validation):",
    ),
}

# Every ###/#### heading each section carried BEFORE the move, verified
# against README.md at commit eced1f386 (the base this move started from,
# `git show eced1f386:README.md`) -- hardcoded rather than re-derived from
# git history on every run, since a squash merge can drop that commit from
# the branch this test later runs on while the content fact stays true.
ORIGINAL_INTERIOR_HEADINGS = {
    "computing-the-slug-outside-bash.md": [
        "### 1. Read the slug this session computed",
        "### 2. Find the record when the slug names its directory",
        "### 3. Check your implementation against `docs/slug-vectors.json`",
    ],
    "reading-the-transcript-path.md": [],
    "configuration.md": [
        "### Environment variables",
    ],
    "measuring-lock-hold-times.md": [],
    "external-storage-mode.md": [
        "### Enable",
        "### `{slug}` expansion",
        "### Handoff path",
        "### Per-project identity override",
        "### Back up your memory",
        "#### Automatic commits",
        "#### Logs in a backup you made before this version",
        "#### When a push does not go through",
        "#### When a commit does not happen",
        "#### Restoring on a second machine (off by default)",
    ],
    "running-tests.md": [
        "### Skips print their reason",
        "### A test that dominates the suite (`#510`)",
        "### Measuring the warm path (`tests/env_cache.py`)",
        "### The Python floor guard",
    ],
}


def test_readme_no_longer_carries_the_six_section_headings():
    text = README.read_text(encoding="utf-8")
    for heading in MOVED_SECTIONS:
        assert f"## {heading}\n" not in text, (
            f"'## {heading}' is still a top-level README section -- #505 "
            "asked for it to move to docs/, not stay in place."
        )


def test_readme_has_a_reference_section_pointing_at_each_doc():
    text = README.read_text(encoding="utf-8")
    assert "## Reference" in text, "README.md has no ## Reference heading"
    for slug, _ in MOVED_SECTIONS.values():
        pointer = f"docs/{slug}"
        assert pointer in text, f"README.md's ## Reference section has no pointer to {pointer}"


def test_each_moved_doc_exists_and_carries_its_heading():
    for heading, (slug, _) in MOVED_SECTIONS.items():
        doc = REPO_ROOT / "docs" / slug
        assert doc.is_file(), f"docs/{slug} does not exist -- #505's move is incomplete"
        text = doc.read_text(encoding="utf-8")
        assert text.startswith(f"## {heading}\n"), (
            f"docs/{slug} does not open with '## {heading}' -- has the move "
            "rewritten rather than relocated the section?"
        )


def test_each_moved_doc_carries_its_distinctive_sentence_verbatim():
    for slug, needle in MOVED_SECTIONS.values():
        doc = (REPO_ROOT / "docs" / slug).read_text(encoding="utf-8")
        assert needle in doc, (
            f"docs/{slug} does not carry its own distinctive sentence "
            f"verbatim -- 'move, do not rewrite' (#505). Missing: {needle!r}"
        )


def test_each_moved_docs_interior_headings_survived_verbatim():
    """The stronger check: every ###/#### heading the section carried BEFORE
    the move must still be a heading in the new file -- not just present as
    text somewhere (a heading whose '### ' marker was dropped still shows up
    in a plain substring search, silently reattached under whatever heading
    precedes it, which is exactly the regression this test exists to catch).
    """
    for slug, expected_headings in ORIGINAL_INTERIOR_HEADINGS.items():
        doc = REPO_ROOT / "docs" / slug
        lines = doc.read_text(encoding="utf-8").splitlines()
        actual_headings = {line for line in lines if line.startswith("#")}
        missing = [h for h in expected_headings if h not in actual_headings]
        assert not missing, (
            f"docs/{slug} is missing interior heading(s) it carried before "
            f"the #505 move: {missing} -- its body text may still be present "
            "but silently reattached under the wrong heading."
        )


def test_no_new_readme_only_dead_anchor_into_a_moved_section():
    """Every internal README anchor -- the ](#...) markdown link shape --
    must resolve to a heading that is still actually in README.md -- a
    pointer left behind after a section moved out from under it is a dead
    link nobody notices until they click it."""
    text = README.read_text(encoding="utf-8")
    anchor_targets = set(re.findall(r"\]\(#([a-z0-9\-]+)\)", text))

    def slugify(heading: str) -> str:
        s = heading.lower()
        s = re.sub(r"[^a-z0-9\s\-`()]", "", s)
        s = s.replace("`", "").replace("(", "").replace(")", "")
        s = re.sub(r"\s+", "-", s.strip())
        return s

    headings = re.findall(r"^#{1,6}\s+(.+)$", text, re.MULTILINE)
    known_anchors = {slugify(heading) for heading in headings}

    dangling = sorted(anchor_targets - known_anchors)
    assert not dangling, (
        f"README.md links to internal anchor(s) with no matching heading: "
        f"{dangling} -- did a #505 move leave a same-file link pointing at a "
        "section that is no longer in this file?"
    )
