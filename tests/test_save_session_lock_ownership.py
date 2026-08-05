"""A session that correctly skips must not delete the lock holder's lock file.

``save-session.sh`` installs ``trap cleanup EXIT`` *before* acquiring
``save.lock``, and ``cleanup()`` used to unlink ``$LOCK_FILE``
unconditionally. So the "someone else holds it, skipping" path (``exit 0``)
also ran that same trap — and deleted the *holder's* lock file, not its own
(it never held one). A third process racing in afterward then found no lock
file at all and acquired cleanly, while the first process was still running:
two concurrent ``save-session.sh``, two Haiku calls, two position writes.

The fix is a ``HAVE_LOCK`` flag, set on both acquisition paths (fresh
acquire, and stale-lock takeover) and checked before ``cleanup()`` unlinks
anything. A process that skips because another PID holds the lock never
sets it, so its cleanup is a no-op on the lock file.

This test reproduces the bug with three real processes: P1 acquires the
lock and blocks (a slow stub "extract" call stands in for the real
extraction/Haiku work); P2 starts while P1 holds it, sees the lock, and
exits 0 (skip); P3 starts after P2 has fully exited. Before the fix, P2's
exit deleted P1's lock file, so P3 acquired and ran concurrently with P1.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Same shape as the shared STUB_SHELL in test_save_session_gates.py, plus a
# configurable sleep in "extract" so a process can be made to hold save.lock
# for as long as the test needs.
STUB_SHELL = '''\
import os, sys, tempfile, time, json

CALLS = os.environ["STUB_CALLS_LOG"]
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
with open(CALLS, "a") as f:
    f.write(" ".join([cmd] + sys.argv[2:]) + "\\n")

if cmd == "extract":
    hold = os.environ.get("STUB_EXTRACT_HOLD_FILE", "")
    if hold:
        cap = time.monotonic() + float(os.environ.get("STUB_EXTRACT_HOLD_CAP", "60"))
        while not os.path.exists(hold) and time.monotonic() < cap:
            time.sleep(0.01)
    sleep_s = float(os.environ.get("STUB_EXTRACT_SLEEP", "0"))
    if sleep_s:
        time.sleep(sleep_s)
    fd, path = tempfile.mkstemp(suffix="-extract")
    with os.fdopen(fd, "w") as f:
        f.write("Human: something\\nAssistant: something else\\n")
    print("POSITION=500")
    print("HUMAN_COUNT=5")
    print("ASSISTANT_COUNT=1")
    print("EXCHANGE_COUNT=6")
    print(f"EXTRACT_FILE={path}")
elif cmd == "save-position":
    last_save_file, session_id, position = sys.argv[2], sys.argv[3], sys.argv[4]
    with open(last_save_file, "w") as f:
        json.dump({"session": session_id, "line": int(position)}, f)
elif cmd == "build-prompt":
    with open(sys.argv[6], "w") as f:
        f.write("a prompt with no placeholders\\n")
elif cmd == "call-haiku":
    fd, path = tempfile.mkstemp(suffix="-haiku")
    with os.fdopen(fd, "w") as f:
        f.write("SKIP\\n")
    print("IS_SKIP=true")
    print(f"HAIKU_TEXT_FILE={path}")
    print("TK_IN=0"); print("TK_OUT=0"); print("TK_CACHE=0"); print("TK_COST=0")
'''


def _make_env(tmp_path: Path, name: str, extract_sleep: float = 0.0, hold_file=None):
    project = tmp_path / "project"
    (project / ".remember" / "tmp").mkdir(parents=True, exist_ok=True)
    (project / ".remember" / "logs").mkdir(parents=True, exist_ok=True)

    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True, exist_ok=True)
    (plugin / "pipeline").mkdir(parents=True, exist_ok=True)
    (plugin / "pipeline" / "__init__.py").write_text("")
    (plugin / "pipeline" / "haiku.py").write_text("# marker\n")
    (plugin / "pipeline" / "shell.py").write_text(STUB_SHELL)
    for script in ("save-session.sh", "resolve-paths.sh", "detect-tools.sh",
                   "bootstrap-dirs.sh", "log.sh", "lib-memory-dir.sh",
                   "lib-lock.sh", "lib-staging-lock.sh", "lib-slug.sh",
                   "lib-clock.sh"):
        (plugin / "scripts" / script).write_text((REPO_ROOT / "scripts" / script).read_text())

    cfg = {"cooldowns": {"save_seconds": 0, "ndc_seconds": 999999},
           "thresholds": {"min_human_messages": 3},
           "features": {"ndc_compression": False}}
    cfg_path = tmp_path / f"config-{name}.json"
    cfg_path.write_text(json.dumps(cfg))
    (plugin / "config.json").write_text(json.dumps(cfg))

    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    slug = str(project).replace("/", "-").replace(".", "-").replace("_", "-")
    session_dir = tmp_path / "home" / ".claude" / "projects" / slug
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"{session_id}.jsonl").write_text('{"type":"user"}\n' * 10)

    calls_log = tmp_path / f"calls-{name}.log"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "REMEMBER_CONFIG": str(cfg_path),
        "STUB_CALLS_LOG": str(calls_log),
        "STUB_EXTRACT_SLEEP": str(extract_sleep),
        "STUB_EXTRACT_HOLD_FILE": str(hold_file) if hold_file else "",
        "STUB_EXTRACT_HOLD_CAP": "60",
    }
    return env, project, plugin, calls_log, session_id


# How long a P1 that still looks alive gets to finish exiting before its
# liveness is believed. `poll()` reads None for the whole of save-session.sh's
# EXIT trap, so a bare poll cannot tell "still inside its hold" from "already
# ran lock_release and is two syscalls from gone" — and that is exactly the
# distinction every assertion below rests on. Paid only on the branch that is
# about to fail or skip, never on the passing path.
_TEARDOWN_GRACE = 2.0


def _p1_still_holding(p1) -> bool:
    """Is P1 still inside its lock window, or already on the way out?"""
    if p1.poll() is not None:
        return False
    try:
        p1.wait(timeout=_TEARDOWN_GRACE)
    except subprocess.TimeoutExpired:
        return True
    return False


def _inconclusive(phase: str, detail: str):
    """Report the third outcome.

    A missing save.lock has two innocent readings and one guilty one, and the
    difference is entirely whether P1 was still holding it. When P1 was not,
    the run has observed nothing about the skip path and must say so — the
    house idiom from #293 in tests/test_lock_primitive.py. Calling it a pass
    would retire a real invariant silently; calling it a failure is #312.
    """
    pytest.skip(
        f"P1 had already exited {phase}, so P1 released save.lock itself on "
        f"the path #168 left alone — {detail}. COVERAGE LOST: the skip-path "
        f"lock-ownership invariant (#168/#182/#185) was NOT checked in this "
        f"run, in either direction."
    )


class TestLockOwnership:

    def _lock_ownership_race(self, tmp_path, release_p1_early=False,
                             release_p1_after_p2=False):
        """The three-process choreography from #168, with P1's hold under the
        test's control rather than the scheduler's.

        P1 used to hold the lock for a fixed ``STUB_EXTRACT_SLEEP=3.0``, and
        everything P2 and P3 had to do — spawn bash, source ten scripts, reach
        a skip branch — had to fit inside that window on whatever runner drew
        the job. It did not always fit (#312). Here P1 blocks in its stub
        ``extract`` until this test creates ``release``, so its hold covers P2
        and P3 by construction and no budget has to be guessed. The stub keeps
        a 60s cap so a defect cannot hang a runner.

        ``release_p1_early`` and ``release_p1_after_p2`` deliberately build the
        two cases the fixed budget used to produce by accident: P1 gone before
        P2 starts, and P1 gone before P3 does.
        """
        release = tmp_path / "release-p1"

        # Build every environment BEFORE P1 starts. Each _make_env writes the
        # same ~10 scripts, config.json and session jsonl into the shared
        # plugin/project tree; doing that while P1 is executing those very
        # files raced with P1, and was most of the work that used to have to
        # fit inside P1's window.
        env2, project, plugin, calls2, sid = _make_env(tmp_path, "p2")
        env3, _, _, calls3, _ = _make_env(tmp_path, "p3")
        env1, _, _, calls1, _ = _make_env(tmp_path, "p1", hold_file=release)

        # Since #182 the lock is a DIRECTORY whose `pid` file names the holder.
        lock_dir = project / ".remember" / "tmp" / "save.lock"
        lock_file = lock_dir / "pid"

        p1 = subprocess.Popen(
            ["bash", str(plugin / "scripts" / "save-session.sh"), sid],
            env=env1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for P1 to actually take the lock.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not lock_file.exists():
                time.sleep(0.05)
            assert lock_file.exists(), "P1 never acquired save.lock"
            p1_holder_pid = lock_file.read_text().strip()
            assert p1_holder_pid == str(p1.pid)

            if release_p1_early:
                release.write_text("go", encoding="utf-8")
                p1.wait(timeout=30)

            # P2: same project/plugin, its own env — must see the lock and skip.
            p2 = subprocess.run(
                ["bash", str(plugin / "scripts" / "save-session.sh"), sid],
                env=env2, capture_output=True, text=True, timeout=60,
            )
            assert p2.returncode == 0

            # THE ASSERTION: P1's lock file must still exist and still name P1,
            # even though P2 has fully exited via the skip path.
            #
            # A present lock is unambiguous on its own: P1 cannot have released
            # it and P2 has exited, so nothing but the invariant holding puts it
            # there. An ABSENT lock is not — P1 releasing it correctly and P2
            # tearing it down produce the identical filesystem state, and only
            # P1's liveness separates them. So the liveness gate is consulted on
            # that branch only, which is also the only branch where its cost
            # matters not at all.
            lock_intact = lock_dir.is_dir() and lock_file.exists()
            if not lock_intact and not _p1_still_holding(p1):
                _inconclusive(
                    "by the time P2 finished",
                    "so the lock's absence accuses nobody",
                )
            assert lock_intact, (
                "P2's skip-path cleanup deleted the lock entirely — "
                "P1 is still running and now unprotected"
            )
            assert lock_file.read_text().strip() == p1_holder_pid, (
                "the lock file no longer names P1 — P2's cleanup tore down "
                "the holder's lock, and a third process could now acquire "
                "while P1 is still running"
            )

            if release_p1_after_p2:
                release.write_text("go", encoding="utf-8")
                p1.wait(timeout=30)

            # P3: races in right after P2 exits, while P1 still holds.
            p3 = subprocess.run(
                ["bash", str(plugin / "scripts" / "save-session.sh"), sid],
                env=env3, capture_output=True, text=True, timeout=60,
            )
            assert p3.returncode == 0

            # P1 held the lock across P2, so P2 had no lock of its own to take
            # and must have skipped. This costs no timing and is direct evidence
            # that the run went through the path being pinned, rather than an
            # inference from the lock still being there.
            p2_calls = Path(calls2).read_text() if Path(calls2).exists() else ""
            assert "extract" not in p2_calls, (
                "P2 ran the extraction pipeline while P1 held the lock — it "
                "never took the skip path, so nothing below tests it"
            )

            # Same three-state problem as above, mirrored: P3 running extract is
            # only an accusation if the lock it took was still P1's. If P1 exited
            # mid-run, P3 acquired a free lock, correctly.
            p3_calls = Path(calls3).read_text() if Path(calls3).exists() else ""
            if "extract" in p3_calls and not _p1_still_holding(p1):
                _inconclusive(
                    "during P3's run",
                    "so P3 acquired a lock that was by then legitimately free",
                )
            assert "extract" not in p3_calls, (
                "P3 ran the extraction pipeline concurrently with P1 — it "
                "acquired a lock that P1 still legitimately holds"
            )
        finally:
            release.write_text("go", encoding="utf-8")
            try:
                p1.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p1.kill()
                p1.wait(timeout=10)

        assert p1.returncode == 0

    def test_skip_path_does_not_delete_holders_lock_file(self, tmp_path):
        """Three real processes: P1 holds the lock and is still running when
        P2 skips and exits; P3 must NOT be able to acquire while P1 is alive."""
        self._lock_ownership_race(tmp_path)

    def test_a_p1_that_already_exited_is_inconclusive_not_a_teardown(self, tmp_path):
        """The absence of the lock only accuses P2 if P1 was still holding it.

        P1 is released and reaped before P2 starts, so P1 unlinks save.lock
        itself — correctly, on the path #168 left alone. Everything P2 then
        does is legitimate, and the lock is legitimately gone. A run in that
        shape has not exercised the skip-path invariant in either direction,
        and it must say so rather than pick an outcome. Reporting it as a P2
        teardown is the #312 defect; reporting it as a pass would be worse.
        """
        with pytest.raises(pytest.skip.Exception) as excinfo:
            self._lock_ownership_race(tmp_path, release_p1_early=True)
        message = str(excinfo.value)
        assert "COVERAGE LOST" in message, (
            f"an inconclusive run must name what it failed to check: {message}"
        )
        assert "P1" in message and "exited" in message, (
            f"the reason must point at P1's exit, not at P2: {message}"
        )

    def test_a_p3_that_acquired_after_p1_exited_is_inconclusive(self, tmp_path):
        """The P3 half rests on the same unstated premise as the P2 half.

        P1 is released and reaped only after P2 has skipped, so the invariant
        above IS observed — and then P3 finds a lock that is genuinely free,
        acquires it and runs the pipeline. Nothing concurrent happened. Reading
        P3's extraction as "it acquired a lock P1 still legitimately holds"
        would blame P3 for the scheduler, which is #312 one assertion further
        down the same test.
        """
        with pytest.raises(pytest.skip.Exception) as excinfo:
            self._lock_ownership_race(tmp_path, release_p1_after_p2=True)
        message = str(excinfo.value)
        assert "COVERAGE LOST" in message, (
            f"an inconclusive run must name what it failed to check: {message}"
        )
        assert "P3" in message and "exited" in message, (
            f"the reason must point at P1's exit during P3's run: {message}"
        )
