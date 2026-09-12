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


# A file may carry more than one allowance (#429 and #662 both touch
# lib-memory-dir.sh), so `_SANCTIONED_DIVERGENCE[_REL]` is a list of
# (old_code, new_code) pairs and each is judged on its own. `ref_code` is
# built from every pair's shape at once so the helper sees each one in the
# state under test; the per-pair assertions below say which pair failed.
_PAIRS = _SANCTIONED_DIVERGENCE[_REL]
assert _PAIRS, f"{_REL} carries no sanctioned divergence -- nothing to pin here"


def test_sanctioned_divergence_applies_pre_merge_shape():
    """The sanctioned old->new substitutions are still live PRs: old_code is
    still on origin/main. Substitute them in so the byte-compare judges the fix
    the allowance exempts, not the noise of the still-open PR."""
    ref_code = "before\n" + "\n".join(old for old, _ in _PAIRS) + "\nafter"
    result = _apply_sanctioned_divergence(ref_code, _REL)
    for old_code, new_code in _PAIRS:
        assert new_code in result, new_code
        assert old_code not in result, old_code


def test_sanctioned_divergence_accepts_post_merge_shape():
    """The allowances' own PRs have landed: origin/main now holds new_code and
    old_code is gone. That is the post-merge steady state, not staleness --
    pass ref_code through unchanged rather than asserting."""
    ref_code = "before\n" + "\n".join(new for _, new in _PAIRS) + "\nafter"
    result = _apply_sanctioned_divergence(ref_code, _REL)
    assert result == ref_code


def test_sanctioned_divergence_mixed_states_judge_each_pair_alone():
    """One allowance landed, another still open: the landed one passes
    through, the open one is substituted -- neither state contaminates the
    other's verdict."""
    if len(_PAIRS) < 2:
        pytest.skip(f"{_REL} carries a single allowance -- no mixed state to pin")
    (old_a, new_a), (old_b, new_b) = _PAIRS[0], _PAIRS[1]
    ref_code = f"before\n{new_a}\n{old_b}\nafter"
    result = _apply_sanctioned_divergence(ref_code, _REL)
    assert new_a in result and new_b in result
    assert old_b not in result


def test_sanctioned_divergence_still_asserts_when_genuinely_stale():
    """Neither old_code nor new_code is on origin/main: origin/main moved
    further still and this allowance needs re-deriving, not blindly
    (re-)applied -- the one case that must still fail loudly."""
    ref_code = "something else entirely, unrelated to either shape"
    with pytest.raises(AssertionError):
        _apply_sanctioned_divergence(ref_code, _REL)
