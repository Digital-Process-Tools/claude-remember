"""thresholds.handoff_max_redeliveries: a handoff delivered N times in full
must stop being re-injected verbatim and be listed by path instead (#842).

remember.delivered already tracks how many times the SAME handoff content has
been shown (see tests/test_handoff_preservation.py). Before this fix the count
only ever changed the banner text ("already delivered N times") -- the full
content was cat'd into context again on every single start, forever, until
/remember replaced it. That is wasted budget on content nothing has changed
about, and it is one of the three things #842 asks a total SessionStart
budget to be able to afford NOT doing.

The bar, per test: would this test still pass if the code did nothing? Each
negative ("must not re-inject") is paired with its positive control ("must
still inject below the threshold").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX session-start hook -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START_SCRIPT = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug


def _sandbox(tmp_path: Path, *, handoff_max_redeliveries=None):
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)
    cfg = {"data_dir": ".remember", "features": {"recovery": False}}
    if handoff_max_redeliveries is not None:
        cfg["thresholds"] = {"handoff_max_redeliveries": handoff_max_redeliveries}
    (home / ".remember" / "config.json").write_text(json.dumps(cfg))
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    handoff = project / ".remember" / "remember.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    return project, home, handoff


def _session_start(project: Path, home: Path) -> str:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
    }
    result = subprocess.run(
        ["bash", str(SESSION_START_SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"hook exited {result.returncode}: {result.stderr[:500]}"
    return result.stdout


def _daily_log_text(project: Path) -> str:
    log_dir = project / ".remember" / "logs"
    if not log_dir.is_dir():
        return ""
    return "".join(p.read_text(encoding="utf-8", errors="replace") for p in log_dir.glob("*.log"))


class TestBelowTheThresholdStillInjectsInFull:
    """Positive control: a handoff delivered fewer times than the cap must
    keep reading exactly as it always has."""

    def test_default_threshold_allows_three_full_deliveries(self, tmp_path):
        project, home, handoff = _sandbox(tmp_path)
        handoff.write_text("HANDOFF-BELOW-CAP-842: land the parser fix.\n")

        for _ in range(3):
            out = _session_start(project, home)
            assert "HANDOFF-BELOW-CAP-842" in out, (
                f"a delivery within the default cap (3) dropped the body.\noutput: {out[:800]}"
            )
            assert "not re-injected" not in out.lower()


class TestAtOrOverTheThresholdListsByPath:

    def test_fourth_delivery_under_default_cap_is_listed_by_path_not_content(self, tmp_path):
        """Default handoff_max_redeliveries is 3: the 4th delivery of the SAME
        content must stop injecting the body and list the file instead."""
        project, home, handoff = _sandbox(tmp_path)
        handoff.write_text("HANDOFF-OVER-CAP-842: land the parser fix.\n")

        for _ in range(3):
            _session_start(project, home)
        fourth = _session_start(project, home)

        assert "HANDOFF-OVER-CAP-842" not in fourth, (
            f"the 4th delivery of unchanged content was still injected in full.\noutput: {fourth[:800]}"
        )
        assert str(handoff) in fourth, (
            f"a withheld handoff must be listed by path (#842's own convention).\noutput: {fourth[:800]}"
        )
        assert "handoff_max_redeliveries" in fourth, (
            f"the withholding notice should name the config key responsible.\noutput: {fourth[:800]}"
        )
        assert "=== LAST HANDOFF ===" in fourth, "the section header itself must still appear"

    def test_delivery_count_still_advances_while_capped(self, tmp_path):
        """The record is not frozen just because content stops being shown --
        a later redelivery must still say how long it has been pending."""
        project, home, handoff = _sandbox(tmp_path)
        handoff.write_text("HANDOFF-COUNT-842: land the parser fix.\n")

        for _ in range(3):
            _session_start(project, home)
        fourth = _session_start(project, home)
        fifth = _session_start(project, home)

        assert "4 times" in fourth, f"4th delivery should report count 4.\noutput: {fourth[:800]}"
        assert "5 times" in fifth, f"5th delivery should report count 5.\noutput: {fifth[:800]}"

    def test_a_fresh_handoff_resets_and_is_injected_again(self, tmp_path):
        """Positive control for the reset path: replacing the handoff (the
        normal /remember flow) must bring back full injection immediately,
        even though the cap had already engaged for the old content."""
        project, home, handoff = _sandbox(tmp_path)
        handoff.write_text("HANDOFF-STALE-842: land the parser fix.\n")
        for _ in range(4):
            _session_start(project, home)

        handoff.write_text("HANDOFF-FRESH-842: review the parser fix.\n")
        out = _session_start(project, home)

        assert "HANDOFF-FRESH-842" in out, (
            f"a replaced handoff must be injected in full even though the "
            f"OLD content had already hit the redelivery cap.\noutput: {out[:800]}"
        )
        assert "not re-injected" not in out.lower()


class TestZeroDisablesTheCap:
    """Same '0 is a deliberate no-cap' convention every other threshold in
    this codebase uses (consolidate_max_bytes, memory_inject_max_bytes, ...)."""

    def test_zero_never_withholds_regardless_of_delivery_count(self, tmp_path):
        project, home, handoff = _sandbox(tmp_path, handoff_max_redeliveries=0)
        handoff.write_text("HANDOFF-NOCAP-842: land the parser fix.\n")

        for _ in range(6):
            out = _session_start(project, home)

        assert "HANDOFF-NOCAP-842" in out, (
            f"handoff_max_redeliveries=0 must disable the cap entirely.\noutput: {out[:800]}"
        )


class TestConfiguredThreshold:

    def test_a_lower_configured_threshold_is_honoured(self, tmp_path):
        project, home, handoff = _sandbox(tmp_path, handoff_max_redeliveries=1)
        handoff.write_text("HANDOFF-LOWCAP-842: land the parser fix.\n")

        first = _session_start(project, home)
        assert "HANDOFF-LOWCAP-842" in first, "the very first delivery must never be withheld"

        second = _session_start(project, home)
        assert "HANDOFF-LOWCAP-842" not in second, (
            f"a threshold of 1 should withhold starting at the 2nd delivery.\noutput: {second[:800]}"
        )
        assert str(handoff) in second


class TestMalformedValueIsLoggedAndFallsBackToDefault:
    """Same convention #834 established for thresholds.consolidate_max_bytes:
    a typo'd value is logged, not silently swapped for the default."""

    def test_malformed_value_falls_back_to_three_and_is_logged(self, tmp_path):
        project, home, handoff = _sandbox(tmp_path, handoff_max_redeliveries="3O")
        handoff.write_text("HANDOFF-MALFORMED-842: land the parser fix.\n")

        for _ in range(3):
            out = _session_start(project, home)
            assert "HANDOFF-MALFORMED-842" in out, (
                "a malformed override should fall back to the default (3), "
                f"which still allows this delivery.\noutput: {out[:800]}"
            )
        fourth = _session_start(project, home)
        assert "HANDOFF-MALFORMED-842" not in fourth, (
            f"the fallback default (3) should have engaged the cap by the 4th delivery.\noutput: {fourth[:800]}"
        )

        logs = _daily_log_text(project)
        assert "handoff_max_redeliveries" in logs, (
            f"a malformed override must be logged, naming the key: {logs!r}"
        )
        assert "3O" in logs, f"the log should name the bad value that was discarded: {logs!r}"

    def test_a_valid_override_is_not_logged_as_malformed(self, tmp_path):
        """Positive control."""
        project, home, handoff = _sandbox(tmp_path, handoff_max_redeliveries=2)
        handoff.write_text("HANDOFF-VALIDCFG-842: land the parser fix.\n")

        _session_start(project, home)
        _session_start(project, home)

        logs = _daily_log_text(project)
        assert "handoff_max_redeliveries" not in logs, (
            f"a valid configured override was reported as malformed: {logs!r}"
        )
