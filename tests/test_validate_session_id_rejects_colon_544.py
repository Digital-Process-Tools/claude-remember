"""_validate_session_id must reject ':' (#544).

On NTFS, a colon in a filename is read as an Alternate Data Stream separator
(`filename:stream`), so a session_id such as "foo:bar" joined into
`position.{session_id}` could silently write to/read from an existing
file's ADS instead of a distinct file of its own. `_validate_session_id`
(pipeline/extract.py) already rejects '/', '\\' and '..' but not ':'.

Mirrors tests/test_save_position_validates_session_id_538.py's shape: a
negative case (colon-bearing id must raise) paired with a positive control
(a well-formed UUID id must still pass), so the "must raise" assertion
cannot be trivially true because nothing happened at all.

This is platform-independent by design (CLAUDE.md / agents/developer.md
bar on cross-platform claims): the exploit mechanics are NTFS-specific and
unobserved on this machine (darwin), but the validation itself is a plain
string-shape check with no filesystem dependency, so asserting the ValueError
directly -- rather than gating behind a Windows-only fixture or actually
attempting an ADS write -- exercises the real code path on every platform
CI runs, which a skip-elsewhere fixture would not.
"""

from __future__ import annotations

import pytest

from pipeline.extract import _validate_session_id

WELL_FORMED = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def test_colon_bearing_session_id_is_rejected():
    """A ':' in session_id must raise, never silently pass through to
    become `position.<id>` where NTFS would read the tail as an ADS name."""
    with pytest.raises(ValueError):
        _validate_session_id("foo:bar")


def test_well_formed_session_id_still_passes():
    """Positive control: a plain UUID session_id must not be rejected, so
    the negative case above cannot be passing because validation now
    rejects everything."""
    _validate_session_id(WELL_FORMED)  # must not raise
