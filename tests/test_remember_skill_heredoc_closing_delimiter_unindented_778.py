"""#778: the /remember SKILL.md heredoc example lives inside a 4-space
indented block, so its closing delimiter line reads "    PICK_A_RANDOM_TOKEN"
at 4 columns of indentation rather than at column 0. `<<'X'` in bash only
terminates on a line that is EXACTLY `X` -- no leading or trailing
whitespace -- so a model that copies the example literally, indentation
included, emits a heredoc that never closes: everything after the intended
closing line, including the token line itself, becomes part of the note's
content instead of ending the pipe.

Fix: un-indent the example (e.g. put it in a fenced code block instead of a
4-space indented block) so the closing delimiter line is flush with column 0.

Positive control: a version of SKILL.md that deleted the heredoc example
entirely must not pass this test either -- it must still find the opening
and closing lines of the example before it can check their indentation.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "skills" / "remember" / "SKILL.md"

TOKEN = "PICK_A_RANDOM_TOKEN"


class TestHeredocClosingDelimiterIsUnindented:

    def test_example_opening_and_closing_lines_exist(self):
        """Positive control: the example itself must still be present."""
        lines = SKILL_MD.read_text().splitlines()
        opening = [l for l in lines if "<<" + chr(39) + TOKEN + chr(39) in l]
        closing = [l for l in lines if l.strip() == TOKEN]
        assert opening, "the heredoc opening line (<<'PICK_A_RANDOM_TOKEN') is missing from SKILL.md"
        assert closing, "no line in SKILL.md consists solely of the closing token PICK_A_RANDOM_TOKEN"

    def test_closing_delimiter_line_has_no_leading_whitespace(self):
        """`<<'X'` only terminates on a line that is exactly `X` -- any
        leading whitespace means the heredoc never closes."""
        lines = SKILL_MD.read_text().splitlines()
        closing_lines = [l for l in lines if l.strip() == TOKEN]
        assert closing_lines, "no line in SKILL.md consists solely of the closing token PICK_A_RANDOM_TOKEN"
        for line in closing_lines:
            assert line == TOKEN, (
                f"closing delimiter line {line!r} is not flush with column 0 -- "
                "a bash heredoc with a quoted terminator only ends on a line "
                "that matches the terminator exactly, so an indented copy of "
                "this example never terminates (#778)"
            )
