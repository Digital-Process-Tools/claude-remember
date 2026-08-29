"""`_apply_sanctioned_divergence`'s three-state logic, pinned in isolation (#440).

`tests/test_case_divergence_298.py` carries a whole-file
`pytestmark = pytest.mark.skipif(sys.platform == "win32", ...)` because most of
that module spawns bash and depends on POSIX semantics no Windows runner has.
`_apply_sanctioned_divergence` itself is neither: it is pure Python string
containment and substitution over two hardcoded literals from
`_SANCTIONED_DIVERGENCE`. Keeping its regression tests inside the guarded
module would silently inherit that skip and never run on windows-latest CI at
all -- a module-level `pytestmark` skips every test in the file regardless of
what that individual test needs, and reordering the `def`s within the module
would not change that. Found during #440's own self-review (the reviewer
Explore/auditor pass on this issue's fix), not filed separately, because the
mechanism, the blast radius (three tests, one new file) and the subsystem are
all the same as the fix that motivated them.

`main` was red because the old two-state guard in `test_case_divergence_298.py`
asserted the instant its own sanctioned allowance's PR (#436) merged: once
merged, origin/main held the *new* code and the old code the allowance still
looked for was gone. `_apply_sanctioned_divergence` recognizes a third state --
old code absent AND new code present is the post-merge steady state, not
staleness -- and only asserts when neither is found. These three tests
construct both shapes directly from `_SANCTIONED_DIVERGENCE`'s own recorded
strings rather than depending on where origin/main happens to sit when they
run, so they cannot pass or fail for the wrong reason depending on the
repository's own history.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_case_divergence_298 import (
    _SANCTIONED_DIVERGENCE,
    _apply_sanctioned_divergence,
)

_REL = "scripts/lib-memory-dir.sh"


def test_sanctioned_divergence_applies_pre_merge_shape():
    """The sanctioned old->new substitution is still a live PR: old_code is
    still on origin/main. Substitute it in so the byte-compare judges the fix
    the allowance exempts, not the noise of the still-open PR."""
    old_code, new_code = _SANCTIONED_DIVERGENCE[_REL]
    ref_code = f"before\n{old_code}\nafter"
    result = _apply_sanctioned_divergence(ref_code, _REL)
    assert new_code in result
    assert old_code not in result


def test_sanctioned_divergence_accepts_post_merge_shape():
    """The allowance's own PR has landed: origin/main now holds new_code and
    old_code is gone. That is the post-merge steady state, not staleness --
    pass ref_code through unchanged rather than asserting."""
    _, new_code = _SANCTIONED_DIVERGENCE[_REL]
    ref_code = f"before\n{new_code}\nafter"
    result = _apply_sanctioned_divergence(ref_code, _REL)
    assert result == ref_code


def test_sanctioned_divergence_still_asserts_when_genuinely_stale():
    """Neither old_code nor new_code is on origin/main: origin/main moved
    further still and this allowance needs re-deriving, not blindly
    (re-)applied -- the one case that must still fail loudly."""
    ref_code = "something else entirely, unrelated to either shape"
    with pytest.raises(AssertionError):
        _apply_sanctioned_divergence(ref_code, _REL)
