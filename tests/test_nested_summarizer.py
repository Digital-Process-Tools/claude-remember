"""The nested Haiku summarizer must not scaffold a memory directory (#204).

pipeline/haiku.py spawns `claude -p` with cwd=tempfile.gettempdir() to keep the
summarizer out of the user's repo. Claude Code loads plugins in that child and
derives its CLAUDE_PROJECT_DIR from that cwd — so this plugin's own hooks fire
inside the summarizer, resolve the temp dir as a project, and build a memory
store under its slug:

    ~/.remember/-private-var-folders-xr-...-T/
    ├── logs/
    └── tmp/

Reported by @BlockX-P2P on 0.8.3 / 0.8.7 / 0.8.8, confirmed on Claude Code
2.1.219: a nested `-p` run from a scratch cwd sets CLAUDE_PROJECT_DIR to that
cwd's physical path, reports CLAUDE_CODE_ENTRYPOINT=sdk-cli, and fires both
SessionStart and UserPromptSubmit.

The guard was always intended — session-start-hook.sh documented a no-op for
"the remember pipeline's own Haiku call" — but it keyed on the child having no
CLAUDE_PROJECT_DIR, and the child has one. _child_env() now sets a positive
marker that resolve-paths.sh stops on, so the signal is one we own rather than
one we deleted.

It lives in resolve-paths.sh because SessionStart, UserPromptSubmit and
PostToolUse are all registered and each alone recreates the directory: guarding
only session-start-hook.sh silences its log line and changes nothing on disk.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))

from pipeline.haiku import NESTED_SUMMARIZER_ENV, _child_env  # noqa: E402


def _external_home(tmp_path: Path) -> tuple[Path, Path]:
    """A home in external mode: the store is slugged per project, so a stray
    project root becomes a visible directory beside the real ones."""
    home = tmp_path / "home"
    store = tmp_path / "store"
    (home / ".remember").mkdir(parents=True)
    (home / ".remember" / "config.json").write_text(
        json.dumps({"data_dir": f"{store}/{{slug}}"}), encoding="utf-8"
    )
    store.mkdir()
    return home, store


def _slug_of(project_dir: Path) -> str:
    """The slug the plugin itself would compute — asked of lib-slug.sh rather
    than reimplemented, so the assertion pins the real directory name."""
    script = f"""
    source {REPO_ROOT}/scripts/lib-slug.sh
    session_dir_slug "{project_dir}"
    """
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    return out.stdout.strip()


def _registered_hook_scripts() -> list[str]:
    """Every hook this plugin registers, read from hooks.json.

    Derived rather than hardcoded: a hook added later is covered by these tests
    the day it is registered, which is the property that made a one-hook fix
    insufficient in the first place.
    """
    hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    scripts = []
    for entries in hooks.values():
        for entry in entries:
            for hook in entry["hooks"]:
                scripts.append(hook["command"].rsplit("/", 1)[-1].rstrip('"'))
    return scripts


def _run_hook(script: str, project_dir: Path, home: Path, nested: bool) -> subprocess.CompletedProcess:
    """Run one hook exactly as Claude Code does: CLAUDE_PROJECT_DIR from the
    session's cwd, CLAUDE_PLUGIN_ROOT at the install, stdin closed."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "TMPDIR": str(project_dir),
        "CLAUDE_PROJECT_DIR": str(project_dir),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    }
    if nested:
        env[NESTED_SUMMARIZER_ENV] = "1"
    return subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        stdin=subprocess.DEVNULL,
        cwd=str(project_dir),
    )


def test_child_env_marks_the_nested_summarizer():
    """The spawn side of the contract: the marker is on the child env."""
    assert _child_env()[NESTED_SUMMARIZER_ENV] == "1"


def test_the_marker_does_not_escape_into_the_parent_environment(monkeypatch):
    """_child_env() builds a dict; it must not mutate os.environ, or the
    plugin would go silent in the user's real session too."""
    monkeypatch.delenv(NESTED_SUMMARIZER_ENV, raising=False)
    _child_env()
    import os

    assert NESTED_SUMMARIZER_ENV not in os.environ


def test_the_temp_dir_project_is_reproduced_without_the_marker(tmp_path):
    """The bug itself, pinned. Without the marker the hook builds a store for
    the summarizer's cwd — this is the directory users find and delete."""
    home, store = _external_home(tmp_path)
    tmpdir_project = tmp_path / "T"
    tmpdir_project.mkdir()

    result = _run_hook("session-start-hook.sh", tmpdir_project, home, nested=False)

    assert result.returncode == 0, result.stderr
    assert (store / _slug_of(tmpdir_project)).is_dir(), (
        "the reported behaviour did not reproduce — if the hook no longer "
        "scaffolds here, the guard below is being tested against nothing"
    )


@pytest.mark.parametrize("script", _registered_hook_scripts())
def test_no_registered_hook_scaffolds_for_the_nested_summarizer(script, tmp_path):
    """Every hook in hooks.json, not just SessionStart.

    Each one sources resolve-paths.sh then bootstrap-dirs.sh, so each one alone
    recreates the directory; a guard in one hook only silences that hook's log
    line. The post-condition is absolute — the slug directory does not exist —
    not "the count did not grow".
    """
    home, store = _external_home(tmp_path)
    tmpdir_project = tmp_path / "T"
    tmpdir_project.mkdir()

    result = _run_hook(script, tmpdir_project, home, nested=True)

    assert result.returncode == 0, f"{script} must never block: {result.stderr}"
    assert list(store.iterdir()) == [], (
        f"{script} scaffolded {[p.name for p in store.iterdir()]} for the "
        f"nested summarizer's temp-dir cwd"
    )
    assert not (tmpdir_project / ".remember").exists(), (
        f"{script} left a legacy-mode store in the summarizer's cwd"
    )


def test_a_real_session_still_scaffolds_its_memory_directory(tmp_path):
    """The guard must not pass by disabling the plugin.

    A session with no marker is a user's session: the full tree is created and
    the memory injection still runs.
    """
    home, store = _external_home(tmp_path)
    project = tmp_path / "my-project"
    project.mkdir()

    result = _run_hook("session-start-hook.sh", project, home, nested=False)

    assert result.returncode == 0, result.stderr
    remember_dir = store / _slug_of(project)
    for expected in ("tmp", "logs", "logs/autonomous"):
        assert (remember_dir / expected).is_dir(), f"{expected} not created"
    assert "=== HANDOFF ===" in result.stdout, (
        "session-start no longer injects into a real session — the guard is "
        "firing where it must not"
    )


def test_the_marker_leaves_a_real_session_untouched_when_only_it_is_set(tmp_path):
    """Symmetry check on one hook: same project, same home, marker the only
    difference. Nothing else about the environment decides the outcome."""
    home, store = _external_home(tmp_path)
    project = tmp_path / "my-project"
    project.mkdir()

    nested = _run_hook("user-prompt-hook.sh", project, home, nested=True)
    assert nested.returncode == 0, nested.stderr
    assert list(store.iterdir()) == []

    real = _run_hook("user-prompt-hook.sh", project, home, nested=False)
    assert real.returncode == 0, real.stderr
    assert (store / _slug_of(project)).is_dir()
