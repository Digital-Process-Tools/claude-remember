"""#920 scheduler review round 2, item 7: session-end-hook.sh's own
WARNING for a refused store said "could not create $REMEMBER_DIR/logs/
autonomous" for BOTH an unsafe REMEMBER_DIR (gap 1's own refusal) and an
ordinary mkdir failure (read-only root, missing parent) -- two different
causes calling for two different responses from whoever reads
hook-errors.log, collapsed into one wording that named only the second.

Structural pin (cheap, proportionate to a LOW-severity finding): the two
branches must each have their OWN wording, not share the "could not
create" string the unsafe case no longer uses.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_END = REPO_ROOT / "scripts" / "session-end-hook.sh"


def test_must_fire_unsafe_and_mkdir_failure_report_different_wording():
    text = SESSION_END.read_text(encoding="utf-8")
    assert 'REMEMBER_DIR_UNSAFE:-0}" = 1 ]; then' in text, (
        "the unsafe-dir branch is no longer a distinct `if`, or its "
        "condition changed shape -- re-derive this pin"
    )
    assert "was refused as unsafe" in text, (
        "the unsafe-dir branch's WARNING no longer names the refusal "
        "distinctly from an ordinary mkdir failure"
    )
    assert 'elif ! mkdir -p "$REMEMBER_DIR/logs/autonomous"' in text, (
        "the mkdir-failure branch is no longer a separate `elif` from the "
        "unsafe-dir branch"
    )


def test_must_not_fire_the_mkdir_failure_wording_is_unchanged():
    """Positive control: the ordinary mkdir-failure WARNING (the one real
    users who are NOT hitting #920's gaps actually see) must still read
    exactly as it always has."""
    text = SESSION_END.read_text(encoding="utf-8")
    assert "WARNING: could not create $REMEMBER_DIR/logs/autonomous" in text
