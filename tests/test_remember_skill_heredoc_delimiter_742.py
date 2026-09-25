"""
#742: the /remember heredoc used a static, predictable terminator to pipe a
model-written note into write-handoff.sh. The note is written from session
content that can include untrusted text (a fetched page, an issue body, a
hostile file read earlier in the session). If that text contains a line
matching the delimiter exactly, the heredoc ends there, and anything after
it in the same Bash call gets parsed as shell commands instead of note
content.

Fix: the skill instructs a fresh, unpredictable terminator per invocation
instead of the old fixed one -- something the note is vanishingly unlikely
to contain by chance, and the caller can check for and avoid on purpose.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "skills" / "remember" / "SKILL.md"
WRITE_HANDOFF_SCRIPT = REPO_ROOT / "scripts" / "write-handoff.sh"

VULNERABLE_DELIMITER = "<<" + chr(39) + "EOF" + chr(39)


class TestSkillInstructionDoesNotUseAStaticPredictableDelimiter:

    def test_skill_md_does_not_use_the_literal_eof_heredoc(self):
        text = SKILL_MD.read_text()
        assert VULNERABLE_DELIMITER not in text, (
            "SKILL.md still tells the model to pipe an untrusted, "
            "model-written note through a static EOF heredoc -- a note "
            "containing a bare EOF line truncates it and lets the "
            "remainder run as shell (#742)."
        )

    def test_skill_md_instructs_a_fresh_unpredictable_terminator(self):
        """Positive control: the instruction must still tell the model HOW
        to save the note -- this must not pass on a version of SKILL.md that
        simply deleted the save instructions."""
        text = SKILL_MD.read_text()
        assert "write-handoff.sh" in text, "the save instruction itself must still be present"
        assert "<<" + chr(39) in text, "the note must still be piped in via a quoted heredoc (no shell expansion of its content)"
        assert (
            "random" in text.lower() or "unpredictable" in text.lower()
        ), "the instruction must tell the model to pick a fresh, unpredictable terminator, not a fixed one"

    def test_write_handoff_usage_comment_matches_the_new_convention(self):
        """The script's own USAGE doc-comment (not executed, but the contract
        the skill follows) must not still advertise the vulnerable literal
        EOF heredoc shape either -- a stale comment here is exactly the kind
        of drift that would mislead the next person editing SKILL.md."""
        text = WRITE_HANDOFF_SCRIPT.read_text()
        assert VULNERABLE_DELIMITER not in text, (
            "write-handoff.sh's own USAGE comment still documents the "
            "vulnerable static EOF heredoc shape (#742)"
        )
