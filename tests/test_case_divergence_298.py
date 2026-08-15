"""The store is known by more than one spelling, and only case tells them apart (#298).

── What this is NOT ─────────────────────────────────────────────────────────────

#298 was filed on a premise that did not survive measurement. It claimed a store
created under the pre-#263 drive-letter spelling (`C--Users-…`) is orphaned by the
post-#263 resolver (`c--Users-…`), stranding the memory written before the upgrade.

@jqit-ricky has the only Windows box any of this can be pointed at, and measured
it instead of arguing about it: `Test-Path` on both spellings returns `True` for
the **same directory object**, with 88 files reachable through either name. NTFS is
case-insensitive, so two stores differing only in case is not a state that
filesystem can be in. Nothing is stranded, the plugin reads that store, and the
orphaning cannot happen on the platform #263 came from.

So there is nothing here to migrate, nothing to merge, and nothing to rename. This
file tests **disclosure only**, and `test_nothing_here_repairs_anything` is the
guard that keeps it that way.

── What survived, and it is not what was filed ──────────────────────────────────

Git's index is case-sensitive where NTFS is not. On the reporter's machine:

    on disk : C--Users-ricky-dev-jqit-project-b
    in git  : c--Users-ricky-dev-jqit-project-b

That costs nothing while the store stays on NTFS. It stops being free on a
case-sensitive filesystem, and the git backup exists precisely so the store can be
restored somewhere else. Verified on a real case-sensitive APFS volume rather than
reasoned about: one repository carrying both spellings, one plain `git clone`, and

    out/C--proj/remember.md
    out/c--proj/remember.md

two directories, two `remember.md`, and a resolver that reads exactly one of them.

── What is checked, and what deliberately is not ────────────────────────────────

Two probes over one question — *is this store known by a second spelling that
differs only in case?* — asked of the two places that can answer:

* **disk** — the directory names in the store root that case-fold to the one we
  resolved. On a case-insensitive filesystem there is exactly one entry and the
  question is whether it is spelled the way we resolved it; that is the state the
  reporter measured, and it is the reason this is not implemented as "does a
  sibling store root exist". On NTFS there is never a sibling, so a sibling check
  would be dead code on the platform it was written for.

* **git** — the top-level names in `HEAD`'s tree of the store's own repository.
  `HEAD`'s tree and not the history: a clone checks out `HEAD`, so **history
  carrying both spellings is inert while `HEAD` carries one**. That distinction is
  load-bearing and is why this does not run `git log --all`.

Whether any real store's *history* holds both spellings is still unknown, and
nothing here assumes an answer.

── Three states, and the fourth that is not a state ─────────────────────────────

`ok` (compared, they agree) / `diverged` / `unavailable` (could not compare —
no git, not a repository, nothing committed). An absence produced by the check
must never read as agreement; that conflation is this repo's recurring defect and
the reason #296 and #299 look the way they do.

`not-applicable` is separate from all three and means the comparison does not
exist: the legacy layout, where `REMEMBER_DIR` is `<project>/.remember` inside the
user's own repository. That is not "we looked and found nothing", it is "there is
nothing here that could diverge".

── The tone the message has to hold ─────────────────────────────────────────────

On the reporter's machine this condition is TRUE and completely harmless today.
A reader must not be able to conclude the opposite of the truth from it: it is a
heads-up about a restore, not a report that memory is broken.
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
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"
LIB = REPO_ROOT / "scripts" / "lib-case-divergence.sh"
POST_TOOL = REPO_ROOT / "scripts" / "post-tool-hook.sh"
LIB_SLUG = REPO_ROOT / "scripts" / "lib-slug.sh"
LIB_MEMORY_DIR = REPO_ROOT / "scripts" / "lib-memory-dir.sh"

RECORD_NAME = "case-divergence"
NOTICE_NAME = "case-divergence-notice"
SESSION_ID = "eeeeeeee-0000-4000-8000-000000000298"


# ── Harness ──────────────────────────────────────────────────────────────────


def _other_spelling(slug: str) -> str:
    """The same slug with the case of its first letter flipped.

    Not `slug[0].swapcase()`: a POSIX slug begins with a dash (the leading `/`
    of the path), and `"-".upper()` is `"-"` — a helper that quietly returned
    the slug unchanged would make every assertion below vacuous, and did until
    a doctor test reported OK on a store it had been told to break. The Windows
    form this issue is about (`c--Users-…`) has its letter first; the POSIX form
    these tests can build does not, and both must exercise the same code.
    """
    for i, ch in enumerate(slug):
        if ch.isalpha():
            return slug[:i] + ch.swapcase() + slug[i + 1:]
    raise AssertionError(f"no letter to re-case in {slug!r}")


def _payload(session_id: str = SESSION_ID) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })


def _configure(home: Path, data_dir: str) -> None:
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


def _env(home: Path, project: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("REMEMBER_DIR", "REMEMBER_STORE_ROOT", "_LIB_MEMORY_DIR_LOADED")}
    env.update({
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    })
    return env


def _run(home: Path, project: Path, session_id: str = SESSION_ID):
    return subprocess.run(
        ["bash", str(SESSION_START)],
        env=_env(home, project), input=_payload(session_id),
        capture_output=True, text=True, timeout=180,
    )


def _doctor(home: Path, project: Path):
    return subprocess.run(
        ["bash", str(DOCTOR)], env=_env(home, project),
        capture_output=True, text=True, timeout=180,
    )


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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _external_store(tmp_path: Path, name: str = "proj"):
    """The layout `config.user.example.json` ships, with a real store root."""
    home = tmp_path / "home"
    template = str(tmp_path / "store") + "/{slug}"
    _configure(home, template)
    project = _project(home, tmp_path, name)
    store_root = tmp_path / "store"
    store_root.mkdir(parents=True, exist_ok=True)
    return home, project, store_root, store_root / _slug(str(project))


def _init_repo(store_root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(store_root)], check=True)


def _commit_tracked_name(store_root: Path, name: str) -> None:
    """Put `name/remember.md` into HEAD without needing it on disk.

    A blob written straight into the index is the only way to build the state
    under test on a case-insensitive filesystem, which is every machine this repo
    is developed on and most of the runners. It is also the *honest* way: the
    condition being detected is a name in git's index, and git's index is exactly
    what this writes. A case-sensitive volume can be built on macOS with
    `hdiutil create -fs "Case-sensitive APFS"` — the maintainer did, to confirm
    the clone really does produce two directories — but a test that needs one
    cannot run on a CI runner, and this needs no volume at all.
    """
    blob = subprocess.run(["git", "-C", str(store_root), "hash-object", "-w", "--stdin"],
                          input="memory\n", capture_output=True, text=True,
                          check=True).stdout.strip()
    _git(store_root, "-c", "core.ignorecase=false", "update-index", "--add",
         "--cacheinfo", f"100644,{blob},{name}/remember.md")
    _git(store_root, "commit", "-qm", f"add {name}")


# ── They agree ───────────────────────────────────────────────────────────────


def test_a_store_spelled_one_way_everywhere_reports_ok(tmp_path):
    """The overwhelmingly common case, and the one that must stay quiet."""
    home, project, store_root, remember = _external_store(tmp_path)
    _init_repo(store_root)
    _run(home, project)
    _commit_tracked_name(store_root, _slug(str(project)))
    _run(home, project)

    rec = _record(remember)
    assert rec.get("status") == "ok", rec
    assert rec.get("disk_state") == "ok", rec
    assert rec.get("git_state") == "ok", rec
    assert not (remember / "tmp" / NOTICE_NAME).exists(), "notified about nothing"


def test_other_stores_in_the_root_are_not_our_business(tmp_path):
    """Several projects share one store root. A neighbour whose own name differs
    in case from ours is a different project, not a second spelling of this one —
    and reporting on it would be the sibling scan the reporter argued against."""
    home, project, store_root, remember = _external_store(tmp_path, "proj")
    _init_repo(store_root)
    (store_root / "C--somebody-elses-project").mkdir()
    (store_root / "c--somebody-elses-project-too").mkdir()
    _run(home, project)
    _commit_tracked_name(store_root, _slug(str(project)))
    _run(home, project)

    assert _record(remember).get("status") == "ok", _record(remember)


# ── Git names it differently ─────────────────────────────────────────────────


def test_git_carrying_both_spellings_is_disclosed(tmp_path):
    """The state that splits a restore: `HEAD` holds two names differing only in
    case, so a case-sensitive checkout materialises two directories and the
    resolver reads one of them."""
    home, project, store_root, remember = _external_store(tmp_path)
    _init_repo(store_root)
    _run(home, project)
    slug = _slug(str(project))
    other = _other_spelling(slug)
    _commit_tracked_name(store_root, slug)
    _commit_tracked_name(store_root, other)
    _run(home, project)

    rec = _record(remember)
    assert rec.get("status") == "diverged", rec
    assert rec.get("git_state") == "diverged", rec
    assert other in rec.get("git_names", "").split(","), rec
    assert rec.get("resolved") == slug, rec


def test_git_carrying_only_the_other_spelling_is_disclosed(tmp_path):
    """A store whose backup repository still names it the pre-#263 way. A restore
    produces one directory this plugin will not read, which looks exactly like a
    store that was never written to."""
    home, project, store_root, remember = _external_store(tmp_path)
    _init_repo(store_root)
    _run(home, project)
    slug = _slug(str(project))
    other = _other_spelling(slug)
    _commit_tracked_name(store_root, other)
    _run(home, project)

    rec = _record(remember)
    assert rec.get("status") == "diverged", rec
    assert rec.get("git_state") == "diverged", rec
    assert other in rec.get("git_names", "").split(","), rec


def test_a_tracked_name_that_merely_looks_similar_does_not_fire(tmp_path):
    """Only a case-fold match counts. A comparison that fires on ordinary
    differences is worse than no comparison at all."""
    home, project, store_root, remember = _external_store(tmp_path)
    _init_repo(store_root)
    _run(home, project)
    slug = _slug(str(project))
    _commit_tracked_name(store_root, slug)
    _commit_tracked_name(store_root, slug + "-2")
    _commit_tracked_name(store_root, slug[:-1])
    _run(home, project)

    assert _record(remember).get("status") == "ok", _record(remember)


# ── The disk names it differently ────────────────────────────────────────────


def test_a_store_directory_spelled_differently_on_disk_is_disclosed(tmp_path):
    """The reporter's own machine, reproduced without their machine: the store
    directory exists under the pre-#263 spelling, and the resolver computes the
    post-#263 one.

    On a case-insensitive filesystem the pre-created directory IS the one the hook
    then writes into, and the store root holds a single entry spelled the other
    way. On a case-sensitive one there are two entries. Both are the same finding
    — this store is known by a second spelling — so the assertion is on the
    finding, not on the count, and this test needs no particular filesystem.
    """
    home, project, store_root, remember = _external_store(tmp_path)
    slug = _slug(str(project))
    other = _other_spelling(slug)
    (store_root / other).mkdir(parents=True)
    _init_repo(store_root)
    _run(home, project)

    rec = _record(store_root / slug) or _record(store_root / other)
    assert rec.get("status") == "diverged", rec
    assert rec.get("disk_state") == "diverged", rec
    assert other in rec.get("disk_names", "").split(","), rec


# ── Could not check is not agreement ─────────────────────────────────────────


def test_a_store_that_is_not_a_repository_cannot_report_git_ok(tmp_path):
    """There is no tracked path to compare against, so there is no agreement to
    report. Saying `ok` here would be the defect #296 and #299 are about."""
    home, project, store_root, remember = _external_store(tmp_path)
    _run(home, project)

    rec = _record(remember)
    assert rec.get("git_state") == "unavailable", rec
    assert rec.get("git_reason") == "not-a-repository", rec
    assert rec.get("status") != "ok", rec
    assert rec.get("status") == "unavailable", rec


def test_a_repository_with_nothing_committed_cannot_report_git_ok(tmp_path):
    home, project, store_root, remember = _external_store(tmp_path)
    _init_repo(store_root)
    _run(home, project)

    rec = _record(remember)
    assert rec.get("git_state") == "unavailable", rec
    assert rec.get("git_reason") == "nothing-committed", rec
    assert rec.get("status") == "unavailable", rec


def test_a_divergence_on_disk_outranks_an_unavailable_git(tmp_path):
    """`unavailable` may never swallow a `diverged` that another probe found."""
    home, project, store_root, remember = _external_store(tmp_path)
    slug = _slug(str(project))
    other = _other_spelling(slug)
    (store_root / other).mkdir(parents=True)
    _run(home, project)

    rec = _record(store_root / slug) or _record(store_root / other)
    assert rec.get("git_state") == "unavailable", rec
    assert rec.get("status") == "diverged", rec


def test_the_legacy_layout_is_not_applicable_rather_than_ok(tmp_path):
    """`<project>/.remember` inside the user's own repository. The store is not
    named by the slug and is not its own backup repository, so there is nothing
    that could diverge — and nothing this check may go poking at either."""
    home = tmp_path / "home"
    _configure(home, ".remember")
    project = _project(home, tmp_path, "proj")
    subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)

    _run(home, project)

    rec = _record(project / ".remember")
    assert rec.get("status") == "not-applicable", rec
    assert "git_state" not in rec, "inspected the user's own project repository"


# ── Disclosure only ──────────────────────────────────────────────────────────


def test_nothing_here_repairs_anything(tmp_path):
    """No rename, no merge, no migration. An allowlist of what the library may
    run, not a denylist of what it may not — a denylist has to guess the name of
    the next dangerous verb, and this is a poor place to guess."""
    body = LIB.read_text(encoding="utf-8")
    code = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("#"))
    for forbidden in ("mv ", "cp ", "rm ", "mkdir", "rmdir", "ln ",
                      "git mv", "git add", "git commit", "git checkout",
                      "git clone", "git fetch", "git pull", "git merge",
                      "git rebase", "git reset", "git push"):
        assert forbidden not in code, f"{forbidden!r} appears in {LIB.name}"


def test_a_divergent_store_is_left_exactly_as_it_was(tmp_path):
    home, project, store_root, remember = _external_store(tmp_path)
    slug = _slug(str(project))
    other = _other_spelling(slug)
    (store_root / other).mkdir(parents=True)
    (store_root / other / "remember.md").write_text("old memory\n", encoding="utf-8")
    _init_repo(store_root)
    _commit_tracked_name(store_root, other)

    _run(home, project)

    assert (store_root / other / "remember.md").read_text(encoding="utf-8") == "old memory\n"
    assert _git(store_root, "ls-tree", "--name-only", "HEAD").split() == [other]


# ── What the human is told ───────────────────────────────────────────────────


def test_the_notice_says_it_is_a_heads_up_and_not_a_fault(tmp_path):
    """On the reporter's machine this condition is true and entirely harmless.
    Someone reading the notice must not be able to conclude the opposite."""
    home, project, store_root, remember = _external_store(tmp_path)
    slug = _slug(str(project))
    other = _other_spelling(slug)
    (store_root / other).mkdir(parents=True)
    _run(home, project)

    notice_dir = store_root / slug if (store_root / slug).is_dir() else store_root / other
    notice = (notice_dir / "tmp" / NOTICE_NAME).read_text(encoding="utf-8")
    lowered = notice.lower()
    assert "case" in lowered
    assert "case-sensitive" in lowered, "does not say when it would matter"
    assert "restore" in lowered, "does not say what would trigger it"
    for alarming in ("broken", "lost", "corrupt", "stranded", "not being read"):
        assert alarming not in lowered, f"reads as an alarm: {alarming!r}"


def test_the_notice_is_written_once_rather_than_every_session(tmp_path):
    """A condition that never clears itself and is harmless today must not become
    wallpaper on the one channel the human actually sees."""
    home, project, store_root, remember = _external_store(tmp_path)
    slug = _slug(str(project))
    other = _other_spelling(slug)
    (store_root / other).mkdir(parents=True)
    _run(home, project)

    notice_dir = store_root / slug if (store_root / slug).is_dir() else store_root / other
    notice = notice_dir / "tmp" / NOTICE_NAME
    assert notice.exists()
    notice.unlink()  # what user-prompt-hook.sh does: consumed on read

    _run(home, project, session_id="eeeeeeee-0000-4000-8000-000000000299")
    assert not notice.exists(), "re-notified about an unchanged, harmless condition"


def test_the_notice_is_delivered_on_the_channel_the_human_sees(tmp_path):
    """`systemMessage` via user-prompt-hook.sh's notice loop, which is where every
    other finding a human must act on is delivered (#200, #253)."""
    body = (REPO_ROOT / "scripts" / "user-prompt-hook.sh").read_text(encoding="utf-8")
    assert NOTICE_NAME in body, f"{NOTICE_NAME} is never consumed by any hook"


# ── /remember:doctor ─────────────────────────────────────────────────────────


def test_doctor_reports_the_divergence(tmp_path):
    """A user who suspects their memory went missing had no way to check."""
    home, project, store_root, remember = _external_store(tmp_path)
    slug = _slug(str(project))
    other = _other_spelling(slug)
    (store_root / other).mkdir(parents=True)
    _init_repo(store_root)
    _commit_tracked_name(store_root, other)

    out = _doctor(home, project).stdout
    assert "Store spelling" in out, out
    assert other in out, out
    assert "WARN" in out, out


def test_doctor_says_could_not_check_rather_than_ok(tmp_path):
    home, project, store_root, remember = _external_store(tmp_path)

    out = _doctor(home, project).stdout
    assert "Store spelling" in out, out
    assert "could not" in out.lower(), out


def test_doctor_is_quiet_when_the_spellings_agree(tmp_path):
    home, project, store_root, remember = _external_store(tmp_path)
    _init_repo(store_root)
    remember.mkdir(parents=True, exist_ok=True)
    _commit_tracked_name(store_root, _slug(str(project)))

    out = _doctor(home, project).stdout
    line = [ln for ln in out.splitlines() if "Store spelling" in ln]
    assert line and line[0].startswith("OK"), out


# ── The hot path ─────────────────────────────────────────────────────────────


def test_the_per_tool_call_path_is_not_touched(tmp_path):
    """#299 asserts this and it stays asserted. `session_dir_slug` runs on every
    tool call; this check runs once per session and nowhere near it.

    For `post-tool-hook.sh` the property is now asserted directly rather than as
    byte-equality with `origin/main` (#322). Byte-equality says "nobody has
    edited this file since main", which is a different claim from "the hot path
    is still cheap" and fails in both directions: it stopped an unrelated
    one-token arithmetic guard in that file, and it goes vacuous the moment the
    edit it objected to lands on main. What #298/#299 actually care about is
    that the divergence check is unreachable from the per-tool-call path and
    that the path spawns no git, so that is what is checked.

    The other two arms keep the byte compare, over CODE LINES only (#350). They
    were never about prose: they exist so that nothing reaches into these two
    hot-path libraries and adds a spawn, and a comment cannot add one. Comparing
    whole files made the guard fire on a paragraph — this time on a comment in
    `lib-memory-dir.sh` that had itself become false, saying `bootstrap-dirs.sh`
    is sourced on every tool call when #350 had just stopped that. A guard that
    forbids correcting a stale comment is a guard arguing for the stale comment,
    and prose that no longer matches the code is this repo's own defect class.
    Strip the comments from both sides — the same one-liner the arm above
    already uses — and every executable byte stays pinned exactly as before.
    """
    body = POST_TOOL.read_text(encoding="utf-8")
    code = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "lib-case-divergence" not in code, (
        "the per-tool-call hook now sources the divergence library — it runs on "
        "every tool call and the check is a once-per-session cost (#299)"
    )
    assert "case_divergence" not in code, (
        "the per-tool-call hook now calls into the divergence check (#299)"
    )
    assert "git " not in code, (
        "the per-tool-call hook now spawns git — that is the cost #298 moved to "
        "session start in the first place"
    )
    base = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", "origin/main:scripts/post-tool-hook.sh"],
        capture_output=True, text=True)
    if base.returncode != 0:
        pytest.skip("origin/main not available")
    def _code(text: str) -> str:
        return "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("#"))

    for path, rel in ((LIB_SLUG, "scripts/lib-slug.sh"),
                      (LIB_MEMORY_DIR, "scripts/lib-memory-dir.sh")):
        ref = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"origin/main:{rel}"],
                             capture_output=True, text=True)
        assert ref.returncode == 0
        # The premise, asserted rather than assumed: a file that produced no
        # code lines would compare equal to anything and pin nothing.
        assert _code(ref.stdout).strip(), (
            f"{rel} on origin/main has no non-comment lines — this compare "
            "would pass against any file at all"
        )
        assert _code(ref.stdout) == _code(path.read_text(encoding="utf-8")), rel


def test_the_check_costs_one_git_invocation_at_most(tmp_path):
    """Session start is the right place for it, and it still has a budget."""
    body = LIB.read_text(encoding="utf-8")
    code = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("#"))
    assert code.count("git -C ") <= 1, "more than one git invocation"
