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


def _make_env(tmp_path: Path, name: str, extract_sleep: float = 0.0):
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
    }
    return env, project, plugin, calls_log, session_id


class TestLockOwnership:

    def test_skip_path_does_not_delete_holders_lock_file(self, tmp_path):
        """Three real processes: P1 holds the lock and is still running when
        P2 skips and exits; P3 must NOT be able to acquire while P1 is alive."""
        env1, project, plugin, calls1, sid = _make_env(tmp_path, "p1", extract_sleep=3.0)
        # Since #182 the lock is a DIRECTORY whose `pid` file names the holder.
        lock_dir = project / ".remember" / "tmp" / "save.lock"
        lock_file = lock_dir / "pid"

        p1 = subprocess.Popen(
            ["bash", str(plugin / "scripts" / "save-session.sh"), sid],
            env=env1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Wait for P1 to actually take the lock.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not lock_file.exists():
            time.sleep(0.05)
        assert lock_file.exists(), "P1 never acquired save.lock"
        p1_holder_pid = lock_file.read_text().strip()
        assert p1_holder_pid == str(p1.pid)

        # P2: same project/plugin, its own env — must see the lock and skip.
        env2, _, _, calls2, _ = _make_env(tmp_path, "p2")
        env2["CLAUDE_PROJECT_DIR"] = env1["CLAUDE_PROJECT_DIR"]
        env2["CLAUDE_PLUGIN_ROOT"] = env1["CLAUDE_PLUGIN_ROOT"]
        env2["HOME"] = env1["HOME"]
        p2 = subprocess.run(
            ["bash", str(plugin / "scripts" / "save-session.sh"), sid],
            env=env2, capture_output=True, text=True, timeout=15,
        )
        assert p2.returncode == 0

        # THE ASSERTION: P1's lock file must still exist and still name P1,
        # even though P2 has fully exited via the skip path.
        assert lock_dir.is_dir() and lock_file.exists(), (
            "P2's skip-path cleanup deleted the lock entirely — "
            "P1 is still running and now unprotected"
        )
        assert lock_file.read_text().strip() == p1_holder_pid, (
            "the lock file no longer names P1 — P2's cleanup tore down "
            "the holder's lock, and a third process could now acquire "
            "while P1 is still running"
        )

        # P3: races in right after P2 exits, while P1 is still sleeping.
        env3, _, _, calls3, _ = _make_env(tmp_path, "p3")
        env3["CLAUDE_PROJECT_DIR"] = env1["CLAUDE_PROJECT_DIR"]
        env3["CLAUDE_PLUGIN_ROOT"] = env1["CLAUDE_PLUGIN_ROOT"]
        env3["HOME"] = env1["HOME"]
        p3 = subprocess.run(
            ["bash", str(plugin / "scripts" / "save-session.sh"), sid],
            env=env3, capture_output=True, text=True, timeout=15,
        )
        assert p3.returncode == 0
        assert "extract" not in Path(calls3).read_text() if Path(calls3).exists() else True, (
            "P3 ran the extraction pipeline concurrently with P1 — it "
            "acquired a lock that P1 still legitimately holds"
        )

        p1.wait(timeout=15)
        assert p1.returncode == 0
