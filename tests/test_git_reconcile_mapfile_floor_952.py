"""#952/#953 follow-up: the scoped-restore rewrite (60-git-reconcile.sh) uses
`mapfile`, a bash 4.0+ builtin, to populate `_GRC_TOUCHED_PATHS` and
`_grc_existing`. This repo's documented floor is bash 3.2 (stock macOS
`/bin/bash` -- see scripts/lib-clock.sh, scripts/lib-lock.sh, and the other
`_bash_has_bashpid`/`_bash_honors_xtracefd` floor tests already in this
suite), and GitHub Actions' `macos-latest` runner resolves plain `bash` on
PATH to exactly that floor interpreter, not a homebrew bash.

On that floor, `mapfile` is not a builtin at all ("mapfile: command not
found"). The array `mapfile -t ARR < <(cmd)` was meant to populate is left
empty, so every downstream `"${ARR[@]}"` use is silently a no-op:
`_grc_compute_touched_paths`'s output never reaches `_GRC_TOUCHED_PATHS`, so
`git checkout HEAD -- "${_GRC_TOUCHED_PATHS[@]}"` and the upstream-addition
prune never run, and the scoped-abort restore this whole file exists to
perform does nothing at all -- exactly the six macOS CI failures observed on
PR #962 (conflicting file left with markers, another slug's upstream-only
change never restored, no log line, no log file at all, a stray
upstream-added file left behind).

Two layers, same shape as test_printf_v_empty_format_898.py:

- a static guard over shipped shell, which runs on every platform (the
  Linux and Windows legs have no bash 3.2 to show the bug on), and
- the real hook, run end-to-end against this machine's own floor bash when
  one is available, with a positive control proving that interpreter really
  lacks `mapfile` -- a "the file was restored" assertion on an interpreter
  that has `mapfile` proves nothing about the floor.

The module-level win32 skip below carries the same reason string and
verdict as test_git_reconcile_903's own (whose fixtures this module
imports and reuses directly) -- same shape as this doc's other
"same reason string and same verdict" rows, see
docs/windows-skip-triage.md.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from shell_parse import KIND_BASH, discover_interpreters

from .test_git_reconcile_903 import (
    REPO_ROOT as _RR,
)
from .test_git_reconcile_903 import (
    _enabled_config,
    _git,
    _head,
    _store,
    _wait_quiesce,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics - not portable to Windows runners (#79)",
)

HOOK = REPO_ROOT / "hooks.d" / "after_save" / "60-git-reconcile.sh"

# `mapfile` (or its `readarray` alias) as a bare command word -- the bash 4.0+
# builtin this floor does not have. Excludes a path/word that merely
# CONTAINS the substring (e.g. a comment mentioning "mapfile" by name).
# Review finding (two independent reviewers, same gap): the anchor set
# originally covered only `;`/`&`/`|`/`(`/`then`/`do` -- missing `else`,
# `elif`, `{`, `}`, `)` (a `case` branch), and `!`/`||`/`&&` run straight
# into the keyword with no separating whitespace captured above. Widened
# to every shell command-start token this repo's own shell actually uses,
# not just the ones the three real call sites in this commit happened to
# use.
_MAPFILE_CALL = re.compile(
    r"(?:^|[;&|(){}!]|\b(?:then|do|else|elif)\b)\s*(?:mapfile|readarray)\b"
)


def _shipped_shell() -> list:
    out = []
    for sub in ("scripts", "hooks", "hooks.d"):
        root = REPO_ROOT / sub
        if root.is_dir():
            out.extend(p for p in root.rglob("*.sh") if p.is_file())
    return sorted(out)


def test_the_guard_pattern_matches_the_defect_shape():
    """Positive control for the static guard below."""
    assert _MAPFILE_CALL.search('    mapfile -t _grc_existing < <(cmd)')
    assert _MAPFILE_CALL.search("if true; then readarray -t X < <(cmd); fi")
    # Review finding (two independent reviewers): the original anchor set
    # missed these real shell command-start shapes.
    assert _MAPFILE_CALL.search("else mapfile -t X < <(cmd)")
    assert _MAPFILE_CALL.search("elif mapfile -t X < <(cmd); then")
    assert _MAPFILE_CALL.search("pat) mapfile -t X < <(cmd) ;;")
    assert _MAPFILE_CALL.search("{ mapfile -t X < <(cmd); }")
    assert not _MAPFILE_CALL.search("# mapfile is a bash 4+ builtin, avoid it")
    assert not _MAPFILE_CALL.search('log "git-reconcile" "about mapfile semantics"')


def test_no_shipped_shell_calls_mapfile_or_readarray():
    files = _shipped_shell()
    assert files, "sanity: no shipped shell scripts found"
    hits = []
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _MAPFILE_CALL.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}")
    assert not hits, (
        "mapfile/readarray is a bash 4.0+ builtin and is absent on this repo's "
        "documented floor (bash 3.2, stock macOS /bin/bash, the GitHub Actions "
        "macos-latest runner's own plain `bash`) -- it fails as 'command not "
        "found', silently leaving the target array empty. Use a "
        "while-IFS=-read-into-array loop instead:\n" + "\n".join(hits)
    )


def _floor_bash():
    for interp in discover_interpreters():
        if interp.kind == KIND_BASH and interp.is_floor:
            return interp.path
    return None


def test_floor_bash_really_lacks_mapfile():
    """Positive control for the behavioural test below."""
    bash = _floor_bash()
    if bash is None:
        pytest.skip("no bash below 4.0 on this machine -- the static guard above covers this leg")
    r = subprocess.run([bash, "-c", 'mapfile -t X < <(printf "a\nb\n"); echo "${#X[@]}"'],
                        capture_output=True, text=True, check=False)
    assert "command not found" in r.stderr or r.returncode != 0, (
        "expected this interpreter's own shell to not recognise mapfile", r.stdout, r.stderr,
    )


def test_conflict_abort_restores_the_tree_on_the_floor_bash(tmp_path):
    """The real #962 CI failure, reproduced directly: the hook run under the
    floor bash must still abort a real conflict and restore the file -- not
    silently skip the restore because `mapfile` does not exist there."""
    bash = _floor_bash()
    if bash is None:
        pytest.skip("no bash below 4.0 on this machine -- the static guard above covers this leg")

    home, remember, remote, slug_dir, project = _store(tmp_path)

    other = tmp_path / "other-machine"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)],
                    check=True, capture_output=True)
    _git(other, ["config", "user.email", "other@test"])
    _git(other, ["config", "user.name", "Other"])
    (other / "test-slug" / "now.md").write_text(
        "## 10:00 | test\nFROM THE OTHER MACHINE\n", encoding="utf-8")
    _git(other, ["add", "-A"])
    _git(other, ["commit", "-q", "-m", "other machine edit"])
    _git(other, ["push", "-q", "origin", "main"])

    (slug_dir / "now.md").write_text(
        "## 10:00 | test\nFROM THIS MACHINE\n", encoding="utf-8")
    _git(remember, ["add", "-A"])
    _git(remember, ["commit", "-q", "-m", "this machine edit"])
    local_head = _head(remember)
    local_content = (slug_dir / "now.md").read_text(encoding="utf-8")

    cfg = _enabled_config(tmp_path)
    env = {
        **os.environ,
        "HOME": str(home),
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(_RR),
        "REMEMBER_DIR": str(slug_dir),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_PROJECT": str(project),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test",
        "REMEMBER_CONFIG": str(cfg),
    }
    subprocess.run([bash, str(HOOK)], env=env, capture_output=True, text=True,
                    timeout=120, check=False)
    _wait_quiesce(remember)

    assert _head(remember) == local_head, (
        "a conflicting rebase changed HEAD on the floor bash -- it must abort "
        "and leave the tree exactly as it was"
    )
    assert (slug_dir / "now.md").read_text(encoding="utf-8") == local_content, (
        "the conflicting file was modified despite the abort, on the floor "
        "bash -- this is the #962 macOS CI failure: mapfile left "
        "_GRC_TOUCHED_PATHS empty, so the scoped checkout restored nothing"
    )
