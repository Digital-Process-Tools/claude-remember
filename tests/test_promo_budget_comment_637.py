"""The promo budget comment's stated number must match the enforced value (#637).

`scripts/session-start-hook.sh` enforces a message-length budget with
`[ "${#msg}" -gt N ]`. A comment a few lines above narrates the budget's
history ("the budget moved instead (X -> Y)"). If the comment's claimed
current value drifts from what the code actually enforces, a maintainer
sizing a future promo string trusts the wrong number -- the exact defect
#637 reports (comment said 150, code enforces 170).

This asserts the two numbers agree by parsing both out of the live file,
rather than hardcoding either -- so the test fails loudly again the next
time they drift, whatever the new enforced value turns out to be.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"


def _enforced_budget():
    text = SESSION_START.read_text(encoding="utf-8")
    match = re.search(r'\[\s*"\$\{#msg\}"\s*-gt\s*(\d+)\s*\]', text)
    assert match, "could not find the enforced budget guard in session-start-hook.sh"
    return int(match.group(1))


def _comment_claimed_budget():
    text = SESSION_START.read_text(encoding="utf-8")
    # The comment narrates the budget's history as one or more
    # "(X -> Y)" or "(X -> Y -> Z)" arrow chains near "budget moved instead".
    match = re.search(
        r"budget moved instead\s*\n\s*#\s*\((\d+(?:\s*->\s*\d+)+)", text
    )
    assert match, "could not find the 'budget moved instead (...)' comment"
    numbers = [int(n) for n in re.findall(r"\d+", match.group(1))]
    assert numbers, f"no numbers parsed out of comment chain: {match.group(1)!r}"
    return numbers[-1]


def test_promo_budget_comment_matches_enforced_value():
    """The comment's final claimed value must equal what the code enforces.

    This test is red against the pre-fix comment, which claims the budget
    is 150 while the code enforces 170 -- a 20-character gap that produces
    silent under-estimation of headroom for a future promo string.
    """
    enforced = _enforced_budget()
    claimed = _comment_claimed_budget()
    assert claimed == enforced, (
        f"promo budget comment claims {claimed}, but the code enforces "
        f"{enforced} -- update the comment in scripts/session-start-hook.sh"
    )
