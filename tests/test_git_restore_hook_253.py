"""A restore counterpart to the git backup (issue #253, part 2).

The plugin pushed and never read back. Grepping 0.10.0 for `git pull`, `fetch`,
`merge` and `rebase` outside `tests/` returned nothing, so a store used from two
machines drifted: the second machine read stale local memory, committed on top
of it, and from then on could never push at all. The reporter lost twelve days
of memory to that shape.

`hooks.d/before_session_start/50-git-restore.sh` is the mirror of
`after_save/50-git-backup.sh`, and it inherits that file's discipline:

  * **Fast-forward only.** Never merge, never rebase, and never a destructive
    verb. `recent.md` and `archive.md` are rewritten wholesale by consolidation
    rather than appended, so a conflict there is real and a wrong automatic
    resolution corrupts memory silently. A diverged store is REFUSED and said
    out loud.
  * **Three states, never two.** Part 1 fixed a write path that called a
    permanent rejection a transient deferral. The read side has the identical
    trap one layer over: a store whose fetch has been failing for a week looks
    exactly like a store that is genuinely current, unless "could not check" is
    a state of its own.
  * **Off by default.** Nothing changes for an existing install.

And one property part 1 did not need: this runs in front of the user's first
prompt. #227 was a plugin putting ~8.7s there. So the network half is detached
and lands next session, the synchronous half touches only refs an earlier
session already fetched, and `TestSessionStartIsNotBlocked` measures it.

Real repositories throughout — a real bare remote advanced by a real second
clone. Mocking git's output would pin our parser against a string we wrote.
Note the fixture hazard part 1 found and this file inherits: cloning a bare
repo without `-b main` lands on an unborn branch, and every assertion below
would then be vacuous. `_advance_remote` asserts the tip actually moved.
"""

import json
import re
import shutil
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX flock/git semantics — not portable to Windows runners (#79)",
)

from .test_git_backup_hook import (  # noqa: E402
    REPO_ROOT,
    _git,
    hook_state,
    make_external_remember_repo,
)
from .test_git_backup_push_rejected_253 import (  # noqa: E402
    _advance_remote,
    _log_text,
    _remote_head,
)

HOOK = REPO_ROOT / "hooks.d" / "before_session_start" / "50-git-restore.sh"
USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"

NOTICE_NAME = "git-restore-notice"
FETCH_STATE = ".git-restore-fetch"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, **git_restore) -> Path:
    cfg = tmp_path / "remember-config.json"
    body: dict = {}
    if git_restore:
        body["git_restore"] = git_restore
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def _store(tmp_path: Path):
    """A memory store with one slug, a bare remote, and a project dir."""
    home, remember, remote = make_external_remember_repo(tmp_path)
    slug_dir = remember / "test-slug"
    slug_dir.mkdir()
    (slug_dir / "now.md").write_text("## 10:00 | test\nlocal memory\n", encoding="utf-8")
    _git(remember, ["add", "-A"])
    _git(remember, ["commit", "-q", "-m", "local"])
    _git(remember, ["push", "-q", "origin", "main"])
    project = tmp_path / "project"
    project.mkdir()
    return home, remember, remote, slug_dir, project


def _run(slug_dir: Path, project: Path, home: Path, cfg: Path = None, **extra):
    env = {
        **os.environ,
        "HOME": str(home),
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(REPO_ROOT),
        "REMEMBER_DIR": str(slug_dir),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_PROJECT": str(project),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    if cfg is not None:
        env["REMEMBER_CONFIG"] = str(cfg)
    env.update(extra)
    return subprocess.run(["bash", str(HOOK)], env=env, capture_output=True,
                          text=True, timeout=120)


def _head(repo: Path) -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def _fetch_now(remember: Path) -> None:
    """Stand in for the PREVIOUS session's detached fetch. The synchronous half
    of the hook reads refs somebody else fetched; that is the whole latency
    design, so the tests for it must not fetch inline either."""
    _git(remember, ["fetch", "-q", "origin"])


def _wait_for_fetch(remember: Path, timeout: float = 60) -> dict:
    """Block until the detached fetch has written its completion record."""
    state = hook_state(remember, FETCH_STATE, create_dir=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if state.exists():
            body = dict(
                line.split("=", 1)
                for line in state.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            if "finished" in body:
                return body
        time.sleep(0.1)
    raise TimeoutError(f"detached fetch never recorded a result in {state}")


# ── Making the Linux branch reachable on macOS ─────────────────────────────
# `_take_lock` has two paths: flock(1) where it exists, and a noclobber
# fallback where it does not. Linux has flock and macOS does not, so the branch
# EVERY Linux install takes is the one the maintainers' own machines can never
# execute — and it shipped with a bug that made the escalation counter unable to
# pass 1. #208 (Debian `dash` cannot exec a builtin Apple's `dash` can) and #250
# (`MAX_ARG_STRLEN` caps one envp string at 128 KB on Linux only) are the same
# shape: the platform you develop on is the one that cannot see its own
# constraint.
#
# So the flock path is made executable here. The shim implements exactly the
# call the hooks make — `flock -n <fd>` against an inherited descriptor — through
# the same syscall. Where a real flock(1) exists it is used instead.
_FLOCK_SHIM = """#!/usr/bin/env python3
import fcntl, sys
args = [a for a in sys.argv[1:] if a != "-n"]
try:
    fcntl.flock(int(args[0]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)
sys.exit(0)
"""


def _flock_env(tmp_path: Path) -> dict:
    """Env that guarantees the hook takes its flock branch."""
    if shutil.which("flock"):
        return {}
    shim_dir = tmp_path / "flock-shim"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "flock"
    shim.write_text(_FLOCK_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    return {"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}


def _diverge_local(remember: Path) -> None:
    """A commit here that the remote does not have."""
    (remember / "test-slug" / "now.md").write_text(
        "## 11:00 | test\nlocal-only work\n", encoding="utf-8"
    )
    _git(remember, ["add", "-A"])
    _git(remember, ["commit", "-q", "-m", "local only"])


# ── Off by default ───────────────────────────────────────────────────────────


class TestOffByDefault:
    """The reporter asked for config-gated and default off so nothing changes
    for existing installs, and that is also the only reason this may sit on the
    session-start path at all."""

    def test_unset_does_nothing_at_all(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _fetch_now(remember)
        before = _head(remember)

        result = _run(slug_dir, project, home, _config(tmp_path))

        assert result.returncode == 0, result.stderr
        assert _head(remember) == before, (
            "the store was fast-forwarded with git_restore.enabled unset — "
            "every existing install would silently change behaviour on upgrade"
        )
        assert not (hook_state(remember, FETCH_STATE, create_dir=True)).exists(), (
            "a background fetch was spawned with the feature off — that is "
            "network I/O on the session-start path of every install that never "
            "asked for this"
        )

    def test_explicit_false_does_nothing(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _fetch_now(remember)
        before = _head(remember)

        _run(slug_dir, project, home, _config(tmp_path, enabled=False))

        assert _head(remember) == before
        assert not (hook_state(remember, FETCH_STATE, create_dir=True)).exists()

    def test_legacy_mode_never_activates(self, tmp_path):
        """Memory inside the project repo: restoring there would fast-forward
        the user's SOURCE TREE from the project's origin (#138)."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _fetch_now(remember)
        before = _head(remember)

        # PROJECT_DIR == the store root is exactly the legacy layout.
        _run(slug_dir, remember, home, _config(tmp_path, enabled=True))

        assert _head(remember) == before, (
            "the hook fast-forwarded a store whose parent is the project dir"
        )


# ── The feature ──────────────────────────────────────────────────────────────


class TestCleanlyBehindFastForwards:

    def test_a_behind_store_is_restored(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        remote_tip = _advance_remote(tmp_path, remote)
        _fetch_now(remember)
        assert _head(remember) != remote_tip, "fixture: store is not behind"

        result = _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        assert result.returncode == 0, result.stderr
        assert _head(remember) == remote_tip, (
            "a cleanly-behind store was not fast-forwarded\n--- log ---\n"
            + _log_text(slug_dir)
        )
        assert (remember / "from-another-machine.md").exists(), (
            "the other machine's memory is not on disk — the ref moved but the "
            "working tree did not, so the session still reads stale files"
        )

    def test_it_lands_before_memory_is_read(self, tmp_path):
        """before_session_start is dispatched at session-start-hook.sh:86 and
        memory is injected far below it. If that ordering ever inverts, a
        restore can only ever affect the NEXT session."""
        body = (REPO_ROOT / "scripts" / "session-start-hook.sh").read_text(encoding="utf-8")
        dispatch_at = body.index('dispatch "before_session_start"')
        inject_at = body.index('echo "=== MEMORY ==="')
        assert dispatch_at < inject_at, (
            "before_session_start now runs after memory injection — the restore "
            "would land one whole session late"
        )

    def test_the_restore_is_reported_with_a_count(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _fetch_now(remember)

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        log = _log_text(slug_dir)
        assert "restored 1 commit" in log, (
            "the restore did not say how much it restored\n--- log ---\n" + log
        )


# ── Diverged: refuse, and say so ─────────────────────────────────────────────


class TestADivergedStoreIsRefused:
    """The reporter's 'what I am not asking for'. A conflict in recent.md or
    archive.md is real, because consolidation rewrites them wholesale, and a
    wrong resolution corrupts memory silently."""

    def _diverged(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _diverge_local(remember)
        _fetch_now(remember)
        # Deliberately on the flock branch. Everything in this class counts
        # consecutive RUNS, so it is the first thing to break if one run can
        # lock the next one out — which is exactly what shipped, and what only
        # Linux could see.
        self.env = _flock_env(tmp_path)
        return home, remember, remote, slug_dir, project

    def test_a_diverged_store_is_not_touched(self, tmp_path):
        home, remember, remote, slug_dir, project = self._diverged(tmp_path)
        before = _head(remember)
        remote_before = _remote_head(remote)

        result = _run(slug_dir, project, home, _config(tmp_path, enabled=True), **self.env)

        assert result.returncode == 0, result.stderr
        assert _head(remember) == before, (
            "a DIVERGED store was moved — this is the case the reporter "
            "explicitly asked never to be resolved automatically\n--- log ---\n"
            + _log_text(slug_dir)
        )
        assert _remote_head(remote) == remote_before, "the hook wrote to the remote"

    def test_the_divergence_is_reported_as_a_divergence(self, tmp_path):
        home, remember, remote, slug_dir, project = self._diverged(tmp_path)

        _run(slug_dir, project, home, _config(tmp_path, enabled=True), **self.env)

        log = _log_text(slug_dir)
        assert "DIVERGED" in log, (
            "a diverged store produced no divergence report — it is "
            "indistinguishable from an ordinary quiet session\n--- log ---\n" + log
        )
        assert "up to date" not in log, (
            "a diverged store was ALSO called up to date\n--- log ---\n" + log
        )

    def test_it_says_both_counts_and_what_to_run(self, tmp_path):
        home, remember, remote, slug_dir, project = self._diverged(tmp_path)

        _run(slug_dir, project, home, _config(tmp_path, enabled=True), **self.env)

        log = _log_text(slug_dir)
        assert "1 local commit" in log and "1 remote commit" in log, (
            "the report does not say how far apart the two sides are\n"
            "--- log ---\n" + log
        )
        assert "git -C" in log, (
            "the refusal names no command the human can run\n--- log ---\n" + log
        )
        assert "merge or rebase" in log, (
            "the refusal does not promise that nothing will be resolved "
            "automatically\n--- log ---\n" + log
        )

    def test_one_divergence_does_not_yet_interrupt_the_human(self, tmp_path):
        home, remember, remote, slug_dir, project = self._diverged(tmp_path)

        _run(slug_dir, project, home, _config(tmp_path, enabled=True), **self.env)

        assert not (slug_dir / "tmp" / NOTICE_NAME).exists(), (
            "the most intrusive channel in the codebase fired on the first "
            "occurrence"
        )

    def test_a_persistent_divergence_reaches_the_human(self, tmp_path):
        home, remember, remote, slug_dir, project = self._diverged(tmp_path)

        for _ in range(3):
            _run(slug_dir, project, home, _config(tmp_path, enabled=True), **self.env)

        notice = slug_dir / "tmp" / NOTICE_NAME
        assert notice.exists(), (
            "three session starts against a permanently diverged store and the "
            "human was never told — a divergence never clears itself, so a "
            "threshold can only postpone a true report, never swallow one"
        )
        body = notice.read_text(encoding="utf-8")
        assert "DIVERGED" in body and "git -C" in body

    def test_the_threshold_is_configurable(self, tmp_path):
        home, remember, remote, slug_dir, project = self._diverged(tmp_path)

        _run(slug_dir, project, home, _config(tmp_path, enabled=True,
                                              diverged_notice_after=1), **self.env)

        assert (slug_dir / "tmp" / NOTICE_NAME).exists()

    def test_zero_disables_the_interruption(self, tmp_path):
        home, remember, remote, slug_dir, project = self._diverged(tmp_path)

        for _ in range(4):
            _run(slug_dir, project, home, _config(tmp_path, enabled=True,
                                                  diverged_notice_after=0), **self.env)

        assert not (slug_dir / "tmp" / NOTICE_NAME).exists()
        assert "DIVERGED" in _log_text(slug_dir), (
            "disabling the interruption also silenced the log line"
        )

    def test_the_notice_reaches_the_human_as_a_system_message(self, tmp_path):
        """Authorship is not delivery. systemMessage is the only hook output
        the HUMAN sees."""
        home = tmp_path / "home"
        project = tmp_path / "project"
        remember = project / ".remember"
        (remember / "tmp").mkdir(parents=True)
        (home / ".claude").mkdir(parents=True)
        (remember / "tmp" / NOTICE_NAME).write_text(
            "remember: your memory store has DIVERGED\n", encoding="utf-8"
        )

        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            env={
                **os.environ,
                "HOME": str(home),
                "CLAUDE_PROJECT_DIR": str(project),
                "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
                "REMEMBER_DIR": str(remember),
                "_LIB_MEMORY_DIR_LOADED": "1",
            },
            capture_output=True, text=True, timeout=60,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert "DIVERGED" in payload.get("systemMessage", ""), (
            "the git-restore notice was not delivered on systemMessage — the "
            "one channel the human actually sees\n" + result.stdout
        )
        assert not (remember / "tmp" / NOTICE_NAME).exists(), (
            "the notice was not consumed on read; it would repeat on every prompt"
        )

    def test_a_repaired_store_clears_the_count(self, tmp_path):
        home, remember, remote, slug_dir, project = self._diverged(tmp_path)
        _run(slug_dir, project, home, _config(tmp_path, enabled=True), **self.env)
        _run(slug_dir, project, home, _config(tmp_path, enabled=True), **self.env)

        # The human resolves it the only way this tool ever endorses: by hand.
        _git(remember, ["push", "-q", "-f", "origin", "main"])
        _fetch_now(remember)
        _run(slug_dir, project, home, _config(tmp_path, enabled=True), **self.env)

        assert not (hook_state(remember, ".git-restore-diverged")).exists(), (
            "the consecutive-divergence count survived the repair, so the next "
            "unrelated divergence would interrupt on its first occurrence"
        )


# ── Three states, not two ────────────────────────────────────────────────────


class TestCouldNotCheckIsItsOwnState:
    """Part 1's defect, mirrored onto the read side: 'I could not reach the
    remote' must never render as 'you are up to date'. `docs/validators.md`
    calls this declining instead of guessing."""

    def test_a_failed_fetch_is_not_reported_as_up_to_date(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _git(remember, ["remote", "set-url", "origin",
                        str(tmp_path / "does-not-exist.git")])

        result = _run(slug_dir, project, home, _config(tmp_path, enabled=True))
        state = _wait_for_fetch(remember)
        assert state["rc"] != "0", "fixture: the fetch against a dead remote succeeded"

        # Second session start: now there is a failure record to report on.
        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        log = _log_text(slug_dir)
        assert "could NOT check" in log, (
            "a store whose fetch is failing was not told apart from a store "
            "that is genuinely current — this is exactly the defect part 1 "
            "fixed, rebuilt on the read side\n--- log ---\n" + log
        )
        assert result.returncode == 0

    def test_the_first_ever_run_says_it_has_not_fetched_yet(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        log = _log_text(slug_dir)
        assert "no fetch has completed yet" in log, (
            "the very first run compared against refs nothing had fetched and "
            "said nothing about it\n--- log ---\n" + log
        )

    def test_a_missing_remote_ref_is_not_up_to_date(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)

        _run(slug_dir, project, home,
             _config(tmp_path, enabled=True, branch="no-such-branch"))

        log = _log_text(slug_dir)
        assert "nothing to restore FROM" in log, (
            "a branch that does not exist on the remote read as a clean, "
            "current store\n--- log ---\n" + log
        )
        assert "already up to date" not in log

    def test_an_unborn_local_branch_is_refused_out_loud(self, tmp_path):
        """The fixture hazard part 1 hit, in its production form: a store whose
        branch has no commits. Every comparison below it answers vacuously."""
        home = tmp_path / "home"
        remember = home / ".remember"
        slug_dir = remember / "test-slug"
        slug_dir.mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        _git(remember, ["init", "-q", "-b", "main"])
        remote = home / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)],
                       check=True, capture_output=True)
        _git(remember, ["remote", "add", "origin", str(remote)])

        result = _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        assert result.returncode == 0, result.stderr
        log = _log_text(slug_dir)
        assert "unborn" in log, (
            "an unborn branch produced no report — the hook decided something "
            "about a repo with no commits\n--- log ---\n" + log
        )

    def test_up_to_date_says_so_only_after_a_real_fetch(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _fetch_now(remember)
        (hook_state(remember, FETCH_STATE, create_dir=True)).write_text(
            f"started={int(time.time())}\nfinished={int(time.time())}\nrc=0\n",
            encoding="utf-8",
        )

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        log = _log_text(slug_dir)
        assert "already up to date" in log, "--- log ---\n" + log
        assert "could NOT check" not in log

    def test_an_abandoned_fetch_is_reported(self, tmp_path):
        """A detached process nobody is watching is the house defect. If it
        dies, hangs or is killed, the NEXT session has to be able to say so."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _fetch_now(remember)
        (hook_state(remember, FETCH_STATE, create_dir=True)).write_text(
            f"started={int(time.time()) - 9999}\n", encoding="utf-8"
        )

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        log = _log_text(slug_dir)
        assert "never completed" in log, (
            "a background fetch that never came back was indistinguishable "
            "from one that returned nothing new\n--- log ---\n" + log
        )

    def test_a_leading_zero_start_time_is_read_as_decimal(self, tmp_path):
        """#327, the restore half. `started=08` is all digits, so it clears the
        digits-only guard in `_fetch_health` and is then read as OCTAL.

        The arithmetic fails, bash abandons the whole `if [ -z "$_f" ]` body,
        and control falls through to the `rc` branch with `_rc` empty — so a
        fetch that never came back is reported as one that FAILED with an
        unknown status. The same three-state vocabulary, the wrong state, and
        the remedy the user is handed ("run git fetch to see git's own error")
        is for a failure that did not happen.

        `date +%s` never emits a leading zero, so this is reachable only through
        a corrupt or truncated state file — which is that guard's whole premise.
        """
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _fetch_now(remember)
        (hook_state(remember, FETCH_STATE, create_dir=True)).write_text(
            "started=08\n", encoding="utf-8"
        )

        result = _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        for phrase in ("syntax error", "invalid arithmetic operator",
                       "value too great for base", "unbound variable"):
            assert phrase not in result.stderr, (
                "the state file reached the arithmetic evaluator — bash said "
                f"{phrase!r}: {result.stderr.strip()}"
            )
        log = _log_text(slug_dir)
        assert "never completed" in log, (
            "a fetch with a leading-zero start time was not reported as "
            "abandoned — the octal read fell through to the FAILED branch, so "
            "the session is told a fetch failed when none did\n--- log ---\n" + log
        )

    def test_a_future_dated_start_time_does_not_stick_the_fetch_in_flight(self, tmp_path):
        """#326's fifth site, found reviewing PR #336 rather than by the issue.

        #326 enumerates four cooldown gates and stops. The fetch state file has
        the identical shape and was not on the list: `started=` is read through
        the same digits-only `case`, which bounds its SYNTAX and not its RANGE.

        A `started` ahead of now makes `_age` negative, `-lt "$FETCH_TIMEOUT"`
        is then true, and `_fetch_health` answers `in-flight` — the one state
        that means "wait, something is already running". Nothing is running.

        And it compounds the way the four named sites do: `_spawn_fetch` runs
        the same comparison and takes its `return 0`, so the replacement fetch
        is never started — and the ONLY writers of the state file are inside
        the subshell that early return skips. The self-heal is unreachable from
        the path that needs it, exactly as at the other four.
        """
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _fetch_now(remember)
        (hook_state(remember, FETCH_STATE, create_dir=True)).write_text(
            f"started={int(time.time()) + 3600}\n", encoding="utf-8"
        )

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        log = _log_text(slug_dir)
        assert "never completed" in log, (
            "a fetch whose start time is an hour in the future was reported as "
            "IN-FLIGHT. Nothing is in flight; the clock moved. The session is "
            "told to wait for a process that does not exist, and will be told "
            "the same thing every session until the wall clock catches up."
            "\n--- log ---\n" + log
        )

    def test_a_future_dated_start_time_is_replaced_by_a_real_fetch(self, tmp_path):
        """The structural half: the state file has to become writable again.

        Reporting the right state is not enough. `_spawn_fetch` must decline to
        treat a future `started` as a live claim on the job, or nothing ever
        rewrites the file and the wrong state is permanent.
        """
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _fetch_now(remember)
        state = hook_state(remember, FETCH_STATE, create_dir=True)
        future = int(time.time()) + 3600
        state.write_text(f"started={future}\n", encoding="utf-8")

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        deadline = time.monotonic() + 60
        started = future
        while time.monotonic() < deadline:
            body = dict(
                line.split("=", 1)
                for line in state.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            started = int(body.get("started", future))
            if started != future:
                break
            time.sleep(0.2)

        assert started <= int(time.time()) + 2, (
            f"the fetch state file still holds a future start time ({started}) "
            "— _spawn_fetch took its early return, so no fetch was spawned, so "
            "nothing rewrote the file. The store never fetches again."
        )


# ── The detached fetch ───────────────────────────────────────────────────────


class TestTheDetachedFetch:

    def test_it_actually_fetches(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        remote_tip = _advance_remote(tmp_path, remote)

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))
        state = _wait_for_fetch(remember)

        assert state["rc"] == "0", f"the detached fetch failed: {state}"
        got = subprocess.run(
            ["git", "-C", str(remember), "rev-parse", "refs/remotes/origin/main"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert got == remote_tip, (
            "the detached fetch recorded success without moving the "
            "remote-tracking ref"
        )

    def test_the_next_session_restores_what_it_fetched(self, tmp_path):
        """The whole latency design in one test: session N fetches in the
        background, session N+1 fast-forwards with no network at all."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        remote_tip = _advance_remote(tmp_path, remote)

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))
        _wait_for_fetch(remember)
        assert _head(remember) != remote_tip, (
            "the first session restored — the fetch was NOT detached, and the "
            "network is on the critical path after all"
        )

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        assert _head(remember) == remote_tip, (
            "the second session did not restore what the first one fetched"
        )

    def test_a_dead_remote_never_interrupts_the_human(self, tmp_path):
        """The opposite failure. A backup that gets noisy on every flaky
        network is one nobody reads."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _git(remember, ["remote", "set-url", "origin",
                        str(tmp_path / "does-not-exist.git")])

        for _ in range(5):
            _run(slug_dir, project, home, _config(tmp_path, enabled=True))
            _wait_for_fetch(remember)

        assert not (slug_dir / "tmp" / NOTICE_NAME).exists(), (
            "an unreachable remote interrupted the human — only a divergence, "
            "which is permanent until someone acts, may do that"
        )

    def test_it_does_not_stack_fetches(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        (hook_state(remember, FETCH_STATE, create_dir=True)).write_text(
            f"started={int(time.time())}\n", encoding="utf-8"
        )

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))
        time.sleep(1)

        body = (hook_state(remember, FETCH_STATE, create_dir=True)).read_text(encoding="utf-8")
        assert "finished" not in body, (
            "a second fetch was spawned on top of one still inside its timeout "
            "window — session starts are frequent and this is how you get a "
            "pile of git processes"
        )


# ── Never destroy local work ─────────────────────────────────────────────────


class TestItCannotDestroyLocalWork:

    def test_no_destructive_verb_appears_anywhere_in_the_file(self):
        """Structural, and permanent. A fast-forward cannot destroy local work
        by definition — but only for as long as nothing else in this file can.
        Part 1 has the mirror of this for fetch/pull/merge/rebase in the backup
        hook; the damage here is silent in exactly the same way."""
        body = HOOK.read_text(encoding="utf-8")
        # An ALLOWLIST of git subcommands, not a denylist of dangerous ones.
        # A denylist has to guess what the next dangerous verb is called, and
        # this file is a poor place to guess in: its own log tag is
        # "git-restore" and its refusals say the words "push", "merge" and
        # "rebase" out loud, because saying them is what makes the refusal
        # legible. So prose is excluded, and what remains must be a subcommand
        # that was reviewed.
        code = "\n".join(
            line for line in body.splitlines()
            if not line.lstrip().startswith("#")
            and 'log "git-restore"' not in line
            and "printf " not in line
        )
        invoked = set(re.findall(r"\bgit\b(?:\s+-[Cc]\s+\S+)*\s+([a-z][a-z-]*)", code))
        assert invoked, "no git invocation found at all — the scan is vacuous"
        allowed = {"rev-parse", "symbolic-ref", "rev-list", "merge", "fetch"}
        assert invoked <= allowed, (
            f"50-git-restore.sh invokes {sorted(invoked - allowed)} — the "
            "restore half may only read refs and fast-forward (#253). A "
            "fast-forward cannot destroy local work by definition, but only "
            "for as long as nothing else in this file can."
        )
        assert 'merge --ff-only "$REMOTE_REF"' in code, (
            "the only merge is no longer pinned to --ff-only, which is the "
            "entire safety property"
        )
        assert code.count("merge") == 1, (
            "a second merge invocation appeared and this test only vouches "
            "for the first"
        )

    def test_a_dirty_worktree_is_not_overwritten(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _fetch_now(remember)
        # Uncommitted local edit to a file the incoming commit also touches is
        # the dangerous shape; git refuses, and the hook must not "help".
        (remember / "from-another-machine.md").write_text(
            "MINE, uncommitted\n", encoding="utf-8"
        )

        result = _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        assert result.returncode == 0, result.stderr
        assert (remember / "from-another-machine.md").read_text(encoding="utf-8") \
            == "MINE, uncommitted\n", (
            "uncommitted local work was overwritten by the restore\n--- log ---\n"
            + _log_text(slug_dir)
        )
        log = _log_text(slug_dir)
        assert "REFUSED by git" in log, (
            "the refused fast-forward was not reported\n--- log ---\n" + log
        )

    def test_it_never_writes_to_the_remote(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        remote_tip = _advance_remote(tmp_path, remote)
        _fetch_now(remember)

        _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        assert _remote_head(remote) == remote_tip, (
            "the restore half pushed — reading and writing are separate hooks "
            "on purpose"
        )


# ── Interaction with the backup half ─────────────────────────────────────────


class TestItSharesTheBackupLock:
    """A session start and a save now touch one repo from two lifecycle events.
    The fast-forward moves HEAD and rewrites the working tree; a backup may be
    part-way through `git add`/`git commit` against the same index."""

    def test_the_fast_forward_takes_the_backup_lock(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _fetch_now(remember)
        before = _head(remember)

        lock = hook_state(remember, ".git-backup.lock", create_dir=True)
        # Hold it the way the backup hook's fallback path does.
        with open(lock, "w") as fh:
            fh.write(str(os.getpid()))
            fh.flush()
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:  # pragma: no cover - platform dependent
                pass
            result = _run(slug_dir, project, home, _config(tmp_path, enabled=True))

        assert result.returncode == 0, result.stderr
        assert _head(remember) == before, (
            "the restore fast-forwarded while the backup lock was held — the "
            "two halves can interleave a merge with a commit on one index"
        )
        assert "store busy" in _log_text(slug_dir)

    def test_it_uses_the_same_lock_file_as_the_backup(self):
        """A second, private lock would be no lock at all.

        Since #261 the lock lives in the git common dir rather than at the store
        root, so the two halves agree by both deriving it from that directory —
        which every worktree of a repo shares. Matching on the basename alone
        would pass on two locks sitting in two different directories, which is
        exactly the failure this test exists to catch.
        """
        body = HOOK.read_text(encoding="utf-8")
        backup = (REPO_ROOT / "hooks.d" / "after_save"
                  / "50-git-backup.sh").read_text(encoding="utf-8")
        pattern = r'LOCK_FILE="\$G[BR]_STATE_DIR/git-backup\.lock"'
        assert re.search(pattern, body), (
            "the restore takes a different lock from the backup, so the two "
            "cannot exclude each other"
        )
        assert re.search(pattern, backup), (
            "the backup half no longer names the lock the restore half expects"
        )
        for name, text in (("restore", body), ("backup", backup)):
            assert 'BACKUP_COMMON_DIR/remember' in text, (
                f"the {name} half derives its state directory from something "
                "other than the shared git common dir, so the two locks can "
                "land in different places"
            )


class TestTheDetachedFetchDoesNotHoldTheLock:
    """The detached fetch must not inherit the backup lock.

    `_take_lock` holds its flock on fd 9, and a forked child inherits every
    open descriptor — an inherited fd holds the SAME lock. So the background
    fetch kept the whole store locked for its entire life: every session start
    inside `fetch_timeout_seconds` logged "store busy" and skipped, and any
    `after_save` backup landing in that window was blocked too. That is
    precisely the failure the hook's own comments argue the fetch must not
    cause — holding a repo-wide lock across network I/O.

    Invisible on macOS, which has no flock(1) and takes the noclobber fallback
    where nothing is inherited. Total on Linux. These tests force the flock
    branch on both.
    """

    def test_a_second_session_start_is_not_locked_out(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _diverge_local(remember)
        _fetch_now(remember)
        env = _flock_env(tmp_path)
        cfg = _config(tmp_path, enabled=True)

        _run(slug_dir, project, home, cfg, **env)
        _run(slug_dir, project, home, cfg, **env)

        assert "store busy" not in _log_text(slug_dir), (
            "the second session start was locked out by the FIRST one's "
            "detached fetch — the fetch inherited the lock fd, so nothing "
            "that counts consecutive runs can ever pass 1"
        )

    def test_the_divergence_counter_can_pass_one(self, tmp_path):
        """The consequence, stated as the thing users lose: a threshold that
        can never be reached is a report that never arrives."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _diverge_local(remember)
        _fetch_now(remember)
        env = _flock_env(tmp_path)
        cfg = _config(tmp_path, enabled=True)

        for _ in range(3):
            _run(slug_dir, project, home, cfg, **env)

        count = (hook_state(remember, ".git-restore-diverged")).read_text(encoding="utf-8").strip()
        assert count == "3", (
            f"three runs against a permanently diverged store counted {count!r} "
            "— the escalation can never fire and the refusal is logged forever "
            "with nothing to raise it"
        )

    def test_a_backup_can_still_take_the_lock_during_the_fetch(self, tmp_path):
        """The other half of the damage. The restore's fetch must not block the
        backup half, which is the thing that actually gets memory off the box."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _advance_remote(tmp_path, remote)
        _fetch_now(remember)
        env = _flock_env(tmp_path)

        _run(slug_dir, project, home,
             _config(tmp_path, enabled=True, fetch_timeout_seconds=30), **env)

        # Immediately after the hook returns, its fetch is still in flight.
        # Whoever wants the store next must be able to have it.
        lock = hook_state(remember, ".git-backup.lock", create_dir=True)
        with open(lock, "a+") as fh:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:  # pragma: no cover - the bug being fixed
                raise AssertionError(
                    "the detached fetch is still holding the backup lock, so "
                    "an after_save backup starting now would silently skip"
                )


# ── The session-start budget ─────────────────────────────────────────────────


class TestSessionStartIsNotBlocked:
    """#227 was a plugin blocking ~8.7s on every prompt and #204/#230 are the
    same family. This hook sits in front of the user's first prompt, so a
    restore that costs seconds would be a bigger regression than the bug it
    fixes — and it would hit every user who enables it."""

    def test_the_synchronous_half_does_no_network_io(self, tmp_path):
        """The hook is pointed at a remote that black-holes. If the fetch were
        synchronous, this would take the full timeout."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        _fetch_now(remember)

        started = time.monotonic()
        result = _run(slug_dir, project, home,
                      _config(tmp_path, enabled=True, fetch_timeout_seconds=30),
                      **{"GIT_CONFIG_COUNT": "1",
                         "GIT_CONFIG_KEY_0": "remote.origin.url",
                         "GIT_CONFIG_VALUE_0": "https://10.255.255.1/black-hole.git"})
        elapsed = time.monotonic() - started

        assert result.returncode == 0, result.stderr
        assert elapsed < 10, (
            f"session start blocked {elapsed:.1f}s against an unroutable "
            "remote — the fetch is on the critical path"
        )

    def test_the_disabled_path_is_cheap(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)

        started = time.monotonic()
        for _ in range(5):
            _run(slug_dir, project, home, _config(tmp_path))
        elapsed = (time.monotonic() - started) / 5

        assert elapsed < 1.0, (
            f"the default-off path costs {elapsed * 1000:.0f}ms per session "
            "start for users who never asked for this feature"
        )
