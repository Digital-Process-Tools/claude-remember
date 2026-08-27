"""save-session.sh --force must not lose the save.lock race silently (#369).

The lock used to be acquired with a 0-second timeout BEFORE the argument
parse that would have seen ``--force``. So a forced call that lost the race
against another save holding ``save.lock`` exited 0 having saved nothing --
the same exit code a genuine "nothing new to flush" no-op produces. The
realistic trigger: post-tool-hook.sh forks a background save on the last
tool call, then SessionEnd fires moments later and forks a second
``save-session.sh --force`` for the same session while the first still
holds the lock.

The fix makes a FORCED acquisition a bounded retry (REMEMBER_FORCE_LOCK_TIMEOUT)
instead of a single failed attempt, and makes an acquisition that still fails
after that wait exit 1 instead of 0 -- distinguishable, and already consumed
by session-end-hook.sh's existing nonzero-exit check.

Two real processes, not a source-text assertion: P1 takes save.lock and holds
it under this test's control (a stub "extract" that blocks on a hold file).
P2 runs `save-session.sh --force` against the same store.

  * test_force_exits_nonzero_when_lock_never_frees -- P1 outlives P2's whole
    bounded wait. P2 must exit nonzero AND the store must be provably
    unchanged, not just an unasserted exit code (the "must not fire" half).
  * test_force_acquires_once_the_holder_releases_in_time -- the positive
    control: P1 releases mid-wait, and P2 must actually acquire and save
    (extract runs, exit 0, store changes) -- proving the retry, and this
    fixture, are not simply inert.
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

STUB_SHELL = """\
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
"""


def _make_env(tmp_path: Path, name: str, hold_file=None, force_lock_timeout=None):
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
        "STUB_EXTRACT_HOLD_FILE": str(hold_file) if hold_file else "",
        "STUB_EXTRACT_HOLD_CAP": "60",
    }
    if force_lock_timeout is not None:
        env["REMEMBER_FORCE_LOCK_TIMEOUT"] = str(force_lock_timeout)
    return env, project, plugin, calls_log, session_id


class TestForceLockRace:

    def test_force_exits_nonzero_when_lock_never_frees(self, tmp_path):
        """P1 outlives P2's whole bounded --force wait.

        Before #369's fix, this raced against a 0s timeout and exited 0 —
        indistinguishable from "nothing new to flush". P2 must now exit
        nonzero, and the store (last-save.json) must be provably untouched:
        a nonzero exit with the store touched would just be a different bug.
        """
        release = tmp_path / "release-p1"
        env1, project, plugin, _calls1, sid = _make_env(tmp_path, "p1", hold_file=release)
        env2, _, _, calls2, _ = _make_env(tmp_path, "p2", force_lock_timeout=1)

        lock_dir = project / ".remember" / "tmp" / "save.lock"
        lock_file = lock_dir / "pid"
        last_save = project / ".remember" / "tmp" / "last-save.json"
        assert not last_save.exists()

        p1 = subprocess.Popen(
            ["bash", str(plugin / "scripts" / "save-session.sh"), sid],
            env=env1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not lock_file.exists():
                time.sleep(0.05)
            assert lock_file.exists(), "P1 never acquired save.lock"

            p2 = subprocess.run(
                ["bash", str(plugin / "scripts" / "save-session.sh"), sid, "--force"],
                env=env2, capture_output=True, text=True, timeout=30,
            )

            assert p2.returncode != 0, (
                "P2's forced save lost the lock race and exited 0 — "
                "indistinguishable from a genuine no-op flush "
                f"(stdout={p2.stdout!r} stderr={p2.stderr!r})"
            )
            assert not last_save.exists(), (
                "the store changed even though the lock acquisition "
                "reported failure — the exit code lied about what happened"
            )
            p2_calls = Path(calls2).read_text() if Path(calls2).exists() else ""
            assert "extract" not in p2_calls, (
                "P2 ran the extraction pipeline while P1 still held the lock"
            )
        finally:
            release.write_text("go", encoding="utf-8")
            try:
                p1.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p1.kill()
                p1.wait(timeout=10)

    def test_force_acquires_once_the_holder_releases_in_time(self, tmp_path):
        """Positive control: the bounded wait is not merely a longer failure.

        P1 releases the lock partway through P2's wait window. P2 must
        actually acquire it and complete a real save — not just exit 0
        with nothing to show for it.
        """
        release = tmp_path / "release-p1"
        env1, project, plugin, _calls1, sid = _make_env(tmp_path, "p1", hold_file=release)
        env2, _, _, calls2, _ = _make_env(tmp_path, "p2", force_lock_timeout=15)

        lock_dir = project / ".remember" / "tmp" / "save.lock"
        lock_file = lock_dir / "pid"
        last_save = project / ".remember" / "tmp" / "last-save.json"

        p1 = subprocess.Popen(
            ["bash", str(plugin / "scripts" / "save-session.sh"), sid],
            env=env1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not lock_file.exists():
                time.sleep(0.05)
            assert lock_file.exists(), "P1 never acquired save.lock"

            def _release_soon():
                time.sleep(1.5)
                release.write_text("go", encoding="utf-8")

            import threading
            threading.Thread(target=_release_soon, daemon=True).start()

            p2 = subprocess.run(
                ["bash", str(plugin / "scripts" / "save-session.sh"), sid, "--force"],
                env=env2, capture_output=True, text=True, timeout=30,
            )

            assert p2.returncode == 0, (
                f"P2 should have acquired the lock once P1 released it and "
                f"completed a save: stdout={p2.stdout!r} stderr={p2.stderr!r}"
            )
            assert last_save.exists(), (
                "P2 reported success but the store was never written — "
                "the retry acquired the lock but did not actually save"
            )
            p2_calls = Path(calls2).read_text() if Path(calls2).exists() else ""
            assert "extract" in p2_calls, (
                "P2 exited 0 without ever running the extraction pipeline"
            )
        finally:
            release.write_text("go", encoding="utf-8")
            try:
                p1.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p1.kill()
                p1.wait(timeout=10)
