"""bootstrap-dirs.sh must stay quiet when it cannot create its own tree (#204).

The secondary defect in #204, distinct from the nested-summarizer recursion but
in the same file family. When ``REMEMBER_DIR`` lands somewhere unwritable — the
reported case is a session opened at the filesystem root, where legacy mode
makes ``REMEMBER_DIR=//.remember`` on a read-only macOS root volume —
bootstrap-dirs.sh does two things in sequence:

    mkdir -p "$REMEMBER_DIR/tmp" ... 2>/dev/null      # fails, silently
    [ -f "$REMEMBER_DIR/.gitignore" ] || echo '*' > "$REMEMBER_DIR/.gitignore" 2>/dev/null

The ``mkdir`` result is never checked, so the script proceeds into file I/O
against a directory it knows nothing about. And the ``2>/dev/null`` on the
second line does not suppress the resulting error: the shell opens redirection
targets before running the command, so the failure to open the target is
reported by the shell itself, outside the scope of the redirect it is being
asked to hide behind. The user sees, as a non-blocking hook failure at session
start:

    .../scripts/bootstrap-dirs.sh: line NN: //.remember/.gitignore: No such file or directory

Reported by @BlockX-P2P alongside the recursion bug. Reproduced here by making
the project root read-only rather than by using ``/``, which is the same
condition without requiring the test to care about the host's root volume.

The second test is the one that matters more: a hook that no-ops itself is a
far worse outcome than a stray error line, so the writable case is pinned in
the same file — bootstrap must still build the full tree and write .gitignore.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX mode bits + bash — read-only enforcement is not portable to NTFS (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# A shell diagnostic naming the script and a line number. This is the exact
# shape of the leak: not a message the plugin chose to print, but bash
# reporting a redirection it could not open.
_SHELL_DIAGNOSTIC = re.compile(r"bootstrap-dirs\.sh: line \d+:")


def _run_session_start(project_dir: Path, home: Path) -> subprocess.CompletedProcess:
    """Run the SessionStart hook the way Claude Code does, in legacy mode.

    No config.json under $HOME, so lib-memory-dir.sh resolves REMEMBER_DIR to
    ``$PROJECT_DIR/.remember`` — which is what puts it inside an unwritable
    root in the reported scenario.
    """
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "TMPDIR": str(home),
        "CLAUDE_PROJECT_DIR": str(project_dir),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    }
    return subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "session-start-hook.sh")],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        stdin=subprocess.DEVNULL,
        cwd=str(project_dir),
    )


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the read-only mode bits this test depends on",
)
def test_unwritable_memory_root_does_not_leak_a_shell_error(tmp_path):
    """The reported symptom: a bash diagnostic escaping to the user's stderr.

    The hook is allowed to do nothing here — there is nowhere to write. It is
    not allowed to be noisy about it, and it is not allowed to walk into file
    I/O against a directory whose creation it never confirmed.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "readonly-project"
    project.mkdir()
    os.chmod(project, 0o555)
    try:
        result = _run_session_start(project, home)
    finally:
        os.chmod(project, 0o755)

    # rc 127 is the documented degraded-env contract: with no writable store
    # log.sh cannot be sourced, and session-start-hook.sh reports that
    # deliberately with its own diagnostic rather than crashing later on an
    # undefined function. That is not what this test is about.
    assert result.returncode in (0, 127), (
        f"outside the degraded-env contract (0, 127): {result.stderr}"
    )
    assert not _SHELL_DIAGNOSTIC.search(result.stderr), (
        "bootstrap-dirs.sh leaked a shell diagnostic to stderr, which surfaces "
        f"as a non-blocking hook failure at session start:\n{result.stderr}"
    )


def test_a_writable_project_still_gets_its_full_tree(tmp_path):
    """The catastrophic case, guarded against.

    Whatever suppresses the noise above must not be reachable in a normal
    session. A real project still gets tmp/, logs/, logs/autonomous/ and the
    .gitignore that keeps the store out of the user's commits — and the hook
    still injects its handoff.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "my-project"
    project.mkdir()

    result = _run_session_start(project, home)

    assert result.returncode == 0, result.stderr
    remember_dir = project / ".remember"
    for expected in ("tmp", "logs", "logs/autonomous"):
        assert (remember_dir / expected).is_dir(), f"{expected} was not created"
    gitignore = remember_dir / ".gitignore"
    assert gitignore.is_file(), ".gitignore was not written into an in-project store"
    assert gitignore.read_text(encoding="utf-8").strip() == "*"
    assert "=== REMEMBER ===" in result.stdout, (
        "session-start no longer injects into a real session — the bootstrap "
        "guard is firing where it must not"
    )
