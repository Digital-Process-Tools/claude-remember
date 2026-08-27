"""Tests for session-end-hook.sh — the SessionEnd flush (#345).

`hooks/hooks.json` registered `SessionStart`, `UserPromptSubmit` and
`PostToolUse` but nothing on the way out, so whatever happened after the last
PostToolUse save that never cleared `cooldowns.save_seconds` or
`thresholds.min_human_messages` was simply never written. This hook calls
`save-session.sh --force` once, at session end, which bypasses exactly those
two gates -- the ones that exist to throttle a *live* session, not its last
moment.

These tests drive the real script with the same stubbed `pipeline.shell` the
save-session.sh gate tests use (test_save_session_gates.py), so the flush is
exercised without a Haiku call. Every "must fire" case here is paired with a
"must not fire" control in the same fixture, per this repo's own testing
rule: an assertion that a save was skipped also passes when nothing ran at
all, so each skip is paired with a case that proves the harness is live.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from subprocess_helpers import subprocess_failure_detail
from test_save_session_gates import _make_env  # shared harness

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_NAME = "session-end-hook.sh"


def _wire_hook(plugin: Path) -> Path:
    """Copy the real hook into the fake plugin _make_env built.

    _make_env copies only the scripts save-session.sh itself needs; this
    hook is a new caller of that same harness and is not on that list.
    """
    dst = plugin / "scripts" / HOOK_NAME
    shutil.copyfile(REPO_ROOT / "scripts" / HOOK_NAME, dst)
    dst.chmod(0o755)
    return dst


def _reap(remember: Path, timeout: float = 30):
    """Wait for the hook's backgrounded flush to finish (#345).

    The hook now forks save-session.sh into a subshell and returns
    immediately (to avoid Claude Code's own ~60s hook-kill) -- the same
    tmp/save-session.pid marker post-tool-hook.sh's own background fork
    writes (see tests/test_post_tool_cooldown.py::_reap, the same shape).
    Without this, calls.log may not have been written yet when a test reads
    it right after subprocess.run() returns.
    """
    pid_file = remember / "tmp" / "save-session.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)


def _run_hook(plugin: Path, env: dict, *, session_id, reason: str = "other",
              no_stdin: bool = False):
    hook = plugin / "scripts" / HOOK_NAME
    stdin_kw = {}
    if no_stdin:
        stdin_kw["stdin"] = subprocess.DEVNULL
    else:
        body = {"reason": reason}
        if session_id is not None:
            body["session_id"] = session_id
        stdin_kw["input"] = json.dumps(body)
    result = subprocess.run(
        ["bash", str(hook)], env=env, capture_output=True, text=True, timeout=60,
        check=False, **stdin_kw,
    )
    _reap(Path(env["CLAUDE_PROJECT_DIR"]) / ".remember")
    return result


class TestFlushIgnoresCooldownAndMinHumanGate:
    """The whole point (#345): a save-session.sh call with NO --force would
    skip here on either gate. session-end-hook.sh must proceed anyway."""

    def test_must_fire_hook_flushes_despite_fresh_cooldown_and_low_human_count(self, tmp_path):
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        _wire_hook(plugin)
        # _make_env's shipped fixture config sets cooldowns.save_seconds=0 —
        # restore a real window (matching the paired control below) so this
        # marker actually gates a plain call, and only --force gets past it.
        #
        # `env["REMEMBER_CONFIG"]` is NOT the file to edit: lib-memory-dir.sh
        # regenerates it fresh, per-PID, at `$TMPDIR/remember-config-$$.json`
        # on every invocation, by merging PIPELINE_DIR/config.json (the
        # plugin-bundled layer _make_env also wrote) with REMEMBER_DIR's own
        # config.json (absent here). The plugin-bundled layer is the one that
        # sticks.
        _cfg_layer = plugin / "config.json"
        cfg = json.loads(_cfg_layer.read_text())
        cfg["cooldowns"]["save_seconds"] = 120
        _cfg_layer.write_text(json.dumps(cfg))
        # A cooldown marker stamped "now" — inside the window save-session.sh
        # would normally throttle on.
        (project / ".remember" / "tmp" / "last-save-ts").write_text(str(int(time.time())))

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        logged_calls = calls.read_text()
        assert "extract" in logged_calls, (
            "the hook did not even attempt an extraction — --force should bypass "
            "both the cooldown and the min-human gate\n" + logged_calls
        )
        assert "call-haiku" in logged_calls, (
            "extraction ran but summarization never fired — the min-human gate "
            "(humans=1) must not block a --force flush\n" + logged_calls
        )

    def test_must_not_fire_control_same_marker_blocks_a_plain_save_session_call(self, tmp_path):
        """Positive control: without --force, this exact fixture DOES skip.

        Proves the fixture's cooldown marker is actually load-bearing, not a
        broken harness that would pass either way.
        """
        from test_save_session_gates import _run
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=4, humans=1)
        # Same override as the paired --force test above, and the same reason
        # (see its comment): the plugin-bundled config layer is what sticks
        # through lib-memory-dir.sh's per-PID merge.
        _cfg_layer = plugin / "config.json"
        cfg = json.loads(_cfg_layer.read_text())
        cfg["cooldowns"]["save_seconds"] = 120
        _cfg_layer.write_text(json.dumps(cfg))
        (project / ".remember" / "tmp" / "last-save-ts").write_text(str(int(time.time())))

        result = _run(plugin, env, sid)  # no --force

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        # A cooldown skip exits before `python -m pipeline.shell` is ever
        # invoked, so calls.log may not exist at all -- read_text() would
        # raise on that, which is not the failure this assertion is for.
        logged_calls = calls.read_text() if calls.exists() else ""
        assert "extract" not in logged_calls, (
            "save-session.sh proceeded despite a fresh cooldown marker with no "
            "--force — the fixture does not actually exercise the cooldown gate, "
            "so the paired --force test above proves nothing"
        )


class TestZeroExchangesCostsNoHaikuCall:
    """Paired with the must-fire case above: --force still must not summon
    Haiku when there is genuinely nothing new to summarize."""

    def test_nothing_to_flush_advances_position_without_a_haiku_call(self, tmp_path):
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=0, humans=0)
        _wire_hook(plugin)

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        logged_calls = calls.read_text()
        assert "extract" in logged_calls, "the hook must still attempt an extraction"
        assert "call-haiku" not in logged_calls, (
            "0 exchanges means nothing to summarize — a --force flush must not "
            "cost a Haiku call for an empty span\n" + logged_calls
        )


class TestFailSoftContract:
    """Matches the other hooks in this plugin: EXIT CODES: 0, always."""

    def test_empty_stdin_still_exits_zero(self, tmp_path):
        env, project, plugin, _calls, _sid = _make_env(tmp_path, exchanges=2, humans=1)
        _wire_hook(plugin)

        result = _run_hook(plugin, env, session_id=None, no_stdin=True)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

    def test_no_project_at_all_still_exits_zero(self, tmp_path):
        """No CLAUDE_PROJECT_DIR, no CLAUDE_PLUGIN_ROOT, no REMEMBER_DIR: the
        resolve-paths soft-fail path post-tool-hook.sh already relies on.
        There is no .remember directory anywhere in this scenario — the
        missing-data-dir case the acceptance criteria name."""
        plugin_scripts = tmp_path / "plugin" / "scripts"
        plugin_scripts.mkdir(parents=True)
        for script in ("lib-clock.sh", "resolve-paths.sh", "detect-tools.sh",
                       "bootstrap-dirs.sh", "log.sh", "lib-memory-dir.sh"):
            shutil.copyfile(REPO_ROOT / "scripts" / script, plugin_scripts / script)
        hook = plugin_scripts / HOOK_NAME
        shutil.copyfile(REPO_ROOT / "scripts" / HOOK_NAME, hook)
        hook.chmod(0o755)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "REMEMBER_DIR",
                            "_LIB_MEMORY_DIR_LOADED")}

        result = subprocess.run(
            ["bash", str(hook)], env=env, capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL, check=False,
        )
        assert result.returncode == 0, (
            f"rc={result.returncode} — a SessionEnd hook that fails must not be "
            f"able to disrupt teardown. stderr={result.stderr[:300]}"
        )

    def test_missing_remember_dir_on_a_real_project_still_exits_zero(self, tmp_path):
        """A project directory exists, but the plugin has never touched it —
        no .remember, no session transcript. bootstrap-dirs.sh will create
        the store on demand; the hook must survive the case where nothing
        was there before it ran."""
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=1, humans=1)
        _wire_hook(plugin)
        shutil.rmtree(project / ".remember")

        result = _run_hook(plugin, env, session_id=sid)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        # Positive control for test_must_fire_unwritable_store_reports_a_warning
        # below (#372): bootstrap-dirs.sh's mkdir succeeds here (the project
        # dir is fully writable), so REMEMBER_DIR is recreated and nothing
        # went wrong -- no WARNING belongs on stderr. Without this half, an
        # assertion that the warning fires elsewhere would be unfalsifiable:
        # a broken harness that always prints WARNING would pass that test
        # too.
        assert "WARNING" not in result.stderr, (
            "the store was recreated successfully (mkdir is writable here) -- "
            "nothing failed, so nothing should be reported\n" + result.stderr
        )

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root ignores the read-only mode bits this test depends on "
               "(same guard as tests/test_bootstrap_readonly_root.py and "
               "friends) -- mkdir would succeed anyway, and the fixture "
               "assertion below would fail for an unrelated reason",
    )
    def test_must_fire_unwritable_store_reports_a_warning(self, tmp_path):
        """#372: when the store genuinely cannot be created -- not merely
        missing, but its parent is unwritable so bootstrap-dirs.sh's own
        mkdir also fails -- log.sh returns before defining log() or
        report_error() at all. Reaching line 158 requires exactly this: the
        hook's own docstring (EXIT CODES) promises the failure is "reported
        loudly ... rather than swallowed silently", and a `command not found`
        does not change the exit status, so returncode alone cannot tell a
        real report from total silence.
        """
        env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=1, humans=1)
        _wire_hook(plugin)
        shutil.rmtree(project / ".remember")
        os.chmod(project, 0o555)
        try:
            result = _run_hook(plugin, env, session_id=sid)
        finally:
            # Restore before any fixture cleanup (tmp_path teardown) tries to
            # remove a now-read-only directory.
            os.chmod(project, 0o755)

        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert not (project / ".remember").exists(), (
            "the fixture must actually fail to create the store, or this test "
            "proves nothing about the degraded path"
        )
        assert "command not found" not in result.stderr, (
            "report_error (or log) was called while genuinely undefined -- "
            "the declare -F guard is missing or bypassed\n" + result.stderr
        )
        assert "WARNING" in result.stderr, (
            "a store that could never be created must be reported, not "
            "swallowed into the same silent no-op as a session with nothing "
            "new to flush\n" + result.stderr
        )
