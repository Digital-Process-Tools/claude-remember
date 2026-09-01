"""sniff_file_envelope() collapses two different facts into the same
"unrecognised" string (#478): an OSError reading the file (permission
error, bad mount, a file that vanished between listing and open) and an
exhausted, fully-parseable file that simply never named a known host shape.
The docstring already names the collapse honestly; the receipt
(scripts/save-session.sh:276) does not -- it points a user whose transcript
is simply unreadable at a shape-sniffing function, the wrong place to look.

This repository's scripts/ lane is held by another agent right now, so the
log-line half of the fix is out of scope here. What is fixed in pipeline/ is
the source of the distinction: sniff_file_envelope() keeps returning
"unrecognised" for both cases (so the shell bridge's existing
``[ "$ENVELOPE" = "unrecognised" ]`` check, which the #450 quarantine
depends on, is untouched), and a new status call carries the OSError case
out separately, the same "keep the old contract, add a new signal" shape
#458's fix took one function over.

Every "must not fire" case (a genuinely empty/unrecognised-but-readable
file) is paired with a "must fire" case (the same file, made unreadable).
"""

from __future__ import annotations

import os
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.extract import sniff_file_envelope, sniff_file_envelope_status


def test_missing_file_is_unrecognised_and_flagged_unreadable(tmp_path):
    """The defect itself: a transcript path that does not exist at all --
    the exact OSError arm the issue is about -- must still return
    "unrecognised" (contract preserved) but must ALSO say it could not even
    be opened, distinctly from a file that was read and found no known
    shape.
    """
    missing = str(tmp_path / "does-not-exist.jsonl")
    envelope, unreadable = sniff_file_envelope_status(missing)
    assert envelope == "unrecognised"
    assert unreadable is True
    # Old call site's contract is unchanged.
    assert sniff_file_envelope(missing) == "unrecognised"


def test_permission_denied_file_is_flagged_unreadable(tmp_path):
    """A second OSError shape -- a file that exists but cannot be opened --
    must be flagged the same way as a missing file, not treated as
    "read it, found no known shape"."""
    if os.name == "nt" or os.geteuid() == 0:
        import pytest
        pytest.skip("permission bits are not enforced the same way here")
    path = tmp_path / "no-read.jsonl"
    path.write_text('{"type": "user"}\n', encoding="utf-8")
    path.chmod(0)
    try:
        envelope, unreadable = sniff_file_envelope_status(str(path))
        assert envelope == "unrecognised"
        assert unreadable is True
    finally:
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)


def test_a_readable_but_shapeless_file_is_not_flagged_unreadable(tmp_path):
    """MUST-FIRE control's pair: a file that WAS opened and read to
    exhaustion, and simply never contained a line naming a known host
    shape, is the "read it, did not recognise it" case -- not the
    "could not read it at all" case -- and must not be flagged unreadable.
    """
    path = tmp_path / "shapeless.jsonl"
    path.write_text('{"nothing": "recognisable"}\n', encoding="utf-8")
    envelope, unreadable = sniff_file_envelope_status(str(path))
    assert envelope == "unrecognised"
    assert unreadable is False


def test_an_empty_file_is_not_flagged_unreadable(tmp_path):
    """MUST-FIRE control's pair, the other exhausted-fallthrough shape: an
    entirely empty file offers no line to sniff, and is genuinely "read it,
    nothing there", not "could not read it".
    """
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    envelope, unreadable = sniff_file_envelope_status(str(path))
    assert envelope == "unrecognised"
    assert unreadable is False
