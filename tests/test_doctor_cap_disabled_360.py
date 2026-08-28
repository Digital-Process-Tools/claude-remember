"""thresholds.consolidate_max_bytes: 0 must read as *disabled*, not 0 bytes (#360).

``pipeline/shell.py`` documents and implements ``0`` as the cap being off --
``max_prompt_bytes: ... "0" disables it`` (:452), enforced by
``if max_prompt_bytes > 0:`` (:542). The store-size check doctor.sh added in
#348 reads the same key with a digits-only guard, and ``0`` is all digits, so
it became a literal 0-byte cap: every non-empty store then failed against it,
even though the pipeline itself consolidates that store on every round.
"""

from __future__ import annotations

import sys

import pytest

from tests.test_doctor_oversized_store_348 import _fill, _project, _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)


def _verdict(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("VERDICT:"):
            return line
    raise AssertionError("no VERDICT line in output:\n" + stdout)


def test_a_zero_cap_is_reported_as_disabled_not_as_a_zero_byte_limit(tmp_path):
    """A store that is perfectly ordinary must not become the loudest FAIL.

    Before the fix, ``consolidate_max_bytes: 0`` parsed as a literal 0-byte
    cap, so a 4000-byte recent.md -- utterly healthy -- failed every later
    comparison and reached the ``_STORE_NEEDS_A_HUMAN`` verdict arm, the same
    "cannot heal itself" alarm #348 added for the one store shape rotation
    truly cannot fix.
    """
    home, project, remember = _project(tmp_path)
    cfg = tmp_path / "merged-config.json"
    cfg.write_text('{"thresholds": {"consolidate_max_bytes": 0}}', encoding="utf-8")
    _fill(remember / "recent.md", 4000)

    result = _run(home, project, remember, {"REMEMBER_CONFIG": str(cfg)})

    assert result.returncode == 0, result.stderr
    assert "too large to consolidate" not in result.stdout.lower(), (
        "a disabled cap (0) was read as a literal 0-byte cap and invented a "
        "false alarm on a healthy store:\n" + result.stdout
    )
    assert "cannot heal itself" not in result.stdout.lower(), (
        "a disabled cap reached the arm reserved for the one store shape "
        "nothing in the pipeline will clear on its own:\n" + result.stdout
    )
    assert "over the prompt cap" not in _verdict(result.stdout), (
        "a disabled consolidation cap reached the verdict as though staging "
        "were over a real cap:\n" + result.stdout
    )
    assert "disabled" in result.stdout.lower(), (
        "the report never says the cap is disabled -- silently skipping the "
        "check renders identically to a check that passed:\n" + result.stdout
    )


def test_a_configured_nonzero_cap_still_fires(tmp_path):
    """Positive control: 0 is special-cased, not every small number.

    Without this, a fix that special-cased "any cap at or under the store
    size" -- rather than specifically 0 -- would pass the test above and
    silently swallow every genuinely small configured cap too.
    """
    home, project, remember = _project(tmp_path)
    cfg = tmp_path / "merged-config.json"
    cfg.write_text('{"thresholds": {"consolidate_max_bytes": 100}}', encoding="utf-8")
    _fill(remember / "recent.md", 4000)

    result = _run(home, project, remember, {"REMEMBER_CONFIG": str(cfg)})

    assert result.returncode == 0, result.stderr
    assert "too large to consolidate" in result.stdout.lower(), (
        "a genuinely small nonzero configured cap did not fire -- the "
        "disabled-cap special case may be swallowing ordinary caps too:\n"
        + result.stdout
    )
    assert "100" in result.stdout, (
        "the configured cap itself never reached the report:\n" + result.stdout
    )


def test_the_built_in_default_is_unaffected_by_the_disabled_case(tmp_path):
    """No config at all must still use the 600000 default, not read as disabled.

    ``_CONSOLIDATE_MAX_BYTES`` defaults to 600000 before any config is read;
    this pins that the disabled-cap branch only fires when config genuinely
    says 0, not whenever the grep comes back empty.
    """
    home, project, remember = _project(tmp_path)
    _fill(remember / "recent.md", 4000)

    result = _run(home, project, remember)

    assert "disabled" not in result.stdout.lower(), (
        "the built-in default (no config) was reported as a disabled cap:\n"
        + result.stdout
    )
    assert "600000" in result.stdout, (
        "the built-in default cap is not in the report:\n" + result.stdout
    )
