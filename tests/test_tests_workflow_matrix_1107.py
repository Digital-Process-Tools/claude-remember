"""Issue 1107: ubuntu-latest moved to Ubuntu 26.04, which has no prebuilt
Python 3.9 x64 build for actions/setup-python, breaking the
"pytest (ubuntu-latest, 3.9)" leg at the setup step before any test runs
(observed on PR 1105 job 127504329601 and PR 1106 job 127520449349: the
version 3.9 with architecture x64 was not found for Ubuntu 26.04). The fix
keeps 3.9 coverage by moving that one matrix cell onto an image that still
ships it (ubuntu-24.04) via exclude/include, rather than dropping 3.9 from
the declared matrix.

Written before the fix: the workflow had no exclude/include keys at all, so
every assertion below failed -- a test that could not tell "matrix
excludes/includes nothing" from "matrix routes the broken cell away" would
pass on the pre-fix file just as readily as on the fixed one.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _pytest_matrix() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["pytest"]["strategy"]["matrix"]


def test_declared_matrix_still_lists_39_through_312():
    matrix = _pytest_matrix()
    assert matrix["python-version"] == ["3.9", "3.10", "3.11", "3.12"], (
        "tests/pep604_floor.py declared_floor() parses this exact "
        "python-version list with a regex -- the #1107 fix must not remove it"
    )


def test_ubuntu_latest_39_cell_is_excluded():
    matrix = _pytest_matrix()
    excludes = matrix.get("exclude", [])
    assert {"os": "ubuntu-latest", "python-version": "3.9"} in excludes, (
        "ubuntu-latest moved to Ubuntu 26.04 (#1107), which has no prebuilt "
        "Python 3.9 x64 build -- the (ubuntu-latest, 3.9) cell must be "
        "excluded from the cross-product rather than left to fail "
        "actions/setup-python"
    )


def test_other_os_39_cells_remain_in_the_cross_product():
    matrix = _pytest_matrix()
    excludes = matrix.get("exclude", [])
    assert {"os": "macos-latest", "python-version": "3.9"} not in excludes
    assert {"os": "windows-latest", "python-version": "3.9"} not in excludes


def test_39_coverage_is_restored_on_an_ubuntu_image_that_has_it():
    matrix = _pytest_matrix()
    includes = matrix.get("include", [])
    restored = [
        entry for entry in includes
        if entry.get("python-version") == "3.9"
        and str(entry.get("os", "")).startswith("ubuntu-")
    ]
    assert restored, (
        "excluding (ubuntu-latest, 3.9) must not simply drop 3.9 coverage on "
        "Linux -- #1107 asks for the cell to move to an ubuntu image that "
        "still has 3.9 (e.g. ubuntu-24.04), not to disappear"
    )
    assert restored[0]["os"] != "ubuntu-latest", (
        "the included replacement must not be ubuntu-latest again -- that "
        "is the exact image #1107 says has no prebuilt 3.9"
    )
