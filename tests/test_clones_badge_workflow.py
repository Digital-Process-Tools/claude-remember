"""#828 -- the clones badge nightly workflow follows this repo's own security
posture and the README badge points at where it publishes.

Written before the workflow file existed: every assertion here failed on an
absent `.github/workflows/clones-badge.yml` and an unmodified README.md, which
is the point -- a test that could not tell "missing" from "wrong shape" would
pass on a typo just as readily as on a correct file.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
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

    A plain text-search for the guard's own source (e.g. `-z ... GH_TOKEN`)
    cannot tell a guard that actually skips from one that only *logs* and
    falls through to `gh api` anyway -- both contain the same substring. This
    actually runs the step's own shell script with GH_TOKEN unset and a
    stubbed `gh` that would leave a marker file if invoked, so a regression
    that deletes the guard's `exit 0` (letting execution reach `gh api`) fails
    this test rather than passing it.

    Both scripts are written to real files with an explicit LF newline and
    bash is invoked on the FILE PATH rather than via `bash -c "<multi-line
    string>"`: passing a multi-line script as one command-line argument, and
    `Path.write_text`'s platform-default newline translation (CRLF on
    Windows), are both real cross-platform risk here -- windows-latest CI
    caught this test itself failing for exactly that class of reason (#828
    PR review round), independently of the guard logic under test.
    """
    doc = _load_workflow()
    steps = doc["jobs"]["update"]["steps"]
    fetch_step = next(
        (s for s in steps if s.get("name") == "Fetch traffic/clones"), None
    )
    assert fetch_step is not None, (
        f"{WORKFLOW} has no step named 'Fetch traffic/clones' to guard-test"
    )
    assert "TRAFFIC_TOKEN" in WORKFLOW.read_text(encoding="utf-8"), (
        f"{WORKFLOW} never references the TRAFFIC_TOKEN secret described in issue #828"
    )

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no `bash` on PATH -- cannot exercise the guard's own shell script here")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        marker = tmp_path / "gh_was_called"

        fake_gh = tmp_path / "gh"
        with open(fake_gh, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        fake_gh.chmod(0o755)

        script = tmp_path / "fetch-step.sh"
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(fetch_step["run"])

        env = {k: v for k, v in os.environ.items() if k.upper() != "GH_TOKEN"}
        env["GH_REPO"] = "example/example"
        env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

        result = subprocess.run(
            [bash, str(script)],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    assert result.returncode == 0, (
        f"the TRAFFIC_TOKEN guard did not skip cleanly with GH_TOKEN unset -- "
        f"bash={bash!r}, exit {result.returncode}, "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert not marker.exists(), (
        "the guard let execution reach `gh api` even though GH_TOKEN was unset "
        "-- a maintainer has not created the TRAFFIC_TOKEN secret yet (issue "
        "#828 step 1), so every nightly run would fail until they do"
    )


def test_merge_step_warns_when_traffic_json_has_no_clones_key():
    """#877 -- `gh api` only treats a non-2xx HTTP status as a failure. A 200
    response whose body has no `clones` key (a schema change, or any other
    200 shape) makes `// []` silently substitute an empty array, and the
    workflow still reports success -- it just adds nothing to history.json
    that day, with no signal anywhere that the fetch step actually ran and
    produced an unparseable shape.

    This runs the "Merge into history and write the badge" step's own shell
    script against a crafted traffic.json missing the `clones` key, and
    asserts it still exits 0 (a schema hiccup must not redden a nightly
    cron) while printing a loud `::warning::` annotation naming the gap --
    the positive control below pins that the warning does NOT fire when
    `clones` is present, so a guard that always warns would fail that half.
    """
    doc = _load_workflow()
    steps = doc["jobs"]["update"]["steps"]
    merge_step = next(
        (s for s in steps if s.get("name") == "Merge into history and write the badge"),
        None,
    )
    assert merge_step is not None, (
        f"{WORKFLOW} has no step named 'Merge into history and write the badge' to guard-test"
    )

    bash = shutil.which("bash")
    jq = shutil.which("jq")
    if bash is None or jq is None:
        pytest.skip("no `bash`/`jq` on PATH -- cannot exercise the merge step's own shell script here")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "history.json").write_text("[]", encoding="utf-8")
        (tmp_path / "traffic.json").write_text('{"count": 1, "uniques": 1}', encoding="utf-8")

        script = tmp_path / "merge-step.sh"
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(merge_step["run"])

        result = subprocess.run(
            [bash, str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    assert result.returncode == 0, (
        f"a traffic.json missing the 'clones' key must not fail the step -- "
        f"exit {result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert "::warning::" in result.stdout, (
        "the merge step produced no ::warning:: annotation when traffic.json had no "
        f"'clones' key -- stdout={result.stdout!r}"
    )


def test_merge_step_does_not_warn_when_clones_key_is_present():
    """Positive control for the test above: a traffic.json shaped the way
    GitHub's API actually documents it (a `clones` key present, even if
    empty) must not trip the schema-drift warning -- a guard that warns
    unconditionally would pass the test above and fail this one.
    """
    doc = _load_workflow()
    steps = doc["jobs"]["update"]["steps"]
    merge_step = next(
        (s for s in steps if s.get("name") == "Merge into history and write the badge"),
        None,
    )
    assert merge_step is not None

    bash = shutil.which("bash")
    jq = shutil.which("jq")
    if bash is None or jq is None:
        pytest.skip("no `bash`/`jq` on PATH -- cannot exercise the merge step's own shell script here")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "history.json").write_text("[]", encoding="utf-8")
        (tmp_path / "traffic.json").write_text(
            '{"count": 1, "uniques": 1, "clones": [{"timestamp": "2026-01-01T00:00:00Z", "count": 1, "uniques": 1}]}',
            encoding="utf-8",
        )

        script = tmp_path / "merge-step.sh"
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(merge_step["run"])

        result = subprocess.run(
            [bash, str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    assert result.returncode == 0, (
        f"exit {result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert "::warning::" not in result.stdout, (
        "the merge step warned about a missing 'clones' key even though traffic.json "
        f"had one -- stdout={result.stdout!r}"
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
