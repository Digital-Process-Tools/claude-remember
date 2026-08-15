"""The store-root index, so the slug record can be reached in the layout we ship (#297).

#294 stopped a non-bash caller from having to reimplement `session_dir_slug`, and
#296 wrote the answer down at `<REMEMBER_DIR>/tmp/session-slug`. That record is
reachable in the default layout, where `REMEMBER_DIR` is `<project>/.remember` and
a caller holding `project_dir` can name it.

It is *not* reachable in the layout `config.user.example.json` ships. That file's
own `_purpose` is "Copy this file to `~/.remember/config.json` to enable external
storage mode", and it carries `"data_dir": "~/.remember/{slug}"` — so
`REMEMBER_DIR` is itself named by the slug, and a caller must already know the
answer to open the file holding it. The hole was documented when #296 shipped; the
reporter of #297 pointed out that it is not an edge, it is the recommended
configuration, and that those are exactly the installs with a non-bash caller.

── Why an index and not a second copy of the record ─────────────────────────────

The obvious repair is a second copy at a path the slug does not name:
`<store_root>/tmp/session-slug-<key derived from project_dir>`. Any such key is a
second algorithm over `project_dir` that both sides must agree on — which is the
thing #294/#296 exist to delete, re-created one layer down.

The escape from "our own algorithm" is a language-neutral encoding, and it does
not survive this repo's own test data. base64 of an N-byte path is `4*ceil(N/3)`
bytes: the 260-character paths #294 was about encode to 348, and the 300-character
one in `tests/slug_vectors.py` to 400 — past the 255-byte filename limit of ext4,
APFS, NTFS and every other filesystem in play. Truncating the encoding to fit
means appending a hash, which is a second slug algorithm wearing a different name.

So the caller derives nothing. The index is keyed by a string the caller already
holds verbatim — `project_dir` — and it matches on it rather than computing from
it. That is also what the reporter's own directory scan does, and why their scan
"cannot produce a wrong answer, only fail to find one". This gives that property a
documented path and a file read instead of a directory listing.

── Where it lives, and where it deliberately does not ───────────────────────────

`<store_root>/tmp/sessions`, where `store_root` is the `data_dir` template
truncated at `{slug}` — a path a caller can name from the template alone, which is
the whole point.

It is written **only when the template contains `{slug}`**. In every other layout
the store root and `REMEMBER_DIR` coincide, `<REMEMBER_DIR>/tmp/session-slug` is
already nameable from `project_dir` and the template, and an index there would be
a second file saying the same thing with a second chance to disagree. The default
layout pays nothing for the external one.

── Three states, again ──────────────────────────────────────────────────────────

The same three states #296 established, one level up:

* **no index file** — nothing is claimed. Older version, default layout, or the
  hook never ran;
* **an index with no row for your `project_dir`** — this store has never seen that
  project. Explicitly not an answer, and never an empty slug;
* **a row, `status=ok`, with a non-empty `slug=`** — the only usable case.

An unrepresentable `project_dir` (a newline is legal on POSIX and the index is
line-based) gets **no row**, because a row cannot be keyed by a value it cannot
hold. The per-project record still takes `status=unavailable` for that project, as
#296 defined. A caller holding such a path finds no row and falls back — the same
outcome as never having run, which is the honest one.

── Staleness ────────────────────────────────────────────────────────────────────

No timestamps, for the reason `_remember_write_slug_record` gives: the slug is a
pure function of the path, so age cannot make a row wrong. A row for a deleted
directory can never be *matched* by a caller holding a live `project_dir`, and if
that path is ever recreated the row is still correct. Rows are therefore never
expired by age and never pruned by testing whether the directory exists — an
unmounted network share would lose a correct row to that test.

Position in the file is last-write order, maintained by the rewrite, and it is
used for one thing only: which rows to drop at the cap. It is never a staleness
test, and no reader may treat it as one.
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
LIB_MEMORY_DIR = REPO_ROOT / "scripts" / "lib-memory-dir.sh"
GIT_BACKUP = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"

INDEX_NAME = "sessions"
RECORD_NAME = "session-slug"
MAX_ROWS = 1000
SESSION_ID = "eeeeeeee-0000-4000-8000-000000000297"


# ── Harness ──────────────────────────────────────────────────────────────────


def _payload(session_id: str = SESSION_ID) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })


def _configure(home: Path, data_dir: str) -> None:
    """Write the user-global config, the file `config.user.example.json` is copied to."""
    cfg_dir = home / ".remember"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"data_dir": data_dir, "features": {"recovery": False}}),
        encoding="utf-8",
    )


def _project(home: Path, root: Path, name: str) -> Path:
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True, exist_ok=True)
    return project


def _run(home: Path, project: Path, session_id: str = SESSION_ID):
    env = {k: v for k, v in os.environ.items()
           if k not in ("REMEMBER_DIR", "REMEMBER_STORE_ROOT", "_LIB_MEMORY_DIR_LOADED")}
    env.update({
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    })
    return subprocess.run(
        ["bash", str(SESSION_START)],
        env=env, input=_payload(session_id), capture_output=True, text=True, timeout=180,
    )


# ── The reader, written the way an external caller must write it ─────────────


def _store_root(template: str, home: Path) -> Path:
    """`data_dir` truncated at `{slug}`, `~` expanded. All a caller derives."""
    prefix = template.split("{slug}")[0]
    if prefix.startswith("~"):
        prefix = str(home) + prefix[1:]
    return Path(prefix.rstrip("/") or "/")


def _index(store_root: Path):
    """The index as a caller parses it, or None when there is no index at all.

    Line 1 is `format=1`. Every later line is one row, tab-separated, with
    `project_dir=` LAST — so a tab inside a project path stays inside the field
    that may hold one. Hence the bounded split.
    """
    path = store_root / "tmp" / INDEX_NAME
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "format=1", lines[:1]
    rows = []
    for line in lines[1:]:
        if not line:
            continue
        row = {}
        for field in line.split("\t", 3):
            key, _, value = field.partition("=")
            row[key] = value
        rows.append(row)
    return rows


def _lookup(store_root: Path, project: Path):
    rows = _index(store_root)
    if rows is None:
        return None
    for row in rows:
        if row.get("project_dir") == str(project):
            return row
    return None


def _record(remember_dir: Path) -> dict:
    path = remember_dir / "tmp" / RECORD_NAME
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


# ── The feature ──────────────────────────────────────────────────────────────


def test_the_slug_is_reachable_from_the_template_and_project_dir_alone(tmp_path):
    """The reporter's exact position: they hold the `data_dir` template and
    `project_dir`, and nothing else. That must be enough."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    project = _project(home, tmp_path, "proj")

    result = _run(home, project)
    assert result.returncode == 0, result.stderr

    row = _lookup(_store_root(template, home), project)
    assert row is not None, "no row for this project"
    assert row["status"] == "ok"
    assert row["slug"] == _slug(str(project))
    assert row["memory_dir"] == str(tmp_path / "store" / _slug(str(project)))


def test_the_layout_config_user_example_ships_works_verbatim(tmp_path):
    """`~/.remember/{slug}`, `~` and all — the string in the file whose `_purpose`
    is to be copied to `~/.remember/config.json`."""
    home = tmp_path / "home"
    template = "~/.remember/{slug}"
    _configure(home, template)
    project = _project(home, tmp_path, "proj")

    assert _run(home, project).returncode == 0
    row = _lookup(_store_root(template, home), project)
    assert row is not None and row["status"] == "ok"
    assert row["slug"] == _slug(str(project))


def test_the_row_is_the_same_answer_the_record_gives(tmp_path):
    """Not a re-derivation. Two files disagreeing about one slug would be the
    #294 defect with our own filenames on it."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    project = _project(home, tmp_path, "proj")
    _run(home, project)

    row = _lookup(_store_root(template, home), project)
    rec = _record(Path(row["memory_dir"]))
    assert rec["status"] == "ok"
    assert row["slug"] == rec["slug"]
    assert row["memory_dir"] == rec["memory_dir"]


# ── One store, many projects ─────────────────────────────────────────────────


def test_a_second_project_does_not_displace_the_first(tmp_path):
    """The whole difference from the per-project record: this file is shared, so
    the last writer must add to it rather than own it."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    a = _project(home, tmp_path, "alpha")
    b = _project(home, tmp_path, "beta")

    _run(home, a)
    _run(home, b)

    root = _store_root(template, home)
    assert _lookup(root, a)["slug"] == _slug(str(a))
    assert _lookup(root, b)["slug"] == _slug(str(b))


def test_starting_the_same_project_again_updates_its_row_rather_than_adding_one(tmp_path):
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    project = _project(home, tmp_path, "proj")

    _run(home, project)
    _run(home, project, session_id="eeeeeeee-0000-4000-8000-000000000298")

    rows = _index(_store_root(template, home))
    mine = [r for r in rows if r.get("project_dir") == str(project)]
    assert len(mine) == 1, rows


def test_concurrent_session_starts_do_not_lose_a_row(tmp_path):
    """Several projects — and several worktrees of one project — start sessions
    against one store root. A read-modify-write with no story here loses rows
    silently, which is the failure this file cannot have."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    projects = [_project(home, tmp_path, f"p{i}") for i in range(6)]

    procs = []
    for i, project in enumerate(projects):
        env = {k: v for k, v in os.environ.items()
               if k not in ("REMEMBER_DIR", "REMEMBER_STORE_ROOT", "_LIB_MEMORY_DIR_LOADED")}
        env.update({
            "HOME": str(home),
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        })
        procs.append(subprocess.Popen(
            ["bash", str(SESSION_START)],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True,
        ))
    for proc, project in zip(procs, projects):
        proc.communicate(input=_payload(f"eeeeeeee-0000-4000-8000-00000000030{projects.index(project)}"),
                         timeout=180)

    root = _store_root(template, home)
    missing = [str(p) for p in projects if _lookup(root, p) is None]
    assert not missing, f"rows lost under concurrency: {missing}"


# ── Three states ─────────────────────────────────────────────────────────────


def test_an_index_without_your_row_is_not_an_answer(tmp_path):
    """State two. The index exists because another project wrote it; that says
    nothing about yours, and must not read as an empty slug."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    a = _project(home, tmp_path, "alpha")
    _run(home, a)

    never_started = tmp_path / "gamma"
    root = _store_root(template, home)
    assert _index(root) is not None, "state one, not state two"
    assert _lookup(root, never_started) is None


def test_a_project_dir_the_index_cannot_hold_is_left_out_rather_than_written_wrong(tmp_path):
    """A newline is legal in a POSIX path and the index is line-based, so a row
    keyed by such a path would split into two rows that both parse and one of
    which is false. There is no way to name it in this file, so it gets no row —
    and the per-project record still says `status=unavailable`, as #296 defined."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    ok = _project(home, tmp_path, "alpha")
    bad = _project(home, tmp_path, "pro\nject")

    _run(home, ok)
    assert _run(home, bad).returncode == 0

    root = _store_root(template, home)
    rows = _index(root)
    assert _lookup(root, ok)["slug"] == _slug(str(ok))
    assert all("\n" not in r.get("project_dir", "") for r in rows)
    assert _lookup(root, bad) is None

    rec = _record(Path(str(tmp_path / "store") + "/" + _slug(str(bad))))
    assert rec.get("status") == "unavailable"
    assert "slug" not in rec


def test_every_row_carries_a_status_and_a_non_empty_slug(tmp_path):
    """An empty slug resolves to `~/.claude/projects/` itself, a directory that
    exists and holds every project's transcripts. A row that spelled an absence
    that way would be the worst outcome wearing the best one's label."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    _run(home, _project(home, tmp_path, "proj"))

    for row in _index(_store_root(template, home)):
        assert row.get("status") == "ok", row
        assert row.get("slug"), row


# ── The layouts that must pay nothing ────────────────────────────────────────


def test_the_default_layout_gets_no_index(tmp_path):
    """`<project>/.remember`: `REMEMBER_DIR` is nameable from `project_dir` and
    the template, so the record is already reachable. A second file here would be
    a second answer with a second chance to disagree."""
    home = tmp_path / "home"
    _configure(home, ".remember")
    project = _project(home, tmp_path, "proj")

    assert _run(home, project).returncode == 0
    remember = project / ".remember"
    assert _record(remember)["slug"] == _slug(str(project))
    assert not (remember / "tmp" / INDEX_NAME).exists()
    assert not (project / "tmp" / INDEX_NAME).exists()


def test_a_store_without_a_slug_placeholder_gets_no_index(tmp_path):
    """External, but one directory for every project: store root and
    `REMEMBER_DIR` coincide, so `<REMEMBER_DIR>/tmp/session-slug` is nameable and
    there is nothing an index could add."""
    home = tmp_path / "home"
    shared = tmp_path / "shared"
    _configure(home, str(shared))
    project = _project(home, tmp_path, "proj")

    assert _run(home, project).returncode == 0
    assert _record(shared)["slug"] == _slug(str(project))
    assert not (shared / "tmp" / INDEX_NAME).exists()


# ── Staleness and growth ─────────────────────────────────────────────────────


def test_a_row_outlives_its_project_directory(tmp_path):
    """Deliberate, and the same reasoning as the record carrying no timestamp: the
    slug is a pure function of the path. A row for a path that no longer exists
    can never be matched by a caller holding a live `project_dir`, and if the path
    is recreated the row is still right. Pruning by existence would instead delete
    correct rows for anything on an unmounted share."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    gone = _project(home, tmp_path, "gone")
    stays = _project(home, tmp_path, "stays")

    _run(home, gone)
    gone.rmdir()
    _run(home, stays)

    root = _store_root(template, home)
    assert _lookup(root, gone) is not None, "a correct row was pruned"
    assert _lookup(root, stays) is not None


def test_the_row_carries_no_timestamp(tmp_path):
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    project = _project(home, tmp_path, "proj")
    _run(home, project)

    row = _lookup(_store_root(template, home), project)
    assert not any(k in row for k in ("written_at", "timestamp", "mtime", "age"))


def test_the_index_is_bounded_and_drops_the_least_recently_written(tmp_path):
    """Nothing expires rows, so the only bound is a cap — and with no timestamp
    the ordering that decides what to drop is position, maintained by the
    rewrite. Dropping a row costs a caller one fallback; it never produces a
    wrong answer."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    project = _project(home, tmp_path, "proj")

    root = _store_root(template, home)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    seeded = [f"/synthetic/{i}" for i in range(MAX_ROWS + 50)]
    (root / "tmp" / INDEX_NAME).write_text(
        "format=1\n" + "".join(
            f"status=ok\tslug=-synthetic-{i}\tmemory_dir=/nope\tproject_dir={p}\n"
            for i, p in enumerate(seeded)
        ),
        encoding="utf-8",
    )

    _run(home, project)

    rows = _index(root)
    assert len(rows) <= MAX_ROWS, len(rows)
    assert _lookup(root, project) is not None, "the writer dropped its own row"
    assert _lookup(root, Path(seeded[0])) is None, "the oldest row survived the cap"
    assert _lookup(root, Path(seeded[-1])) is not None, "a recent row was dropped"


# ── The costs this must not add ──────────────────────────────────────────────


def test_the_per_tool_call_path_is_not_touched():
    """`session_dir_slug` runs on EVERY tool call. #296 put nothing on that path
    and said so with a test rather than a promise; this must not be the change
    that quietly ends that."""
    fragment = f"tmp/{INDEX_NAME}"
    assert fragment not in POST_TOOL.read_text(encoding="utf-8")
    assert fragment not in LIB_SLUG.read_text(encoding="utf-8")


@pytest.mark.parametrize("template,expected", [
    ("/a/b/{slug}", "/a/b"),
    ("/a/b/{slug}/data", "/a/b"),
    ("/a/b//{slug}", "/a/b"),
    ("~/.remember/{slug}", "$HOME/.remember"),
    ("C:/dev/.remember/{slug}", "C:/dev/.remember"),
    # No placeholder: REMEMBER_DIR is the store root, and the record in it is
    # already nameable. Nothing to publish.
    (".remember", ""),
    ("/a/b", ""),
    # A store root of "/" or a bare drive would put a plugin-owned file at
    # /tmp/sessions — a shared, world-writable directory. Refused.
    ("/{slug}", ""),
    ("C:/{slug}", ""),
])
def test_the_store_root_is_the_template_truncated_at_the_placeholder(tmp_path, template, expected):
    """The caller's derivation and ours must be the same string operation, or the
    index is written where nobody looks for it."""
    home = tmp_path / "home"
    home.mkdir()
    script = (
        f'PROJECT_DIR="{tmp_path}"; PIPELINE_DIR="{tmp_path}"; HOME="{home}"; '
        f'source "{LIB_MEMORY_DIR}"; '
        f'_set_store_root "$1"; printf "%s" "$REMEMBER_STORE_ROOT"'
    )
    result = subprocess.run(["bash", "-c", script, "bash", template],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected.replace("$HOME", str(home))


def test_resolving_the_store_root_costs_no_fork():
    """`lib-memory-dir.sh` is sourced by `bootstrap-dirs.sh` and by `log.sh`,
    both of which `post-tool-hook.sh` reaches on the tool call that resolves —
    the first of a session, and the first after any config edit (#350 made the
    rest of them replay a published resolution instead). `session-start-hook.sh`
    and `save-session.sh` reach it unconditionally. A `$(...)` here is one fork
    on every one of those — the cost #230 removed and #296 declined to add back.
    The helper therefore assigns rather than echoes, and this says so."""
    source = LIB_MEMORY_DIR.read_text(encoding="utf-8")
    assert "$(_set_store_root" not in source
    assert "`_set_store_root" not in source
    assert "_set_store_root" in source


def test_the_index_never_reaches_a_backup_remote():
    """The git backup's repo root IS the store root (`dirname "$REMEMBER_DIR"`),
    so unlike the per-project record this file sits at the top of the user's own
    backup repository. The plugin's own `git add` is pathspec-limited to
    `$SLUG/` and cannot take it, but the user's `git add -A` can — and it names
    every project on one machine and their absolute paths, which is #285's
    subject exactly."""
    backup = GIT_BACKUP.read_text(encoding="utf-8")
    assert '"/tmp/"' in backup, (
        "the store root's tmp/ must be excluded where the per-slug ones are"
    )
