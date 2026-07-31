"""A rejected push is not a deferred push (issue #253, part 1).

`50-git-backup.sh` funnelled every non-zero `git push` into one line:

    push deferred (will retry next backup)

That is true for a network blip. It is false for a non-fast-forward rejection,
which is what you get the moment the remote has moved ahead: that push cannot
succeed on any later attempt, no later attempt will fix it, and the backup has
in fact stopped. The reporter ran twelve days believing memory was being backed
up — 15 `push deferred` against 8 `pushed` — with nothing anywhere saying
otherwise.

Two properties are load-bearing here and they pull in opposite directions:

  * A permanent rejection must NOT render as a transient deferral. This is the
    test that fails on the old code; a test that only exercises an unreachable
    remote passes on the broken version and proves nothing.
  * An ordinary transient failure must stay exactly as quiet as it was. A
    backup that gets noisy on every flaky network is a backup nobody reads the
    logs of, and the escalation channel here is the one surface the human
    cannot ignore. Being wrong in that direction is worse than the bug.

The classification comes from `git push --porcelain`'s per-ref status lines,
never from free text — the discipline claude-supertool#641 settled after a
pre-push hook printing "fetch first" was read as a remote rejection. So these
tests use REAL repositories: a real bare remote advanced by a real second
clone, producing a real `!  <ref>  [rejected] (fetch first)` from git itself.
Mocking that output would pin our own parser against a string we wrote, which
is exactly the failure claude-supertool#649 documents.
"""

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

from .test_git_backup_hook import (  # noqa: E402
    HOOK,
    REPO_ROOT,
    _git,
    _run_hook,
    make_external_remember_repo,
    wait_for_lock_release,
)

USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"

DEFERRED = "push deferred"
NOTICE_NAME = "git-backup-notice"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, name: str = "remember-config.json", **git_backup) -> Path:
    cfg = tmp_path / name
    body = {"cooldowns": {"git_backup_seconds": 0}}
    if git_backup:
        body["git_backup"] = git_backup
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def _remote_head(remote: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _advance_remote(tmp_path: Path, remote: Path) -> str:
    """Move the bare remote ahead of the local store, the way a second machine
    would. Returns the remote's new tip.

    Asserted, not assumed: a fixture that quietly failed to diverge would leave
    every test below passing against a push that was never rejected at all.
    """
    before = _remote_head(remote)
    other = tmp_path / f"other-machine-{before[:7]}"
    # -b main explicitly: the bare remote's HEAD is whatever `git init --bare`
    # picked from the runner's init.defaultBranch, which is not necessarily the
    # branch the store actually pushed. Cloning without it silently lands on an
    # unborn `master` and the commit below would go somewhere harmless.
    subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)],
                   check=True, capture_output=True)
    _git(other, ["config", "user.email", "other@test"])
    _git(other, ["config", "user.name", "Other"])
    (other / "from-another-machine.md").write_text(
        f"elsewhere, on top of {before}\n", encoding="utf-8"
    )
    _git(other, ["add", "-A"])
    _git(other, ["commit", "-q", "-m", "from another machine"])
    _git(other, ["push", "-q", "origin", "main"])
    after = _remote_head(remote)
    assert after != before, (
        "fixture did not diverge the remote — no push below can be rejected, "
        "so every assertion here would be vacuous"
    )
    return after


def _log_text(slug_dir: Path) -> str:
    files = list((slug_dir / "logs").glob("memory-*.log"))
    assert files, "hook wrote no log file at all"
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


def _store(tmp_path: Path, *, diverge: bool = True):
    """A memory store, a bare remote, and (by default) a remote that has moved
    ahead so the next push is genuinely non-fast-forward."""
    home, remember, remote = make_external_remember_repo(tmp_path)
    slug_dir = remember / "test-slug"
    slug_dir.mkdir()
    (slug_dir / "now.md").write_text("## 10:00 | test\nMemory.\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    if diverge:
        _advance_remote(tmp_path, remote)
    return home, remember, remote, slug_dir, project


def _save_again(remember: Path, slug_dir: Path, n: int) -> None:
    """New content plus a cleared cooldown, so the next hook run does real work."""
    (remember / ".last-git-backup-ts").write_text("0", encoding="utf-8")
    (slug_dir / "now.md").write_text(f"## 10:0{n} | test\nMemory {n}.\n", encoding="utf-8")


def _run(slug_dir, project, home, remember, cfg):
    result = _run_hook(slug_dir, project, home, config_path=cfg)
    wait_for_lock_release(remember / ".git-backup.lock")
    return result


# ── The defect ───────────────────────────────────────────────────────────────


class TestARejectionIsNotADeferral:

    def test_a_non_fast_forward_rejection_is_not_reported_as_transient(self, tmp_path):
        """The load-bearing one. This fails on the code #253 reports."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path)

        _run(slug_dir, project, home, remember, cfg)

        log = _log_text(slug_dir)
        assert DEFERRED not in log, (
            "a non-fast-forward rejection was logged as a transient deferral: "
            "it promises a retry that cannot succeed, and the backup has "
            "actually stopped\n--- log ---\n" + log
        )
        assert "REJECTED" in log, (
            "nothing in the log says the push was rejected\n--- log ---\n" + log
        )
        assert "stopped" in log.lower(), (
            "the log does not say the backup has stopped — the whole point of "
            "#253 is that a stopped backup looked like a running one"
        )

    def test_the_rejected_push_really_was_rejected(self, tmp_path):
        """Proof the scenario is the reported one and not a fixture artefact:
        the remote does not move, and the memory commit stays local-only."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        expected = _remote_head(remote)
        cfg = _config(tmp_path)

        _run(slug_dir, project, home, remember, cfg)

        assert _remote_head(remote) == expected, "the remote moved — nothing was rejected"
        local = subprocess.run(
            ["git", "-C", str(remember), "log", "--oneline", "-1", "--", "test-slug/"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "auto: test-slug" in local, (
            "the hook did not even commit locally, so this test is not "
            "exercising the push path at all"
        )

    def test_the_rejection_says_what_to_do_and_promises_no_auto_resolution(self, tmp_path):
        """#253 asks explicitly that nothing auto-merge or auto-rebase: recent.md
        and archive.md are rewritten wholesale by consolidation, so a wrong
        resolution corrupts memory silently. Refusing and reporting is the
        deliverable, and the report has to be actionable."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path)

        _run(slug_dir, project, home, remember, cfg)

        log = _log_text(slug_dir)
        assert "git -C" in log, (
            "the rejection names no command the human can run to see the state "
            "for themselves\n--- log ---\n" + log
        )

    def test_the_hook_never_fetches_merges_or_rebases(self, tmp_path):
        """The fix must not grow a resolution path. Structural, because the
        damage is silent: a wrong auto-resolution looks exactly like a clean
        backup."""
        body = HOOK.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        for forbidden in ("git -C \"$REPO_ROOT\" fetch", "git -C \"$REPO_ROOT\" pull",
                          "git -C \"$REPO_ROOT\" merge", "git -C \"$REPO_ROOT\" rebase"):
            assert forbidden not in code, (
                f"{forbidden!r} — the backup hook must never resolve a divergence "
                "on its own (#253)"
            )


# ── The opposite failure: a blip must stay quiet ─────────────────────────────


class TestATransientFailureStaysQuiet:
    """claude-remember#204: a reporter's fix that worked on their machine would
    have caused a silent total outage elsewhere. Making an ordinary network
    failure loud, or fatal, would be the same mistake in a new place."""

    def test_an_unreachable_remote_is_still_a_deferral(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path, diverge=False)
        _git(remember, ["remote", "set-url", "origin", "/nonexistent/path.git"])
        cfg = _config(tmp_path)

        _run(slug_dir, project, home, remember, cfg)

        log = _log_text(slug_dir)
        assert DEFERRED in log, (
            "an unreachable remote stopped being reported as a retry — it will "
            "succeed on the next backup and must stay quiet\n--- log ---\n" + log
        )
        assert "REJECTED" not in log, "a network failure was called a rejection"

    def test_an_unreachable_remote_never_interrupts_the_human(self, tmp_path):
        """Not once, not after five. The escalation surface is the one output
        the human cannot ignore; a flaky network must never reach it."""
        home, remember, remote, slug_dir, project = _store(tmp_path, diverge=False)
        _git(remember, ["remote", "set-url", "origin", "/nonexistent/path.git"])
        cfg = _config(tmp_path)

        for n in range(5):
            _save_again(remember, slug_dir, n)
            _run(slug_dir, project, home, remember, cfg)

        assert not (slug_dir / "tmp" / NOTICE_NAME).exists(), (
            "five network failures raised a notice at the human — this is the "
            "#204 shape: a fix that makes the ordinary case louder"
        )

    def test_the_hook_still_exits_zero_on_a_rejection(self, tmp_path):
        """It runs on the after_save dispatch. Loud is the message, never the
        exit status."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path)

        result = _run(slug_dir, project, home, remember, cfg)

        assert result.returncode == 0, (
            f"hook exited {result.returncode}: {result.stderr}"
        )


# ── Escalation ───────────────────────────────────────────────────────────────


class TestEscalationToTheHuman:
    """A log line is demonstrably not enough — that is the whole issue. But
    `systemMessage` is the most intrusive surface in the codebase and using it
    for everything destroys the value of using it for anything, so it fires on
    a repeated rejection rather than the first one.

    The delay costs nothing real: once a push is rejected it stays rejected
    until a human acts, so the threshold postpones the notice by a few backups
    and can never swallow it. What it does buy is that nothing accidental can
    ever reach that channel.
    """

    def test_one_rejection_does_not_yet_interrupt_the_human(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path)

        _run(slug_dir, project, home, remember, cfg)

        assert "REJECTED" in _log_text(slug_dir), "the log did not record the rejection"
        assert not (slug_dir / "tmp" / NOTICE_NAME).exists(), (
            "the first rejection went straight to the human channel"
        )

    def test_repeated_rejections_reach_the_human(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path)

        for n in range(3):
            _save_again(remember, slug_dir, n)
            _run(slug_dir, project, home, remember, cfg)

        notice = slug_dir / "tmp" / NOTICE_NAME
        assert notice.exists(), (
            "three consecutive permanent rejections and the human was never "
            "told — this is #253's twelve silent days"
        )
        text = notice.read_text(encoding="utf-8")
        assert "backup" in text.lower()
        assert "stopped" in text.lower(), "the notice does not say the backup has stopped"

    def test_the_threshold_is_configurable(self, tmp_path):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path, reject_notice_after=1)

        _run(slug_dir, project, home, remember, cfg)

        assert (slug_dir / "tmp" / NOTICE_NAME).exists(), (
            "git_backup.reject_notice_after=1 did not bring the notice forward"
        )

    def test_a_successful_push_clears_the_count(self, tmp_path):
        """The counter is consecutive rejections, not lifetime ones. A store
        that was repaired must start from zero, or the next single rejection
        arrives pre-escalated."""
        home, remember, remote, slug_dir, project = _store(tmp_path)
        cfg = _config(tmp_path, reject_notice_after=2)

        _save_again(remember, slug_dir, 1)
        _run(slug_dir, project, home, remember, cfg)
        assert not (slug_dir / "tmp" / NOTICE_NAME).exists()

        # Repair the store the way a human would, by hand.
        _git(remember, ["fetch", "-q", "origin"])
        _git(remember, ["rebase", "-q", "origin/main"])

        _save_again(remember, slug_dir, 2)
        _run(slug_dir, project, home, remember, cfg)
        assert "pushed" in _log_text(slug_dir), "the repaired store still did not push"

        _advance_remote(tmp_path, remote)
        _save_again(remember, slug_dir, 3)
        _run(slug_dir, project, home, remember, cfg)

        assert not (slug_dir / "tmp" / NOTICE_NAME).exists(), (
            "a single rejection after a successful push escalated immediately — "
            "the count survived the repair"
        )

    def test_the_notice_reaches_the_human_as_a_system_message(self, tmp_path):
        """Delivery, not just authorship. `systemMessage` is the only hook
        output the HUMAN sees; a notice only the model sees is how #200 stayed
        invisible for a day, and it is how this stayed invisible for twelve."""
        home = tmp_path / "home"
        project = tmp_path / "project"
        remember = project / ".remember"
        (remember / "tmp").mkdir(parents=True)
        (home / ".claude").mkdir(parents=True)
        (remember / "tmp" / NOTICE_NAME).write_text(
            "remember: git backup has STOPPED\n", encoding="utf-8"
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
        assert "STOPPED" in payload.get("systemMessage", ""), (
            "the git-backup notice was not delivered on systemMessage — the one "
            "channel the human actually sees\n" + result.stdout
        )
        assert not (remember / "tmp" / NOTICE_NAME).exists(), (
            "the notice was not consumed on read; it would repeat on every prompt"
        )


# ── One decision, not three ──────────────────────────────────────────────────


class TestEveryPushSiteClassifies:
    """#253 names one message; the funnel had three mouths (the first push to a
    newly recorded remote, the allow_remote_change override, and the steady
    state). Fixing one and leaving two would leave the reporter's exact case
    reachable through the other doors."""

    def _run_rejecting(self, tmp_path, *, state, cfg):
        home, remember, remote, slug_dir, project = _store(tmp_path)
        if state is not None:
            (remember / ".git-backup-remote").write_text(
                state.format(remote=remote), encoding="utf-8"
            )
        _run(slug_dir, project, home, remember, cfg)
        return _log_text(slug_dir)

    def test_first_push_site(self, tmp_path):
        log = self._run_rejecting(tmp_path, state=None, cfg=_config(tmp_path))
        assert DEFERRED not in log and "REJECTED" in log, log

    def test_steady_state_site(self, tmp_path):
        log = self._run_rejecting(tmp_path, state="{remote}", cfg=_config(tmp_path))
        assert "git backup configured to push to" not in log, (
            "this did not exercise the steady-state site"
        )
        assert DEFERRED not in log and "REJECTED" in log, log

    def test_allow_remote_change_site(self, tmp_path):
        cfg = _config(tmp_path, allow_remote_change=True)
        log = self._run_rejecting(tmp_path, state="/some/other/url.git", cfg=cfg)
        assert "allow_remote_change=true" in log, (
            "this did not exercise the override site"
        )
        assert DEFERRED not in log and "REJECTED" in log, log

    def test_the_three_sites_share_one_decision(self):
        """Structural backstop: the classification lives in one place, so the
        next person to add a fourth push site cannot reintroduce the funnel by
        copy-paste."""
        body = HOOK.read_text(encoding="utf-8")
        code = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
        occurrences = [line for line in code if DEFERRED in line]
        assert len(occurrences) == 1, (
            "the deferral message appears at more than one site, so the three "
            f"push paths can drift apart again (#253): {occurrences}"
        )


# ── The channel the verdict is read from ─────────────────────────────────────


class TestTheVerdictComesFromGitsOwnChannel:
    """claude-supertool#641: `_is_non_fast_forward` scanned merged stdout+stderr
    for the phrase "fetch first", so a pre-push hook printing that phrase in its
    own advice was read as a remote rejection. The answer there was git's
    machine-readable per-ref status. Same answer here."""

    def test_the_hook_asks_git_for_porcelain_status(self):
        body = HOOK.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        assert "--porcelain" in code, (
            "the push verdict is not read from git's machine-readable channel"
        )

    def test_a_pre_push_hook_saying_fetch_first_is_not_a_rejection(self, tmp_path):
        """The #641 case, reproduced against a real hook on a real push that
        really succeeds."""
        home, remember, remote, slug_dir, project = _store(tmp_path, diverge=False)
        hooks = remember / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        pre_push = hooks / "pre-push"
        pre_push.write_text(
            "#!/bin/sh\n"
            "echo 'policy: rejected — non-fast-forward, fetch first'\n"
            "echo 'policy: rejected — non-fast-forward, fetch first' >&2\n"
            "exit 0\n",
            encoding="utf-8",
        )
        pre_push.chmod(0o755)
        cfg = _config(tmp_path)

        _run(slug_dir, project, home, remember, cfg)

        log = _log_text(slug_dir)
        assert "pushed" in log, (
            "a push git accepted was not reported as pushed — the hook's own "
            "words reached the verdict (#641)\n--- log ---\n" + log
        )
        assert "REJECTED" not in log
        assert _remote_head(remote) != "", "fixture: remote has no main"
