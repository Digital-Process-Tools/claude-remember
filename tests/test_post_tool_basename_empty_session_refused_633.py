"""post-tool-hook.sh's basename sanitiser (#620) can empty SESSION_ID to ""
with no non-empty gate between the sanitiser and the fork (#633).

The stdin route's own guard (post-tool-hook.sh:419-423, #610) is safe because
an empty STDIN_SESSION_ID is caught by STDIN_SESSION_ID_TRUSTED, which
requires `-n "$STDIN_SESSION_ID"` before it is used at all. The basename
route has no equivalent: after the #620 sanitiser sets SESSION_ID="" for a
hostile transcript basename, that empty value used to flow straight into
`nohup "$SAVE_SCRIPT" "$SESSION_ID" ... &` (post-tool-hook.sh:922 at the time
of writing). save-session.sh reads an empty argv[1] as "no id given"
(save-session.sh:273) and silently substitutes the newest .jsonl by mtime,
converting what should be a loud refusal into a completed save for a session
nobody named.

This test pins the invariant documented at post-tool-hook.sh:561-563: a
value must "fall through ... rather than hand save-session.sh an empty
string, which is not 'trusted', it is absent" -- extended here to the
basename route, which must refuse to fork at all once the sanitiser empties
SESSION_ID, rather than silently substituting.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

ASSISTANT_LINE = '{"type":"assistant","message":{"content":"x"}}\n'
TRANSCRIPT_LINES = 100

REAL_SESSION_ID = "eeeeeeee-0000-4000-8000-000000000005"
# Fails the #620 character class (contains "!", outside [A-Za-z0-9._-]) but
# is still a legal filename -- the hostile-filename case #620 was written
# for, not a path-escape attempt.
HOSTILE_BASENAME = "not!valid.jsonl"


def _fake_plugin_root(tmp_path: Path, ledger: Path) -> Path:
    """Mirrors REPO_ROOT via symlinks except scripts/save-session.sh, which
    is replaced with a stub recording its own invocation (argv count and
    joined values) to `ledger`, so a fork that DOES happen is visible and
    distinguishable from one that never happened at all."""
    fake = tmp_path / "fake-plugin"
    fake.mkdir()
    for entry in REPO_ROOT.iterdir():
        if entry.name == "scripts":
            continue
        (fake / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    fake_scripts = fake / "scripts"
    fake_scripts.mkdir()
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.name == "save-session.sh":
            continue
        (fake_scripts / entry.name).symlink_to(entry)
    stub = fake_scripts / "save-session.sh"
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s:%s\\n" "$#" "$*" >> "{ledger}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return fake


def _setup(tmp_path: Path, *, basename: str):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)

    transcript = session_dir / basename
    transcript.write_text(ASSISTANT_LINE * TRANSCRIPT_LINES, encoding="utf-8")

    ledger = tmp_path / "save-argv.log"
    return home, project, remember, ledger


def _env(home: Path, project: Path, remember: Path, plugin_root: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _run(env: dict):
    """No stdin at all -- the basename route is only reached when stdin
    never supplies a trusted session id."""
    return subprocess.run(
        ["bash", str(HOOK)], env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60, check=False,
    )


def _reap(remember: Path, timeout: float = 30) -> None:
    pid_file = remember / "tmp" / "save-session.pid"
    deadline = time.monotonic() + timeout
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    # give the stub a moment to flush its ledger write after the process
    # itself has exited
    time.sleep(0.2)


def test_hostile_basename_must_be_refused_not_silently_substituted(tmp_path):
    """MUST NOT FIRE: the exploit. The only transcript in this project's
    session dir has a basename that fails the #620 sanitiser. Before the
    #633 fix, SESSION_ID becomes "" and still reaches save-session.sh's
    argv, which reads the empty value as "no id given" and silently saves
    the newest .jsonl instead of refusing. The fix must skip the fork
    entirely."""
    home, project, remember, ledger = _setup(tmp_path, basename=HOSTILE_BASENAME)
    plugin_root = _fake_plugin_root(tmp_path, ledger)

    result = _run(_env(home, project, remember, plugin_root))
    assert result.returncode == 0, result.stderr
    _reap(remember)

    assert not ledger.exists(), (
        "save-session.sh was invoked despite the transcript basename "
        f"{HOSTILE_BASENAME!r} failing the sanitiser -- expected the fork "
        f"to be skipped entirely, but got: {ledger.read_text() if ledger.exists() else None!r}"
    )


def test_an_ordinary_transcript_basename_still_saves_normally(tmp_path):
    """MUST FIRE (positive control): an ordinary UUID-shaped transcript
    basename must still reach save-session.sh's argv as itself -- proving
    the fix does not block every save, only the hostile-basename case."""
    home, project, remember, ledger = _setup(
        tmp_path, basename=f"{REAL_SESSION_ID}.jsonl"
    )
    plugin_root = _fake_plugin_root(tmp_path, ledger)

    result = _run(_env(home, project, remember, plugin_root))
    assert result.returncode == 0, result.stderr
    _reap(remember)

    assert ledger.exists(), "the background save never forked -- broken harness"
    argv_line = ledger.read_text().strip()
    assert argv_line == f"1:{REAL_SESSION_ID}", (
        f"an ordinary transcript basename did not reach save-session.sh "
        f"argv unchanged: {argv_line!r}"
    )
