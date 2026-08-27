"""PostToolUse must replay a resolution it already paid for (#350).

`user-prompt-hook.sh` got the #227 env-cache fast path; `post-tool-hook.sh` did
not, and it is the hotter of the two — tool calls outnumber prompts roughly ten
to one in an agentic session, and this hook registers with **no matcher**, so it
runs on every single one. The reporter measured 750-1000 ms per tool call on
Windows 11 / Git Bash against ~90 ms for the prompt hook on the same machine.

The reason it was left alone is written down in
``tests/test_post_tool_hook_spawns.py``: this hook needs ``config()``, and
therefore the merged config file, "caching which is a materially bigger risk
than caching a handful of paths". That objection is answered rather than
ignored — the merged config (which can carry a live OAuth token, #232) is still
never cached. Two *scalars* derived from it are, in the same 0600 file that has
carried ``REMEMBER_TZ`` and ``REMEMBER_PROMPT_STAMP`` since #227/#301, under the
same config-mtime invalidation.

Every "the chain did not run" assertion below is paired with a positive control
in the same fixture. An assertion that no `git` was spawned also passes when the
hook never ran at all, and this file exists because a hook that silently stops
doing its job is this repo own recurring defect (#144, #200, #263).
"""

from __future__ import annotations

import json
import os
import shutil
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
PROMPT_HOOK = REPO_ROOT / "scripts" / "user-prompt-hook.sh"

from pipeline.slug import session_dir_slug as _slug  # noqa: E402
from tests.env_cache import EnvCacheProbe, write_config  # noqa: E402
from tests.spawn_counting import make_shim_dir, spawns as _spawn_lines  # noqa: E402

TRANSCRIPT_LINE = "{\"type\":\"assistant\",\"message\":{\"content\":\"x\"}}\n"

# The warm run measured on macOS bash 3.2.57 with the shared counted-command
# list: `sed` (the slug), `ls -t`, `head -1`, `wc -l`, `mv -f` (capture-alive)
# and `date +%s` (the cooldown clock; a builtin on bash >= 4.2). Against 14 for
# the same fixture before this change.
#
# The slack is for the platforms that are not this one — Git Bash spends a
# `cygpath` more, bash >= 4.2 a `date` less — not for a regression.
FAST_PATH_SPAWN_MEASURED = 6
FAST_PATH_SPAWN_BUDGET = FAST_PATH_SPAWN_MEASURED + 2


def _project(tmp_path: Path, *, jsonl_lines: int = 60, config: dict | None = None,
             cooldown_ts: int | None = None, session_id: str = "sess-1"):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (session_dir / (session_id + ".jsonl")).write_text(
        TRANSCRIPT_LINE * jsonl_lines, encoding="utf-8",
    )
    cfg = {"thresholds": {"delta_lines_trigger": 50}} if config is None else config
    # Through write_config, so the layer is unambiguously older than any cache
    # published after it — otherwise bash whole-second `-nt` decides at random
    # whether the "warm" run below is warm (#303).
    write_config(remember / "config.json", cfg)
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
    }
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED", "REMEMBER_TZ",
                  "REMEMBER_NESTED_SUMMARIZER"):
        env.pop(stale, None)
    if extra:
        env.update(extra)
    return env


def _run(env: dict, *, count_into: Path | None = None, shims: Path | None = None,
         script: Path | None = None, stdin: bytes = b""):
    if count_into is not None:
        count_into.write_text("", encoding="utf-8")
        env = {**env, "SPAWN_LOG": str(count_into),
               "PATH": str(shims) + os.pathsep + env["PATH"]}
    return subprocess.run(
        ["bash", str(script or HOOK)], capture_output=True, env=env,
        timeout=120, input=stdin,
    )


def _prime(env: dict) -> None:
    """Publish a resolution the way a real session does — SessionStart and
    UserPromptSubmit both call `_remember_env_cache_publish`, and by the time
    the first tool call fires the cache is already there."""
    result = _run(env, script=PROMPT_HOOK)
    assert result.returncode == 0, "priming run failed: " + repr(result.stderr[:400])


def _reap(remember: Path) -> None:
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


def _cmds(lines):
    return [line.split(" ", 1)[0] for line in lines]


def _listing(lines):
    return "\n  " + "\n  ".join(lines)


def _fast_path_run(env: dict, tmp_path: Path, label: str):
    """Run the hook and prove FROM THE RUN ITSELF that it took the fast path.

    Not `EnvCacheProbe.assert_warm`, and the difference matters. The probe
    answers "was the published resolution left untouched", which a hook that
    never opened the cache satisfies just as well as one that replayed it — so
    against a cache primed by another hook it returns `warm` for the slow path
    too. It is exactly right for the headline test, where the first run is what
    publishes; it is a false witness everywhere else in this file.

    The spawn log has no such ambiguity. `jq` is the three-layer merge plus the
    one-pass flatten, and neither can be absent from a run that sourced log.sh.

    Paired with a positive control, because "no jq" is also what a hook that
    exited on line 1 spawns: `ls -t` is how the transcript is found, so a run
    that spawned it got as far as looking.
    """
    log = tmp_path / ("spawns-" + label + ".log")
    shims = make_shim_dir(tmp_path)
    result = _run(env, count_into=log, shims=shims)
    lines = _spawn_lines(log)
    assert result.returncode == 0, repr(result.stderr[:400])
    assert "ls" in _cmds(lines), (
        "the " + label + " run never looked for a transcript, so nothing below "
        "is attributable to the fast path. Spawned:" + _listing(lines)
    )
    assert "jq" not in _cmds(lines), (
        "the " + label + " run forked jq, so it took the bootstrap chain — this "
        "test claims to measure the fast path and did not reach it. Spawned:"
        + _listing(lines)
    )
    return result, lines


# -- The fast path runs, and the chain it skips is the expensive one ----------

def test_a_warm_post_tool_call_replays_instead_of_re_resolving(tmp_path):
    """The measurement, as an assertion — with the control that says the number
    describes a hook that ran.

    `git rev-parse` comes from lib-memory-dir.sh worktree probe, and the two
    `jq`s are the three-layer merge and the one-pass flatten. All three are the
    bootstrap chain; none of them can be absent from a run that took it.
    """
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    env = _env(tmp_path, home, project)
    log = tmp_path / "spawns.log"
    shims = make_shim_dir(tmp_path)

    cold = _run(env, count_into=log, shims=shims)
    assert cold.returncode == 0, "cold run failed: " + repr(cold.stderr[:400])
    cold_lines = _spawn_lines(log)

    # POSITIVE CONTROL: the chain this test claims the warm run skips is a
    # chain that demonstrably runs when it is not skipped.
    assert "git" in _cmds(cold_lines), (
        "the cold run did not spawn git — the harness is not observing the "
        "bootstrap chain at all, so the warm assertion below proves nothing. "
        "Spawned:" + _listing(cold_lines)
    )
    assert "jq" in _cmds(cold_lines), (
        "the cold run did not spawn jq — see above. Spawned:" + _listing(cold_lines)
    )

    probe = EnvCacheProbe(env["TMPDIR"])
    probe.snapshot()
    warm = _run(env, count_into=log, shims=shims)
    assert warm.returncode == 0, "warm run failed: " + repr(warm.stderr[:400])
    warm_lines = _spawn_lines(log)
    probe.assert_warm("the post-tool fast path")
    _reap(remember)

    # POSITIVE CONTROL: the warm run did work. `ls -t` is how the hook finds
    # the transcript, so a run that spawned it got as far as looking.
    assert "ls" in _cmds(warm_lines), (
        "the warm run spawned no `ls` — it exited before it looked for a "
        "transcript, so no-git-no-jq says nothing. Spawned:"
        + _listing(warm_lines)
    )

    assert "git" not in _cmds(warm_lines), (
        "the warm run still forks git — the bootstrap chain is not being "
        "skipped. Spawned:" + _listing(warm_lines)
    )
    assert "jq" not in _cmds(warm_lines), (
        "the warm run still forks jq — the merged config is still being built "
        "and flattened on every tool call. Spawned:" + _listing(warm_lines)
    )
    assert len(warm_lines) <= FAST_PATH_SPAWN_BUDGET, (
        str(len(warm_lines)) + " external spawns on a warm tool call (cold run: "
        + str(len(cold_lines)) + "). Spawned:" + _listing(warm_lines)
    )


def test_the_fast_path_leaves_nothing_on_stderr(tmp_path):
    """`log`, `config` and `dispatch` all live in log.sh, which the fast path
    does not source. An undefined one is a `command not found` per tool call —
    and bootstrap-dirs.sh stderr redirect is skipped too, so it would land in
    front of the user rather than in hook-errors.log."""
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    env = _env(tmp_path, home, project)
    _run(env)
    warm = _run(env)
    _reap(remember)

    assert warm.returncode == 0, repr(warm.stderr[:400])
    assert warm.stderr == b"", "the warm run wrote to stderr: " + repr(warm.stderr[:400])
    assert warm.stdout == b"", (
        "the warm run wrote to stdout, which Claude Code shows the user: "
        + repr(warm.stdout[:400])
    )


# -- What the fast path must not have been bought with -----------------------

def test_the_fast_path_still_writes_the_wiring_marker_with_no_tmp_dir(tmp_path):
    """#200. `tmp/post-tool-ran` is how /doctor answers "is PostToolUse wired",
    and it is written before every early exit. bootstrap-dirs.sh is what creates
    `$REMEMBER_DIR/tmp`, and the fast path does not source it — so the marker
    has to survive the directory being absent rather than assume it."""
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    env = _env(tmp_path, home, project)
    _prime(env)

    # The chain has never created a tmp/ in this store.
    for path in sorted((remember / "tmp").iterdir()):
        path.unlink()
    (remember / "tmp").rmdir()

    _fast_path_run(env, tmp_path, "wiring-marker")
    _reap(remember)

    assert (remember / "tmp" / "post-tool-ran").exists(), (
        "post-tool-ran is missing after a fast-path run with no tmp/ — /doctor "
        "now tells the user a wired hook has never fired, which is the exact "
        "regression #200 fixed"
    )


def test_the_fast_path_still_records_which_session_it_captured(tmp_path):
    """#206. SessionStart reads this to decide whether the previous session was
    captured; without it every healthy session reads as a capture gap."""
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()),
                                       session_id="sess-abc")
    env = _env(tmp_path, home, project)
    _prime(env)
    _fast_path_run(env, tmp_path, "capture-alive")
    _reap(remember)

    present = sorted(p.name for p in (remember / "tmp").iterdir())
    assert (remember / "tmp" / "capture-alive.d" / "sess-abc").exists(), (
        "per-session capture-alive marker missing on the fast path. Present: "
        + repr(present)
    )
    assert (remember / "tmp" / "capture-alive").read_text(encoding="utf-8") == "sess-abc"


@pytest.mark.parametrize(
    "threshold,expect_fork",
    [(100000, False), (1, True)],
    ids=["a-high-threshold-holds-the-fork-back", "a-low-threshold-lets-it-fire"],
)
def test_the_fast_path_still_reads_the_configured_threshold(tmp_path, threshold,
                                                            expect_fork):
    """`config()` is the reason #227 skipped this hook. Both directions, in one
    parametrised fixture: a threshold that must suppress the fork and one that
    must produce it. Only asserting the first would pass on a hook that had
    stopped forking altogether."""
    home, project, remember = _project(
        tmp_path, jsonl_lines=60,
        config={"thresholds": {"delta_lines_trigger": threshold}},
    )
    env = _env(tmp_path, home, project)
    _prime(env)
    _fast_path_run(env, tmp_path, "threshold-" + str(threshold))
    forked = (remember / "tmp" / "save-session.pid").exists()
    _reap(remember)

    assert forked is expect_fork, (
        "delta_lines_trigger=" + str(threshold) + " with a 60-line transcript: "
        "expected fork=" + str(expect_fork) + ", got fork=" + str(forked)
        + " — the configured threshold is not reaching the fast path"
    )


@pytest.mark.parametrize(
    "position,expect_fork",
    [(55, False), (0, True)],
    ids=["a-recent-position-suppresses-the-fork", "position-zero-lets-it-fire"],
)
def test_the_fast_path_still_consults_the_saved_position(tmp_path, position,
                                                         expect_fork):
    """The delta is CURRENT_LINES minus the last saved position, and the
    position comes from `$PYTHON -m pipeline.shell read-position`. PYTHON is set
    by detect-tools.sh, which the fast path does not source up front — so this
    is the assertion that says the interpreter is still found when it is needed.

    Both directions again: a position 5 lines back must suppress the fork
    against the default threshold of 50, and a position of 0 must produce it."""
    home, project, remember = _project(tmp_path, jsonl_lines=60)
    (remember / "tmp" / "last-save.json").write_text(
        json.dumps({"sessions": {"sess-1": position}, "session": "sess-1",
                    "line": position}),
        encoding="utf-8",
    )
    env = _env(tmp_path, home, project)
    _prime(env)
    _fast_path_run(env, tmp_path, "position-" + str(position))
    forked = (remember / "tmp" / "save-session.pid").exists()
    _reap(remember)

    assert forked is expect_fork, (
        "saved position " + str(position) + " of a 60-line transcript: expected "
        "fork=" + str(expect_fork) + ", got fork=" + str(forked)
        + " — read-position is not reaching the fast path"
    )


def test_the_nested_summarizer_guard_survives_the_fast_path(tmp_path):
    """#204. The guard lives in resolve-paths.sh, which the fast path does not
    source. Without it the summarizer own tool calls scaffold a memory store
    under its temp dir — and a live cache is exactly the state in which the fast
    path is taken."""
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    env = _env(tmp_path, home, project)
    _prime(env)

    # POSITIVE CONTROL: without the variable, this same fixture writes markers
    # — and does so on the fast path, which is where the guard has to hold.
    _fast_path_run(env, tmp_path, "nested-control")
    assert (remember / "tmp" / "post-tool-ran").exists(), (
        "the control run wrote no marker, so the absence asserted below is not "
        "attributable to the guard"
    )
    _reap(remember)
    (remember / "tmp" / "post-tool-ran").unlink()

    nested = _run(_env(tmp_path, home, project,
                       {"REMEMBER_NESTED_SUMMARIZER": "1"}))
    assert nested.returncode == 0, repr(nested.stderr[:400])
    assert not (remember / "tmp" / "post-tool-ran").exists(), (
        "a nested summarizer ran the whole hook — #204 is back on the fast path"
    )


def test_the_fast_path_still_reports_a_slug_that_matches_no_session_dir(tmp_path):
    """#144, and the reason `log` is upgraded on the fast path rather than
    stubbed out.

    A slug that matches no directory Claude Code created leaves nothing to read
    and the whole pipeline no-ops for the life of the session. That used to be a
    bare `exit 0` and the only symptom was memory quietly never appearing. The
    warning it was replaced with lives on a code path the fast path reaches, and
    `log` lives in log.sh, which the fast path does not source — so a stub here
    would delete the diagnostic to save a process.

    Both halves are asserted from the same fixture: the run with a matching slug
    must write NOTHING about a missing transcript (otherwise "the warning
    appeared" is not evidence of anything), and the run without one must.
    """
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    env = _env(tmp_path, home, project)
    _prime(env)

    def _logged():
        logs = remember / "logs"
        if not logs.is_dir():
            return ""
        return "".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(logs.glob("memory-*.log"))
        )

    # POSITIVE CONTROL, in the inverse direction: a healthy fast-path run says
    # nothing about a missing transcript.
    _fast_path_run(env, tmp_path, "healthy")
    _reap(remember)
    assert "no .jsonl transcript" not in _logged(), (
        "a healthy run already logs the missing-transcript warning, so its "
        "presence below would say nothing"
    )

    # Now break the slug the way #144 did: the session directory Claude Code
    # created no longer holds a transcript this project can read.
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    for path in sorted(session_dir.iterdir()):
        path.unlink()

    log = tmp_path / "spawns-no-transcript.log"
    shims = make_shim_dir(tmp_path)
    result = _run(env, count_into=log, shims=shims)
    assert result.returncode == 0, repr(result.stderr[:400])
    _reap(remember)

    body = _logged()
    assert "no .jsonl transcript" in body, (
        "a session directory with no transcript produced no diagnostic on the "
        "fast path — #144's symptom is back: memory quietly never appears and "
        "nothing anywhere says why. Log tail: " + repr(body[-2000:])
    )


def test_a_store_that_cannot_hold_a_log_does_not_exec_the_system_log_binary(tmp_path):
    """The other end of the same `log` upgrade, and a real bug it had.

    log.sh returns early — before it defines `log()` — on a store whose logs/
    directory it cannot create. The upgrade therefore has to install a no-op,
    and the obvious guard for that is `type log`. On macOS `type log` is TRUE
    regardless, because /usr/bin/log is Apple's unified logging CLI: the stub is
    never installed, the hook execs that binary once per diagnostic, and a hook
    documented "EXIT CODES: 0 Always" prints `log: Unknown subcommand 'hook'`
    and exits 64.

    Reproduced here by making `logs` a FILE, which is the cheapest thing that
    makes log.sh's `mkdir -p` fail on every platform. The fast path's own
    `[ -d ]` guard means stderr is not redirected either, so whatever the hook
    says lands where this test can see it — which is also where the user would
    have seen it.
    """
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    env = _env(tmp_path, home, project)
    _prime(env)

    # A run that reaches a `log` call: no transcript to be found (#144).
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    for path in sorted(session_dir.iterdir()):
        path.unlink()
    # And a store log.sh cannot open a log in.
    logs = remember / "logs"
    if logs.exists():
        shutil.rmtree(logs)
    logs.write_text("not a directory", encoding="utf-8")

    result = _run(env)
    _reap(remember)

    assert result.returncode == 0, (
        "the hook exited " + str(result.returncode) + " — it is documented to "
        "always exit 0. stderr: " + repr(result.stderr[:600])
    )
    assert b"Unknown subcommand" not in result.stderr, (
        "the hook shelled out to the system `log` binary instead of its own "
        "log() — stderr: " + repr(result.stderr[:600])
    )
    assert result.stdout == b"", repr(result.stdout[:400])


# -- The listener gate -------------------------------------------------------

def test_the_shipped_gitkeep_directory_does_not_disable_the_fast_path(tmp_path):
    """`user-prompt-hook.sh` gates on `[ ! -d hooks.d/after_user_prompt ]`, and
    the distribution ships no such directory. It DOES ship
    `hooks.d/after_post_tool/` holding a `.gitkeep`, so the same `-d` test here
    would turn the fast path off for every user who never opted out of it. The
    gate has to ask what dispatch asks: is there anything EXECUTABLE."""
    assert (REPO_ROOT / "hooks.d" / "after_post_tool" / ".gitkeep").exists(), (
        "this test is premised on the shipped tree containing "
        "hooks.d/after_post_tool/.gitkeep — it no longer does, so the gate it "
        "pins may be testing nothing"
    )
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    env = _env(tmp_path, home, project)
    _prime(env)
    _, lines = _fast_path_run(env, tmp_path, "shipped-tree")
    _reap(remember)

    assert "jq" not in _cmds(lines), (
        "the shipped hooks.d/after_post_tool/.gitkeep disabled the fast path — "
        "every user pays the full chain unless they delete a file the "
        "distribution puts there. Spawned:" + _listing(lines)
    )


def test_an_executable_listener_takes_the_slow_path_and_is_dispatched(tmp_path):
    """The other half of the gate. A listener needs log.sh dispatch(), so
    installing one must give up the fast path — and must actually be run."""
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    plugin = tmp_path / "plugin"
    subprocess.run(["cp", "-R", str(REPO_ROOT), str(plugin)], check=True,
                   capture_output=True)
    listener_dir = plugin / "hooks.d" / "after_post_tool"
    listener_dir.mkdir(parents=True, exist_ok=True)
    witness = tmp_path / "listener-ran"
    listener = listener_dir / "50-witness.sh"
    listener.write_text("#!/bin/bash\ntouch " + str(witness) + "\n", encoding="utf-8")
    listener.chmod(0o755)

    env = _env(tmp_path, home, project, {"CLAUDE_PLUGIN_ROOT": str(plugin)})
    _prime(env)
    result = _run(env)
    assert result.returncode == 0, repr(result.stderr[:400])
    _reap(remember)

    assert witness.exists(), (
        "the after_post_tool listener was never dispatched — the fast path "
        "stubbed out dispatch() for a user who had installed one"
    )


def test_the_slow_path_does_not_exec_system_log_or_call_undefined_dispatch(tmp_path):
    """#361: the fast path above already guards `log` (`declare -F log`) and
    stubs `dispatch()` outright. The SLOW path — taken by every session's
    FIRST tool call, because there is no cache yet to replay — guarded
    neither: it sourced log.sh with stderr suppressed and then called `log`
    and `dispatch` unconditionally.

    Reproduced exactly as
    ``test_a_store_that_cannot_hold_a_log_does_not_exec_the_system_log_binary``
    does — logs/ as a FILE, the cheapest cross-platform way to make log.sh's
    `mkdir -p` fail — but deliberately WITHOUT ``_prime(env)`` first, so this
    run actually takes the slow path instead of the fast one that test (and
    the one it's modeled on, at line ~448) restricts itself to. The issue's
    own claim was that dropping ``_prime`` from that fixture makes it fail
    today; verified directly against this branch's pre-fix tree before the
    guard below was added — it did.

    Paired with a positive control in the same fixture, on the same project
    and the same installed listener: a HEALTHY store still dispatches to it.
    That is the difference between a fallback for a chain that failed and a
    stub that would silently swallow every real listener too.
    """
    home, project, remember = _project(tmp_path, cooldown_ts=int(time.time()))
    plugin = tmp_path / "plugin"
    subprocess.run(["cp", "-R", str(REPO_ROOT), str(plugin)], check=True,
                   capture_output=True)
    listener_dir = plugin / "hooks.d" / "after_post_tool"
    listener_dir.mkdir(parents=True, exist_ok=True)
    witness = tmp_path / "listener-ran"
    listener = listener_dir / "50-witness.sh"
    listener.write_text("#!/bin/bash\ntouch " + str(witness) + "\n", encoding="utf-8")
    listener.chmod(0o755)

    env = _env(tmp_path, home, project, {"CLAUDE_PLUGIN_ROOT": str(plugin)})
    # No _prime(): this IS the first tool call of a session — the case #361
    # measured — and there is nothing yet to make it take the fast path.

    logs = remember / "logs"
    if logs.exists():
        shutil.rmtree(logs)
    logs.write_text("not a directory", encoding="utf-8")

    result = _run(env)
    _reap(remember)

    assert result.returncode == 0, (
        "documented EXIT CODES: 0 Always — got " + str(result.returncode)
        + ": " + repr(result.stderr[:600])
    )
    assert b"Unknown subcommand" not in result.stderr, (
        "the slow path shelled out to the system `log` binary instead of "
        "its own no-op — stderr: " + repr(result.stderr[:600])
    )
    assert b"dispatch: command not found" not in result.stderr, (
        "the slow path called an undefined dispatch() — stderr: "
        + repr(result.stderr[:600])
    )

    # POSITIVE CONTROL: a HEALTHY store, same listener, still dispatches —
    # the guard above is a fallback for a broken chain, not a permanent stub.
    logs.unlink()
    result2 = _run(env)
    _reap(remember)
    assert result2.returncode == 0, repr(result2.stderr[:400])
    assert witness.exists(), (
        "the #361 guard disabled dispatch on the healthy path too — an "
        "installed after_post_tool listener no longer runs at all"
    )
