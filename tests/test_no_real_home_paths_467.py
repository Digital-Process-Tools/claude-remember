"""Committed fixtures must not carry a real OS home-directory path (#467).

## The mechanism

`tests/fixtures/codex-rollout.jsonl` was committed as a real capture,
deliberately sanitised: `cwd`, `git` and `turn_context` were replaced with
`/sanitized/project/dir`. The sanitisation missed two *nested JSON-string
blobs* on lines 6 and 7, where the maintainer's real home directory and OS
username sat inside a serialised string value rather than at a top-level
field a field-keyed sanitiser would walk. `.agents/plugins/marketplace.json`
declares `"source": "local", "path": "./"`, so the install copies the tree,
`tests/` included -- the leak reached every install of the affected release,
not just this repository's own history.

Re-sanitised in this same change (both occurrences replaced with the
placeholder segment `sanitized-user`, matching the fixture's own existing
`/sanitized/project/dir` convention). This file is the durable half: a check
that runs on every diff, so the next real capture cannot carry the next
thing nobody thought to look at.

## Scope, argued

Tree-wide (`git ls-files`) was considered and rejected. `/Users/` and
`/home/` already appear *legitimately* throughout this repository outside
fixtures -- README.md's worked examples (`/Users/you/...`,
`/home/alice/...`), docs/slug-vectors.json's synthetic slug vectors,
scripts/bench-slug.sh's own benchmark input, and tests/test_hooks_json.py's
deliberately adversarial PowerShell-escaping parametrize list
(`C:/Users/Jane Doe/...`, `C:/Users/cafe/...`) are all hand-authored example
paths, chosen by whoever wrote them, and can never carry a real person's
real credentials by the mechanism that produced #467 -- nobody accidentally
pastes their own home directory into a parametrize list the way a sanitiser
silently misses a nested blob inside a real capture. A tree-wide sweep would
need an allowlist covering all of those (and would still not be complete,
since a new adversarial-path test could add another entry at any time), and
the issue's own reasoning ("a guard that fails on those will be disabled
within a week") is exactly why that path was rejected.

The actual risk surface -- the one mechanism this guard exists to close --
is narrower and matches the issue's own diagnosis: a *real captured
transcript* committed under `tests/fixtures/`. That is the only place a
real, uninvited username can arrive by accident rather than by choice, so
this guard is scoped to `tests/fixtures/` alone. Anything under
`tests/fixtures/` that names a `/Users/<x>/` or `/home/<x>/` segment must
use the placeholder segment this repository has now standardised on
(`sanitized-user`) -- any other segment fails the guard.

Scoped OUT, by name: `tests/*.py` (hand-authored literals, argued above),
`README.md` / `CHANGELOG.md` / `docs/` / `skills/*/SKILL.md` (documentation,
explicitly excluded by the issue itself), and `scripts/bench-slug.sh` (its
own benchmark input, already carved out of the #405 ASCII guard for the
same reason -- it is the subject matter, not a leak).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

# The one placeholder segment this repository's fixtures are sanitised to.
# Anything else captured after /Users/ or /home/ in a fixture file is either
# an un-sanitised real path or a new convention nobody taught this guard --
# either way, it must not pass silently.
_ALLOWED_HOME_SEGMENT = "sanitized-user"

_HOME_PATH = re.compile(r"(?:/Users/|/home/)([A-Za-z0-9_.-]+)")


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES_DIR.iterdir() if p.is_file())


def test_scanned_at_least_one_fixture_file():
    """Positive control for the enumeration itself: a scan that silently
    found zero files must not let the assertion below pass by vacuous
    truth."""
    files = _fixture_files()
    assert len(files) >= 2, (
        f"expected at least 2 files under {FIXTURES_DIR}, found {len(files)} "
        "-- the scan likely did not read the real directory"
    )


def test_detector_fires_on_a_planted_real_looking_home_path():
    """Positive control for the regex itself (CLAUDE.md: a negative
    assertion needs a positive control). A detector that matches nothing
    would let the real check below pass vacuously -- this proves the
    pattern actually fires on exactly the shape #467 shipped: a home path
    nested inside a JSON string value, not at a top-level field."""
    planted = (
        r'{"payload": {"text": "some scaffolding text (file: '
        r'/Users/realname/.codex/skills/.system/imagegen/SKILL.md)\n"}}'
    )
    matches = _HOME_PATH.findall(planted)
    assert matches, "detector did not fire on a planted real-looking home path"
    assert "sanitized-user" not in matches


def test_no_fixture_carries_an_unsanitised_home_path():
    """MUST FIRE case: any tests/fixtures/* file naming a /Users/<x>/ or
    /home/<x>/ segment other than the sanitised placeholder is exactly
    #467's mechanism -- a real capture that slipped an un-sanitised path
    into the tree, where the install layout (`.agents/plugins/
    marketplace.json`'s local source) ships tests/ into every install."""
    offenders: list[str] = []
    for f in _fixture_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for segment in _HOME_PATH.findall(text):
            if segment != _ALLOWED_HOME_SEGMENT:
                offenders.append(f"{f.relative_to(REPO_ROOT)}: {segment!r}")
    assert not offenders, (
        "fixture(s) carry an un-sanitised home-directory path -- replace "
        f"with the placeholder segment {_ALLOWED_HOME_SEGMENT!r}:\n"
        + "\n".join(offenders)
    )
