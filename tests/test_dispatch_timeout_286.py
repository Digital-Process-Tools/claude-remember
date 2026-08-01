"""A dispatched hook that never returns must not stall the agent forever (#286).

`dispatch()` ran every `hooks.d/<event>/` listener sequentially, in the
foreground, with nothing bounding how long one may take. A listener that blocks
— a network call with no timeout, a `read` on a pipe nobody writes to, a lock
wait — stalled the agent for as long as it blocked and **logged nothing,
because nothing had failed**. That is the house defect in its most literal
form: the three-state contract says a check that cannot answer must say so, and
a hook that never returns never reaches the point where it could say anything.

What is asserted here is both halves, because the issue is both halves:

    a hook that hangs is STOPPED, and the fact that it was stopped REACHES
    `hook-errors.log` — the file `/remember:doctor` tails.

Two more invariants get their own tests because getting them wrong is how a
timeout becomes worse than the stall it prevents:

  * **A fast hook is not made slower.** A watchdog implemented by polling
    `kill -0` on a one-second tick costs every hook a second it did not spend.
    `after_post_tool` dispatches on every single tool call.
  * **A hook's DETACHED work survives.** Both shipped hooks put every git write
    inside a disowned subshell precisely so it never blocks the save. Killing
    the process group instead of the process would kill a `git push` mid-flight
    and leave a `.git/index.lock` in the very store this plugin exists to
    protect — trading a loud failure for a quiet one.
"""

import os
import subprocess
import sys
import time

import pytest

from tests.test_path_resolution import (
    _create_full_plugin_copy,
    _create_full_project,
)

# Same reason, and the same marker, as the module these helpers come from: on
# windows-latest a bare `bash` resolves to the WSL stub in System32, which
# prints a UTF-16 notice and exits 1 before any script is read. Every assertion
# here drives a real hook through a real bash.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives the real hooks through a bash subprocess — not portable to "
           "Windows (see tests/test_path_resolution.py)",
)

# The budget every test installs. Small enough that a hung hook is caught inside
# a test's patience, large enough that a loaded CI runner starting bash does not
# trip it on an honest hook.
BUDGET = 3
GRACE = 2

# How long a hung fixture sleeps: long enough that if the timeout does NOT fire,
# the test fails on its own subprocess timeout rather than passing by accident.
HANG = 120


def _setup(tmp_path, budget=BUDGET, grace=GRACE):
    plugin = os.path.join(str(tmp_path), "plugin")
    project = os.path.join(str(tmp_path), "project")
    os.makedirs(plugin, exist_ok=True)
    os.makedirs(project, exist_ok=True)
    _create_full_plugin_copy(plugin)
    _create_full_project(project)
    _write_config(plugin, budget, grace)
    return plugin, project


def _write_config(plugin: str, budget, grace) -> None:
    """Overwrite the copied plugin's config.json with the timeout under test.

    `_create_full_plugin_copy` writes this file; the keys added here are the
    ones #286 introduces, so the fixture states them explicitly rather than
    relying on a shipped default it would then be unable to vary.
    """
    import json
    cfg = {
        "timezone": "UTC",
        "cooldowns": {"save_seconds": 120},
        "features": {"recovery": False},
        "hooks": {},
    }
    if budget is not None:
        cfg["hooks"]["dispatch_timeout_seconds"] = budget
    if grace is not None:
        cfg["hooks"]["dispatch_kill_grace_seconds"] = grace
    with open(os.path.join(plugin, "config.json"), "w") as f:
        json.dump(cfg, f)


def _install_hook(plugin: str, event: str, name: str, body: str,
                  mode: int = 0o755) -> str:
    d = os.path.join(plugin, "hooks.d", event)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, mode)
    return path


def _run_prompt_hook(plugin: str, project: str,
                     timeout: float = 60) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT",
                        "REMEMBER_NESTED_SUMMARIZER")}
    env["CLAUDE_PROJECT_DIR"] = project
    env["CLAUDE_PLUGIN_ROOT"] = plugin
    # The env cache short-circuits dispatch entirely when it can; this suite is
    # about what dispatch does, so it is taken out of the way.
    env["REMEMBER_ENV_CACHE"] = "0"
    return subprocess.run(
        ["bash", os.path.join(plugin, "scripts", "user-prompt-hook.sh")],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def _timed_prompt_hook(plugin: str, project: str, timeout: float = 60):
    start = time.monotonic()
    r = _run_prompt_hook(plugin, project, timeout=timeout)
    return r, time.monotonic() - start


def _error_log(project: str) -> str:
    path = os.path.join(project, ".remember", "logs", "hook-errors.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _daily_log(project: str) -> str:
    d = os.path.join(project, ".remember", "logs")
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return ""
    for n in names:
        if n.startswith("memory-") and n.endswith(".log"):
            with open(os.path.join(d, n), encoding="utf-8",
                      errors="replace") as f:
                out.append(f.read())
    return "".join(out)


class TestAHungHookIsStopped:

    def test_a_hook_that_never_returns_does_not_stall_the_prompt(
            self, tmp_path):
        """The load-bearing one.

        `user-prompt-hook.sh` runs in front of a human who has just pressed
        enter. Before the fix this call does not return for HANG seconds — and
        in production, for as long as whatever the hook is blocked on takes,
        which may be forever.

        The bound asserted is deliberately loose (several times the budget):
        this is not a latency test, it is the difference between "bounded" and
        "unbounded".
        """
        plugin, project = _setup(tmp_path)
        _install_hook(plugin, "after_user_prompt", "50-hang.sh",
                      f"#!/bin/bash\nsleep {HANG}\n")

        r, elapsed = _timed_prompt_hook(plugin, project,
                                        timeout=BUDGET + GRACE + 25)
        assert r.returncode == 0, r.stderr[-2000:]
        assert elapsed < BUDGET + GRACE + 20, (
            f"dispatch waited {elapsed:.1f}s on a hook with a {BUDGET}s budget "
            "— the hook was never stopped"
        )

    def test_the_stop_reaches_the_file_the_doctor_reads(self, tmp_path):
        """"logs nothing" is half of what #286 is about.

        `/remember:doctor` tails `hook-errors.log` under "Recent errors", and
        that is the file maintainers ask a reporter to paste. A hook that
        stalled the agent and left no trace there is indistinguishable, in
        every report anyone reads, from a session where nothing happened.
        """
        plugin, project = _setup(tmp_path)
        _install_hook(plugin, "after_user_prompt", "50-hang.sh",
                      f"#!/bin/bash\nsleep {HANG}\n")

        _timed_prompt_hook(plugin, project, timeout=BUDGET + GRACE + 25)

        errors = _error_log(project)
        assert "50-hang.sh" in errors, (
            "the hook that hung is not named in hook-errors.log:\n" + errors
        )
        assert "after_user_prompt" in errors, (
            "the event is not named in hook-errors.log:\n" + errors
        )
        assert str(BUDGET) in errors, (
            "the budget it exceeded is not stated, so a reader cannot tell "
            "whether the hook is hung or the budget is too tight:\n" + errors
        )
        # And in the daily log too, which is where the failure belongs in
        # sequence — the same two destinations _dispatch_report_failure uses.
        assert "50-hang.sh" in _daily_log(project)

    def test_a_timeout_is_reported_as_UNKNOWN_not_as_a_verdict_from_the_hook(
            self, tmp_path):
        """Decline, not finding — the three-state contract (0.12.0).

        A hook that exits non-zero has ANSWERED: it ran and it failed, and
        `_dispatch_report_failure` says so with its exit status and its own
        words. A hook that was killed has answered nothing. Reporting the
        second in the shape of the first ("hook failed (exit 143)") invents a
        verdict the hook never gave, and `/remember:doctor` would show a fault
        where what exists is an absence of information.
        """
        plugin, project = _setup(tmp_path)
        _install_hook(plugin, "after_user_prompt", "50-hang.sh",
                      f"#!/bin/bash\nsleep {HANG}\n")

        _timed_prompt_hook(plugin, project, timeout=BUDGET + GRACE + 25)
        errors = _error_log(project)

        line = next((ln for ln in errors.splitlines() if "50-hang.sh" in ln),
                    "")
        assert line, "nothing was written about the hung hook:\n" + errors
        assert "UNKNOWN" in line, (
            "the report must say that whether the hook did its work is "
            "unknown — that is the whole difference between a decline and a "
            f"finding:\n{line}"
        )
        assert "exit 143" not in line and "exit 137" not in line, (
            "a signal number dressed up as the hook's exit status reads as a "
            f"verdict the hook never gave:\n{line}"
        )


class TestTheBudgetIsPerHook:

    def test_one_slow_hook_does_not_spend_the_next_hook_s_allowance(
            self, tmp_path):
        """#286, question 1.

        A budget for the whole dispatch makes a hook's fate depend on its
        neighbours' latency: the second listener would be killed for the first
        one's slowness, and the log line would name a hook whose only fault was
        being later in the queue.
        """
        plugin, project = _setup(tmp_path)
        _install_hook(plugin, "after_user_prompt", "10-hang.sh",
                      f"#!/bin/bash\nsleep {HANG}\n")
        _install_hook(plugin, "after_user_prompt", "20-fine.sh",
                      "#!/bin/bash\necho i-ran\n")

        r, _ = _timed_prompt_hook(plugin, project,
                                  timeout=BUDGET + GRACE + 25)
        assert "i-ran" in r.stdout, (
            "the second listener was denied its own budget because the first "
            "one spent it:\n" + r.stdout
        )
        errors = _error_log(project)
        assert "20-fine.sh" not in errors, (
            "a healthy hook was reported because a different hook hung:\n"
            + errors
        )


class TestWhatTheHookIsGivenBeforeItIsKilled:

    def test_a_hook_that_traps_gets_to_clean_up_before_KILL(self, tmp_path):
        """#286, question 2.

        Both shipped hooks take a lock, and on the no-`flock(1)` path both
        release it from an EXIT trap. bash runs an EXIT trap when it dies of an
        untrapped SIGTERM and does not when it dies of SIGKILL, so TERM-then-
        grace is the difference between a lock released and a lock file left
        behind holding a dead PID.
        """
        plugin, project = _setup(tmp_path)
        witness = os.path.join(str(tmp_path), "cleaned-up")
        _install_hook(
            plugin, "after_user_prompt", "50-traps.sh",
            "#!/bin/bash\n"
            f"trap 'echo cleaned > \"{witness}\"' EXIT\n"
            f"sleep {HANG}\n")

        _timed_prompt_hook(plugin, project, timeout=BUDGET + GRACE + 25)

        assert os.path.exists(witness), (
            "the hook was killed outright — a listener holding a lock, a temp "
            "file or a partial write never got the chance to unwind it"
        )

    def test_a_hook_that_ignores_TERM_is_still_stopped(self, tmp_path):
        """The grace period is a courtesy, not a veto.

        A listener that traps SIGTERM and keeps going must not be able to hold
        the agent open indefinitely by refusing to die — that would reinstate
        the exact defect, behind an opt-in.
        """
        plugin, project = _setup(tmp_path)
        _install_hook(
            plugin, "after_user_prompt", "50-stubborn.sh",
            "#!/bin/bash\n"
            "trap '' TERM\n"
            f"sleep {HANG} &\n"
            "wait\n")

        r, elapsed = _timed_prompt_hook(plugin, project,
                                        timeout=BUDGET + GRACE + 25)
        assert r.returncode == 0, r.stderr[-2000:]
        assert elapsed < BUDGET + GRACE + 20, (
            f"a hook that ignores SIGTERM held dispatch for {elapsed:.1f}s"
        )

    def test_what_a_hung_hook_managed_to_say_is_still_delivered(
            self, tmp_path):
        """#277's standing argument, applied to the timeout path.

        A hook that says something useful and then hangs has still said it.
        Discarding its stdout because it was later killed throws away the one
        piece of evidence about what it was doing when it stopped.
        """
        plugin, project = _setup(tmp_path)
        _install_hook(
            plugin, "after_user_prompt", "50-says-then-hangs.sh",
            "#!/bin/bash\n"
            "echo about-to-block\n"
            f"sleep {HANG}\n")

        r, _ = _timed_prompt_hook(plugin, project,
                                  timeout=BUDGET + GRACE + 25)
        assert "about-to-block" in r.stdout, (
            "the hook's own words were discarded because it was killed:\n"
            + r.stdout
        )


class TestTheTimeoutDoesNotBreakWhatItGuards:

    def test_a_hook_s_detached_work_survives_the_kill(self, tmp_path):
        """#286, question 4 — the one with teeth, as an executable assertion.

        `hooks.d/after_save/50-git-backup.sh` runs its entire `git add` /
        `commit` / `push` inside a disowned subshell and exits immediately;
        `50-git-restore.sh` does the same with its fetch. Both do it so the
        network never blocks a save. A timeout that killed the process GROUP
        would kill exactly that work — a SIGTERM between `git add` and `git
        commit`, or mid-push — and the next session's backup would then fail on
        a `.git/index.lock` whose owner is gone.

        So the kill targets the hook's own PID and nothing else. This test is
        the guard on that: the detached child outlives the hook that spawned
        it.
        """
        plugin, project = _setup(tmp_path)
        witness = os.path.join(str(tmp_path), "detached-finished")
        _install_hook(
            plugin, "after_user_prompt", "50-detaches.sh",
            "#!/bin/bash\n"
            f"( sleep {BUDGET + GRACE + 3}; echo done > \"{witness}\" ) "
            "</dev/null >/dev/null 2>&1 &\n"
            "disown $! 2>/dev/null || true\n"
            f"sleep {HANG}\n")

        _timed_prompt_hook(plugin, project, timeout=BUDGET + GRACE + 25)
        assert not os.path.exists(witness), (
            "fixture error: the detached child finished before the hook was "
            "killed, so this test proves nothing"
        )

        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if os.path.exists(witness):
                break
            time.sleep(0.2)

        assert os.path.exists(witness), (
            "the hook's disowned background work was killed along with the "
            "hook. Both shipped hooks put every git write there precisely so "
            "it cannot be interrupted — killing it turns an indefinite stall "
            "into a possibly corrupt store, which is the trade #286 exists to "
            "refuse."
        )

    def test_fast_hooks_are_not_made_slower(self, tmp_path):
        """A watchdog that polls on a one-second tick costs every hook a
        second it did not spend.

        `after_post_tool` dispatches on every single tool call, and
        `after_user_prompt` in front of a human who has just pressed enter.
        A timeout whose price is paid by the healthy case is not worth having.

        Compared against the same hook run with NO listeners installed rather
        than against a fixed number: what is being asserted is that the
        watchdog adds no per-hook wait, on a CI runner whose absolute timings
        are not ours to predict.
        """
        plugin, project = _setup(tmp_path)
        _, baseline = _timed_prompt_hook(plugin, project, timeout=60)

        for i in range(3):
            _install_hook(plugin, "after_user_prompt", f"{i}0-fast.sh",
                          "#!/bin/bash\nexit 0\n")
        _, with_hooks = _timed_prompt_hook(plugin, project, timeout=60)

        assert with_hooks < baseline + 2.0, (
            f"three trivial hooks added {with_hooks - baseline:.2f}s "
            f"(baseline {baseline:.2f}s, with hooks {with_hooks:.2f}s). A "
            "per-hook poll interval is being charged to hooks that returned "
            "immediately."
        )

    def test_a_healthy_hook_is_still_not_reported(self, tmp_path):
        """The `2>/dev/null` this codebase removed was buying one real thing:
        silence for hooks that work. Nothing here may spend that.
        """
        plugin, project = _setup(tmp_path)
        _install_hook(plugin, "after_user_prompt", "50-fine.sh",
                      "#!/bin/bash\necho hello\nexit 0\n")

        r, _ = _timed_prompt_hook(plugin, project, timeout=60)
        assert "hello" in r.stdout
        assert "50-fine.sh" not in _error_log(project)

    def test_a_failing_hook_is_still_reported_as_a_failure(self, tmp_path):
        """The timeout path must not swallow the #277 path it sits beside."""
        plugin, project = _setup(tmp_path)
        _install_hook(plugin, "after_user_prompt", "50-fails.sh",
                      "#!/bin/bash\necho >&2 'the reason'\nexit 3\n")

        _run_prompt_hook(plugin, project, timeout=60)
        errors = _error_log(project)
        assert "50-fails.sh" in errors and "exit 3" in errors, errors
        assert "the reason" in errors, errors
        assert "TIMED OUT" not in errors, (
            "a hook that answered was reported as one that could not:\n"
            + errors
        )


class TestTheBudgetIsConfigurable:

    def test_a_zero_budget_disables_the_timeout(self, tmp_path):
        """An escape hatch that is not a guess.

        Every bounded thing in this codebase (`reject_notice_after`,
        `diverged_notice_after`, `no_remote_notice_after`, `extract_max_bytes`)
        takes 0 to mean "off". A user whose listener legitimately takes minutes
        needs the same door, and needs it to be the same door.
        """
        plugin, project = _setup(tmp_path, budget=0)
        _install_hook(plugin, "after_user_prompt", "50-slow.sh",
                      "#!/bin/bash\nsleep 6\n")

        r, elapsed = _timed_prompt_hook(plugin, project, timeout=60)
        assert r.returncode == 0, r.stderr[-2000:]
        assert elapsed > 5, (
            f"the hook was stopped after {elapsed:.1f}s with the timeout "
            "disabled"
        )
        assert "50-slow.sh" not in _error_log(project)

    def test_a_garbage_budget_falls_back_to_the_default(self, tmp_path):
        """Arithmetic on garbage must not decide whether a hook is killed.

        The same direction `git_backup.reject_notice_after` and
        `git_restore.fetch_timeout_seconds` already take: an unparseable value
        is the shipped default, never 0 and never an unbound-variable death
        inside `$(( ))` under `set -u`.
        """
        plugin, project = _setup(tmp_path, budget="soon")
        _install_hook(plugin, "after_user_prompt", "50-fine.sh",
                      "#!/bin/bash\necho hello\n")

        r, _ = _timed_prompt_hook(plugin, project, timeout=60)
        assert r.returncode == 0, r.stderr[-2000:]
        assert "hello" in r.stdout
        assert "50-fine.sh" not in _error_log(project)
