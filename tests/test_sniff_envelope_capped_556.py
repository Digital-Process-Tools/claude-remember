"""sniff_file_envelope_status() folds two different exhaustion causes into
the same ``unreadable=False`` return (#556): a file whose scan hit
``_ENVELOPE_SNIFF_SCAN_CAP`` unplaceable lines and gave up (#543's own cap),
and a file that was read to genuine exhaustion (or found empty) and simply
never named a known host shape. The caller-visible tuple could not tell
these apart, so pipeline/haiku.py's fallback warning named only two causes
("unreadable or an unrecognised shape") for a message that can also be
produced by a third: the cap.

This adds a third element to the returned tuple, ``capped``, true only when
the scan gave up at the cap rather than genuinely exhausting the file.
Every "must fire" case (the cap is actually hit) is paired with a "must not
fire" control (a file that is genuinely exhausted, never capped) so the
test cannot pass by reporting every unrecognised file as capped.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.extract import _ENVELOPE_SNIFF_SCAN_CAP, sniff_file_envelope_status


def test_scan_cap_is_reported_as_capped_not_exhausted(tmp_path):
    """The defect itself: a file whose first _ENVELOPE_SNIFF_SCAN_CAP + 1
    lines are all parseable-but-unplaceable must be distinguishable from a
    genuinely exhausted file. Would still pass if the code did nothing
    (capped stayed unreported) unless this asserts on the actual return
    value distinguishing the two.
    """
    unplaceable = (
        '{"type": "queue-operation", "operation": "enqueue"}\n' * (_ENVELOPE_SNIFF_SCAN_CAP + 1)
    )
    path = tmp_path / "past-the-cap.jsonl"
    path.write_text(unplaceable, encoding="utf-8")

    envelope, unreadable, capped = sniff_file_envelope_status(str(path))
    assert envelope == "unrecognised"
    assert unreadable is False
    assert capped is True


def test_genuine_exhaustion_is_not_reported_as_capped(tmp_path):
    """MUST-FIRE control's pair: a file that is fully read -- every line
    parseable and unplaceable, but fewer than the cap -- exhausts normally
    and must report capped=False, not capped=True. Without this control,
    a fix that reports every "unrecognised" result as capped=True would
    also pass the test above.
    """
    unplaceable = (
        '{"type": "queue-operation", "operation": "enqueue"}\n' * (_ENVELOPE_SNIFF_SCAN_CAP - 1)
    )
    path = tmp_path / "genuinely-exhausted.jsonl"
    path.write_text(unplaceable, encoding="utf-8")

    envelope, unreadable, capped = sniff_file_envelope_status(str(path))
    assert envelope == "unrecognised"
    assert unreadable is False
    assert capped is False


def test_an_empty_file_is_exhausted_not_capped(tmp_path):
    """A second genuine-exhaustion shape: an entirely empty file offers no
    line to scan at all, and must not be reported as capped.
    """
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    envelope, unreadable, capped = sniff_file_envelope_status(str(path))
    assert envelope == "unrecognised"
    assert unreadable is False
    assert capped is False


def test_unreadable_file_is_not_reported_as_capped(tmp_path):
    """The OSError cause and the capped cause are distinct facts about the
    same "unrecognised" verdict; an unopenable file must not be capped=True.
    """
    missing = str(tmp_path / "does-not-exist.jsonl")
    envelope, unreadable, capped = sniff_file_envelope_status(missing)
    assert envelope == "unrecognised"
    assert unreadable is True
    assert capped is False
