"""Tests for post-tool-hook.sh fork throttling (issue #125).

The hook forks save-session.sh when the JSONL delta exceeds
``delta_lines_trigger``. Before the fix it throttled only on *save position*
(last-save.json), which is written solely after a successful save — so until
the first save lands, LAST_LINE stays 0, DELTA is the whole transcript, and the
hook forks a save-session.sh on *every* tool call for the life of an agentic
session (fork storm). save-session.sh itself already rate-limits on the
``last-save-ts`` cooldown marker, discarding each of those forks in
milliseconds.

The fix consults that same cooldown marker hook-side, so a fork is not spawned
while save-session.sh would only discard it. The parent writes save-session.pid
synchronously before backgrounding, so its presence is a reliable "a save was
forked" signal.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"


from pipeline.slug import session_dir_slug as _slug  # noqa: E402  (#177)


def _run_post_tool(tmp_path: Path, *, cooldown_ts: Optional[int], jsonl_lines: int = 60,
                   write_config: bool = True):
    """Set up a project + session JSONL and run the post-tool hook once.

    Args:
        cooldown_ts: value to write into tmp/last-save-ts, or None to leave the
            marker absent.
        jsonl_lines: number of lines in the fake session transcript. Must exceed
            the delta gate; the fixture writes an explicit
            thresholds.delta_lines_trigger rather than leaning on the shipped
            default, which it used to do silently — RAISING that default failed
            these tests loudly, but LOWERING it changed nothing and nobody would
            have known (#177).

    Returns:
        (CompletedProcess, remember_dir Path).
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)

    # A transcript with no human turns: enough lines to clear the delta gate,
    # and save-session.sh (if forked) exits fast on the min-human gate.
    session = session_dir / "sess-1.jsonl"
    session.write_text(
        "".join('{"type":"assistant","message":{"content":"x"}}\n'
                for _ in range(jsonl_lines))
    )

    # Say what this test depends on instead of inheriting it — except where the
    # point IS the inheritance: with write_config=False the hook has to fall
    # back to its own built-in default, which is the branch that stopped being
    # exercised when this fixture started writing a config unconditionally.
    if write_config:
        (remember / "config.json").write_text(
            json.dumps({"thresholds": {"delta_lines_trigger": 50}}), encoding="utf-8"
        )

    if cooldown_ts is not None:
        (remember / "tmp" / "last-save-ts").write_text(str(cooldown_ts))

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        # Prevent lib-memory-dir.sh from overriding the REMEMBER_DIR we set.
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    proc = subprocess.run(["bash", str(HOOK)], env=env,
                          capture_output=True, text=True, timeout=60)
    return proc, remember


def _reap(remember: Path):
    """Wait for any forked save-session.sh to finish so no stray process leaks."""
    pid_file = remember / "tmp" / "save-session.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


def test_the_builtin_delta_default_applies_with_no_config(tmp_path):
    """No config.json at all — the hook's own fallback has to decide.

    Every other test here writes the threshold it depends on, which is right,
    but it left nothing running the `config ".thresholds.delta_lines_trigger"
    50` fallback. A transcript of 60 lines is over that default and one of 40
    is under it, so the two cases together pin the number the hook ships with
    rather than the number a fixture supplied.
    """
    proc, remember = _run_post_tool(
        tmp_path, cooldown_ts=None, jsonl_lines=60, write_config=False
    )
    _reap(remember)
    assert proc.returncode == 0
    assert (remember / "tmp" / "save-session.pid").exists(), (
        "60 lines is over the shipped delta default, but no save was forked"
    )


def test_the_builtin_delta_default_holds_a_small_delta_back(tmp_path):
    """The other side of the same default: under it, nothing forks."""
    proc, remember = _run_post_tool(
        tmp_path, cooldown_ts=None, jsonl_lines=40, write_config=False
    )
    _reap(remember)
    assert proc.returncode == 0
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "40 lines is under the shipped delta default, yet a save was forked — "
        "the default has moved and nothing else would have said so"
    )


def test_fork_suppressed_during_cooldown(tmp_path):
    """A fresh cooldown marker must suppress the fork — save-session.sh would
    only discard it, so the hook should not spawn it at all (issue #125)."""
    proc, remember = _run_post_tool(tmp_path, cooldown_ts=int(time.time()))
    _reap(remember)
    pid_file = remember / "tmp" / "save-session.pid"
    assert proc.returncode == 0
    assert not pid_file.exists(), (
        "hook forked save-session.sh despite being inside the save cooldown"
    )


def test_fork_fires_when_no_cooldown_marker(tmp_path):
    """With no cooldown marker (nothing saved yet, cooldown elapsed), a delta
    over the threshold still forks a save — the normal path is preserved."""
    proc, remember = _run_post_tool(tmp_path, cooldown_ts=None)
    _reap(remember)
    pid_file = remember / "tmp" / "save-session.pid"
    assert proc.returncode == 0
    assert pid_file.exists(), "hook failed to fork a save when eligible"
