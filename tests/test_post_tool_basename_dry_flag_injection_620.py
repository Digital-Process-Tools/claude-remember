"""post-tool-hook.sh's basename-derived SESSION_ID reaches save-session.sh's
argv unguarded, the same class #610 fixed for the stdin route (#620).

When STDIN_SESSION_ID is absent or untrusted, post-tool-hook.sh falls back to
deriving SESSION_ID from the TRANSCRIPT basename
(``SESSION_ID="${TRANSCRIPT##*/}"``, stripped of ``.jsonl``,
post-tool-hook.sh:604-605). Unlike STDIN_SESSION_ID -- guarded at its point of
entry by a case statement that rejects a leading dash (post-tool-hook.sh:419-423,
#610) -- this basename value flowed straight into
``nohup "$SAVE_SCRIPT" "$SESSION_ID" ... &`` (post-tool-hook.sh:910) with no
guard at all.

NOTE on impact, established while writing this test (self-review, see the PR
body): for THIS route specifically, save-session.sh's own auto-detect
(``if [ -z "$SESSION_ID" ]``, save-session.sh:198-200) reads the SAME session
directory with the SAME "newest .jsonl" rule whenever the value it was handed
does not survive its arg loop as a positional -- which is exactly what
happens for both recognised flags ("--dry", "--force"). Because the
basename-derived id and the transcript save-session.sh would independently
re-discover are the same file, guarding this call site does not change
save-session.sh's *behaviour* here the way it did for #610 (where a crafted
stdin id could be paired with an UNRELATED, valid transcript_path,
decoupling the two): a transcript literally named "--dry.jsonl" hits
save-session.sh's own session-id-shape check
(``^[a-f0-9][a-f0-9-]*$``, save-session.sh:209-212) either way and exits 1
with "invalid session ID" rather than silently entering DRY_RUN. What this
test pins is narrower and still worth having: the ARGV save-session.sh
actually receives, which is the reason #610/#600/#576 guard this same class
of value at every entry point rather than trusting a downstream script to
keep saving it -- a downstream change that widens the arg loop or drops the
validate anchor would silently reopen exactly the #610 impact for this
route with no signal here.
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

REAL_SESSION_ID = "dddddddd-0000-4000-8000-000000000004"


def _fake_plugin_root(tmp_path: Path, ledger: Path) -> Path:
    """A plugin root that mirrors REPO_ROOT via symlinks for everything
    except scripts/save-session.sh, which is replaced with a stub that
    records its own argv (count and joined values) to `ledger` rather than
    doing a real save. This isolates the guard under test -- what argv
    post-tool-hook.sh's nohup call actually hands save-session.sh -- from
    save-session.sh's own downstream validation, which the module docstring
    above explains would otherwise mask the difference end-to-end."""
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
    """A project whose only session transcript is named ``basename`` -- the
    basename route is only reached when stdin never trusts a session id, so
    these tests send no stdin at all (the ordinary "old CLI" degrade path
    test_post_tool_session_id.py already pins)."""
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
    """No stdin at all -- the basename route is only reached when stdin never
    supplies a trusted session id."""
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


def test_a_flag_shaped_transcript_basename_must_not_reach_the_argv_unguarded(tmp_path):
    """MUST NOT FIRE: the exploit. The only transcript in this project's
    session dir is named --dry.jsonl. With no stdin, post-tool-hook.sh
    falls back to the basename route. Before the fix, the unguarded
    SESSION_ID -- literally --dry -- reaches save-session.sh's argv[1]
    unchanged. The stub records exactly what it received."""
    home, project, remember, ledger = _setup(tmp_path, basename="--dry.jsonl")
    plugin_root = _fake_plugin_root(tmp_path, ledger)

    result = _run(_env(home, project, remember, plugin_root))
    assert result.returncode == 0, result.stderr
    _reap(remember)

    assert ledger.exists(), "the background save never forked -- broken harness"
    argv_line = ledger.read_text().strip()
    assert argv_line != "1:--dry", (
        'the transcript basename "--dry" reached save-session.sh argv '
        f"unrejected: {argv_line!r} -- the basename route "
        "(post-tool-hook.sh:604-605) must reject a leading dash the same way "
        "the stdin route already does (post-tool-hook.sh:419-423, #610)."
    )


def test_an_ordinary_transcript_basename_still_reaches_the_argv_unchanged(tmp_path):
    """MUST FIRE (positive control): an ordinary UUID-shaped transcript
    basename must still reach save-session.sh's argv as itself -- proving
    the guard does not clear every session id, only the dangerous shape."""
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
