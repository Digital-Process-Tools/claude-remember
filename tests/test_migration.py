"""Tests for one-shot legacy .remember migration in bootstrap-dirs.sh.

When external mode is active (REMEMBER_DIR != ${PROJECT_DIR}/.remember),
bootstrap-dirs.sh should migrate any existing legacy data directory to the
new location on first run and leave a MIGRATED-TO.txt marker behind.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"
LIB_SCRIPT = REPO_ROOT / "scripts" / "lib-memory-dir.sh"


def _source_bootstrap(project_dir: str, pipeline_dir: str, home_dir: str) -> subprocess.CompletedProcess:
    """Source bootstrap-dirs.sh and return the completed process."""
    script = f"""
    set -e
    export PROJECT_DIR={project_dir}
    export PIPELINE_DIR={pipeline_dir}
    export HOME={home_dir}
    source {DETECT_SCRIPT}
    source {BOOTSTRAP_SCRIPT}
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    """
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def _make_legacy_dir(project_dir: Path) -> None:
    """Create a legacy .remember/ with sample files."""
    legacy = project_dir / ".remember"
    (legacy / "tmp").mkdir(parents=True)
    (legacy / "logs").mkdir(parents=True)
    (legacy / "now.md").write_text("## 10:00 | master\nSome work.\n")
    (legacy / "tmp" / "last-save.json").write_text('{"session":"abc","line":5}')


class TestMigration:

    def test_migration_moves_legacy_to_external(self, tmp_path):
        """Legacy .remember/ is moved to external REMEMBER_DIR on first run."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"

        # External data_dir pointing to a slug-based path under home
        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            f'{{"data_dir": "{ext_base}/{{{{slug}}}}"}}'
        )

        result = _source_bootstrap(str(project), str(pipeline), str(home))
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"

        remember_dir = result.stdout.strip().split("REMEMBER_DIR=")[-1].strip()

        # Files must be at new location
        assert (Path(remember_dir) / "now.md").exists(), "now.md not migrated"
        assert (Path(remember_dir) / "tmp" / "last-save.json").exists(), "last-save.json not migrated"

        # Marker must exist in the old location
        assert (legacy / "MIGRATED-TO.txt").exists(), "MIGRATED-TO.txt not created"
        marker_text = (legacy / "MIGRATED-TO.txt").read_text()
        assert remember_dir in marker_text, "marker doesn't reference new location"

    def test_migration_idempotent_when_target_exists(self, tmp_path):
        """Migration is skipped when REMEMBER_DIR already exists."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            f'{{"data_dir": "{ext_base}/{{{{slug}}}}"}}'
        )

        # First run: migrate
        result1 = _source_bootstrap(str(project), str(pipeline), str(home))
        assert result1.returncode == 0

        # Second run: no error, marker still present
        result2 = _source_bootstrap(str(project), str(pipeline), str(home))
        assert result2.returncode == 0
        assert (project / ".remember" / "MIGRATED-TO.txt").exists()

    def test_no_migration_when_legacy_absent(self, tmp_path):
        """No migration when there is no legacy .remember/ to move."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            f'{{"data_dir": "{ext_base}/{{{{slug}}}}"}}'
        )

        result = _source_bootstrap(str(project), str(pipeline), str(home))
        assert result.returncode == 0
        # No legacy dir → no marker
        assert not (project / ".remember" / "MIGRATED-TO.txt").exists()

    def test_no_migration_in_legacy_mode(self, tmp_path):
        """Migration does not run when REMEMBER_DIR is inside PROJECT_DIR."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        home.mkdir()

        _make_legacy_dir(project)
        legacy = project / ".remember"

        # No data_dir override → legacy mode
        (pipeline / "config.json").write_text("{}")

        result = _source_bootstrap(str(project), str(pipeline), str(home))
        assert result.returncode == 0
        # now.md must still be in the original location
        assert (legacy / "now.md").exists()
        assert not (legacy / "MIGRATED-TO.txt").exists()
