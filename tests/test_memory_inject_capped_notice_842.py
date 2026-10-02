"""#842 request 4: when memory_inject_max_bytes is configured BELOW the
bundled 200000 default, the over-cap notice must say "capped by config" and
must not suggest /remember:doctor.

Before this fix the notice was worded identically whether the cap was the
bundled default (a store that really is broken -- consolidation wrote an
unbounded response, see #346) or a cap an operator deliberately lowered on
purpose (e.g. to stay under #842's own session_start_max_bytes). Both cases
produced "run /remember:doctor", which is actionable advice for the first
case and noise for the second.
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
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug


def _store(tmp_path, *, memory_inject_max_bytes, archive_bytes):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    (remember / "identity.md").write_text("IDENTITY-842\n", encoding="utf-8")
    (remember / "archive.md").write_text("A" * archive_bytes, encoding="utf-8")
    config = {
        "thresholds": {
            "memory_inject_max_bytes": memory_inject_max_bytes,
            # Isolate this test from #842's OWN total budget (request 1) --
            # this file is about request 4's wording only.
            "session_start_max_bytes": 0,
        }
    }
    (remember / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return home, project, remember


def _run(tmp_path, *, memory_inject_max_bytes, archive_bytes):
    home, project, remember = _store(
        tmp_path, memory_inject_max_bytes=memory_inject_max_bytes, archive_bytes=archive_bytes
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_CONFIG_CACHE": "0",
    }
    payload = json.dumps({
        "session_id": "bbbbbbbb-0000-4000-8000-000000000842",
        "hook_event_name": "SessionStart",
        "source": "startup",
    })
    result = subprocess.run(
        ["bash", str(SESSION_START)], env=env, input=payload,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, remember


class TestCapBelowBundledDefaultIsWordedAsConfig:

    def test_lowered_cap_says_capped_by_config(self, tmp_path):
        out, _ = _run(tmp_path, memory_inject_max_bytes=1000, archive_bytes=2000)
        assert "capped by config" in out.lower(), (
            f"a deliberately lowered cap should say so.\noutput tail: {out[-800:]}"
        )
        assert "thresholds.memory_inject_max_bytes=1000" in out, (
            f"the notice should name the configured value.\noutput tail: {out[-800:]}"
        )

    def test_lowered_cap_does_not_recommend_doctor(self, tmp_path):
        out, _ = _run(tmp_path, memory_inject_max_bytes=1000, archive_bytes=2000)
        assert "/remember:doctor" not in out, (
            f"a deliberately lowered cap is not a broken store -- /remember:doctor "
            f"has nothing to diagnose here.\noutput tail: {out[-800:]}"
        )


class TestCapAtOrAboveBundledDefaultKeepsTheBrokenStoreWording:
    """Positive control: the ORIGINAL wording (a genuinely oversized file
    under the bundled default) must be unchanged."""

    def test_default_cap_still_recommends_doctor(self, tmp_path):
        out, _ = _run(tmp_path, memory_inject_max_bytes=200000, archive_bytes=250000)
        assert "/remember:doctor" in out, (
            f"the bundled-default cap's own wording regressed.\noutput tail: {out[-800:]}"
        )
        assert "capped by config" not in out.lower(), (
            f"the bundled default is not a deliberately lowered cap.\noutput tail: {out[-800:]}"
        )

    def test_cap_above_the_bundled_default_also_keeps_the_broken_store_wording(self, tmp_path):
        out, _ = _run(tmp_path, memory_inject_max_bytes=300000, archive_bytes=350000)
        assert "/remember:doctor" in out, (
            f"a cap set ABOVE the bundled default is not a deliberately "
            f"lowered one either.\noutput tail: {out[-800:]}"
        )
        assert "capped by config" not in out.lower()


class TestLoweredCapDoesNotHideAGenuinelyMalformedFile:
    """Review of #845: a lowered cap explains a file OVER the cap but within
    the bundled default. It explains nothing about a file larger than the
    bundled default itself -- that is still the #346 shape (consolidation
    wrote an unbounded response), and calling it a deliberate cap would call
    a broken store healthy."""

    def test_file_above_the_bundled_default_keeps_doctor_advice_under_a_lowered_cap(self, tmp_path):
        out, _ = _run(tmp_path, memory_inject_max_bytes=50000, archive_bytes=250000)
        assert "/remember:doctor" in out, (
            f"a file past the bundled 200000 default is malformed whatever the "
            f"configured cap.\noutput tail: {out[-800:]}"
        )
        assert "not a sign of a malformed memory file" not in out, (
            f"a 250000-byte file was called healthy.\noutput tail: {out[-800:]}"
        )

    def test_file_between_lowered_cap_and_bundled_default_is_capped_by_config(self, tmp_path):
        """Positive control: same lowered cap, a file the cap alone explains."""
        out, _ = _run(tmp_path, memory_inject_max_bytes=50000, archive_bytes=100000)
        assert "capped by config" in out.lower(), f"output tail: {out[-800:]}"
        assert "/remember:doctor" not in out, f"output tail: {out[-800:]}"