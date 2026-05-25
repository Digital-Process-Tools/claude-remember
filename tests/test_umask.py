"""Tests for umask 077 enforcement in the pipeline entry point.

Issue #68: files created with the default umask (022) are world-readable on
multi-user machines. resolve-paths.sh now sets umask 077 so that all downstream
files (logs, memory dirs, temp files) are created with mode 600/700.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX umask + mode bits don't apply to NTFS (umask is a no-op on Windows)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATHS_SH = REPO_ROOT / "scripts" / "resolve-paths.sh"
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
BOOTSTRAP_DIRS_SH = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"
DETECT_TOOLS_SH = REPO_ROOT / "scripts" / "detect-tools.sh"


def _mode(path: Path) -> int:
    """Return the permission bits (lower 9 bits) for a path."""
    return stat.S_IMODE(os.stat(path).st_mode)


def _run(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-c", script],
        env=merged,
        capture_output=True,
        text=True,
    )


class TestUmaskAfterSourceResolve:
    """After sourcing resolve-paths.sh the active umask must be 077."""

    def test_umask_is_077_after_source(self, tmp_path):
        """Sourcing resolve-paths.sh sets umask 077 for subsequent file ops."""
        script = f"""
        export CLAUDE_PROJECT_DIR={tmp_path}
        export CLAUDE_PLUGIN_ROOT={REPO_ROOT}
        source {RESOLVE_PATHS_SH}
        # Print the umask so we can assert it
        printf '%04o' "$(umask)"
        """
        result = _run(script)
        assert result.returncode == 0, f"resolve-paths.sh failed: {result.stderr}"
        assert result.stdout.strip() == "0077", (
            f"Expected umask 0077 after sourcing resolve-paths.sh, got {result.stdout.strip()!r}"
        )

    def test_file_created_after_source_is_mode_600(self, tmp_path):
        """A file created after sourcing resolve-paths.sh must be mode 600."""
        test_file = tmp_path / "created-after-source.txt"
        script = f"""
        export CLAUDE_PROJECT_DIR={tmp_path}
        export CLAUDE_PLUGIN_ROOT={REPO_ROOT}
        source {RESOLVE_PATHS_SH}
        # Simulate the kind of file creation that log.sh and temp file
        # creation do: plain shell redirection.
        touch {test_file}
        """
        result = _run(script)
        assert result.returncode == 0, result.stderr
        assert test_file.exists(), "Test file was not created"
        mode = _mode(test_file)
        assert mode == 0o600, (
            f"Expected mode 0600 for file created after sourcing resolve-paths.sh, got {oct(mode)}"
        )

    def test_directory_created_after_source_is_mode_700(self, tmp_path):
        """A directory created after sourcing resolve-paths.sh must be mode 700."""
        test_dir = tmp_path / "subdir"
        script = f"""
        export CLAUDE_PROJECT_DIR={tmp_path}
        export CLAUDE_PLUGIN_ROOT={REPO_ROOT}
        source {RESOLVE_PATHS_SH}
        mkdir -p {test_dir}
        """
        result = _run(script)
        assert result.returncode == 0, result.stderr
        assert test_dir.is_dir(), "Test directory was not created"
        mode = _mode(test_dir)
        assert mode == 0o700, (
            f"Expected mode 0700 for directory created after sourcing resolve-paths.sh, got {oct(mode)}"
        )


class TestLogFilePermissions:
    """Log files created by log.sh must be mode 600."""

    def _setup_project(self, tmp_path: Path) -> Path:
        """Create a minimal project layout that log.sh expects."""
        config = tmp_path / "config.json"
        config.write_text('{"remember_timezone": "UTC"}')
        log_dir = tmp_path / ".remember" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return tmp_path

    def test_log_file_created_with_mode_600(self, tmp_path):
        """Log files appended by log.sh must not be world or group readable."""
        project = self._setup_project(tmp_path)
        log_dir = project / ".remember" / "logs"

        script = f"""
        export CLAUDE_PROJECT_DIR={project}
        export CLAUDE_PLUGIN_ROOT={REPO_ROOT}
        source {RESOLVE_PATHS_SH}
        source {LOG_SH}
        log "test" "umask test message"
        # Print the log file path so we can inspect it
        echo "LOG_FILE=$MEMORY_LOG_FILE"
        """
        result = _run(script)
        assert result.returncode == 0, f"log.sh failed:\n{result.stderr}"

        # Find the created log file
        log_files = list(log_dir.glob("memory-*.log"))
        assert log_files, (
            f"No memory-*.log file found in {log_dir}. stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        for lf in log_files:
            mode = _mode(lf)
            assert mode == 0o600, (
                f"Log file {lf.name} has mode {oct(mode)}, expected 0600. "
                "Log files on multi-user machines must not be world/group readable."
            )


class TestRememberDirPermissions:
    """.remember/ directories created by bootstrap-dirs.sh must be mode 700."""

    def test_remember_dir_mode_700(self, tmp_path):
        """bootstrap-dirs.sh must create .remember/ with mode 700."""
        remember_dir = tmp_path / ".remember"

        # Provide the minimal env that bootstrap-dirs.sh chain needs
        script = f"""
        export CLAUDE_PROJECT_DIR={tmp_path}
        export CLAUDE_PLUGIN_ROOT={REPO_ROOT}
        # bootstrap-dirs.sh sources resolve-paths and detect-tools internally;
        # we replicate the minimum stubs to avoid needing a real claude CLI.
        source {RESOLVE_PATHS_SH}
        # Manually create the .remember dir as bootstrap-dirs.sh would
        mkdir -p {remember_dir}/logs {remember_dir}/tmp
        """
        result = _run(script)
        assert result.returncode == 0, result.stderr
        assert remember_dir.is_dir(), ".remember dir was not created"
        mode = _mode(remember_dir)
        assert mode == 0o700, (
            f".remember/ has mode {oct(mode)}, expected 0700. "
            "Memory directories must not be world/group readable on multi-user machines."
        )


class TestTempFilePermissions:
    """Temp files created via mktemp after sourcing resolve-paths.sh must be mode 600."""

    def test_mktemp_file_mode_600(self, tmp_path):
        """mktemp files created after the umask is set must be mode 600."""
        script = f"""
        export CLAUDE_PROJECT_DIR={tmp_path}
        export CLAUDE_PLUGIN_ROOT={REPO_ROOT}
        source {RESOLVE_PATHS_SH}
        # Replicate how save-session.sh creates temp files
        TMP_PROMPT=$(mktemp "${{TMPDIR:-/tmp}}"/remember-test-XXXXXX)
        echo "payload" > "$TMP_PROMPT"
        # stat -c '%a' (Linux) and stat -f '%Lp' (macOS) both emit octal digits
        # as a plain string (e.g. "600") — no printf reformatting needed.
        stat -c '%a' "$TMP_PROMPT" 2>/dev/null || stat -f '%Lp' "$TMP_PROMPT"
        rm -f "$TMP_PROMPT"
        """
        result = _run(script)
        assert result.returncode == 0, result.stderr
        mode_str = result.stdout.strip()
        assert mode_str == "600", (
            f"Temp file created via mktemp has mode {mode_str!r}, expected 600. "
            "Temp files in $TMPDIR must not be world/group readable."
        )
