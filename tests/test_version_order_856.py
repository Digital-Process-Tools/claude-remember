"""`.github/scripts/check_version_order.py` refuses to publish a release tree
whose plugin version does not move `release` forward (#856).

A patch tag for an older line pushed after a newer release would otherwise add
its tree as a child commit of the newer one, moving the directory backwards.
The CLI is the enforcement point release-branch.yml's `publish` job calls; the
tests exercise main() directly rather than only parse_version(), since the exit
code and the printed ::error:: line are what the workflow actually acts on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_version_order.py"


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#856)"
    spec = importlib.util.spec_from_file_location("check_version_order", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_version_order"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_a_strictly_greater_version_passes(capsys):
    mod = _load()
    assert mod.main(["--new", "0.38.0", "--parent", "0.37.0"]) == 0
    assert "ok to publish" in capsys.readouterr().out


def test_an_older_version_is_refused(capsys):
    mod = _load()
    assert mod.main(["--new", "0.36.2", "--parent", "0.37.0"]) == 1
    out = capsys.readouterr().out
    assert "::error::" in out and "0.36.2" in out and "0.37.0" in out


def test_an_equal_version_is_refused():
    """Equal is not "strictly greater" -- republishing the same tree is caught
    earlier (nothing to push), so landing here at all means something changed
    without the version doing the same."""
    mod = _load()
    assert mod.main(["--new", "0.37.0", "--parent", "0.37.0"]) == 1


def test_allow_regression_skips_the_check_even_when_older(capsys):
    mod = _load()
    assert mod.main(["--new", "0.36.2", "--parent", "0.37.0", "--allow-regression"]) == 0
    assert "allow-regression" in capsys.readouterr().out


def test_parse_version_rejects_a_non_dotted_integer_version():
    mod = _load()
    with pytest.raises(SystemExit):
        mod.parse_version("not-a-version")


@pytest.mark.parametrize("v,expected", [
    ("0.38.0", (0, 38, 0)),
    ("1.2.3", (1, 2, 3)),
    ("10.0.1", (10, 0, 1)),
])
def test_parse_version_parses_each_dotted_part_as_an_integer(v, expected):
    mod = _load()
    assert mod.parse_version(v) == expected


def test_parse_version_compares_as_integers_not_strings():
    """10.0.1 must sort after 2.0.0 -- a string compare ("10" < "2") would get
    this backwards."""
    mod = _load()
    assert mod.parse_version("10.0.1") > mod.parse_version("2.0.0")
