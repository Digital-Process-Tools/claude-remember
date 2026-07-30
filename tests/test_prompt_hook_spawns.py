"""UserPromptSubmit must not fork a process tree to print a timestamp (#227).

@MargeryOlethea measured 256 `UserPromptSubmit` records over 50 sessions on
Windows 11 ARM64 under QEMU: **p50 8718ms, max 30003ms, 6 outright timeouts**.
The cause is not a slow algorithm — it is 27 external process spawns behind a
hook whose entire job is to print `[HH:MM TZ — username]`. On Linux 27 spawns is
~300ms and invisible; at the 150-800ms per spawn measured on that box it is ~8s,
every single prompt.

`UserPromptSubmit` is also the one event where a non-zero exit does not merely
error: it **blocks the prompt and erases what the user typed**. So the budget
below is not a micro-optimisation. Every spawn is an opportunity to fail on a
slow or unusual platform, and six of those 256 runs took the whole prompt down.

What is pinned here:

* a **spawn budget**, counted the way the report counted it — by execution, not
  by wall clock, because wall clock is what differs between platforms and spawn
  count is what causes it;
* that the budget is not bought by printing less, or by losing the configured
  timezone, or by serving a stale one after the config changes;
* that the two timestamp implementations — bash's `printf '%(...)T'` builtin
  (bash >= 4.2) and `date` (macOS ships bash **3.2.57**, where the builtin does
  not exist) — are **byte-identical**, because the fast platform must not lose
  the timestamp to speed up the slow one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

from tests.spawn_counting import make_shim_dir  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"
LIB_CLOCK = REPO_ROOT / "scripts" / "lib-clock.sh"

# COUNTED is imported, not restated (#230). This file carried its own copy and
# #230's copy disagreed with it — `head` was missing here, so `ls -t … | head -1`
# would have been counted at half its true cost had this hook ever grown one. A
# budget test that cannot see part of what it measures reports a number that
# reads as complete. Adding `head` did not move this hook's warm count: it is 1
# on bash 3.2 either way. The omission was a loaded gun, not a wound.

# On bash >= 4.2 the whole hook is spawn-free. On bash 3.2 (stock macOS) there
# is no time builtin at all, so one `date` is the floor. The `jq` that builds
# the capture-gap JSON is only reached when a notice is actually pending.
WARM_SPAWN_BUDGET = 2

TS_RE = re.compile(r"^\[\d{1,2}:\d{2} \S+ — \S+\]$")


def _shim_dir(tmp_path: Path, log: Path) -> Path:
    """A PATH front-end that records every external execution, then execs it."""
    return make_shim_dir(tmp_path, log)


def _project(tmp_path: Path, timezone: str | None = None):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".remember" / "tmp").mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    if timezone is not None:
        (home / ".remember" / "config.json").write_text(
            json.dumps({"timezone": timezone}), encoding="utf-8"
        )
    return home, project


def _env(home: Path, project: Path, extra: dict | None = None) -> dict:
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "TMPDIR": str(project.parent / "tmp"),
    }
    (project.parent / "tmp").mkdir(exist_ok=True)
    env.pop("REMEMBER_DIR", None)
    env.pop("_LIB_MEMORY_DIR_LOADED", None)
    env.pop("REMEMBER_TZ", None)
    if extra:
        env.update(extra)
    return env


def _run(env: dict, *, count_into: Path | None = None, shims: Path | None = None):
    if count_into is not None:
        count_into.write_text("", encoding="utf-8")
        env = {**env, "SPAWN_LOG": str(count_into), "PATH": f"{shims}{os.pathsep}{env['PATH']}"}
    return subprocess.run(
        ["bash", str(USER_PROMPT)],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _spawns(log: Path) -> list[str]:
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── The budget ───────────────────────────────────────────────────────────────

def test_a_warmed_prompt_hook_spawns_almost_nothing(tmp_path):
    """The measurement from the report, as an assertion.

    The first invocation in a project may resolve whatever it needs. Every
    invocation after it is the one the user waits on 256 times a day, and it has
    no work left to do that justifies a process.
    """
    home, project = _project(tmp_path)
    env = _env(home, project)
    log = tmp_path / "spawns.log"
    shims = _shim_dir(tmp_path, log)

    first = _run(env, count_into=log, shims=shims)
    assert first.returncode == 0, f"cold run failed: {first.stderr[:400]}"
    cold = len(_spawns(log))

    second = _run(env, count_into=log, shims=shims)
    assert second.returncode == 0, f"warm run failed: {second.stderr[:400]}"
    warm = _spawns(log)

    assert len(warm) <= WARM_SPAWN_BUDGET, (
        f"{len(warm)} external spawns to print a timestamp (cold run: {cold}). "
        f"At the 150-800ms per spawn measured in #227 that is seconds of blocked "
        f"prompt. Spawned: {sorted(set(warm))}"
    )


def test_the_budget_is_not_bought_by_printing_nothing(tmp_path):
    """The obvious way to pass the test above is to emit nothing at all.

    The hook exists to inject the time. A silent, instant hook is not a fix, it
    is a deletion — and it would exit 0 just as happily.
    """
    home, project = _project(tmp_path)
    env = _env(home, project)
    _run(env)
    result = _run(env)

    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    assert TS_RE.match(line), (
        f"warm run must still print [HH:MM TZ — username]. Got: {result.stdout!r}"
    )


def test_the_configured_timezone_survives_the_fast_path(tmp_path):
    """A timezone in config.json is honoured on every prompt, not just the first.

    The whole reason the hook reads config at all. Anything that skips the read
    to go fast has to keep answering this.
    """
    home, project = _project(tmp_path, timezone="UTC")
    env = _env(home, project)

    first = _run(env)
    second = _run(env)

    assert "UTC" in first.stdout, f"cold run lost the timezone: {first.stdout!r}"
    assert "UTC" in second.stdout, (
        f"warm run lost the configured timezone — the fast path is serving system "
        f"local time. Got: {second.stdout!r}"
    )


def test_editing_the_config_is_not_ignored_by_a_warmed_hook(tmp_path):
    """Cache invalidation, as the only thing that makes caching honest.

    Today a config edit takes effect on the next prompt, because every hook
    re-reads it. Anything remembering the answer has to notice the file it came
    from changed, or the user edits config.json and nothing happens.
    """
    home, project = _project(tmp_path, timezone="UTC")
    env = _env(home, project)

    warm = _run(env)
    assert "UTC" in warm.stdout, f"precondition: {warm.stdout!r}"

    cfg = home / ".remember" / "config.json"
    cfg.write_text(json.dumps({"timezone": "Asia/Tokyo"}), encoding="utf-8")
    now = time.time() + 5
    os.utime(cfg, (now, now))

    after = _run(env)
    assert "UTC" not in after.stdout, (
        f"config.json now says Asia/Tokyo and the hook still prints UTC — the "
        f"edit is invisible until something else clears a cache. Got: {after.stdout!r}"
    )
    assert "JST" in after.stdout, f"expected Tokyo time. Got: {after.stdout!r}"


# ── Portability: two implementations, one output ──────────────────────────────

def _stamp_via(env_extra: dict, fmt: str = "+%H:%M %Z") -> subprocess.CompletedProcess:
    script = (
        f'source "{LIB_CLOCK}"\n'
        f'_remember_date "{fmt}"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True,
        env={**os.environ, **env_extra}, timeout=15,
    )


def test_the_builtin_and_date_timestamps_are_byte_identical(tmp_path):
    """macOS ships bash 3.2.57 — `printf '%(...)T'` is bash >= 4.2.

    So `date` is not a legacy fallback here, it is the only path on a stock Mac
    (and on the #204 reporter's box). Two implementations of the same output is
    the cost of not regressing the platform that is already fast, and the way
    that cost gets paid is this test: run both, compare bytes. Trusting the
    format string is how one path silently starts printing `%(%H:%M %Z)T`.
    """
    have_builtin = subprocess.run(
        ["bash", "-c", 'printf "%(%Y)T" -1'], capture_output=True, text=True
    ).stdout.strip().isdigit()
    if not have_builtin:
        pytest.skip("this bash has no printf '%(...)T' builtin (bash < 4.2)")

    # A minute boundary between the two calls is the one legitimate difference.
    for _ in range(3):
        fast = _stamp_via({"REMEMBER_TZ": ""})
        slow = _stamp_via({"REMEMBER_TZ": "", "REMEMBER_NO_PRINTF_T": "1"})
        assert fast.returncode == 0 and slow.returncode == 0, (
            f"fast={fast.stderr[:200]} slow={slow.stderr[:200]}"
        )
        if fast.stdout == slow.stdout:
            break
    else:
        assert fast.stdout == slow.stdout, (
            f"builtin printed {fast.stdout!r}, date printed {slow.stdout!r} — the "
            f"two paths must be byte-identical, three attempts apart"
        )

    assert TS_RE.match(f"[{fast.stdout.strip()} — x]"), (
        f"builtin output is not a timestamp at all: {fast.stdout!r}"
    )


def test_the_date_fallback_is_what_runs_when_the_builtin_is_refused(tmp_path):
    """The bash 3.2 path, exercised on every bash.

    Without this, the fallback is only ever run on macOS developer machines and
    CI proves nothing about the platform the plugin's own author is on.
    """
    out = _stamp_via({"REMEMBER_TZ": "", "REMEMBER_NO_PRINTF_T": "1"})
    assert out.returncode == 0, out.stderr[:300]
    assert re.match(r"^\d{1,2}:\d{2} \S+$", out.stdout.strip()), (
        f"date fallback produced {out.stdout!r}"
    )


def test_an_explicit_timezone_reaches_both_implementations(tmp_path):
    """REMEMBER_TZ is the documented override and must not be lost to a builtin."""
    for extra in ({}, {"REMEMBER_NO_PRINTF_T": "1"}):
        out = _stamp_via({"REMEMBER_TZ": "UTC", **extra})
        assert out.returncode == 0, out.stderr[:300]
        assert "UTC" in out.stdout, f"REMEMBER_TZ=UTC ignored ({extra}): {out.stdout!r}"


# ── The property that must survive all of it ─────────────────────────────────

def test_a_corrupt_cache_costs_correctness_nothing(tmp_path):
    """Whatever the fast path reads, garbage in it must not become garbage out.

    The file lives in a shared temp directory. A hook that trusts it blindly is
    a hook that prints whatever anyone with write access says — and this is the
    one hook whose failure erases the user's prompt.
    """
    home, project = _project(tmp_path, timezone="UTC")
    env = _env(home, project)
    _run(env)

    tmpdir = Path(env["TMPDIR"])
    caches = list(tmpdir.glob("remember-env-*"))
    assert caches, (
        "no published resolution found — this test cannot say anything about "
        "trusting it. Cache files present: "
        f"{[p.name for p in tmpdir.iterdir()]}"
    )
    for cache in caches:
        cache.write_text(
            "REMEMBER_DIR=/nonexistent/hijacked\nrm -rf /\nnot even a line\n",
            encoding="utf-8",
        )

    result = _run(env)
    assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr[:300]}"
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    assert TS_RE.match(line), f"garbage cache broke the output: {result.stdout!r}"
    assert "UTC" in line, (
        f"garbage cache silently lost the configured timezone: {line!r}"
    )


def test_a_pending_capture_gap_notice_is_still_delivered(tmp_path):
    """#200's notice is the one thing in this hook that needs the memory store.

    It is delivered through `systemMessage`, the only hook output a human ever
    sees. A faster hook that stops finding it re-opens the bug that took the
    reporter a day to see.
    """
    home, project = _project(tmp_path, timezone="UTC")
    env = _env(home, project)
    _run(env)  # warm whatever there is to warm

    notice = project / ".remember" / "tmp" / "capture-gap-notice"
    notice.write_text("previous session was not captured — run /doctor", encoding="utf-8")

    result = _run(env)
    assert result.returncode == 0, result.stderr[:300]

    payload = json.loads(result.stdout)
    assert payload["systemMessage"].startswith("previous session was not captured"), payload
    assert "UTC" in payload["hookSpecificOutput"]["additionalContext"], payload
    assert not notice.exists(), "the notice must be consumed on delivery"


def test_the_hook_exits_zero_even_with_no_project_at_all(tmp_path):
    """EXIT CODES: 0 Always. On UserPromptSubmit a non-zero exit erases the prompt."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "REMEMBER_DIR",
                        "_LIB_MEMORY_DIR_LOADED")}
    result = subprocess.run(
        ["bash", str(USER_PROMPT)], capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, (
        f"rc={result.returncode} — a prompt hook that fails takes the user's typing "
        f"with it. stderr={result.stderr[:300]}"
    )


@pytest.mark.parametrize("written", ["87", "87\n"])
def test_the_context_percentage_is_read_with_or_without_a_trailing_newline(tmp_path, written):
    """`cat` became `read` to save a process, and `read` reports failure on a
    file with no trailing newline — having set the variable anyway. A status line
    writing `87` with no newline is the likely case, not the exotic one, and
    treating that status as "nothing was read" would silently drop the number."""
    home, project = _project(tmp_path, timezone="UTC")
    env = _env(home, project)
    _run(env)
    Path(env["TMPDIR"], "claude-ctx-pct").write_text(written, encoding="utf-8")

    result = _run(env)
    assert result.returncode == 0, result.stderr[:300]
    assert "87%" in result.stdout, (
        f"context percentage lost for input {written!r}: {result.stdout!r}"
    )


def test_a_context_percentage_at_the_limit_still_warns(tmp_path):
    """The >=95 warning is the only output here anybody acts on."""
    home, project = _project(tmp_path, timezone="UTC")
    env = _env(home, project)
    _run(env)
    Path(env["TMPDIR"], "claude-ctx-pct").write_text("96", encoding="utf-8")

    result = _run(env)
    assert "96%" in result.stdout, result.stdout
    assert "/remember" in result.stdout, (
        f"no context-death warning at 96%: {result.stdout!r}"
    )


def test_no_test_fakes_the_clock_on_path_without_disabling_the_builtin():
    """The seam that fix #1 removed, guarded so the next author trips loudly.

    Faking time by putting a `date` in front of PATH is the obvious way to test
    anything time-dependent in a shell pipeline, and it worked for the whole life
    of this repo. It does not work against `printf '%(FMT)T'`: a builtin is not
    on PATH, so the fake is silently ignored and the assertion runs against the
    real clock — green on macOS bash 3.2, red on bash >= 4.2, which is how #227
    found out. Nothing in a time-faking test's own text would say so.

    So a file that shims `date` must name `REMEMBER_NO_PRINTF_T` — the hatch that
    forces the `date` path — even if the value itself is set in a shared helper.
    A comment pointing at it satisfies this, and that comment is the warning.
    """
    tests_dir = Path(__file__).resolve().parent
    shimmers = []
    for path in sorted(tests_dir.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if '/ "date"' not in text and "/ 'date'" not in text:
            continue
        shimmers.append(path.name)
        assert "REMEMBER_NO_PRINTF_T" in text, (
            f"{path.name} puts its own `date` on PATH but never mentions "
            f"REMEMBER_NO_PRINTF_T. On bash >= 4.2 lib-clock.sh does not run "
            f"`date` at all, so the fake clock is ignored and the test asserts "
            f"against the real one."
        )

    assert shimmers, (
        "this guard no longer recognises how any test fakes `date`, so it is "
        "passing by finding nothing. Update the detector, not the assertion."
    )
