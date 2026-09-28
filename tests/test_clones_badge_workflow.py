"""#828 -- the clones badge nightly workflow follows this repo's own security
posture and the README badge points at where it publishes.

Written before the workflow file existed: every assertion here failed on an
absent `.github/workflows/clones-badge.yml` and an unmodified README.md, which
is the point -- a test that could not tell "missing" from "wrong shape" would
pass on a typo just as readily as on a correct file.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "clones-badge.yml"
README = REPO_ROOT / "README.md"

SHA_PINNED_USES_RE = re.compile(r"^([\w.-]+/[\w.-]+)@([0-9a-f]{40})\s*#\s*(v[\w.-]+)\s*$")


def _load_workflow() -> dict:
    assert WORKFLOW.exists(), (
        f"{WORKFLOW} does not exist -- issue #828 asks for a nightly workflow "
        "that fetches traffic/clones and publishes a shields.io endpoint."
    )
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_is_valid_yaml_with_a_job():
    doc = _load_workflow()
    assert doc.get("jobs"), f"{WORKFLOW} parsed but declares no jobs"


def test_workflow_runs_on_schedule_and_manual_dispatch():
    doc = _load_workflow()
    # PyYAML parses the bare key `on:` as the boolean True, not the string "on".
    triggers = doc.get(True, doc.get("on", {}))
    assert "schedule" in triggers, (
        f"{WORKFLOW} has no `schedule:` trigger -- issue #828 asks for a nightly "
        "cron accumulating traffic/clones, which the 14-day API window would "
        "otherwise lose"
    )
    assert "workflow_dispatch" in triggers, (
        f"{WORKFLOW} has no `workflow_dispatch:` trigger -- issue #828 asks for "
        "one so a maintainer can run it on demand"
    )


def test_workflow_default_permissions_are_read_only():
    doc = _load_workflow()
    assert doc.get("permissions", {}).get("contents") == "read", (
        f"{WORKFLOW} should declare `permissions: contents: read` at the "
        "workflow level (least privilege) and grant `contents: write` only on "
        "the one job that pushes to the badges branch, matching this repo's "
        "own oss-changelog.yml convention"
    )


def test_every_action_is_pinned_to_a_commit_sha():
    """This repo's own oss-changelog.yml (#1462) pins every action to a 40-hex
    commit SHA with a trailing `# vX.Y.Z` comment rather than a moving tag,
    because an unpinned tag runs whatever its publisher pushes next under this
    repository's own token. A new hand-rolled workflow must not reintroduce
    the risk that convention exists to close.
    """
    doc = _load_workflow()
    uses_lines = []
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            if "uses" in step:
                uses_lines.append(step["uses"])
    assert uses_lines, f"{WORKFLOW} has no `uses:` steps to check"
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    for uses in uses_lines:
        action_ref = uses.split("@", 1)[0] if "@" in uses else uses
        # Find the full source line (yaml.safe_load strips the trailing comment).
        for line in workflow_text.splitlines():
            stripped = line.strip().lstrip("- ")
            if stripped.startswith("uses:") and action_ref in line:
                uses_value = stripped.split("uses:", 1)[1].strip()
                assert SHA_PINNED_USES_RE.match(uses_value), (
                    f"{WORKFLOW} pins {action_ref!r} with {uses_value!r}, which "
                    "is not a 40-hex commit SHA with a trailing '# vX.Y.Z' "
                    "comment -- see .github/workflows/oss-changelog.yml (#1462)"
                )
                break
        else:
            raise AssertionError(f"could not find the source line for uses: {uses!r}")


def test_workflow_does_not_fail_loudly_when_the_secret_is_absent():
    """Issue #828 step 1 says the TRAFFIC_TOKEN secret is 'Created by a
    maintainer' -- a step that happens after this workflow file merges, not
    before. The workflow must not turn every nightly run red until then.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "TRAFFIC_TOKEN" in text, (
        f"{WORKFLOW} never references the TRAFFIC_TOKEN secret described in issue #828"
    )
    assert re.search(r"-z\s+\S*\{?GH_TOKEN|-z\s+\S*\{?TRAFFIC_TOKEN", text), (
        f"{WORKFLOW} does not appear to guard against an unset TRAFFIC_TOKEN "
        "secret -- a maintainer has not created it yet, and a nightly cron "
        "run should skip cleanly rather than fail"
    )


def test_readme_has_a_clones_badge_pointing_at_the_badges_branch():
    text = README.read_text(encoding="utf-8")
    assert "clones" in text.lower(), "README.md has no clones badge text at all"
    assert (
        "raw.githubusercontent.com/Digital-Process-Tools/claude-remember/badges/clones.json"
        in text
    ), (
        "README.md's clones badge does not point at the badges branch's "
        "clones.json, the shields.io endpoint shape from issue #828"
    )


def test_readme_clones_badge_is_labelled_clones_not_downloads():
    """Issue #828 is explicit: label it 'clones', not 'downloads' -- it is
    GitHub's own clone count, which includes Claude Code re-cloning the
    marketplace for updates, shown as GitHub's number, as-is.
    """
    text = README.read_text(encoding="utf-8")
    badge_lines = [line for line in text.splitlines() if "clones.json" in line]
    assert badge_lines, "no README.md line references clones.json"
    for line in badge_lines:
        assert "downloads" not in line.lower(), (
            f"README.md clones badge line reads {line!r} -- issue #828 says label "
            "it 'clones', not 'downloads'"
        )
