"""Guard: the plugin manifest version must track the released CHANGELOG version (#133).

Claude Code's plugin updater compares ``.claude-plugin/plugin.json`` *versions*,
not source SHAs. When 0.8.4 shipped without bumping the manifest, every
marketplace install kept reporting "already at the latest version (0.8.3)" and
silently never received the release — including fixes whose failure mode is
self-reinforcing. A stale manifest is therefore a shipping bug, not cosmetics.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
README = REPO_ROOT / "README.md"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
RELEASE_HEADING_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]")
README_BADGE_RE = re.compile(
    r"!\[Version\]\(https://img\.shields\.io/badge/version-(\d+\.\d+\.\d+)-orange\)"
)


def _manifest_version() -> str:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]


def _changelog_versions() -> list[str]:
    """Released versions, newest first. ``## [Unreleased]`` is skipped by the regex."""
    return [
        m.group(1)
        for line in CHANGELOG.read_text(encoding="utf-8").splitlines()
        if (m := RELEASE_HEADING_RE.match(line))
    ]


def test_manifest_version_is_semver():
    version = _manifest_version()
    assert SEMVER_RE.match(version), f"plugin.json version is not semver: {version!r}"


def test_changelog_has_released_versions():
    assert _changelog_versions(), "CHANGELOG.md has no '## [x.y.z]' release heading"


def test_manifest_matches_newest_changelog_release():
    """The manifest must declare the newest released version, or the release never ships."""
    manifest_version = _manifest_version()
    newest = _changelog_versions()[0]
    assert manifest_version == newest, (
        f".claude-plugin/plugin.json declares {manifest_version} but the newest "
        f"CHANGELOG.md release is {newest}. Marketplace updaters compare manifest "
        f"versions, so shipping this way makes the release invisible (#133). "
        f"Bump the manifest as part of the release commit."
    )


def test_changelog_releases_are_strictly_descending():
    """Newest-first ordering is what makes 'the newest release' well defined."""
    versions = [tuple(int(p) for p in v.split(".")) for v in _changelog_versions()]
    assert versions == sorted(versions, reverse=True), (
        f"CHANGELOG.md release headings are not in descending order: {_changelog_versions()}"
    )

def test_readme_version_badge_matches_code():
    """A sweep keyed on the outgoing version can never find a badge that stopped
    being bumped (#335): it is not mid-transition, so it never matches the
    pattern any release sweep would grep for. Pin the badge to the manifest
    directly, and fail loud if the badge is not even found -- a regex that
    matched nothing has not checked the badge, and must not read as a pass.
    """
    text = README.read_text(encoding="utf-8")
    match = README_BADGE_RE.search(text)
    assert match is not None, (
        "README.md has no version badge matching the expected shields.io pattern "
        "(![Version](.../badge/version-X.Y.Z-orange)) -- the badge markup may have "
        "changed shape. Update README_BADGE_RE rather than letting this pass silently."
    )
    badge_version = match.group(1)
    manifest_version = _manifest_version()
    assert badge_version == manifest_version, (
        f"README.md's version badge reads {badge_version} but "
        f".claude-plugin/plugin.json declares {manifest_version}. The badge is "
        f"README.md:9 and drifts silently because release sweeps grep for the "
        f"outgoing version, which a frozen badge never matches (#335)."
    )
