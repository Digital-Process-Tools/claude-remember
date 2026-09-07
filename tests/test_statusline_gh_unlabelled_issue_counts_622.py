"""#622: a GitHub label name may legally contain a comma. The old wire format for
``_gh_unlabelled_issue_counts`` joined a label list with ``,`` and split it back apart
in Python, so a label literally named e.g. ``blocked,lane-storage`` split into two
names, one of which can coincidentally collide with a real declared lane -- counting
an issue as *placed in a lane* when no triage sweep ever placed it there. That is an
under-count of ``no_lane``/``no_priority``, the opposite of this function's own
documented convention (never under-count; return ``None`` when a reading cannot be
trusted).

Each case below stubs ``statusline._run`` directly rather than shelling out to a real
``gh`` -- reproducing the collapse needs no GitHub label-creation rights, only a
comma-bearing label name fed through the same code path the real ``gh api --jq`` call
would produce.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".oss"))

import statusline

LANES = ["lane-capture", "lane-storage", "lane-other"]
PRIORITIES = ["priority:high", "priority:low"]


def test_comma_bearing_label_name_is_not_split_into_a_false_lane_match(monkeypatch):
    """Positive: the defect. A single label literally named
    ``blocked,lane-storage`` must not be read as two labels, one of which
    (``lane-storage``) happens to be a real declared lane -- the issue carries no
    real lane label at all and must count toward ``no_lane``.
    """
    monkeypatch.setattr(
        statusline,
        "_run",
        lambda command, timeout=25: '["blocked,lane-storage"]',
    )
    result = statusline._gh_unlabelled_issue_counts(
        "acme/widgets", 1, PRIORITIES, LANES
    )
    assert result == {"no_priority": 1, "no_lane": 1}


def test_ordinary_comma_free_labels_still_count_correctly(monkeypatch):
    """Positive control: an issue with a real, comma-free lane label and no
    priority label must still read as placed-in-a-lane / no-priority -- the fix
    must not break the ordinary case while closing the comma hole.
    """
    monkeypatch.setattr(
        statusline,
        "_run",
        lambda command, timeout=25: '["lane-storage"]',
    )
    result = statusline._gh_unlabelled_issue_counts(
        "acme/widgets", 1, PRIORITIES, LANES
    )
    assert result == {"no_priority": 1, "no_lane": 0}
