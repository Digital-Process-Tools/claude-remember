"""Committed files must not carry a real OS home-directory path or the
current machine's real OS username (#467).

## The mechanism -- and a correction to this file's own first draft

`tests/fixtures/codex-rollout.jsonl` was committed as a real capture,
deliberately sanitised: `cwd`, `git` and `turn_context` were replaced with
`/sanitized/project/dir`. The sanitisation missed two *nested JSON-string
blobs*, where the maintainer's real home directory and OS username sat
inside a serialised string value rather than at a top-level field a
field-keyed sanitiser would walk.

#467 shipped a SECOND leak in a second file, and this file's first draft
got the shape of it wrong. `tests/test_codex_upsubmit_stdout_451.py` --
ordinary hand-authored `.py` test source, not a fixture -- carried the bare
username `floriandavid` twice, pasted from real captured probe output
into a string literal (`"[10:54 CEST -- floriandavid]"`). That is a BARE
username, not a `/Users/<x>/` or `/home/<x>/` path, and it landed in a
hand-authored `.py` file, not a fixture.

This file's first draft argued that hand-authored `.py` literals
"can never carry a real person's real credentials by the mechanism that
produced #467 -- nobody accidentally pastes their own home directory into
a parametrize list the way a sanitiser silently misses a nested blob
inside a real capture." #467's own second file falsifies that premise: a
real credential got pasted into a hand-authored `.py` literal, in the same
issue this guard exists for. The premise was wrong, not just unproven, and
a guard built on it would have read as covering a class it did not cover.

Two different shapes need two different detectors, because the argument
against a tree-wide PATH sweep (too many legitimate `/Users/`, `/home/`
hits in hand-authored test data and docs -- see below) does not apply to a
USERNAME search, which has no equivalent flood of legitimate hits, but
does need a positive control against firing on nothing.

## Detector 1: un-sanitised home PATHS in fixtures (scope, argued)

Tree-wide path search (`git ls-files`, matching `/Users/` or `/home/`) was
considered and rejected for the PATH shape specifically. `/Users/` and
`/home/` appear *legitimately* throughout this repository outside
fixtures -- README.md's worked examples (`/Users/you/...`,
`/home/alice/...`), docs/slug-vectors.json's synthetic slug vectors,
scripts/bench-slug.sh's own benchmark input, and tests/test_hooks_json.py's
deliberately adversarial PowerShell-escaping parametrize list
(`C:/Users/Jane Doe/...`, `C:/Users/cafe/...`) are all hand-chosen example
paths. A tree-wide PATH sweep would need an allowlist covering all of
those (and would still not be complete, since a new adversarial-path test
could add another entry at any time), and the issue's own reasoning ("a
guard that fails on those will be disabled within a week") is exactly why
that path was rejected FOR THIS SHAPE.

So `test_no_fixture_carries_an_unsanitised_home_path` below stays scoped
to `tests/fixtures/`: the one place a real, uninvited PATH can arrive by
accident (a real capture) rather than by a human's deliberate choice of
example text. Anything under `tests/fixtures/` naming a `/Users/<x>/` or
`/home/<x>/` segment must use the placeholder segment this repository has
standardised on (`sanitized-user`) -- any other segment fails the guard.

**This detector alone does NOT cover #467.** It only ever covered the
first of #467's two leaked files. Restated plainly rather than left to be
inferred: `tests/*.py`, `README.md`, `CHANGELOG.md`, `docs/` and
`scripts/bench-slug.sh` carry no path-shaped check at all, by the argument
above, and a bare username (no `/Users/` or `/home/` prefix) is invisible
to this detector everywhere, fixtures included.

## Detector 2: the current machine's OS username, tree-wide

`test_no_committed_file_leaks_the_current_machine_username` below is the
other half, built for the shape the path detector cannot see. It searches
every git-tracked file for a verbatim occurrence of `getpass.getuser()` --
the account name of whoever is running the suite. On a contributor's own
machine that fires exactly when they have pasted their own identity into
the tree, which is when it matters and where #467's second leak actually
happened. This needed no tree-wide allowlist the way the path sweep would
have, because a *username* has no flood of legitimate look-alike hits the
way `/Users/` does -- nobody's test data is expected to spell a specific
person's login name.

**Stated plainly rather than left for a reader to notice from a green CI
leg:** in CI the runner's account name (`runner`, `root`, or similar) is
not a contributor's real identity, so this test still executes there and
still asserts something true (the runner's own login name is not what got
committed), but that pass is near-vacuous and MUST NOT be read as coverage
of the leak this guard exists for. The real guard is a contributor running
the suite locally, on their own machine, under their own username. Short
or well-known generic account names (`root`, `admin`, `runner`, ...) are
skipped rather than searched for, loudly, via `pytest.skip`, because
searching for a four-letter or generic name across ~300 files would
produce noise no maintainer could act on, not signal.

Scoped OUT of detector 2, by name and why: nothing is scoped out by path --
it is deliberately tree-wide, since a username has no legitimate-hit flood
to defend against -- except this file itself (it names the username in
its own prose, above and in code comments, which would otherwise be a
permanent false positive on whichever machine happens to run the suite
under that username).
"""

from __future__ import annotations

import getpass
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
THIS_FILE = Path(__file__).resolve()

# ---------------------------------------------------------------------------
# Detector 1: un-sanitised /Users/<x>/ or /home/<x>/ segments in fixtures.
# ---------------------------------------------------------------------------

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
    pattern actually fires on exactly the PATH shape #467's first file
    shipped: a home path nested inside a JSON string value, not at a
    top-level field."""
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
    #467's first-file mechanism -- a real capture that slipped an
    un-sanitised path into the tree, where the install layout
    (`.agents/plugins/marketplace.json`'s local source) ships tests/ into
    every install. Covers PATHS only -- see detector 2 below for the bare
    USERNAME shape #467's second file actually shipped, which this
    detector cannot see at any scope."""
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


# ---------------------------------------------------------------------------
# Detector 2: the current machine's real OS username, tree-wide.
# ---------------------------------------------------------------------------

_MIN_USERNAME_LEN = 4

# Account names common enough (a real person's, or a CI runner's default)
# that searching for them verbatim across ~300 files would produce noise,
# not signal -- skipped loudly rather than silently, via pytest.skip.
_GENERIC_USERNAMES = {
    "root", "admin", "administrator", "runner", "user", "test", "ci",
    "docker", "ubuntu", "actions", "github", "vagrant", "guest", "demo",
}


def _current_username() -> str | None:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser() raises platform-varying errors when no login name is resolvable; any of them means "unknown", handled by the caller's None check
        return None
    return name or None


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line.strip()]


def test_scanned_at_least_one_tracked_file():
    """Positive control for the tree-wide enumeration: a `git ls-files`
    that silently returned nothing must not let the real check below pass
    by vacuous truth."""
    files = _tracked_files()
    assert len(files) >= 50, (
        f"expected at least 50 tracked files, found {len(files)} -- "
        "`git ls-files` likely did not run against the real tree"
    )


def test_username_detector_fires_on_a_planted_occurrence():
    """Positive control for detector 2 itself. A detector keyed on a name
    that never appears is the definition of a check that cannot fail --
    this proves the plain substring search actually matches, independent
    of whether today's real username happens to be committed anywhere."""
    username = _current_username() or "planted-fallback-username"
    haystack = f'"additionalContext": "[10:54 CEST -- {username}]"'
    assert username in haystack, "planted haystack did not contain its own planted username"


def test_no_tracked_file_leaks_the_current_machine_username():
    """MUST FIRE case, and the one #467's second file actually needed:
    a BARE username (no /Users/ or /home/ prefix) pasted into a
    hand-authored .py string literal, which test_no_fixture_carries_an_
    unsanitised_home_path above cannot see at any scope. See the module
    docstring for what this detector buys on a contributor's own machine
    versus in CI, and why -- that asymmetry is stated here rather than
    left for a green CI leg to be misread as coverage."""
    username = _current_username()
    if not username:
        pytest.skip("could not determine the current OS username (getpass.getuser() failed)")
    if len(username) < _MIN_USERNAME_LEN or username.lower() in _GENERIC_USERNAMES:
        pytest.skip(
            f"current username {username!r} is too short or a known generic/CI "
            "account name to search for without false positives -- this run "
            "provides no evidence either way, see the module docstring"
        )
    offenders: list[str] = []
    for f in _tracked_files():
        if f.resolve() == THIS_FILE:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except (IsADirectoryError, PermissionError, OSError):
            continue
        if username in text:
            offenders.append(str(f.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"the current machine's OS username {username!r} appears in "
        f"committed file(s) -- #467's own second-file mechanism: {offenders}"
    )
