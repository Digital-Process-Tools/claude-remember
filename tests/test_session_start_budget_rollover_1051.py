"""#1051: the session_start_max_bytes budget is skipped on the first
SessionStart of a new day.

_remember_apply_session_start_budget re-renders the MEMORY section fresh and
requires it to equal, byte for byte, the tail of the already-captured body
before it will trim anything. On a cache HIT, that tail came from a cache
published on a PRIOR calendar day -- a legitimate hit by every check the
loader applies (mtimes, VERSION=) -- so a budget check running TODAY can
never reproduce it with a fresh, TODAY-dated re-render. That is not the #845
race the equality check exists to catch (nothing changes mid-render); it is
two different calendar days producing two different renders by construction.
The mismatch made the function log "MEMORY section changed before the budget
check -- injected as rendered, over budget" and return the body UNTRIMMED,
which is exactly the failure the budget exists to prevent.

The bar: would this test still pass if the code did nothing? Positive
control below: a store that stays under budget across the same rollover is
untouched -- proving the budget machinery still runs on this path at all,
not merely that output vanished because something broke.
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
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

DAY_1 = "2099-06-01"
DAY_2 = "2099-06-02"
NOTICE = "--- not injected (over thresholds.session_start_max_bytes)"
MISMATCH_WARNING = "MEMORY section changed before the budget check"


def _shim_date(bindir: Path, day: str) -> None:
    bindir.mkdir(exist_ok=True)
    shim = bindir / "date"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y-%m-%d" ]; then\n'
        f"  echo {day}\n"
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    shim.chmod(0o755)


def _store(tmp_path: Path, bodies: dict, day: str):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    for name, body in bodies.items():
        (remember / name).write_text(body, encoding="utf-8")
    (remember / "config.json").write_text(
        json.dumps({"features": {"plugin_promos": False}}), encoding="utf-8"
    )
    bindir = tmp_path / "bin"
    _shim_date(bindir, day)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_NO_PRINTF_T": "1",
        "REMEMBER_CONFIG_CACHE": "0",
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
    }
    return env, remember, bindir


def _start(env: dict, timeout: float = 30) -> str:
    payload = json.dumps({
        "session_id": "dddddddd-0000-4000-8000-000000001051",
        "transcript_path": "/does/not/matter/1051.jsonl",
        "hook_event_name": "SessionStart",
        "cwd": "/does/not/matter",
        "source": "startup",
    })
    result = subprocess.run(
        ["bash", str(SESSION_START)], env=env, input=payload,
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _listed(out: str) -> list[str]:
    if NOTICE not in out:
        return []
    block = out.split(NOTICE, 1)[1].split("\n", 1)[1]
    names = []
    for line in block.split("\n"):
        if not line.strip():
            break
        names.append(line.split(" (", 1)[0])
    return names


def _wait_for_cache(remember: Path, timeout: float = 5.0) -> None:
    """The publish that follows a cache MISS is a disowned background
    child -- give it a moment to land before relying on a cache HIT."""
    cache = remember / "tmp" / "start-context.cache"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cache.exists():
            return
        time.sleep(0.05)


class TestBudgetSurvivesADayRollover:
    """The suspected cause from #1051: a cache published on day 1 is still
    a legitimate hit (by mtime and VERSION=) on day 2's first SessionStart,
    but the budget's own live re-render on day 2 can never match day 1's
    cached bytes. The budget must still trim; it must not silently inject
    the full, over-budget body."""

    def test_over_budget_store_still_trimmed_on_the_first_start_of_a_new_day(self, tmp_path):
        bodies = {
            "identity.md": "IDENTITY-1051\n",
            "core-memories.md": "CORE-1051\n",
            "now.md": "NOW-1051\n" + "n" * 400 + "\n",
            "recent.md": "RECENT-1051\n" + "r" * 400 + "\n",
            f"today-{DAY_1}.md": "TODAYFILE-1051\n" + "t" * 400 + "\n",
            "archive.md": "ARCHIVE-1051\n" + "a" * 9000 + "\n",
        }
        env, remember, bindir = _store(tmp_path, bodies, DAY_1)
        _start(env)  # day 1: cache miss, live render + background publish
        _wait_for_cache(remember)

        _shim_date(bindir, DAY_2)
        out = _start(env)  # day 2: cache HIT serving day-1 bytes

        log_file = remember / "logs" / f"memory-{DAY_2}.log"
        log_text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        assert MISMATCH_WARNING not in log_text, (
            "the budget gave up instead of trimming on a day rollover:\n" + log_text
        )
        assert "ARCHIVE-1051" not in out, "archive.md was not dropped on the rollover start"
        assert str(remember / "archive.md") in _listed(out), out[-1500:]
        assert "NOW-1051" in out, "now.md should still fit and must not have been dropped too"
        assert "RECENT-1051" in out, "recent.md should still fit and must not have been dropped too"
        assert len(out.encode("utf-8")) < 9000 + 1500, (
            f"output still over budget after the rollover start:\n{out[-1500:]}"
        )

    def test_under_budget_store_is_untouched_across_the_same_rollover(self, tmp_path):
        """Positive control: the rollover alone must not start dropping
        files that were never over budget in the first place -- the bar
        this whole test module is held to."""
        bodies = {
            "identity.md": "IDENTITY-1051-OK\n",
            "core-memories.md": "CORE-1051-OK\n",
            "now.md": "NOW-1051-OK\n",
        }
        env, remember, bindir = _store(tmp_path, bodies, DAY_1)
        _start(env)
        _wait_for_cache(remember)

        _shim_date(bindir, DAY_2)
        out = _start(env)

        assert NOTICE not in out, out[-1500:]
        assert "NOW-1051-OK" in out
        assert "IDENTITY-1051-OK" in out
