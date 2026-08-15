"""PostToolUse must not fork a process tree on every tool call (#230).

#227 fixed `user-prompt-hook.sh`: 19 spawns on macOS (27 on the Windows 11
ARM64 / QEMU box @MargeryOlethea measured, at 150-800ms per spawn) down to 1 on
bash 3.2 and 0 on bash >= 4.2. It deliberately left `post-tool-hook.sh` alone,
because that hook needs `config()` and therefore the *merged config file* —
caching which is a materially bigger risk than caching a handful of paths.

This file counts what the post-tool hook actually costs, and pins the reductions
that need none of that caching. The measurement, on macOS bash 3.2.57:

    before: 30 external spawns per tool call
    after:  17

#350 then gave this hook the #227 fast path after all, without conceding the
point above: the merged config is still never cached, only the two scalars
`log.sh` reads out of it. Everything here is the COLD path — the first tool call
of a session, or the first after a config edit — and it stays exactly as it was.
The warm path, which is every other tool call, is measured next door in
`tests/test_post_tool_fast_path_350.py` at 6 spawns. Both numbers are wanted:
this hook must still work on the run that pays full price.

`PostToolUse` fires per tool call — hundreds per session, thousands in an
agentic run — so this is the hook where spawn count multiplies. But it is also
the hook on the **write path**: it is how memory gets captured at all. A cheap
hook that captures nothing is strictly worse than a slow one that works, so the
budget below is not asserted alone. Every post-condition it could have been
bought with — the wiring marker, the per-session capture-alive marker, the
save fork, the configured threshold, and the worktree redirect — is asserted
next to it.

Counted by execution rather than by wall clock, for the reason #227 gives: wall
clock is what differs between platforms, spawn count is what causes it.
"""

from __future__ import annotations

import json
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
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"

from pipeline.slug import session_dir_slug as _slug  # noqa: E402
from tests.spawn_counting import make_shim_dir, spawns as _spawn_lines  # noqa: E402

# Measured on macOS bash 3.2.57 with the shared counted-command list of
# tests/spawn_counting.py: 30 before #230, 20 after it, 16 after #232 collapsed
# config() to a single read of the merged config.
#
# (#230's own note said 17. That number predates #233 folding the two budget
# tests onto one counted-command list, and it left out the two `mkdir -p` calls
# and the `git rev-parse` a non-git PROJECT_DIR still pays. The budget it set,
# 20, therefore had exactly zero slack against the real measurement — the next
# spawn anyone added would have tripped it as a regression it was not. Stating
# the measured number and the slack separately is the fix for that.)
#
# The slack is for the platforms that are not this one — bash >= 4.2 spends
# fewer (no `date`), Git Bash may spend one `cygpath` more — not for a
# regression. What remains is the `jq -s` merge and its `rm` on exit, which
# cannot go without caching the merged config, and that is a credential
# lifetime decision (#232) rather than a spawn one.
POST_TOOL_SPAWN_MEASURED = 16
POST_TOOL_SPAWN_BUDGET = POST_TOOL_SPAWN_MEASURED + 2


def _shim_dir(root: Path) -> Path:
    """A PATH front-end that records every external execution, then execs it."""
    return make_shim_dir(root)


def _project(tmp_path: Path, *, jsonl_lines: int = 60, config: dict | None = None,
             cooldown_ts: int | None = None, session_id: str = "sess-1"):
    """A project with a live transcript, as PostToolUse would find it."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (session_dir / f"{session_id}.jsonl").write_text(
        "".join('{"type":"assistant","message":{"content":"x"}}\n'
                for _ in range(jsonl_lines)),
        encoding="utf-8",
    )
    cfg = {"thresholds": {"delta_lines_trigger": 50}}
    if config is not None:
        cfg = config
    (remember / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if cooldown_ts is not None:
        (remember / "tmp" / "last-save-ts").write_text(str(cooldown_ts), encoding="utf-8")
    return home, project, remember


def _env(tmp_path: Path, home: Path, project: Path, extra: dict | None = None) -> dict:
    tmpdir = tmp_path / "systmp"
    tmpdir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "TMPDIR": str(tmpdir),
        # Pin the COLD path, explicitly (#350). Every budget and post-condition
        # in this file is a statement about the run that performs the
        # resolution, and since #350 the second run of the same fixture
        # replays it instead — so without this the "warm" run these tests count
        # would be measuring a different hook than the docstring claims, and
        # WHICH one it measured would depend on whether the config write and
        # the cache landed in the same whole second (#303). Wrong number,
        # green test, nothing in the output saying so.
        #
        # REMEMBER_ENV_CACHE=0 is lib-env-cache.sh's own documented off switch,
        # not a test-only seam, so what runs here is the shipped cold path.
        "REMEMBER_ENV_CACHE": "0",
    }
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED", "REMEMBER_TZ"):
        env.pop(stale, None)
    if extra:
        env.update(extra)
    return env


def _run(env: dict, *, count_into: Path | None = None, shims: Path | None = None):
    if count_into is not None:
        count_into.write_text("", encoding="utf-8")
        env = {**env, "SPAWN_LOG": str(count_into),
               "PATH": f"{shims}{os.pathsep}{env['PATH']}"}
    return subprocess.run(
        ["bash", str(HOOK)], capture_output=True, env=env, timeout=120, input=b"",
    )


def _spawns(log: Path) -> list[str]:
    return _spawn_lines(log)


def _reap(remember: Path) -> None:
    """Wait out any forked save-session.sh so no stray process leaks."""
    pid_file = remember / "tmp" / "save-session.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


# ── The budget ───────────────────────────────────────────────────────────────

def test_the_merged_config_is_read_once_not_once_per_key(tmp_path):
    """The sharp half of the budget, and the one that fails loudly if #232 is
    reverted while the total happens to stay under its slack.

    The hook asks the merged config five questions per tool call — .timezone,
    .model and .reject_pattern while log.sh is sourced, then
    .cooldowns.save_seconds and .thresholds.delta_lines_trigger — and the file
    does not change between them. Five processes for five questions of one
    unchanging file was #230's largest named remainder.

    The `jq -s` merge that PRODUCES the file is a different thing and is not
    counted here: removing it means publishing the merged config at a stable
    path, and that file can carry a live OAuth credential.
    """
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    env = _env(tmp_path, home, project)
    log = tmp_path / "spawns.log"
    shims = _shim_dir(tmp_path)
    _run(env, count_into=log, shims=shims)
    _reap(remember)

    lines = _spawns(log)
    merges = [l for l in lines if l.startswith("jq ") and " -s " in l]
    reads = [l for l in lines
             if l.startswith("jq ") and " -s " not in l and "remember-config-" in l]

    assert len(merges) == 1, (
        "the three-layer merge should still happen exactly once per process: "
        f"{merges}"
    )
    assert len(reads) <= 1, (
        f"{len(reads)} separate jq reads of one unchanging merged config, one "
        "per key:\n  " + "\n  ".join(reads)
    )



def test_the_post_tool_hook_stays_inside_its_spawn_budget(tmp_path):
    """The measurement, as an assertion.

    PostToolUse fires per tool call. At the 150-800ms per spawn measured in
    #227, thirty of them is seconds of latency multiplied by every tool the
    agent uses — and each one is an independent opportunity to fail.
    """
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    env = _env(tmp_path, home, project)
    log = tmp_path / "spawns.log"
    shims = _shim_dir(tmp_path)

    first = _run(env, count_into=log, shims=shims)
    assert first.returncode == 0, f"cold run failed: {first.stderr[:400]!r}"
    cold = len(_spawns(log))

    second = _run(env, count_into=log, shims=shims)
    assert second.returncode == 0, f"warm run failed: {second.stderr[:400]!r}"
    warm = _spawns(log)
    _reap(remember)

    assert len(warm) <= POST_TOOL_SPAWN_BUDGET, (
        f"{len(warm)} external spawns per tool call (cold run: {cold}). "
        f"Spawned:\n  " + "\n  ".join(warm)
    )


# ── What the budget must not have been bought with ───────────────────────────

def test_the_hook_still_records_that_it_ran(tmp_path):
    """#200's wiring marker. The doctor's answer to "is PostToolUse wired at
    all" is this file existing, and it is written before every early exit."""
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    result = _run(_env(tmp_path, home, project))
    assert result.returncode == 0, result.stderr[:300]
    assert (remember / "tmp" / "post-tool-ran").exists(), (
        "post-tool-ran marker missing — /doctor now reports a wired hook as never fired"
    )


def test_the_hook_still_records_which_session_it_captured(tmp_path):
    """#206's per-session marker, which SessionStart reads to decide whether the
    previous session was captured. One file per session, named by session id."""
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()),
                                       session_id="sess-abc")
    result = _run(_env(tmp_path, home, project))
    assert result.returncode == 0, result.stderr[:300]

    per_session = remember / "tmp" / "capture-alive.d" / "sess-abc"
    assert per_session.exists(), (
        "per-session capture-alive marker missing — every healthy session now "
        f"reads as a capture gap. Present: "
        f"{sorted(p.name for p in (remember / 'tmp').iterdir())}"
    )
    single = remember / "tmp" / "capture-alive"
    assert single.read_text(encoding="utf-8") == "sess-abc", (
        f"the single-slot marker must still name the session: {single.read_text()!r}"
    )


def test_the_save_fork_still_fires_when_the_delta_clears_the_threshold(tmp_path):
    """The whole point of the hook. No cooldown marker, a transcript well past
    the threshold: save-session.sh must be launched, with its log directory
    created for it."""
    home, project, remember = _project(tmp_path, jsonl_lines=200)
    result = _run(_env(tmp_path, home, project))
    assert result.returncode == 0, result.stderr[:300]

    pid_file = remember / "tmp" / "save-session.pid"
    assert pid_file.exists(), (
        "no save was forked past the delta threshold — memory stops being captured"
    )
    assert (remember / "logs" / "autonomous").is_dir(), (
        "the fork's log directory was not created, so its output goes nowhere"
    )
    _reap(remember)


def test_the_configured_threshold_is_still_read(tmp_path):
    """`config()` is why the merged config cannot simply be skipped here. A
    threshold above the transcript length must hold the fork back — if config
    reads were dropped to save processes, the built-in 50 would fire instead."""
    home, project, remember = _project(
        tmp_path, jsonl_lines=60,
        config={"thresholds": {"delta_lines_trigger": 100000}},
    )
    result = _run(_env(tmp_path, home, project))
    assert result.returncode == 0, result.stderr[:300]
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "a delta_lines_trigger of 100000 was ignored — the configured threshold "
        "is no longer being read"
    )
    _reap(remember)


# ── The git short-circuit: correct on both answers ───────────────────────────

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "t")
    (path / "file.txt").write_text("hello\n")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-qm", "init")


def _bootstrap_counting(tmp_path: Path, project: Path, home: Path):
    """Source detect-tools + bootstrap-dirs with a counting PATH, return
    (REMEMBER_DIR, spawn lines)."""
    (home / ".remember").mkdir(parents=True, exist_ok=True)
    log = tmp_path / "boot-spawns.log"
    log.write_text("", encoding="utf-8")
    shims = _shim_dir(tmp_path)
    systmp = tmp_path / "systmp"
    systmp.mkdir(exist_ok=True)
    script = (
        f'export PROJECT_DIR="{project}"\n'
        f'export PIPELINE_DIR="{REPO_ROOT}"\n'
        f'source "{DETECT_SCRIPT}"\n'
        f'source "{BOOTSTRAP_SCRIPT}"\n'
        f'echo "REMEMBER_DIR=$REMEMBER_DIR"\n'
    )
    env = {
        **os.environ, "HOME": str(home), "TMPDIR": str(systmp),
        "SPAWN_LOG": str(log), "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
    }
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED"):
        env.pop(stale, None)
    result = subprocess.run(["bash", "-c", script], capture_output=True,
                            env=env, timeout=120)
    out = result.stdout.decode("utf-8", "replace")
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[:400]
    remember_dir = ""
    for line in out.splitlines():
        if line.startswith("REMEMBER_DIR="):
            remember_dir = line.split("=", 1)[1]
    return remember_dir, _spawns(log)


def test_an_ordinary_checkout_is_settled_without_forking_git(tmp_path):
    """A main checkout has `.git` as a DIRECTORY, and a linked worktree never
    does. So `[ -d "$proj/.git" ]` — a builtin — answers "no redirect needed"
    outright for the common case, and the ~200ms `git rev-parse` is not paid.
    """
    project = tmp_path / "main"
    _init_repo(project)
    remember_dir, spawns = _bootstrap_counting(tmp_path, project, tmp_path / "home")

    assert remember_dir == str(project / ".remember"), remember_dir
    gits = [s for s in spawns if s.startswith("git ")]
    assert gits == [], (
        f"`git` still forked to establish that an ordinary checkout is not a "
        f"worktree: {gits}"
    )


def test_a_linked_worktree_still_redirects_to_the_main_checkout(tmp_path):
    """The short-circuit is on the POSITIVE answer only (#56).

    A linked worktree's `.git` is a file, so it must fall through to the real
    `git` call — and `git` being spawned here is the proof that the fast path
    did not swallow the case it exists to detect.
    """
    main = tmp_path / "main"
    _init_repo(main)
    wt = tmp_path / "wt-feature"
    _git(main, "worktree", "add", "-q", "-b", "feature", str(wt))

    remember_dir, spawns = _bootstrap_counting(tmp_path, wt, tmp_path / "home")

    assert remember_dir == str(main / ".remember"), (
        f"worktree memory must live with the main checkout, got {remember_dir}"
    )
    assert any(s.startswith("git ") for s in spawns), (
        "no `git` was run for a linked worktree — the short-circuit is firing on "
        "the negative answer too, which cannot be right"
    )


def test_a_subdirectory_of_a_worktree_still_redirects(tmp_path):
    """The absence of `.git` proves NOTHING.

    A subdirectory of a linked worktree has no `.git` entry at all, and it still
    needs the redirect. Short-circuiting on "no .git directory found" instead of
    "a .git directory found" would put memory inside the worktree, where
    `git worktree remove` deletes it silently.
    """
    main = tmp_path / "main"
    _init_repo(main)
    wt = tmp_path / "wt-feature"
    _git(main, "worktree", "add", "-q", "-b", "feature", str(wt))
    sub = wt / "nested" / "deeper"
    sub.mkdir(parents=True)

    remember_dir, _ = _bootstrap_counting(tmp_path, sub, tmp_path / "home")

    assert remember_dir == str(main / ".remember"), (
        f"a subdirectory of a worktree lost its redirect: {remember_dir}"
    )


def test_a_non_git_directory_is_unchanged(tmp_path):
    """No repo at all: PROJECT_DIR stands, as it always has."""
    project = tmp_path / "plain"
    project.mkdir()
    remember_dir, _ = _bootstrap_counting(tmp_path, project, tmp_path / "home")
    assert remember_dir == str(project / ".remember"), remember_dir
