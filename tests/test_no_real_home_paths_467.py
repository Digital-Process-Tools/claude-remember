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
username `sanitized-user` twice, pasted from real captured probe output
into a string literal (`"[10:54 CEST -- sanitized-user]"`). That is a BARE
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

`test_no_tracked_file_leaks_the_current_machine_username` below is the
other half, built for the shape the path detector cannot see. It searches
every git-tracked file for a verbatim occurrence of `getpass.getuser()` --
the account name of whoever is running the suite. On a contributor's own
machine that fires exactly when they have pasted their own identity into
the tree, which is when it matters and where #467's second leak actually
happened. This needed no tree-wide allowlist the way the path sweep would
have, because a *username* has no flood of legitimate look-alike hits the
way `/Users/` does -- nobody's test data is expected to spell a specific
person's login name.

### Correction: this detector no longer runs on CI at all -- and why that
### is not the same call #442 made, even though it looks identical in shape

The first version of this detector ran everywhere, including CI, arguing
the CI pass was "near-vacuous" but still worth having. #472 turned that
into a live false positive: `windows-latest` runs as the account
`runneradmin`, which appears -- legitimately, deliberately -- in
`tests/test_stale_config_sweep_362.py` (lines 174 and 186) as a realistic
Windows path fixture rooted at that account's own home directory. Adding
`"runneradmin"` to a skiplist would have fixed that one image and nothing
else: every CI image has its own account name (`runner`, `runneradmin`,
whatever GitHub, GitLab or the next provider calls the next one), each
discovered the same way this one was -- a red build on somebody else's
release. A skiplist that grows one entry per incident is a guard that
costs more than it catches, and it does not even need a new entry to be
wrong; it is wrong on the next image by construction.

This repository already has a precedent for the mirror question:
`tests/test_case_divergence_298.py`'s `_origin_main_should_be_resolvable`
(#442) makes a guard FAIL, not skip, when CI's own env vars are set and
its precondition (`origin/main` resolvable) is not met. Reading it before
deciding here was deliberate, and the two answers differ because the two
preconditions differ in kind, not degree:

- #442's precondition is something CI is SUPPOSED to guarantee -- the
  checkout step is supposed to make `origin/main` resolvable. Its absence
  on a CI runner means the infrastructure did its job wrong, and a skip
  there would hide a real defect behind a green tick. Failing loudly is
  correct because there IS a defect to report.
- This detector's precondition is running under a CONTRIBUTOR's own
  account. A CI runner is never a contributor's own account BY DESIGN --
  not because some setup step failed to arrange it, the way `origin/main`
  failing to resolve means the checkout step failed. There is no
  infrastructure bug for a loud failure to surface here: the runner's
  account name is simply not evidence of anything, on `runneradmin` today
  and on whatever the next CI image calls itself tomorrow. Asserting
  against it does not catch defects, it manufactures false positives, one
  new CI account name at a time -- which is exactly what #472 was.

So: `_username_check_should_run()` below returns `False` whenever `CI` or
`GITHUB_ACTIONS` is set (the same two-variable convention #442 already
uses in this file's sibling), and the check skips there, LOUDLY, naming
why in the skip reason rather than rendering as a silent pass -- `pytest`
reports a skip distinctly from a pass in its own summary line and under
`-rs`, and the reason argues the CI leg is not merely uninformative but is
answering a question that does not apply to it. Both directions of that
decision function are pinned the same way #442 pins its own
(`test_username_check_should_run_true_locally` /
`test_username_check_should_run_false_on_ci`), so a future edit that
silently flips either arm -- always-run (reintroducing #472) or
always-skip (a guard nobody ever runs) -- fails a test, not just a review.

Off CI, the short/generic-account-name skip is unchanged and unrelated to
this: a contributor's own machine can still legitimately be named `admin`
or `test`, and searching for a four-or-fewer-letter or dictionary-common
account name across ~300 tracked files would produce noise no maintainer
could act on, not signal. That skip is also loud (`pytest.skip`, reason
stated) and is a genuinely different question from "is this CI at all".

Scoped OUT of detector 2, by name and why: nothing is scoped out by path --
it is deliberately tree-wide, and that now includes this file itself
(#474). An earlier draft exempted THIS_FILE from the tree-wide scan, on
the argument that a file documenting the mechanism has to be able to
quote a real leaked value, and a single-file skip is the smallest
exemption that permits that. #474 found the composition that argument
missed: the exemption plus a real username quoted under it means the
tree ships the value, the install copies it, and the one file that
could have caught it is the one file the guard cannot see. The guard
went green on a tree containing exactly what it exists to detect.

The docstring above now names the mechanism with the same placeholder
the fixture already standardised on (`sanitized-user`), which explains
detector 2 identically without quoting anything real -- so the
exemption's only remaining job would be to permit a FUTURE quotation,
not to excuse a present one. That job is not worth keeping: a
placeholder says everything a real value would, and keeping a
self-exemption around "just in case" is exactly the kind of narrow,
individually-defensible carve-out that produced this issue in the first
place. Removing it means the next person who pastes a real username
into this file's prose gets a failing test on their own machine, in the
same run, rather than a guard that stays green because it cannot look
at itself. That is a strictly better failure mode than the one this
issue reports, so THIS_FILE is no longer exempted and is no longer
defined below.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

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
    """Recursive by design (#480): `iterdir()` only ever saw the
    immediate children of tests/fixtures/, so a fixture arriving inside
    a subdirectory -- exactly the shape a future real capture could
    land in -- was invisible to Detector 1 at any depth, and its own
    positive control (`test_scanned_at_least_one_fixture_file`'s
    `>= 2`) could not have noticed, because it counts files rather than
    walking the tree it claims to cover. `rglob("*")` walks every
    subdirectory; the `>= 2` control below is unchanged (still catches
    an empty/broken walk), and `test_fixture_scan_reports_how_many_files_it_examined`
    adds the other half the issue asked for -- a receipt cross-checked
    against `git ls-files`, so a walk that silently drops a subtree
    disagrees with a number nobody has to trust the walk itself for."""
    return sorted(p for p in FIXTURES_DIR.rglob("*") if p.is_file())


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


def test_fixture_scan_descends_into_subdirectories(monkeypatch, tmp_path):
    """MUST FIRE case for the ENUMERATION itself (#480): a fixture file
    living in a subdirectory of tests/fixtures/ must be visible to the
    scan, not just a file sitting at the top level. The real
    tests/fixtures/ has no subdirectories today, so this cannot be
    exercised against the real tree either way -- a synthetic tree is
    built here instead, with one file at the top and one nested two
    levels down, so depth is actually tested rather than assumed.

    Before the fix (`FIXTURES_DIR.iterdir()`), this fails: iterdir()
    yields only immediate children, so the nested file never appears in
    the returned list. That is exactly what this issue reports --
    Detector 1 enumerates non-recursively -- and it is why
    `test_scanned_at_least_one_fixture_file` above could not have caught
    it: its `>= 2` count is satisfied by the top-level files alone, so a
    missed subtree changes nothing it can observe."""
    monkeypatch.setattr(f"{__name__}.FIXTURES_DIR", tmp_path)
    (tmp_path / "top.txt").write_text("nothing interesting here", encoding="utf-8")
    nested_dir = tmp_path / "captures" / "2026-08"
    nested_dir.mkdir(parents=True)
    nested_file = nested_dir / "leak.jsonl"
    nested_file.write_text("no home path in this one either", encoding="utf-8")

    found = _fixture_files()

    assert nested_file in found, (
        f"fixture scan did not descend into a subdirectory -- found only "
        f"{[str(p) for p in found]}, missing {nested_file}"
    )


def test_detector_fires_on_a_home_path_nested_in_a_fixture_subdirectory(
    monkeypatch, tmp_path
):
    """MUST FIRE case for the DETECTOR end to end (#480), at the depth
    the real bug lived at: this issue's whole point is that a real
    #467-shaped leak arriving inside a subdirectory of tests/fixtures/
    was invisible to both the enumeration and this detector's own
    positive control. Plants the same JSON-string-nested shape #467's
    first file actually shipped, two directories deep, and drives it
    through the real detector logic (not just the bare regex the way
    test_detector_fires_on_a_planted_real_looking_home_path does) --
    this is the case that would still pass if only the regex were
    proven to fire and the walk stayed blind to the subtree."""
    monkeypatch.setattr(f"{__name__}.FIXTURES_DIR", tmp_path)
    nested_dir = tmp_path / "captures" / "2026-08"
    nested_dir.mkdir(parents=True)
    leaking = nested_dir / "codex-rollout.jsonl"
    leaking.write_text(
        r'{"payload": {"cwd": "/Users/realname/project"}}', encoding="utf-8"
    )

    offenders: list[str] = []
    for f in _fixture_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for segment in _HOME_PATH.findall(text):
            if segment != _ALLOWED_HOME_SEGMENT:
                offenders.append(f"{f}: {segment!r}")

    assert offenders, (
        "detector missed a real-looking home path nested in a fixtures "
        "subdirectory -- the walk is not reaching that depth"
    )


def test_detector_does_not_fire_on_a_sanitised_path_in_a_fixture_subdirectory(
    monkeypatch, tmp_path
):
    """MUST-NOT-FIRE case pairing the one above (CLAUDE.md: a negative
    assertion needs a positive control, and the reverse holds too --
    widening the walk must not start over-firing on the placeholder
    segment this repository already standardised on, at any depth)."""
    monkeypatch.setattr(f"{__name__}.FIXTURES_DIR", tmp_path)
    nested_dir = tmp_path / "captures" / "2026-08"
    nested_dir.mkdir(parents=True)
    clean = nested_dir / "codex-rollout.jsonl"
    clean.write_text(
        r'{"payload": {"cwd": "/Users/sanitized-user/project"}}',
        encoding="utf-8",
    )

    offenders: list[str] = []
    for f in _fixture_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for segment in _HOME_PATH.findall(text):
            if segment != _ALLOWED_HOME_SEGMENT:
                offenders.append(f"{f}: {segment!r}")

    assert not offenders, (
        f"detector false-fired on the sanitised placeholder segment in a "
        f"fixtures subdirectory: {offenders}"
    )


def test_fixture_scan_reports_how_many_files_it_examined(capsys):
    """Receipt (#480): the issue's own fix note asks for recursion PLUS a
    receipt naming how many files were actually examined, so an empty
    walk cannot read as a clean one -- the failure mode this whole issue
    is about is a scan that silently looked at fewer files than it
    should have and a positive control that could not tell. Printing the
    count (visible under `pytest -s` / `-rs` in CI logs) turns "the guard
    was green" into a number a human reviewing a release audit can sanity
    -check against how many files are actually tracked under
    tests/fixtures/, the same way the module docstring's audit trail
    already quotes `git grep -c` output as its own receipt.

    Deliberately a SUBSET check, not an exact-count comparison: an
    earlier draft asserted `len(files) == len(tracked)`, which a
    reviewer caught failing on a file that exists on disk under
    tests/fixtures/ but is not git-tracked -- a stray `.DS_Store` was the
    reproduction, and it is `.gitignore`d in this repo but still visible
    to `rglob("*")`, which walks the filesystem rather than the git
    index. That is noise unrelated to #480 (a contributor who has ever
    opened tests/fixtures/ in Finder could fail this test locally for a
    reason that has nothing to do with the detector), so the receipt now
    asserts every git-tracked fixture was found by the walk, without
    demanding the walk find NOTHING else -- that subset direction is
    exactly the one a dropped subtree would violate."""
    found = set(_fixture_files())
    print(f"[receipt] examined {len(found)} fixture file(s) under {FIXTURES_DIR}")
    tracked = subprocess.run(
        ["git", "ls-files", "tests/fixtures"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    tracked_paths = {REPO_ROOT / line for line in tracked}
    missing = tracked_paths - found
    assert not missing, (
        f"fixture scan missed {len(missing)} git-tracked file(s) under "
        f"tests/fixtures/ that git ls-files reports: {sorted(missing)} -- "
        "the walk and the tree disagree, which is exactly what a silent "
        "scope hole looks like"
    )
    captured = capsys.readouterr()
    assert "[receipt] examined" in captured.out


# ---------------------------------------------------------------------------
# Detector 2: the current machine's real OS username, tree-wide.
# ---------------------------------------------------------------------------

_MIN_USERNAME_LEN = 5

# Account names common enough on a CONTRIBUTOR's own local machine (not a
# CI runner -- that question is answered separately by
# _username_check_should_run, below) that searching for them verbatim
# across ~300 files would produce noise, not signal -- skipped loudly
# rather than silently, via pytest.skip. Deliberately NOT the place a new
# CI runner account name gets added when the next one is discovered: see
# the module docstring's #472 correction for why that list is the wrong
# fix, and _username_check_should_run for the actual one.
_GENERIC_USERNAMES = {
    "admin", "administrator", "user", "test", "guest", "demo",
}


def _username_check_should_run(env: dict) -> bool:
    """A pure decision, kept separate from the scan so it can be tested
    without depending on the environment actually running this suite --
    mirrors tests/test_case_divergence_298.py's own
    _origin_main_should_be_resolvable(#442) in shape and answers the
    opposite question on purpose. See the module docstring's "Correction"
    section for the argument; restated here as the one-line contract this
    function keeps: False (skip) whenever `CI` or `GITHUB_ACTIONS` is set,
    because a CI runner's own account name is never a contributor's real
    identity and is not evidence of anything -- there is no infrastructure
    defect for a loud failure to surface, unlike #442's own check, where
    an unresolved `origin/main` on CI means the checkout step itself
    failed. True everywhere else, which is where this detector's evidence
    actually lives."""
    return not (env.get("CI") == "true" or env.get("GITHUB_ACTIONS") == "true")


def test_username_check_should_run_true_locally():
    """Positive control's other half: no CI signal means a contributor's
    own machine, where this detector must actually run."""
    assert _username_check_should_run({}) is True
    assert _username_check_should_run({"PATH": "/usr/bin"}) is True
    assert _username_check_should_run({"CI": "false"}) is True


def test_username_check_should_run_false_on_ci():
    """The load-bearing case for #472: a version of this function that
    always returned True would reproduce #472 (a false positive on every
    CI image's own account name) the moment a new one shows up; a version
    that always returned False would mean this detector never runs at
    all. This is the one that catches either regression."""
    assert _username_check_should_run({"CI": "true"}) is False
    assert _username_check_should_run({"GITHUB_ACTIONS": "true"}) is False


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
    by vacuous truth. Runs unconditionally, on CI included -- `git
    ls-files` behaves identically everywhere, unlike the username scan
    below."""
    files = _tracked_files()
    assert len(files) >= 50, (
        f"expected at least 50 tracked files, found {len(files)} -- "
        "`git ls-files` likely did not run against the real tree"
    )


def test_username_detector_fires_on_a_planted_occurrence():
    """Positive control for detector 2 itself. A detector keyed on a name
    that never appears is the definition of a check that cannot fail --
    this proves the plain substring search actually matches, independent
    of whether today's real username happens to be committed anywhere.
    Runs unconditionally, CI included: this is a pure string check, not a
    tree scan, so it carries none of the CI-account-name false-positive
    risk the real check below is skipped for."""
    username = _current_username() or "planted-fallback-username"
    haystack = f'"additionalContext": "[10:54 CEST -- {username}]"'
    assert username in haystack, "planted haystack did not contain its own planted username"


def test_no_tracked_file_leaks_the_current_machine_username():
    """MUST FIRE case, and the one #467's second file actually needed:
    a BARE username (no /Users/ or /home/ prefix) pasted into a
    hand-authored .py string literal, which test_no_fixture_carries_an_
    unsanitised_home_path above cannot see at any scope. See the module
    docstring for what this detector buys on a contributor's own machine,
    why it is skipped -- loudly, naming why -- on CI rather than asserted
    against a runner's own account name (#472), and why that skip is not
    the same call #442 makes for `origin/main`."""
    if not _username_check_should_run(os.environ):
        pytest.skip(
            "CI environment detected (CI or GITHUB_ACTIONS set) -- this "
            "detector's precondition (running under a CONTRIBUTOR's own "
            "account) is never true on a CI runner BY DESIGN, not by a "
            "setup failure, so it is skipped here rather than asserted "
            "against the runner's own account name (#472: 'runneradmin' on "
            "windows-latest legitimately appears in "
            "tests/test_stale_config_sweep_362.py's Windows path fixtures). "
            "See the module docstring's 'Correction' section for why this "
            "differs from tests/test_case_divergence_298.py's #442 check, "
            "which fails loudly on CI instead. The real guard for this "
            "class is a contributor running the suite locally."
        )
    username = _current_username()
    if not username:
        pytest.skip("could not determine the current OS username (getpass.getuser() failed)")
    if len(username) < _MIN_USERNAME_LEN or username.lower() in _GENERIC_USERNAMES:
        pytest.skip(
            f"current username {username!r} is too short or a known generic "
            "local-account name to search for without false positives -- "
            "this run provides no evidence either way, see the module "
            "docstring's _GENERIC_USERNAMES comment"
        )
    offenders: list[str] = []
    for f in _tracked_files():
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
