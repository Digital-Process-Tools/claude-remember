"""The session tree moves when CLAUDE_CONFIG_DIR is set (issue #166).

Claude Code relocates its whole config tree — projects/ included — when
CLAUDE_CONFIG_DIR is set, which people use to run a separate account per
project. The plugin hardcoded ~/.claude, so it listed a directory with no
current transcripts and the entire save pipeline no-oped in silence: the
reporter had five days and ~2000 PostToolUse invocations with zero saves, an
empty logs/autonomous, and nothing in the logs suggesting a problem.

The quiet failure is not the worst case. A stale transcript left in the default
tree with more lines than delta_lines_trigger would have been summarized into
memory as though it were the live session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.extract import _session_dir  # noqa: E402


def test_session_dir_follows_claude_config_dir(monkeypatch, tmp_path):
    """The python side reads the relocated tree, not ~/.claude."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "alt"))

    resolved = _session_dir("/some/project")

    assert resolved.startswith(str(tmp_path / "alt")), resolved
    assert resolved.endswith("/projects/-some-project"), resolved


def test_session_dir_falls_back_to_home(monkeypatch, tmp_path):
    """Unset is the common case and must be untouched."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert _session_dir("/some/project") == f"{tmp_path / 'home'}/.claude/projects/-some-project"


def test_a_trailing_slash_does_not_double_up(monkeypatch, tmp_path):
    """direnv exports are hand-written; a trailing slash is ordinary."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "alt") + "/")

    assert "//" not in _session_dir("/some/project")


@pytest.mark.skipif(sys.platform == "win32", reason="bash hook — POSIX only (#79)")
def test_the_hook_finds_the_transcript_in_the_relocated_tree(tmp_path):
    """End to end: the reported scenario, where nothing ever saved.

    The transcript lives only under CLAUDE_CONFIG_DIR. Before the fix the hook
    listed ~/.claude/projects, found nothing, and exited 0 — which since #144
    at least logs a warning, and that warning is what must NOT appear here.
    """
    home = tmp_path / "home"
    alt = tmp_path / "alt-config"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (remember / "logs").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)

    slug = str(project).replace("/", "-").replace(".", "-").replace("_", "-")
    session_dir = alt / "projects" / slug
    session_dir.mkdir(parents=True)
    (session_dir / "sess-1.jsonl").write_text('{"type":"user"}\n' * 10, encoding="utf-8")

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(alt),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    result = subprocess.run(["bash", str(REPO_ROOT / "scripts" / "post-tool-hook.sh")],
                            env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    logs = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in (remember / "logs").glob("*.log"))
    assert "no session dir" not in logs, (
        f"the hook looked in the wrong tree and found no transcript:\n{logs}"
    )
