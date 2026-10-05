"""The legacy in-project store notice in bootstrap-dirs.sh (#898).

Up to v0.39.0, bootstrap-dirs.sh moved an in-project `.remember/` into an
external REMEMBER_DIR on first run. That automatic migration is gone (#898):
nothing is moved, nothing is deleted. When the old in-project `.remember/`
still holds memory data while data_dir points elsewhere, and that new
location does not exist yet, one stderr line says so and the operator moves
it by hand (see docs/external-storage-mode.md and /remember:doctor).

Every "silent" case is paired with the firing case in the same fixture
shape: a notice that never prints would pass every silent assertion.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"
LIB_SCRIPT = REPO_ROOT / "scripts" / "lib-memory-dir.sh"


def _bash_path(p) -> str:
    """Forward-slash drive form, usable by BOTH Git Bash and the Windows
    ``python3`` that the bash scripts invoke (jq fallback).

    `C:\\Users\\x` -> `C:/Users/x`. Git Bash and Windows Python both accept
    forward-slash drive paths; the MSYS `/c/x` form works in bash but Windows
    Python can't ``open()`` it. On POSIX the path is returned unchanged.
    """
    return str(p).replace("\\", "/")


def _find_bash():
    """Return the bash executable to use for subprocess calls.

    On POSIX, plain "bash". On Windows, the Git-for-Windows bash (NOT the
    System32 WSL launcher, which CreateProcess finds first on PATH). Returns
    None on Windows when Git Bash isn't installed → the module is skipped.
    """
    if sys.platform != "win32":
        return "bash"
    import shutil
    candidates = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "bash.exe")
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    resolved = shutil.which("bash")
    if resolved and "git" in resolved.replace("\\", "/").lower():
        return resolved
    return None


_BASH = _find_bash()

pytestmark = pytest.mark.skipif(_BASH is None, reason="Git Bash not found (Windows without Git for Windows)")


def _source_bootstrap(project_dir: str, pipeline_dir: str, home_dir: str) -> subprocess.CompletedProcess:
    """Source bootstrap-dirs.sh and return the completed process."""
    script = f"""
    set -e
    export PROJECT_DIR="{_bash_path(project_dir)}"
    export PIPELINE_DIR="{_bash_path(pipeline_dir)}"
    export HOME="{_bash_path(home_dir)}"
    source "{_bash_path(DETECT_SCRIPT)}"
    source "{_bash_path(BOOTSTRAP_SCRIPT)}"
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    """
    return subprocess.run([_BASH, "-c", script], capture_output=True, text=True)


def _make_legacy_dir(project_dir: Path) -> None:
    """Create a legacy .remember/ with sample files."""
    legacy = project_dir / ".remember"
    (legacy / "tmp").mkdir(parents=True)
    (legacy / "logs").mkdir(parents=True)
    (legacy / "now.md").write_text("## 10:00 | master\nSome work.\n")
    (legacy / "tmp" / "last-save.json").write_text('{"session":"abc","line":5}')




def _external_config(pipeline: Path, ext_base: Path) -> None:
    (pipeline / "config.json").write_text(
        f'{{"data_dir": "{_bash_path(ext_base)}/{{{{slug}}}}"}}'
    )


def _layout(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    pipeline = tmp_path / "plugin"
    pipeline.mkdir()
    home = tmp_path / "home"
    (home / ".remember").mkdir(parents=True)
    return project, pipeline, home


def _remember_dir(result: subprocess.CompletedProcess) -> str:
    return result.stdout.strip().split("REMEMBER_DIR=")[-1].strip()


def _notice_lines(result: subprocess.CompletedProcess) -> list:
    return [line for line in result.stderr.splitlines() if "holds memory data" in line]


class TestLegacyStoreNotice:

    def test_notice_fires_and_nothing_moves(self, tmp_path):
        """The must-fire case: memory data in the legacy store, data_dir
        external, the external store not created yet. One line naming both
        directories; every legacy file still where it was."""
        project, pipeline, home = _layout(tmp_path)
        _make_legacy_dir(project)
        legacy = project / ".remember"
        _external_config(pipeline, tmp_path / "ext")

        result = _source_bootstrap(str(project), str(pipeline), str(home))
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
        remember_dir = _remember_dir(result)

        lines = _notice_lines(result)
        assert len(lines) == 1, f"expected exactly one notice line, got:\n{result.stderr}"
        assert lines[0].startswith("remember: ")
        assert _bash_path(legacy) in lines[0]
        assert remember_dir in lines[0]
        assert "/remember:doctor" in lines[0]

        assert (legacy / "now.md").read_text() == "## 10:00 | master\nSome work.\n"
        assert (legacy / "tmp" / "last-save.json").exists()
        assert not (legacy / "MIGRATED-TO.txt").exists()
        assert not (Path(remember_dir) / "now.md").exists(), "memory data was moved"

    def test_silent_once_the_external_store_exists(self, tmp_path):
        """REMEMBER_DIR already exists: the second run says nothing. The
        first run of the same fixture is the positive control."""
        project, pipeline, home = _layout(tmp_path)
        _make_legacy_dir(project)
        _external_config(pipeline, tmp_path / "ext")

        first = _source_bootstrap(str(project), str(pipeline), str(home))
        assert first.returncode == 0, first.stderr
        assert len(_notice_lines(first)) == 1, first.stderr
        assert Path(_remember_dir(first)).is_dir()

        second = _source_bootstrap(str(project), str(pipeline), str(home))
        assert second.returncode == 0, second.stderr
        assert _notice_lines(second) == [], second.stderr
        assert (project / ".remember" / "now.md").exists()

    def test_silent_when_legacy_store_absent(self, tmp_path):
        project, pipeline, home = _layout(tmp_path)
        _external_config(pipeline, tmp_path / "ext")

        result = _source_bootstrap(str(project), str(pipeline), str(home))
        assert result.returncode == 0, result.stderr
        assert _notice_lines(result) == [], result.stderr
        assert not (project / ".remember").exists()

    def test_silent_when_legacy_store_holds_no_memory_data(self, tmp_path):
        """A `.remember/` holding only a config.json is a project config
        layer, not memory data to move."""
        project, pipeline, home = _layout(tmp_path)
        (project / ".remember").mkdir()
        (project / ".remember" / "config.json").write_text("{}")
        _external_config(pipeline, tmp_path / "ext")

        result = _source_bootstrap(str(project), str(pipeline), str(home))
        assert result.returncode == 0, result.stderr
        assert _notice_lines(result) == [], result.stderr

    def test_silent_in_legacy_mode(self, tmp_path):
        """REMEMBER_DIR is the legacy directory itself: nothing to point at."""
        project, pipeline, home = _layout(tmp_path)
        _make_legacy_dir(project)
        legacy = project / ".remember"
        (pipeline / "config.json").write_text("{}")

        result = _source_bootstrap(str(project), str(pipeline), str(home))
        assert result.returncode == 0, result.stderr
        assert _remember_dir(result) == _bash_path(legacy)
        assert _notice_lines(result) == [], result.stderr
        assert (legacy / "now.md").exists()

    def test_silent_when_the_store_lives_inside_the_legacy_directory(self, tmp_path):
        """A session opened in $HOME: the "legacy" directory is ~/.remember,
        the user-global config home, and REMEMBER_DIR is a slug directory
        inside it (#132). Moving one into the other is meaningless, so even
        with memory data there the notice stays silent."""
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "now.md").write_text("kept\n")
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        _external_config(pipeline, home / ".remember")

        result = _source_bootstrap(str(home), str(pipeline), str(home))
        assert result.returncode == 0, result.stderr
        assert _remember_dir(result).startswith(_bash_path(home / ".remember") + "/")
        assert _notice_lines(result) == [], result.stderr
        assert (home / ".remember" / "now.md").read_text() == "kept\n"


DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"


def _doctor(project: Path, home: Path, remember_dir: Path) -> subprocess.CompletedProcess:
    """doctor.sh with REMEMBER_DIR handed in directly, lib-memory-dir.sh's
    reentrancy guard set so the config merge is skipped (the same harness
    tests/test_doctor_oversized_store_348.py uses)."""
    env = {
        **os.environ,
        "HOME": _bash_path(home),
        "CLAUDE_PROJECT_DIR": _bash_path(project),
        "CLAUDE_PLUGIN_ROOT": _bash_path(REPO_ROOT),
        "REMEMBER_DIR": _bash_path(remember_dir),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    return subprocess.run([_BASH, DOCTOR.as_posix()], env=env,
                          capture_output=True, text=True, timeout=180, check=False)


class TestDoctorNamesTheLegacyStore:
    """The notice points at /remember:doctor, so doctor must say something
    about the same condition -- a pointer that lands on silence reads as
    "nothing is wrong"."""

    def test_doctor_warns_when_the_legacy_store_holds_memory_data(self, tmp_path):
        project, _, home = _layout(tmp_path)
        _make_legacy_dir(project)
        external = tmp_path / "ext" / "store"
        external.mkdir(parents=True)

        result = _doctor(project, home, external)
        assert result.returncode == 0, result.stderr
        warn = [line for line in result.stdout.splitlines() if line.startswith("WARN Legacy store")]
        assert len(warn) == 1, result.stdout
        assert _bash_path(project / ".remember") in warn[0]
        assert _bash_path(external) in warn[0]
        assert (project / ".remember" / "now.md").exists()

    def test_doctor_is_silent_in_legacy_mode(self, tmp_path):
        project, _, home = _layout(tmp_path)
        _make_legacy_dir(project)

        result = _doctor(project, home, project / ".remember")
        assert result.returncode == 0, result.stderr
        assert "Legacy store" not in result.stdout, result.stdout
        assert "Storage mode: legacy" in result.stdout, result.stdout

    def test_doctor_is_silent_when_the_legacy_store_holds_no_memory_data(self, tmp_path):
        project, _, home = _layout(tmp_path)
        (project / ".remember").mkdir()
        (project / ".remember" / "MIGRATED-TO.txt").write_text("moved\n")
        external = tmp_path / "ext" / "store"
        external.mkdir(parents=True)

        result = _doctor(project, home, external)
        assert result.returncode == 0, result.stderr
        assert "Legacy store" not in result.stdout, result.stdout
        assert "Storage mode: external" in result.stdout, result.stdout
