"""~/.remember must never be migrated as if it were a legacy project store.

It is the user-global config home — the file lib-memory-dir.sh reads to resolve
REMEMBER_DIR at all. Open a session with cwd = $HOME and the one-shot migration
in bootstrap-dirs.sh saw a `.remember` next to the "project", found the three
conditions satisfied, and moved the whole directory into the external store.
The config that directed the migration was consumed by it.

What made it expensive rather than merely wrong: every later session in every
project then found no user config, fell back to `data_dir=".remember"`, and
wrote memory into the working tree while the central store went stale. And it
never re-fired to reveal itself, because the home-slug directory now existed
(issue #132, reported by @jqit-ricky, who lost a day of memory across two
projects to it).

Only external-mode users could hit it — precisely the users the user-global
config exists to serve.
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


def _run_bootstrap(project_dir: Path, home_dir: Path) -> subprocess.CompletedProcess:
    """Source detect-tools + bootstrap-dirs exactly as a hook does."""
    script = f"""
    export PROJECT_DIR={project_dir}
    export PIPELINE_DIR={REPO_ROOT}
    export HOME={home_dir}
    source {REPO_ROOT}/scripts/detect-tools.sh
    source {REPO_ROOT}/scripts/bootstrap-dirs.sh
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    """
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


def _external_home(tmp_path: Path) -> tuple[Path, Path]:
    """A home in external mode: ~/.remember/config.json with an absolute store."""
    home = tmp_path / "home"
    store = tmp_path / "store"
    (home / ".remember").mkdir(parents=True)
    (home / ".remember" / "config.json").write_text(
        json.dumps({"data_dir": f"{store}/{{slug}}"}), encoding="utf-8"
    )
    return home, store


def test_a_session_in_the_home_directory_keeps_the_user_config(tmp_path):
    """The reported loss: cwd = $HOME swept ~/.remember into the store."""
    home, store = _external_home(tmp_path)
    config = home / ".remember" / "config.json"

    result = _run_bootstrap(home, home)

    assert result.returncode == 0, result.stderr
    assert config.is_file(), (
        "the user-global config was migrated away; every later session in every "
        "project would fall back to data_dir='.remember' and leak memory into "
        "the working tree"
    )
    assert json.loads(config.read_text())["data_dir"].endswith("{slug}")
    assert not (home / ".remember" / "MIGRATED-TO.txt").exists()


def test_home_reached_by_a_different_path_is_still_recognised(tmp_path):
    """$HOME and the project dir can name one directory by two paths.

    A symlinked home, or /tmp against /private/tmp on macOS. A textual compare
    misses that, and the cost of missing is the user's config.
    """
    home, store = _external_home(tmp_path)
    link = tmp_path / "home-link"
    link.symlink_to(home)

    result = _run_bootstrap(link, home)

    assert result.returncode == 0, result.stderr
    assert (home / ".remember" / "config.json").is_file(), (
        "home reached through a symlink was migrated away"
    )


def test_a_real_project_still_migrates(tmp_path):
    """The guard must not disable the migration it is narrowing.

    A genuine legacy project store still moves into the external location.
    """
    home, store = _external_home(tmp_path)
    project = tmp_path / "project"
    legacy = project / ".remember"
    legacy.mkdir(parents=True)
    (legacy / "now.md").write_text("## 10:00 | main\nreal memory\n", encoding="utf-8")

    result = _run_bootstrap(project, home)

    assert result.returncode == 0, result.stderr
    remember_dir = next(
        line.split("=", 1)[1] for line in result.stdout.splitlines()
        if line.startswith("REMEMBER_DIR=")
    )
    assert (Path(remember_dir) / "now.md").read_text() == "## 10:00 | main\nreal memory\n"
    assert (legacy / "MIGRATED-TO.txt").is_file(), "no migration marker left behind"


def test_the_default_mode_home_session_is_untouched(tmp_path):
    """Without a config, REMEMBER_DIR is ~/.remember itself — a no-op path.

    This is why the bug only ever hit external-mode users: in default mode the
    first condition (REMEMBER_DIR != legacy) already fails.
    """
    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)
    (home / ".remember" / "now.md").write_text("kept\n", encoding="utf-8")

    result = _run_bootstrap(home, home)

    assert result.returncode == 0, result.stderr
    assert (home / ".remember" / "now.md").read_text() == "kept\n"
