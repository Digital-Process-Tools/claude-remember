"""COOLDOWN_MARKER and NDC_MARKER collapse absent-vs-unreadable, same as #619 (#625).

#619 fixed this exact shape at NDC_GEN_FILE: `cat FILE 2>/dev/null || echo 0`
cannot tell "never created" (legitimately 0) from "exists but a read of it
failed" (permission or I/O error, or a directory in its place) -- both
produce the identical fallback 0. save-session.sh's two OTHER timestamp
markers, tmp/last-save-ts (COOLDOWN_MARKER) and tmp/last-ndc.ts (NDC_MARKER),
read with the same bare `cat ... || echo 0` and have the identical collapse.

Lower stakes than the NDC_GEN_FILE case -- an unreadable marker here reads as
"very old", so the cooldown it guards is bypassed (an extra Haiku call), not a
silently lost entry -- but the same defect class: a durably unreadable marker
now silently defeats its cooldown on EVERY invocation, forever, with no trace
in the log. The fix must make that state visible (a WARNING naming the marker
and "could not be read"), the same way #619 made NDC_GEN_FILE's failure mode
visible via the new "SKIPPED commit" log line.

A directory in place of the marker reproduces "exists but every read fails"
without depending on this process's uid ever being denied a permission bit --
a bit CI's usual root uid ignores outright. Same technique #619's own test
suite used for NDC_GEN_FILE (tests/test_ndc_commit_lock.py).
"""

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

from .subprocess_helpers import subprocess_failure_detail
from .test_ndc_truncate_race import _ndc_env, _wait_for_background_ndc
from .test_save_session_gates import _make_env, _memory_log_text, _run

UNREADABLE_PHRASE = "could not be read"


def _hook_errors_text(project: Path) -> str:
    log = project / ".remember" / "logs" / "hook-errors.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""


class TestCooldownMarkerUnreadable:

    def test_save_proceeds_and_warns_when_cooldown_marker_is_unreadable(self, tmp_path):
        """A directory in place of last-save-ts: every `cat` of it fails.

        Before the fix this silently proceeds with no trace at all -- the
        exact same silence #619 closed at NDC_GEN_FILE. After the fix it must
        still proceed (fail-open is the correct call here, see module
        docstring) but must say so.
        """
        env, project, plugin, calls, session_id = _make_env(
            tmp_path, exchanges=40, humans=5)
        cfg_path = Path(env["REMEMBER_CONFIG"])
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["save_seconds"] = 3600
        for target in (cfg_path, plugin / "config.json"):
            target.write_text(json.dumps(cfg))
        marker = project / ".remember" / "tmp" / "last-save-ts"
        marker.mkdir()  # exists, but `cat` on a directory always fails

        proc = _run(plugin, env, session_id)

        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")
        ran = calls.read_text() if calls.is_file() else ""
        assert "extract" in ran, (
            "an unreadable cooldown marker must fail OPEN (proceed with the "
            f"save), not stay silently throttled forever. calls: {ran!r}"
        )
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert str(marker) in seen and UNREADABLE_PHRASE in seen, (
            "a cooldown marker that exists but cannot be read must be logged "
            "as such -- it is not the same fact as a marker that was never "
            f"created, and the silence must not repeat #619's failure mode.\n{seen}"
        )

    def test_save_proceeds_quietly_when_cooldown_marker_is_absent(self, tmp_path):
        """Positive control: no marker has ever been written -- 0 is legitimate.

        Paired with the unreadable-marker test above -- both must let the save
        proceed, but only the unreadable one is a fact worth a WARNING.
        """
        env, project, plugin, calls, session_id = _make_env(
            tmp_path, exchanges=40, humans=5)
        cfg_path = Path(env["REMEMBER_CONFIG"])
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["save_seconds"] = 3600
        for target in (cfg_path, plugin / "config.json"):
            target.write_text(json.dumps(cfg))
        marker = project / ".remember" / "tmp" / "last-save-ts"
        assert not marker.exists(), "setup must start from a marker that was never created"

        proc = _run(plugin, env, session_id)

        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")
        ran = calls.read_text() if calls.is_file() else ""
        assert "extract" in ran, f"a never-created marker must not throttle the save. calls: {ran!r}"
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert UNREADABLE_PHRASE not in seen, (
            "an absent marker is not a read failure; it must not be reported as one"
        )


class TestNdcMarkerUnreadable:

    def test_ndc_proceeds_and_warns_when_marker_is_unreadable(self, tmp_path):
        """Same collapse, at tmp/last-ndc.ts (NDC_MARKER)."""
        env, project, plugin, _calls, sid = _ndc_env(tmp_path)
        env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- an entry\n"
        cfg_path = Path(env["REMEMBER_CONFIG"])
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["ndc_seconds"] = 3600
        for target in (cfg_path, plugin / "config.json"):
            target.write_text(json.dumps(cfg))
        memory_file = project / ".remember" / "now.md"
        marker = project / ".remember" / "tmp" / "last-ndc.ts"
        marker.mkdir()  # exists, but `cat` on a directory always fails

        proc = _run(plugin, env, sid)
        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")

        _wait_for_background_ndc(memory_file)
        today_text = "".join(
            f.read_text() for f in (project / ".remember").glob("today-*.md")
        )
        assert "compressed summary" in today_text, (
            "an unreadable NDC marker must fail OPEN (compress now), not stay "
            "silently throttled forever"
        )
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert str(marker) in seen and UNREADABLE_PHRASE in seen, (
            "an NDC marker that exists but cannot be read must be logged as "
            f"such, distinct from one that was never created.\n{seen}"
        )

    def test_ndc_proceeds_quietly_when_marker_is_absent(self, tmp_path):
        """Positive control for the NDC marker."""
        env, project, plugin, _calls, sid = _ndc_env(tmp_path)
        env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- an entry\n"
        cfg_path = Path(env["REMEMBER_CONFIG"])
        cfg = json.loads(cfg_path.read_text())
        cfg["cooldowns"]["ndc_seconds"] = 3600
        for target in (cfg_path, plugin / "config.json"):
            target.write_text(json.dumps(cfg))
        memory_file = project / ".remember" / "now.md"
        marker = project / ".remember" / "tmp" / "last-ndc.ts"
        assert not marker.exists(), "setup must start from a marker that was never created"

        proc = _run(plugin, env, sid)
        assert proc.returncode == 0, subprocess_failure_detail(
            proc, project / ".remember")

        _wait_for_background_ndc(memory_file)
        today_text = "".join(
            f.read_text() for f in (project / ".remember").glob("today-*.md")
        )
        assert "compressed summary" in today_text, "a never-created marker must not throttle NDC"
        seen = _memory_log_text(project) + _hook_errors_text(project)
        assert UNREADABLE_PHRASE not in seen, (
            "an absent marker is not a read failure; it must not be reported as one"
        )
