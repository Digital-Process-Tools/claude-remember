"""Two live processes racing to take over the same dead-PID lock must never
both proceed (stale-lock takeover race, sibling of #168).

Before this fix, the stale-lock branch in ``save-session.sh`` was a plain
overwrite:

    log "lock" "stale lock (PID $LOCK_PID dead), taking over"
    echo $$ > "$LOCK_FILE"
    HAVE_LOCK=true

``echo $$ > "$LOCK_FILE"`` has no compare-and-swap. Two processes that both
observe the same dead PID both "take over" — both write their own PID over
each other's, both set ``HAVE_LOCK=true``, and both fall through into the
extraction/Haiku pipeline. Two saves, two Haiku calls, two position writes.
Reproduced with two real processes against a pre-seeded dead-PID lock in 4
of 5 runs (see commit under test).

The fix replaces the overwrite with unlink-then-noclobber-recreate, then a
read-back to confirm this process actually holds the file it just made:

    rm -f "$LOCK_FILE"
    if ! ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null; then
        exit 0   # someone else recreated it first
    fi
    if [ "$(cat "$LOCK_FILE" 2>/dev/null)" != "$$" ]; then
        exit 0   # someone else's takeover clobbered ours after we wrote it
    fi
    HAVE_LOCK=true

Because this is a race between two OS-scheduled processes, a single run
proves nothing either way — the bug was itself only 4/5, not 5/5. This test
drives the real ``save-session.sh`` twice, concurrently, against a
pre-seeded lock naming PID 999999 (almost certainly dead — see
``test_git_backup_hook.py::test_stale_lock_takeover`` for the same
convention), and repeats the whole scenario several times, asserting the
"at most one winner" invariant holds on every iteration.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# 999999 is an almost-certainly-dead PID (see test_git_backup_hook.py's
# test_stale_lock_takeover, same convention).
DEAD_PID = "999999"

# Same shape as the shared STUB_SHELL in test_save_session_lock_ownership.py
# and test_save_session_gates.py: every call `pipeline.shell` receives is
# appended to a per-process calls log, so a test can assert what ran without
# needing a real Haiku/`claude` call.
STUB_SHELL = '''\
import os, sys, tempfile

CALLS = os.environ["STUB_CALLS_LOG"]
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
with open(CALLS, "a") as f:
    f.write(" ".join([cmd] + sys.argv[2:]) + "\\n")

if cmd == "extract":
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
    import json
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


def _make_env(base_dir: Path, name: str):
    """Build an isolated project+plugin fixture rooted under base_dir, exactly
    like test_save_session_lock_ownership.py's helper of the same name. Each
    call gets its own project/plugin/home so the fixture is self-contained;
    callers that want two processes racing over the SAME lock file overwrite
    CLAUDE_PROJECT_DIR / CLAUDE_PLUGIN_ROOT / HOME on the second env to point
    at the first's, the same way the existing P2/P3 tests do.
    """
    project = base_dir / "project"
    (project / ".remember" / "tmp").mkdir(parents=True, exist_ok=True)
    (project / ".remember" / "logs").mkdir(parents=True, exist_ok=True)

    plugin = base_dir / "plugin"
    (plugin / "scripts").mkdir(parents=True, exist_ok=True)
    (plugin / "pipeline").mkdir(parents=True, exist_ok=True)
    (plugin / "pipeline" / "__init__.py").write_text("")
    (plugin / "pipeline" / "haiku.py").write_text("# marker\n")
    (plugin / "pipeline" / "shell.py").write_text(STUB_SHELL)
    for script in ("save-session.sh", "resolve-paths.sh", "detect-tools.sh",
                   "bootstrap-dirs.sh", "log.sh", "lib-memory-dir.sh"):
        (plugin / "scripts" / script).write_text((REPO_ROOT / "scripts" / script).read_text())

    cfg = {"cooldowns": {"save_seconds": 0, "ndc_seconds": 999999},
           "thresholds": {"min_human_messages": 3},
           "features": {"ndc_compression": False}}
    cfg_path = base_dir / f"config-{name}.json"
    cfg_path.write_text(json.dumps(cfg))
    (plugin / "config.json").write_text(json.dumps(cfg))

    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    slug = str(project).replace("/", "-").replace(".", "-").replace("_", "-")
    session_dir = base_dir / "home" / ".claude" / "projects" / slug
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"{session_id}.jsonl").write_text('{"type":"user"}\n' * 10)

    calls_log = base_dir / f"calls-{name}.log"
    env = {
        **os.environ,
        "HOME": str(base_dir / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "REMEMBER_CONFIG": str(cfg_path),
        "STUB_CALLS_LOG": str(calls_log),
    }
    return env, project, plugin, calls_log, session_id


N_ITERATIONS = 8


class TestStaleLockTakeoverRace:

    def test_at_most_one_winner_across_n_iterations(self, tmp_path):
        """Spawn two real save-session.sh processes concurrently against a
        pre-seeded dead-PID lock, N times over. In every iteration, at most
        one process may reach the extraction/Haiku pipeline — the "extract"
        (and "call-haiku") stub calls are how we observe that from outside,
        the same signal the docstring's "two Haiku calls" describes.
        """
        winners_per_iteration = []

        for i in range(N_ITERATIONS):
            iter_dir = tmp_path / f"iter{i}"
            iter_dir.mkdir()

            env_a, project, plugin, calls_a, sid = _make_env(iter_dir, "a")
            env_b, _, _, calls_b, _ = _make_env(iter_dir, "b")
            # Same lock file, same session — this is what makes it a race.
            env_b["CLAUDE_PROJECT_DIR"] = env_a["CLAUDE_PROJECT_DIR"]
            env_b["CLAUDE_PLUGIN_ROOT"] = env_a["CLAUDE_PLUGIN_ROOT"]
            env_b["HOME"] = env_a["HOME"]

            lock_file = project / ".remember" / "tmp" / "save.lock"
            lock_file.write_text(DEAD_PID)

            script = str(plugin / "scripts" / "save-session.sh")
            p_a = subprocess.Popen(["bash", script, sid], env=env_a,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p_b = subprocess.Popen(["bash", script, sid], env=env_b,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ret_a = p_a.wait(timeout=15)
            ret_b = p_b.wait(timeout=15)

            # Losing the race is a normal outcome, not a failure: the loser
            # skips this cycle and must say so with 0. This assertion was
            # briefly left out while the loser could legitimately exit 1 —
            # `LOCK_PID=$(cat "$LOCK_FILE" ...)` is a plain top-level
            # assignment, so under `set -e` a lock file that vanished between
            # the failed noclobber write and the read aborted the script. The
            # takeover branch's own `rm -f` made that window easy to hit
            # (~2/12 runs). It is guarded with `|| true` now, so the exit code
            # is meaningful again and is checked here — an exit 1 means the
            # guard regressed.
            assert ret_a == 0, f"iteration {i}: process A exited {ret_a}, expected 0"
            assert ret_b == 0, f"iteration {i}: process B exited {ret_b}, expected 0"

            log_a = calls_a.read_text() if calls_a.exists() else ""
            log_b = calls_b.read_text() if calls_b.exists() else ""

            extracted = int("extract" in log_a) + int("extract" in log_b)
            haiku_called = int("call-haiku" in log_a) + int("call-haiku" in log_b)

            winners_per_iteration.append(extracted)

            assert extracted <= 1, (
                f"iteration {i}: both processes proceeded past the stale "
                f"lock takeover (extract ran {extracted} times) — the "
                f"plain-overwrite race let two winners through.\n"
                f"A calls:\n{log_a}\nB calls:\n{log_b}"
            )
            assert haiku_called <= 1, (
                f"iteration {i}: Haiku was called {haiku_called} times for "
                f"one dead-PID takeover — two concurrent saves went through"
            )

        # Sanity check the harness itself is exercising the takeover path at
        # all (not silently no-op'ing on this platform/CI) — every iteration
        # should have exactly one winner, never zero.
        assert all(w == 1 for w in winners_per_iteration), (
            f"expected exactly one winner per iteration, got {winners_per_iteration} — "
            "a 0 means neither process took over the stale lock at all, which "
            "would mean this test isn't exercising the race"
        )
