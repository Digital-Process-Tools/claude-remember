"""The slug is written down once a session, so nobody has to fork bash for it (#294).

`session_dir_slug` is a pure function of `PROJECT_DIR`, which does not change
mid-session — but a non-bash caller had no way to ask for it except by sourcing
`scripts/lib-slug.sh` in a subshell, once per tool call. The reporter of #294
drives `remember` from PowerShell and answered that by maintaining a port, which
is how the long-path divergence was found: a second implementation of the one
function whose disagreements are silent.

So `session-start-hook.sh` writes the answer to `$REMEMBER_DIR/tmp/session-slug`,
beside the locks and the cooldown markers and the delivery record — per-clone
runtime state, already excluded from the git backup (#285), already created by
`bootstrap-dirs.sh`. The hook computes `PROJECT_PATH_SLUG` for its own use two
lines above, so this costs one `mv`; **the per-tool-call path is not touched at
all**, and a test below says so rather than leaving it to review.

── Three states, not two ────────────────────────────────────────────────────

This repo's recurring defect is an absence produced by the tool being read as an
absence in the world (#144, #156, #166, #253, #257). An empty slug is the sharp
end of it: it does not resolve to nothing, it resolves to `~/.claude/projects/`
**itself**, a directory that exists and holds every project's transcripts. A
reader that cannot tell "no slug was written" from "the slug is empty" will read
someone else's session and never know.

So the record always carries `status=`, and there are three answers:

* **the file is absent** — this plugin never wrote it (older version, or the
  session-start hook never ran). Nothing is claimed;
* **`status=unavailable`** with a `reason=`, and **no `slug=` key at all** — the
  hook ran and could not answer. Explicitly not an empty slug;
* **`status=ok`** with a non-empty `slug=`. The only case a caller may use.

── Staleness ────────────────────────────────────────────────────────────────

The file names one session's `PROJECT_DIR`, and one store can be written by more
than one project: git worktrees deliberately share a `REMEMBER_DIR` with the main
checkout (#56) while each worktree keeps its own `PROJECT_DIR`, so the last
session to start wins the file.

The rule that follows is short, and it is the one the README publishes:
**compare `project_dir`; ignore everything else.** A record written by a dead
session is not stale — the slug is a pure function of the path, so age cannot
make it wrong. A record written by a *different* project is wrong immediately,
however fresh. That is why the record carries no timestamp: a timestamp here
would only invite the wrong decision.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.slug import session_dir_slug as _slug  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
POST_TOOL = REPO_ROOT / "scripts" / "post-tool-hook.sh"
LIB_SLUG = REPO_ROOT / "scripts" / "lib-slug.sh"
GIT_BACKUP = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"

RECORD_NAME = "session-slug"
SESSION_ID = "dddddddd-0000-4000-8000-000000000004"


def _payload(session_id: str = SESSION_ID) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })


def _project(tmp_path: Path, name: str = "project"):
    home = tmp_path / "home"
    project = tmp_path / name
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    return home, project, remember


def _run(home: Path, project: Path, remember: Path):
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    return subprocess.run(
        ["bash", str(SESSION_START)],
        env=env, input=_payload(), capture_output=True, text=True, timeout=120,
    )


def _record(remember: Path) -> dict:
    """The record as a caller would parse it: one `key=value` per line."""
    path = remember / "tmp" / RECORD_NAME
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        key, _, value = line.partition("=")
        out[key] = value
    return out


# ── The feature ──────────────────────────────────────────────────────────────


def test_the_slug_is_written_where_a_caller_that_is_not_bash_can_read_it(tmp_path):
    home, project, remember = _project(tmp_path)
    result = _run(home, project, remember)
    assert result.returncode == 0, result.stderr

    rec = _record(remember)
    assert rec.get("status") == "ok", rec
    assert rec.get("slug") == _slug(str(project))
    assert rec.get("project_dir") == str(project)
    assert rec.get("memory_dir") == str(remember)
    assert rec.get("sessions_dir") == str(
        home / ".claude" / "projects" / _slug(str(project))
    )
    assert rec.get("format") == "1"


def test_the_record_is_the_same_answer_the_shell_function_gives(tmp_path):
    """Not `pipeline/slug.py`'s answer, and not a re-derivation — the value the
    hook itself computed. Two implementations agreeing is what #157/#158/#174
    were about; publishing a third would be the same mistake with a file."""
    home, project, remember = _project(tmp_path)
    _run(home, project, remember)

    shell = subprocess.run(
        ["bash", "-c", f'PIPELINE_DIR="{REPO_ROOT}"; source "{LIB_SLUG}"; session_dir_slug "$1"',
         "bash", str(project)],
        capture_output=True, text=True, timeout=60,
    )
    assert shell.returncode == 0, shell.stderr
    assert _record(remember)["slug"] == shell.stdout.rstrip("\n")


def test_the_record_carries_no_timestamp(tmp_path):
    """Deliberate. The slug is a pure function of the path, so age cannot make
    a record wrong, and only `project_dir` can. A timestamp would offer a
    reader a staleness test that answers the wrong question."""
    home, project, remember = _project(tmp_path)
    _run(home, project, remember)
    rec = _record(remember)
    assert not any(k in rec for k in ("written_at", "timestamp", "mtime", "age"))


# ── Three states ─────────────────────────────────────────────────────────────


def test_an_unwritable_record_leaves_no_record_rather_than_an_empty_slug(tmp_path):
    """State one: absent. The hook must not leave a truncated or half-written
    file behind, because `slug=` read off one resolves to the projects root."""
    home, project, remember = _project(tmp_path)
    tmp_dir = remember / "tmp"
    tmp_dir.chmod(0o500)
    try:
        result = _run(home, project, remember)
        assert result.returncode == 0, result.stderr
        assert not (tmp_dir / RECORD_NAME).exists()
    finally:
        tmp_dir.chmod(0o700)


def test_a_project_dir_the_record_cannot_hold_says_so_instead_of_lying(tmp_path):
    """State two: `status=unavailable`, and **no `slug=` key**.

    A newline is a legal byte in a POSIX filename and the record is line-based,
    so a project path containing one would put `slug=…` on a line of its own
    inside the value of `project_dir=` — a record that parses cleanly and says
    something false. The hook refuses instead, and says which state it is in.
    """
    home, project, remember = _project(tmp_path, name="pro\nject")
    result = _run(home, project, remember)
    assert result.returncode == 0, result.stderr

    rec = _record(remember)
    assert rec.get("status") == "unavailable", rec
    assert rec.get("reason"), "an unavailable record must say why"
    assert "slug" not in rec, "an absence must never be spelled as a slug"


def test_status_ok_always_carries_a_non_empty_slug(tmp_path):
    """State three, and the invariant that makes the other two worth having.

    An empty slug resolves to `~/.claude/projects/` itself — a directory that
    exists and holds every project's transcripts — so `status=ok` with an empty
    value would be the worst of the three outcomes wearing the best one's label.
    """
    home, project, remember = _project(tmp_path)
    _run(home, project, remember)
    rec = _record(remember)
    if rec.get("status") == "ok":
        assert rec.get("slug")


# ── Staleness: one store, more than one project ──────────────────────────────


def test_a_record_left_by_another_project_is_replaced_not_merged(tmp_path):
    """Worktrees share a REMEMBER_DIR with the main checkout (#56) while keeping
    their own PROJECT_DIR, so the last session to start owns this file. It must
    therefore describe exactly one project, completely — no fields surviving
    from the previous writer."""
    home, project, remember = _project(tmp_path)
    (remember / "tmp" / RECORD_NAME).write_text(
        "format=1\nstatus=ok\nproject_dir=/somewhere/else\nslug=-somewhere-else\n"
        "sessions_dir=/nope\nmemory_dir=/nope\nsession_id=stale\nleftover=yes\n",
        encoding="utf-8",
    )
    _run(home, project, remember)

    rec = _record(remember)
    assert rec["project_dir"] == str(project)
    assert rec["slug"] == _slug(str(project))
    assert "leftover" not in rec, "the record was merged, not replaced"
    assert rec.get("session_id") != "stale"


# ── The costs this must not add ──────────────────────────────────────────────


def test_the_per_tool_call_path_is_not_touched():
    """`session_dir_slug` runs on EVERY tool call, and `lib-slug.sh` avoids
    forks in its common path for that reason (#144). Writing the slug once a
    session must not put a single byte of this feature on that path — and the
    cheapest way to keep that true is to assert it, not to remember it."""
    assert RECORD_NAME not in POST_TOOL.read_text(encoding="utf-8")
    assert RECORD_NAME not in LIB_SLUG.read_text(encoding="utf-8")


def test_the_record_never_reaches_a_backup_remote():
    """It names one machine's session and one machine's absolute paths. #285 is
    what happens when per-clone state is committed like memory: another
    machine's first session read it as its own history."""
    backup = GIT_BACKUP.read_text(encoding="utf-8")
    assert '"/$SLUG/tmp/"' in backup, (
        "the record lives in tmp/ precisely because that directory is excluded; "
        "if the exclusion moved, this file moves with it"
    )
