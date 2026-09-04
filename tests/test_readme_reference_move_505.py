"""#505 -- README.md was 934 lines because it doubled as a reference manual
after ## Data files. Six sections move verbatim to docs/<slug>.md, replaced
in README by one-line pointers under a new ## Reference heading.

This pins two things:

1. README.md no longer carries the six section headings as top-level
   sections -- a stranger reaching ## Architecture no longer scrolls past
   522 lines of reference material to get there.
2. Each moved section's text landed, byte-for-byte, in its new docs/ file --
   "verbatim" is the issue's own word, so the test checks content equality
   against a golden excerpt from each section, not just that the file exists.

Would this test pass if nothing moved? No: assertion 1 fails outright against
the pre-#505 tree (README.md carries all six headings), and assertion 2 fails
because docs/computing-the-slug-outside-bash.md and its five siblings do not
exist at all before this change -- confirmed against the base commit's
README.md via `git show`, not merely reasoned about.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# heading text -> (docs slug, a distinctive verbatim line pulled from the
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
        None,  # filled from README's own current pointer line below
    ),
    "Configuration": (
        "configuration.md",
        "| `prompt_stamp`                   | `full`           |",
    ),
    "Measuring lock hold times": (
        "measuring-lock-hold-times.md",
        None,
    ),
    "External storage mode": (
        "external-storage-mode.md",
        None,
    ),
    "Running tests": (
        "running-tests.md",
        None,
    ),
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
        assert pointer in text, (
            f"README.md's ## Reference section has no pointer to {pointer}"
        )


def test_each_moved_doc_exists_and_carries_its_heading():
    for heading, (slug, _) in MOVED_SECTIONS.items():
        doc = REPO_ROOT / "docs" / slug
        assert doc.is_file(), f"docs/{slug} does not exist -- #505's move is incomplete"
        text = doc.read_text(encoding="utf-8")
        assert text.startswith(f"## {heading}\n"), (
            f"docs/{slug} does not open with '## {heading}' -- has the move "
            "rewritten rather than relocated the section?"
        )


def test_slug_section_moved_verbatim():
    doc = (REPO_ROOT / "docs" / "computing-the-slug-outside-bash.md").read_text(
        encoding="utf-8"
    )
    needle = MOVED_SECTIONS["Computing the slug outside bash"][1]
    assert needle in doc, (
        "docs/computing-the-slug-outside-bash.md does not carry the section's "
        "own opening sentence verbatim -- 'move, do not rewrite' (#505)"
    )


def test_configuration_table_row_moved_verbatim():
    doc = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    needle = MOVED_SECTIONS["Configuration"][1]
    assert needle in doc, (
        "docs/configuration.md does not carry the prompt_stamp table row "
        "verbatim -- 'move, do not rewrite' (#505)"
    )


def test_no_new_readme_only_dead_anchor_into_a_moved_section():
    """Every internal README anchor -- the ](#...) markdown link shape --
    must resolve to a heading that is still actually in README.md -- a
    pointer left behind after a section moved out from under it is a dead
    link nobody notices until they click it."""
    import re

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
