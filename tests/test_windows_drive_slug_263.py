"""The memory slug must not depend on which shape CLAUDE_PROJECT_DIR arrives in (#263).

`hooks.d/after_save/50-git-backup.sh` committed nothing for twelve days on a
Windows install while logging `nothing to commit … skip` every few minutes. The
store was fine; the pathspec was not.

Two independent halves, and the report contains evidence of both.

**The slug was not stable.** `resolve-paths.sh` normalised the drive letter only
for a POSIX-shaped path (`/c/Users/...`). The reporter's `CLAUDE_PROJECT_DIR`
arrived Windows-shaped with a lowercase drive (`c:/Users/ricky/...`), which that
regex cannot match, so nothing was converted. Their own log carries BOTH
spellings within one day — `C--Users-ricky-…` at 15:55 and `c--Users-ricky-…` at
18:00 — which they could not explain. It follows from the code: with `cygpath`
absent from the hook's PATH, `session_dir_slug`'s lowercasing is skipped
entirely, so the POSIX shape (uppercased by resolve-paths) and the Windows shape
(untouched) slug differently. With `cygpath` present both shapes converge and
there is no bug at all — which is why this needs a machine without it.

**Nothing could tell that apart from an empty store.** NTFS is case-insensitive,
so `REMEMBER_DIR` resolved and memory saved normally. Git's pathspecs are
case-sensitive, so `git add -- "$SLUG/"` matched nothing, and the guard right
after it took the *success* branch. That is this repo's recurring defect class:
an absence produced by the tool, read as an absence in the world. Same answer as
#252 and #253 — three states, not two.

Both halves are invisible on macOS and Linux by construction: `$OSTYPE` is never
`msys`, and there is no drive letter to get wrong. So the platform is simulated
rather than required — `OSTYPE` is forced, `cygpath` is a stub, and the git half
needs no Windows at all because git's pathspec casing is the same everywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess assertions — not portable to Windows runners (#79)",
)

from .test_git_backup_hook import hook_state  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"
LIB_SLUG_SH = REPO_ROOT / "scripts" / "lib-slug.sh"
HOOK = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"

# The reporter's project, and the slug their store is tracked under.
WIN_PROJECT = r"Users\ricky\dev\jqit\project-b"
EXPECTED_SLUG = "c--Users-ricky-dev-jqit-project-b"


# ── Half 1: the slug must not depend on the shape it arrives in ───────────────

# A stand-in for MSYS `cygpath`, covering the one direction session_dir_slug
# uses. `-w` converts to the native Win32 form and normalises the drive letter
# to upper case, which is what the real one does with every shape below.
CYGPATH_STUB = r"""#!/usr/bin/env bash
if [ "$1" = "-w" ]; then
    p="$2"
    p="${p//\//\\}"
    case "$p" in
        \\cygdrive\\?\\*)
            d=$(printf '%s' "${p:10:1}" | tr 'a-z' 'A-Z')
            printf '%s:\\%s\n' "$d" "${p:12}" ;;
        \\?\\*)
            d=$(printf '%s' "${p:1:1}" | tr 'a-z' 'A-Z')
            printf '%s:\\%s\n' "$d" "${p:3}" ;;
        ?:\\*)
            d=$(printf '%s' "${p:0:1}" | tr 'a-z' 'A-Z')
            printf '%s:\\%s\n' "$d" "${p:3}" ;;
        *) printf '%s\n' "$p" ;;
    esac
    exit 0
fi
printf '%s\n' "$2"
"""


def _slug_for(project_dir: str, *, ostype: str, with_cygpath: bool, tmp_path: Path) -> str:
    """PROJECT_DIR through resolve-paths.sh, then through session_dir_slug.

    This is the whole chain that names the memory store, run exactly as a hook
    runs it — except that `$OSTYPE` is forced and the existence check at the end
    of resolve-paths.sh cannot pass, because `C:\\Users\\...` is not a directory
    on a POSIX box. That check fails *after* the normalisation, and with
    REMEMBER_PATHS_SOFT_FAIL=1 it `return`s rather than exiting, so PROJECT_DIR
    still holds the normalised value in the calling shell. The alternative — a
    fake `C:` tree — would test a path no Windows machine ever has.
    """
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env["REMEMBER_PATHS_SOFT_FAIL"] = "1"

    if with_cygpath:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "cygpath"
        stub.write_text(CYGPATH_STUB, encoding="utf-8")
        stub.chmod(0o755)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    else:
        # A PATH with no cygpath on it, which is the reporter's situation and
        # the only one in which the two spellings can diverge.
        bindir = tmp_path / "nocygbin"
        bindir.mkdir(exist_ok=True)
        env["PATH"] = env["PATH"]

    script = (
        f"OSTYPE={ostype}\n"
        f'source "{RESOLVE_PATHS}" 2>/dev/null\n'
        f'source "{LIB_SLUG_SH}"\n'
        'printf "%s\\n" "$(session_dir_slug "$PROJECT_DIR")"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()


# Every shape Claude Code has been observed to hand a Git Bash / Cygwin hook.
# They name the same directory and must therefore name the same store.
SHAPES = [
    ("msys", "/c/" + WIN_PROJECT.replace("\\", "/")),
    ("msys", "c:/" + WIN_PROJECT.replace("\\", "/")),
    ("msys", "C:/" + WIN_PROJECT.replace("\\", "/")),
    ("msys", "c:\\" + WIN_PROJECT),
    ("msys", "C:\\" + WIN_PROJECT),
    ("cygwin", "/cygdrive/c/" + WIN_PROJECT.replace("\\", "/")),
]


@pytest.mark.parametrize("with_cygpath", [False, True], ids=["no-cygpath", "cygpath"])
@pytest.mark.parametrize("ostype,project_dir", SHAPES, ids=[s[1] for s in SHAPES])
def test_every_shape_of_project_dir_gives_the_same_slug(
    ostype, project_dir, with_cygpath, tmp_path
):
    """The defect, stated directly.

    `c:/Users/ricky/...` and `/c/Users/ricky/...` are the same directory. If they
    slug differently, the store splits in two and git can only ever see one of
    them — which is what produced twelve days of `nothing to commit`.
    """
    assert (
        _slug_for(project_dir, ostype=ostype, with_cygpath=with_cygpath, tmp_path=tmp_path)
        == EXPECTED_SLUG
    )


def test_the_drive_letter_stays_lower_case(tmp_path):
    """Which case is not a free choice — it is whatever the working majority
    already has on disk.

    `session_dir_slug` lowercases the drive deliberately, to match the
    `~/.claude/projects/<slug>/` directory Claude Code creates, and every Windows
    install that has `cygpath` on its PATH (the default in Git for Windows) has
    been producing the lowercase spelling all along. Uppercasing here would fix
    the reporter and rename the store under everyone else — trading a loud bug
    for a quiet one. So the stability above must be achieved at `c--`, not `C--`.
    """
    slug = _slug_for(
        "/c/" + WIN_PROJECT.replace("\\", "/"),
        ostype="msys",
        with_cygpath=True,
        tmp_path=tmp_path,
    )
    assert slug.startswith("c--"), slug


def test_a_posix_path_is_untouched(tmp_path):
    """The normalisation is Windows-only and must stay that way: on Linux and
    macOS `$OSTYPE` never matches, and a path with a colon in it is a legal
    POSIX filename, not a drive."""
    slug = _slug_for(
        "/home/ricky/dev/project-b", ostype="linux-gnu", with_cygpath=False, tmp_path=tmp_path
    )
    assert slug == "-home-ricky-dev-project-b", slug


# ── Half 2: a pathspec that cannot match is not an empty store ────────────────


def _git(repo: Path, args: list[str]) -> None:
    subprocess.run(["git", "-C", str(repo)] + args, check=True, capture_output=True)


def _store_with_tracked_slug(tmp_path: Path, tracked_slug: str):
    """A memory store repo with `tracked_slug/` committed, and a bare remote."""
    home = tmp_path / "home"
    store = home / ".remember"
    remote = home / ".remember-remote.git"
    store.mkdir(parents=True)
    _git(store, ["init", "-q", "-b", "main"])
    _git(store, ["config", "user.email", "test@test"])
    _git(store, ["config", "user.name", "Test"])
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _git(store, ["remote", "add", "origin", str(remote)])
    (store / ".gitignore").write_text(
        ".git-backup.lock\n.last-git-backup-ts\n.git-backup-remote\n*/logs/\n*/tmp/\n"
    )
    slug_dir = store / tracked_slug
    slug_dir.mkdir()
    (slug_dir / "recent.md").write_text("tracked memory\n")
    _git(store, ["add", "-A"])
    _git(store, ["commit", "-q", "-m", "init"])
    _git(store, ["push", "-q", "-u", "origin", "main"])
    return home, store


def _run_hook(store: Path, home: Path, slug: str, tmp_path: Path) -> str:
    """Run the backup hook for `slug` and return everything it logged.

    The hook does its work in a backgrounded subshell, so the log file is the
    only place the verdict lands.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    remember_dir = store / slug
    (remember_dir / "logs").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "remember-config.json"
    cfg.write_text('{"cooldowns": {"git_backup_seconds": 0}}')

    env = {
        **os.environ,
        "HOME": str(home),
        "PROJECT_DIR": str(project_dir),
        "PIPELINE_DIR": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember_dir),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_PROJECT": str(project_dir),
        "REMEMBER_CONFIG": str(cfg),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.run(["bash", str(HOOK)], env=env, capture_output=True, text=True, timeout=60)

    deadline = __import__("time").monotonic() + 20
    lock = hook_state(store, ".git-backup.lock")
    while __import__("time").monotonic() < deadline and lock.exists():
        __import__("time").sleep(0.1)

    logs = remember_dir / "logs"
    return "\n".join(
        p.read_text(errors="replace") for p in sorted(logs.glob("*.log"))
    )


def test_a_slug_that_cannot_match_its_tracked_spelling_is_not_reported_as_clean(tmp_path):
    """The reported failure, reproduced without Windows.

    The store is tracked under `C--Users-ricky-…`; this session computed
    `c--Users-ricky-…`. Git's pathspecs are case-sensitive on every platform, so
    `git add -- "c--…/"` stages nothing — exactly as it did for twelve days. The
    hook must not call that "nothing to commit": it has to say the pathspec
    cannot match, and name the spelling git actually holds.
    """
    home, store = _store_with_tracked_slug(tmp_path, "C--Users-ricky-dev-jqit-project-b")

    output = _run_hook(store, home, "c--Users-ricky-dev-jqit-project-b", tmp_path)

    assert "nothing to commit" not in output, (
        "a slug whose pathspec cannot match anything git tracks was reported as "
        f"an empty store — the #263 failure, unchanged:\n{output}"
    )
    assert "C--Users-ricky-dev-jqit-project-b" in output, (
        "the report must name the spelling git actually tracks, or it is not "
        f"actionable:\n{output}"
    )


def test_the_case_mismatch_interrupts_the_human(tmp_path):
    """A log line alone is what let #252 and #253 run for weeks. This condition
    cannot clear itself — no later backup will make a case-only mismatch match —
    so it takes the same escalation a permanently rejected push takes."""
    home, store = _store_with_tracked_slug(tmp_path, "C--Users-ricky-dev-jqit-project-b")
    slug = "c--Users-ricky-dev-jqit-project-b"

    _run_hook(store, home, slug, tmp_path)

    notice = store / slug / "tmp" / "git-backup-notice"
    assert notice.exists(), "no systemMessage notice was written for a stopped backup"
    assert "C--Users-ricky-dev-jqit-project-b" in notice.read_text()


def test_a_genuinely_clean_store_still_reports_nothing_to_commit(tmp_path):
    """The other half of "three states, not two": the new state must not swallow
    the old one. A store tracked under its own spelling with no pending change is
    clean, and must keep saying so — a guard that cries wolf on every quiet
    backup is a guard nobody reads."""
    slug = "c--Users-ricky-dev-jqit-project-b"
    home, store = _store_with_tracked_slug(tmp_path, slug)

    output = _run_hook(store, home, slug, tmp_path)

    assert "nothing to commit" in output, output
    assert "cannot match" not in output, output


def test_a_brand_new_slug_is_not_a_mismatch(tmp_path):
    """A store's first backup has no tracked path under its slug either, and
    that is the normal case — it must commit, not report a mismatch."""
    home, store = _store_with_tracked_slug(tmp_path, "c--some-other-project")
    slug = "c--Users-ricky-dev-jqit-project-b"
    new_dir = store / slug
    new_dir.mkdir()
    (new_dir / "recent.md").write_text("first memory\n")

    output = _run_hook(store, home, slug, tmp_path)

    assert "cannot match" not in output, output
    assert f"committed {slug}" in output, output
