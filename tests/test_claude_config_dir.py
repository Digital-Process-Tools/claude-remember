"""Hooks must honour ``CLAUDE_CONFIG_DIR``, not hardcode ``$HOME/.claude``.

Claude Code supports relocating its whole config root via ``CLAUDE_CONFIG_DIR``
(e.g. a second account on one machine: ``CLAUDE_CONFIG_DIR=~/.claude-max``).
When set, session transcripts live under
``$CLAUDE_CONFIG_DIR/projects/<slug>/``, not ``$HOME/.claude/projects/<slug>/``.

Four sites hardcoded the default path outright: ``scripts/save-session.sh``,
``scripts/session-start-hook.sh``, ``scripts/post-tool-hook.sh``, and
``pipeline/extract.py:_session_dir``. Under a non-default
``CLAUDE_CONFIG_DIR`` every one of them looked in the *wrong account's*
``~/.claude/projects/`` — either finding nothing (silently no-op'ing memory
for the whole account), or, if both accounts happen to run in the same
project directory, reading and summarizing the *other* account's transcripts
into this one's memory.

An empty-string ``CLAUDE_CONFIG_DIR`` (unset-but-exported, or exported empty
by some shell init) must fall back to the default rather than producing a
path that starts with "/projects".
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestExtractSessionDirHonoursConfigDir:

    def test_uses_claude_config_dir_when_set(self, monkeypatch, tmp_path):
        # No importlib.reload: _session_dir reads os.environ at call time,
        # not at import time, so a plain import is enough — and reloading
        # pipeline.extract here would rebind its functions to new objects,
        # breaking the `is`-identity check in
        # test_position_store.py::test_the_two_modules_share_one_reader for
        # any test that runs afterward in the same session.
        sys.path.insert(0, str(REPO_ROOT))
        import pipeline.extract as extract_mod

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom-config"))
        result = extract_mod._session_dir("/Users/foo/myproject")
        assert result == str(tmp_path / "custom-config") + "/projects/-Users-foo-myproject"
        assert ".claude/projects" not in result

    def test_falls_back_to_home_claude_when_unset(self, monkeypatch, tmp_path):
        sys.path.insert(0, str(REPO_ROOT))
        import pipeline.extract as extract_mod

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        result = extract_mod._session_dir("/Users/foo/myproject")
        assert result == str(tmp_path / "home" / ".claude") + "/projects/-Users-foo-myproject"

    def test_empty_string_falls_back_not_projects_root(self, monkeypatch, tmp_path):
        """An empty (but exported) CLAUDE_CONFIG_DIR must not produce a path
        that starts with '/projects' — it must fall back like unset."""
        sys.path.insert(0, str(REPO_ROOT))
        import pipeline.extract as extract_mod

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
        result = extract_mod._session_dir("/Users/foo/myproject")
        assert not result.startswith("/projects")
        assert result == str(tmp_path / "home" / ".claude") + "/projects/-Users-foo-myproject"


def _slug(path: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


class TestShellHooksHonourConfigDir:
    """save-session.sh / post-tool-hook.sh / session-start-hook.sh must find
    the *right* account's transcript directory under CLAUDE_CONFIG_DIR."""

    def _project_and_home(self, tmp_path, config_dir_name="custom-config"):
        home = tmp_path / "home"
        project = tmp_path / "project"
        remember = project / ".remember"
        (remember / "tmp").mkdir(parents=True)
        (remember / "logs").mkdir(parents=True)
        config_dir = tmp_path / config_dir_name
        slug = _slug(str(project))
        session_dir = config_dir / "projects" / slug
        session_dir.mkdir(parents=True)
        # A decoy under the DEFAULT ~/.claude/projects/ — if a hook falls back
        # to the hardcoded default despite CLAUDE_CONFIG_DIR being set, it
        # will find this session instead of the real one below.
        decoy_dir = home / ".claude" / "projects" / slug
        decoy_dir.mkdir(parents=True)
        (decoy_dir / "decoy-session-0000.jsonl").write_text('{"type":"user"}\n')
        return home, project, remember, config_dir, session_dir

    def test_save_session_finds_transcript_under_config_dir(self, tmp_path):
        home, project, remember, config_dir, session_dir = self._project_and_home(tmp_path)
        real_session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        (session_dir / f"{real_session_id}.jsonl").write_text('{"type":"user"}\n' * 5)

        plugin = tmp_path / "plugin"
        (plugin / "scripts").mkdir(parents=True)
        (plugin / "pipeline").mkdir(parents=True)
        (plugin / "pipeline" / "__init__.py").write_text("")
        (plugin / "pipeline" / "haiku.py").write_text("# marker\n")
        # Auto-detect session-id path: script must pick the real session
        # (from CLAUDE_CONFIG_DIR/projects/), not the decoy under $HOME/.claude.
        stub_shell = '''\
import sys, os, tempfile
CALLS = os.environ["STUB_CALLS_LOG"]
with open(CALLS, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "extract":
    fd, path = tempfile.mkstemp(suffix="-extract")
    with os.fdopen(fd, "w") as f:
        f.write("Human: something\\nAssistant: something else\\n")
    print("POSITION=5")
    print("HUMAN_COUNT=5")
    print("ASSISTANT_COUNT=1")
    print("EXCHANGE_COUNT=6")
    print(f"EXTRACT_FILE={path}")
'''
        (plugin / "pipeline" / "shell.py").write_text(stub_shell)
        for script in ("save-session.sh", "resolve-paths.sh", "detect-tools.sh",
                       "bootstrap-dirs.sh", "log.sh", "lib-memory-dir.sh"):
            (plugin / "scripts" / script).write_text(
                (REPO_ROOT / "scripts" / script).read_text())

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text('{"cooldowns": {"save_seconds": 0}}')
        (plugin / "config.json").write_text('{}')
        calls_log = tmp_path / "calls.log"

        env = {
            **os.environ,
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(plugin),
            "REMEMBER_CONFIG": str(cfg_path),
            "STUB_CALLS_LOG": str(calls_log),
        }
        # No explicit session-id arg — the script must auto-detect the latest
        # jsonl under CLAUDE_CONFIG_DIR/projects/<slug>/, not $HOME/.claude's.
        result = subprocess.run(
            ["bash", str(plugin / "scripts" / "save-session.sh"), "--dry"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        calls_text = calls_log.read_text() if calls_log.exists() else ""
        assert real_session_id in calls_text, (
            f"save-session.sh did not extract the session under "
            f"CLAUDE_CONFIG_DIR — calls were: {calls_text!r}"
        )
        assert "decoy-session-0000" not in calls_text, (
            "save-session.sh fell back to $HOME/.claude/projects (the decoy) "
            "instead of honouring CLAUDE_CONFIG_DIR"
        )
