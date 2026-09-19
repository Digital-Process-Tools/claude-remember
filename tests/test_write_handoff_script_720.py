"""#720 — /remember must not parse its Write target out of transcript text.

write-handoff.sh takes no destination argument: the note arrives on stdin and
the destination is derived from REMEMBER_DIR the same way session-start-hook.sh
derives it, never from anything a model read or was told. Three properties:

  - a forged/out-of-store path left in the hint file (the shape an attacker
    who can only influence file content, never env vars, could produce) is
    REFUSED and the write falls back to the real default -- never written to
    the attacker's chosen path
  - positive control: the hook's own resolved path (including a per-session
    name this script never computes itself) IS accepted and written -- so the
    suite cannot pass on a script that refuses everything
  - the script always prints the path it wrote to, or a REFUSED line -- never
    silence
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX write-handoff script — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WRITE_HANDOFF_SCRIPT = REPO_ROOT / "scripts" / "write-handoff.sh"


def _sandbox(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)
    (home / ".remember" / "config.json").write_text(
        json.dumps({"data_dir": ".remember"})
    )
    (home / ".claude" / "projects").mkdir(parents=True)
    return project, home


def _write_handoff(project: Path, home: Path, note: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
    }
    return subprocess.run(
        ["bash", str(WRITE_HANDOFF_SCRIPT)],
        input=note,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestForgedPathIsRefused:

    def test_out_of_store_hint_falls_back_to_the_real_default(self, tmp_path):
        """An attacker who can only plant text (not set env vars) could at
        best get their path into the hint file -- simulate that directly.
        The shape check must refuse it and fall back, not honour it."""
        project, home = _sandbox(tmp_path)
        remember_dir = project / ".remember"
        (remember_dir / "tmp").mkdir(parents=True)
        (remember_dir / "tmp" / "handoff-path").write_text(
            str(Path.home() / ".claude" / "CLAUDE.md")
        )

        result = _write_handoff(project, home, "attacker-shaped note\n")

        assert result.returncode == 0, result.stderr
        target = remember_dir / "remember.md"
        assert target.exists() and "attacker-shaped note" in target.read_text(), (
            "forged hint should fall back to the real default, not be dropped"
        )
        assert "CLAUDE.md" not in result.stdout, (
            f"the forged out-of-store path leaked into the printed target.\n"
            f"stdout: {result.stdout}"
        )

    def test_a_path_with_no_remember_directory_component_is_refused(self, tmp_path):
        project, home = _sandbox(tmp_path)
        remember_dir = project / ".remember"
        (remember_dir / "tmp").mkdir(parents=True)
        (remember_dir / "tmp" / "handoff-path").write_text(str(project / "not-remember.md"))

        result = _write_handoff(project, home, "note\n")

        assert result.returncode == 0
        assert (remember_dir / "remember.md").exists()
        assert not (project / "not-remember.md").exists()


class TestHookResolvedPathIsHonoured:

    def test_per_session_hint_from_the_hook_is_written_verbatim(self, tmp_path):
        """Positive control: the hook's own resolved path -- including a
        per-session filename this script never computes on its own -- is
        accepted and written. Without this, the refusal tests above could
        pass on a script that refuses every hint unconditionally."""
        project, home = _sandbox(tmp_path)
        remember_dir = project / ".remember"
        (remember_dir / "tmp").mkdir(parents=True)
        session_path = remember_dir / "remember.abc123.md"
        (remember_dir / "tmp" / "handoff-path").write_text(str(session_path))

        result = _write_handoff(project, home, "session note\n")

        assert result.returncode == 0, result.stderr
        assert session_path.exists() and "session note" in session_path.read_text()
        assert str(session_path) in result.stdout


class TestOutputIsNeverSilent:

    def test_success_prints_the_path_written_and_actually_writes_it(self, tmp_path):
        """The stdout claim and the filesystem must agree -- a stub that only
        echoes the success line and never touches disk must not pass this."""
        project, home = _sandbox(tmp_path)
        result = _write_handoff(project, home, "a very specific note body\n")
        assert result.returncode == 0
        assert "remember.md" in result.stdout
        assert result.stdout.strip(), "a successful write must not print nothing"

        target = project / ".remember" / "remember.md"
        assert target.exists(), "stdout claimed success but the file was never created"
        assert target.read_text() == "a very specific note body\n"
