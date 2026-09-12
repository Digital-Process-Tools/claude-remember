"""50-git-restore.sh must not re-source the full config-merge chain to learn
that it is switched off (#663, part of #660).

`session-start-hook.sh` sources `log.sh` (which sources `lib-memory-dir.sh`)
before it ever dispatches `before_session_start` hooks, so by the time this
hook runs as a CHILD process it already has an inherited, already-merged
`REMEMBER_CONFIG` on disk and exported into its environment. Before this fix,
`50-git-restore.sh` ignored that inheritance completely: because
`_LIB_MEMORY_DIR_LOADED` is not (and should not casually be made to be, see
the comment on the fix itself) exported to the child, `source
"$PIPELINE_DIR/scripts/log.sh"` at the top of the "now we can afford logging
+ config" section re-ran the WHOLE three-layer merge (`lib-memory-dir.sh`:
mktemp, one `jq` per config layer carrying `data_dir`, one `jq -s` deep
merge) plus `log.sh`'s own one-pass flatten (another `jq` call) and its cache
publish -- unconditionally, on every dispatch of this hook, on every install
that has git_restore switched off (the shipped default, #253) -- before ever
reading the one boolean that decides whether any of that was needed.

Measured on this file (spawn-counting harness, external `data_dir` layout,
git_restore.enabled=false, `_LIB_MEMORY_DIR_LOADED` deliberately unset to
exercise the real re-source): 11 spawns before the fix (2 `git`, 1 `sed`
from an unrelated slug computation `_resolve_remember_dir` triggers whenever
`data_dir` is absolute, 3 `jq`, 2 `mkdir`, 1 `mv`, 1 `rm`, 1 more `sed`) --
all of it spent finding out the answer was "no". After the fix: 2 spawns (1
`git` for the pre-existing toplevel check, 1 `jq` reading the flag straight
out of the already-merged `REMEMBER_CONFIG` the parent exported).

Positive control in the same fixture, same environment shape, only
`git_restore.enabled=true`: the fast read still fires first, but the hook
then falls through to the full chain -- exactly as it must, since the actual
restore logic (a `git rev-list --left-right --count` divergence check, among
much else) still needs it. This is the pairing #663's own issue text asks
for: a "must not fire" assertion (no re-source) paired with a "must fire"
one in the same fixture, so a gate that skipped the hook outright -- which
would trivially pass the negative case -- fails this one instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks.d" / "before_session_start" / "50-git-restore.sh"

sys.path.insert(0, str(REPO_ROOT))
from tests.spawn_counting import make_shim_dir, spawns
from tests.test_git_backup_hook import _git, make_external_remember_repo


def _setup(tmp_path: Path, enabled: bool):
    """External (`data_dir`) layout, real git repos throughout, and --
    unlike every OTHER caller of this hook in this test suite --
    `_LIB_MEMORY_DIR_LOADED` deliberately left UNSET. Every existing fixture
    for this hook (see `test_git_restore_hook_253.py::_run`) sets that guard
    to bypass `lib-memory-dir.sh` entirely, which is the right choice for
    tests about the RESTORE logic but would make this file measure nothing:
    the whole point here is the child dispatch() actually spawns, where the
    guard is genuinely absent.

    `REMEMBER_DIR`, `PIPELINE_DIR`, `PROJECT_DIR` and `REMEMBER_CONFIG` are
    set exactly as `dispatch()` (scripts/log.sh) would leave them for a real
    child: `REMEMBER_CONFIG` points at a file that already carries the
    answer, because the PARENT already merged one before dispatching. Home's
    own `.remember/config.json` carries the same `git_restore.enabled` (plus
    the `data_dir` pointing at the external store) so that IF the hook falls
    through to the real merge -- the positive-control path -- it recomputes
    the identical REMEMBER_DIR rather than silently drifting to a legacy
    default.
    """
    home, remember, _remote = make_external_remember_repo(tmp_path)
    slug_dir = remember / "test-slug"
    slug_dir.mkdir()
    (slug_dir / "now.md").write_text("## 10:00 | test\nlocal memory\n", encoding="utf-8")
    _git(remember, ["add", "-A"])
    _git(remember, ["commit", "-q", "-m", "local"])
    _git(remember, ["push", "-q", "origin", "main"])

    project = tmp_path / "project"
    project.mkdir()

    (home / ".remember").mkdir(parents=True, exist_ok=True)
    (home / ".remember" / "config.json").write_text(
        json.dumps({"data_dir": str(slug_dir), "git_restore": {"enabled": enabled}}),
        encoding="utf-8",
    )

    # The file the PARENT already merged and exported -- this is what the
    # cheap pre-check reads instead of re-merging.
    cfg = tmp_path / "remember-config.json"
    cfg.write_text(json.dumps({"git_restore": {"enabled": enabled}}), encoding="utf-8")

    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)

    env = {
        **os.environ,
        "HOME": str(home),
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(REPO_ROOT),
        "REMEMBER_DIR": str(slug_dir),
        "REMEMBER_CONFIG": str(cfg),
        "REMEMBER_PROJECT": str(project),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test",
        "SPAWN_LOG": str(log),
        "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
    }
    return env, log, cfg, remember


def _run(env):
    return subprocess.run(
        ["bash", str(HOOK)], env=env, capture_output=True, text=True, timeout=60,
        check=False,
    )


def test_disabled_does_not_resource_the_config_merge_chain(tmp_path):
    """Negative: git_restore.enabled=false. Would still pass if the code did
    nothing at all -- the positive control below is what rules that out."""
    env, log, cfg, remember = _setup(tmp_path, enabled=False)
    result = _run(env)
    assert result.returncode == 0, result.stderr

    got = spawns(log)
    # Exactly the pre-existing toplevel check, plus the one new cheap read.
    # No `mkdir` (REMEMBER_DIR/logs, REMEMBER_DIR/tmp), no `mv` (cache
    # publish), no second or third `jq` (the layered merge, the flatten) --
    # every one of those is a spawn the full chain would have cost.
    assert got == [
        f"git -C {remember} rev-parse --show-toplevel",
        f"jq -r .git_restore.enabled // false {cfg}",
    ], got


def test_enabled_still_runs_the_restore(tmp_path):
    """Positive control, same fixture: a gate that skipped the whole hook
    (rather than only skipping the RE-MERGE) would pass the test above and
    fail this one -- the restore's own divergence check must still run."""
    env, log, cfg, remember = _setup(tmp_path, enabled=True)
    result = _run(env)
    assert result.returncode == 0, result.stderr

    got = spawns(log)
    # The cheap read still fires first, with the exact same shape as the
    # disabled case -- it is not skipped just because the answer is "yes".
    assert got[0] == f"git -C {remember} rev-parse --show-toplevel", got
    assert got[1] == f"jq -r .git_restore.enabled // false {cfg}", got
    # Falls through to the full chain: the layered merge's own `jq -s`, the
    # cache publish (`mv`), and -- the actual restore, not just bookkeeping
    # -- the divergence check `git rev-list --left-right --count`.
    joined = " ".join(got)
    assert "jq -s reduce" in joined, got
    assert "rev-list --left-right --count" in joined, got
    assert len(got) > 10, got
