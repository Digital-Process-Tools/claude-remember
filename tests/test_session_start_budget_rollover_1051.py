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

import io
import json
import os
import subprocess
import sys
import tarfile
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

from .spawn_counting import make_shim_dir, spawns

DAY_1 = "2099-06-01"
DAY_2 = "2099-06-02"
NOTICE = "--- not injected (over thresholds.session_start_max_bytes)"
MISMATCH_WARNING = "MEMORY section changed before the budget check"
REMEMBER_HEADER = "=== REMEMBER ==="


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


def _assert_no_stray_tmp_files(remember: Path) -> None:
    """Self-review (#1051): an earlier version of this fix left a
    PID-scoped marker file behind
    (REMEMBER_DIR/tmp/start-context.served-bytes.<pid>) -- consumed by the
    budget check on the NEXT call, but never created at all once a session
    stays entirely under budget (the common case this test exercises), so
    it survived on disk indefinitely. The current design has no marker at
    all, by any name -- pin that directly rather than trying to enumerate
    every OTHER file this codebase's unrelated machinery is allowed to
    leave in tmp/ (case-divergence, handoff-path.*, session-slug,
    roster-epoch, capture-alive.d, ...)."""
    tmp_dir = remember / "tmp"
    if not tmp_dir.is_dir():
        return
    stray = [p.name for p in tmp_dir.iterdir() if "served-bytes" in p.name]
    assert not stray, f"leftover marker file(s) in {tmp_dir}: {stray}"


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
        # Self-review (#1051): the head/tail split this trim depends on is
        # recovered by re-reading start-context.cache directly (no marker
        # file any more) -- a wrong split would corrupt the legend/MEMORY
        # boundary, not just drop the wrong file. The handoff legend must
        # still appear exactly once, intact.
        assert out.count(REMEMBER_HEADER) == 1, (
            f"{REMEMBER_HEADER!r} should appear exactly once:\n{out[-1500:]}"
        )
        _assert_no_stray_tmp_files(remember)

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
        assert out.count(REMEMBER_HEADER) == 1, (
            f"{REMEMBER_HEADER!r} should appear exactly once:\n{out[-1500:]}"
        )
        _assert_no_stray_tmp_files(remember)


def _resolve_main_ref() -> str | None:
    """A PR's own CI checkout is commonly shallow and single-ref -- just
    the merge commit fetched at depth 1, with main's own history and tree
    never fetched at all, so a bare "main" or even "origin/main" can be
    genuinely unresolvable (or resolvable yet treeless) there. A fresh,
    shallow fetch of main's own tip is the most robust first move: it
    works whether or not the checkout already happened to populate a
    local ref, and every GitHub Actions runner has the network access to
    do it against the same "origin" the checkout action itself set up."""
    fetch = subprocess.run(
        ["git", "fetch", "--quiet", "--depth", "1", "origin", "main"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if fetch.returncode == 0:
        return "FETCH_HEAD"
    for candidate in ("origin/main", "main", "origin/HEAD"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            return candidate
    return None


def _archive_ref_tree(ref: str, dest: Path) -> None:
    """Export `ref`'s whole tree into `dest` -- used to run the SAME
    scenario against main's actual code, rather than against a hardcoded
    spawn count that goes stale the moment either branch moves."""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "archive", ref], cwd=REPO_ROOT, capture_output=True, check=True,
    )
    extract_kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        tar.extractall(dest, **extract_kwargs)


def _same_day_cache_served_spawns(tmp_path: Path, plugin_root: Path) -> list[str]:
    """One miss (+ background publish) followed by a SAME-day SessionStart
    that hits the cache, with every external process the warm run forks
    recorded via the shared #679 counting harness. Same calendar day on
    purpose: a rollover takes the mismatch-recovery branch, which is not
    the common path this pin is about."""
    bodies = {
        "identity.md": "IDENTITY-SPAWNPIN\n",
        "core-memories.md": "CORE-SPAWNPIN\n",
        "now.md": "NOW-SPAWNPIN\n",
    }
    env, remember, _bindir = _store(tmp_path, bodies, DAY_1)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    session_start = plugin_root / "scripts" / "session-start-hook.sh"

    def _run(extra_env: dict) -> str:
        payload = json.dumps({
            "session_id": "eeeeeeee-0000-4000-8000-000000001051",
            "transcript_path": "/does/not/matter/1051-spawn.jsonl",
            "hook_event_name": "SessionStart",
            "cwd": "/does/not/matter",
            "source": "startup",
        })
        result = subprocess.run(
            ["bash", str(session_start)], env={**env, **extra_env}, input=payload,
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    _run({})  # miss: live render + disowned background publish
    _wait_for_cache(remember)

    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    out = _run({
        "SPAWN_LOG": str(log),
        "PATH": f"{shims}{os.pathsep}{env['PATH']}",
    })
    assert "NOW-SPAWNPIN" in out, "positive control: the cache-served run produced no output at all"
    return spawns(log)


def test_cache_served_spawn_count_does_not_exceed_main(tmp_path):
    """Self-review (#1051): an earlier version of this fix wrote a
    PID-scoped marker file on every cache-served SessionStart (mktemp + mv
    to write it, a second `cat` to measure it, `rm` to consume it) --
    measured 26 -> 30 process launches with the #679 counting harness, on
    the COMMON path, not the rare over-budget one. The current code
    recovers the same information by re-reading start-context.cache
    directly with a bash builtin, only on the mismatch path. Pinned by
    running the actual scenario against BOTH this branch's code and
    main's, rather than a hardcoded number either one could drift past."""
    main_ref = _resolve_main_ref()
    if main_ref is None:
        pytest.skip(
            "no local ref for main resolvable in this checkout (a shallow, "
            "single-branch CI checkout of just the PR ref has neither "
            "origin/main nor a local main) -- cannot pin against main's own "
            "code here"
        )
    main_tree = tmp_path / "main-tree"
    try:
        _archive_ref_tree(main_ref, main_tree)
    except subprocess.CalledProcessError as exc:
        # A shallow, single-ref CI checkout can resolve "origin/main" (the
        # ref/tracking-branch pointer exists) without the commit's TREE
        # actually being present locally -- `git archive` needs the latter,
        # and fails exactly here rather than at the rev-parse above.
        pytest.skip(
            f"main_ref {main_ref!r} resolved but `git archive` could not "
            f"read its tree in this checkout (likely shallow): {exc.stderr!r}"
        )

    branch_spawns = _same_day_cache_served_spawns(tmp_path / "branch", REPO_ROOT)
    main_spawns = _same_day_cache_served_spawns(tmp_path / "main", main_tree)

    assert main_spawns, "positive control: main's own cache-served run forked nothing at all"
    assert len(branch_spawns) <= len(main_spawns), (
        f"a cache-served SessionStart now forks MORE than main does:\n"
        f"this branch ({len(branch_spawns)}): {branch_spawns}\n"
        f"main ({len(main_spawns)}): {main_spawns}"
    )


def test_same_day_845_concurrent_republish_falls_back_to_the_warning(tmp_path):
    """#845: recovering the head/tail split (#1051 self-review) by
    re-reading start-context.cache must distrust that read when a
    concurrent republish has overwritten the cache between the original
    serve and this later read. No real concurrency is needed to pin it:
    the cache on disk is set to content that matches NEITHER the served
    tail NOR a fresh render, simulating exactly that window. Chosen
    behaviour: fall back to the pre-#1051 shape (warn, leave the caller's
    own copy of the body untouched) rather than trust a split that could
    be wrong."""
    remember = tmp_path / "remember"
    (remember / "tmp").mkdir(parents=True)
    (remember / "now.md").write_text("FRESH-TODAY-845\n", encoding="utf-8")
    cache = remember / "tmp" / "start-context.cache"
    cache.write_text("=== MEMORY ===\nRACED-REPUBLISH-845\n", encoding="utf-8")
    served_tail = "=== MEMORY ===\nSERVED-BEFORE-RACE-845\n"
    text_value = "HANDOFF-HEAD-845\n" + served_tail

    script = f"""
    set -e
    config() {{ printf '%s' "$2"; }}
    config_into() {{ printf -v "$1" '%s' "$3"; }}
    _remember_forward_slash() {{ printf '%s' "$1"; }}
    _remember_forward_slash_into() {{ printf -v "$1" '%s' "$2"; }}
    log() {{ printf 'LOG %s: %s\n' "$1" "$2" 1>&2; }}
    source "{REPO_ROOT}/scripts/lib-clock.sh"
    source "{REPO_ROOT}/scripts/lib-memory-context.sh"
    _remember_memory_paths
    OUT="UNTOUCHED-SENTINEL-845"
    _remember_apply_session_start_budget OUT 5 "$TEXT_VALUE"
    printf '%s' "$OUT"
    """
    env = {
        **os.environ,
        "REMEMBER_DIR": str(remember),
        "PROJECT_DIR": str(tmp_path / "project"),
        "PLUGIN_ROOT": str(REPO_ROOT),
        "TODAY": "2099-06-10",
        "TEXT_VALUE": text_value,
    }
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True,
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert MISMATCH_WARNING in result.stderr, (
        f"a raced cache was trusted instead of falling back:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # The outvar must stay exactly as the caller set it -- in the real
    # caller (session-start-hook.sh) this IS the untrimmed, already-
    # captured body: a function that gives up must never touch it.
    assert result.stdout == "UNTOUCHED-SENTINEL-845", (
        f"OUT was mutated despite falling back to the warning path: {result.stdout!r}"
    )
