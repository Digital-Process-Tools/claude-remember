"""Tests for scripts/doctor.sh — the diagnostics command (issue #200).

A diagnostic is only worth having if its verdict is trustworthy in both
directions. These pin the two ways it could lie: calling a working install
broken, and calling a broken one fine.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

from pipeline.slug import session_dir_slug as _slug  # noqa: E402


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    return home, project, remember, session_dir


def _run(home: Path, project: Path, remember: Path):
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    return subprocess.run(["bash", str(DOCTOR)], env=env,
                          capture_output=True, text=True, timeout=120)


def _verdict(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("VERDICT:"):
            return line
    raise AssertionError(f"no VERDICT line in output:\n{stdout}")


def test_doctor_always_exits_zero_even_with_nothing_resolvable(tmp_path):
    """A diagnostic that dies instead of reporting is the problem, not the tool."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["HOME"] = str(tmp_path / "home")
    result = subprocess.run(["bash", str(DOCTOR)], env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "VERDICT:" in result.stdout, "died before reaching a verdict"


def test_a_never_captured_project_is_reported_as_broken(tmp_path):
    home, project, remember, _ = _project(tmp_path)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "PostToolUse has never fired" in result.stdout
    assert "Restart Claude Code" in result.stdout, (
        "named the problem without naming the fix — the whole point of #200 "
        "is that the user had no way to discover the remedy"
    )
    assert "problem" in _verdict(result.stdout)


def test_an_install_predating_the_marker_is_not_called_broken(tmp_path):
    """capture-alive shipped with the #200 fix. An existing install has none
    until its next tool call, but a completed save proves PostToolUse has run.

    Reporting that as "never fired" would send a working user off to restart
    for nothing — and the report would contradict itself two lines later, where
    it prints the successful save.
    """
    home, project, remember, _ = _project(tmp_path)
    (remember / "tmp" / "last-save.json").write_text(
        json.dumps({"session": "sess-1", "line": 500}), encoding="utf-8"
    )

    result = _run(home, project, remember)

    assert "has never fired" not in result.stdout, (
        "called a working install broken because a marker it predates is absent"
    )
    assert "capture is working" in _verdict(result.stdout)


def test_a_healthy_project_is_reported_as_working(tmp_path):
    home, project, remember, _ = _project(tmp_path)
    (remember / "tmp" / "capture-alive").write_text("sess-1")
    (remember / "tmp" / "last-save.json").write_text(
        json.dumps({"session": "sess-1", "line": 500}), encoding="utf-8"
    )

    result = _run(home, project, remember)

    assert "capture is working" in _verdict(result.stdout)


def test_a_slug_mismatch_is_not_answered_with_restart_claude_code(tmp_path):
    """The verdict is the line the operator acts on, and commands/doctor.md
    tells them not to second-guess it.

    Every cause of "capture is not running" used to collapse into "restart
    Claude Code" because the generic branch was tested first — so a user whose
    slug does not match was confidently sent to do the one thing that cannot
    help them, with the correct FAIL printed two sections above.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    # The hook IS wired and running — it just exits early, finding no
    # transcript under a slug Claude Code never created.
    (remember / "tmp" / "post-tool-ran").write_text("")

    result = _run(home, project, remember)

    verdict = _verdict(result.stdout)
    assert "#144" in verdict, f"verdict did not name the real cause: {verdict}"
    # Matching bare "restart" would fail on the verdict's own "restarting will
    # not help" — the advice is what must be absent, not the word.
    assert "restart claude code" not in verdict.lower(), (
        f"told a slug-mismatch victim to restart Claude Code: {verdict}"
    )


def test_a_wired_hook_that_never_serviced_a_session_says_so(tmp_path):
    """"Wired" and "working" are different questions. post-tool-ran is written
    before every early exit; capture-alive only once a transcript is found."""
    home, project, remember, _ = _project(tmp_path)
    (remember / "tmp" / "post-tool-ran").write_text("")

    result = _run(home, project, remember)

    assert "has never fired" not in result.stdout, (
        "reported a running hook as never fired, which points at a restart "
        "instead of at the early exit that is actually happening"
    )
    assert "exiting early" in result.stdout


def test_a_slug_mismatch_is_named_outright(tmp_path):
    """#144's silent failure: the plugin computes a session dir Claude Code
    never created, and capture no-ops for the life of the project."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)  # exists, but not the slug

    result = _run(home, project, remember)

    assert "Session dir MISSING" in result.stdout
    assert "#144" in result.stdout, "did not point at the known cause"
