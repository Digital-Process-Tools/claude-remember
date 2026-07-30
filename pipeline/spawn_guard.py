"""A bound on summarizer spawns that no host can strip (#204).

The nested ``claude -p`` was kept out of the plugin by two signals, and both can
fail to arrive:

* ``--setting-sources ''`` (#202) registers none of the user's hooks in the
  child. An older CLI rejects the flag, and ``haiku.call_haiku`` then fails OPEN
  — deliberately, because the alternative is no saves ever again — so the retry
  runs with the user's hooks live.
* ``REMEMBER_NESTED_SUMMARIZER`` (#205) makes this plugin's own hooks no-op in
  the child. It is an environment variable, and a host that redacts env into
  spawned hook subprocesses erases it. This repo has precedent: #131 was
  ``CLAUDE_CODE_OAUTH_TOKEN`` going missing exactly that way.

When both fail there is nothing left. @ehutchinsonSFDC reported the result on
0.9.0: ~19 live ``claude`` processes, four of them summarizers, +7 new
summarizer transcripts during an 8-second idle with no tool calls, and
summarizer prompts that embedded a previous summarizer prompt. Neither existing
brake applies —

* the save lock (``lib-lock.sh``) makes saves in one store take turns; it never
  limits how many take a turn, and a chain that spawns one at a time never
  contends at all;
* the 120s save cooldown is bypassed by ``--force``, which is exactly how
  ``session-start-hook.sh`` recovers a missed session.

So the bound lives here: in the process that does the spawning, recorded on the
filesystem, keyed off nothing but ``HOME``. A parent cannot lose it the way it
can lose an env var, because the parent never has to hand it over — the child
reads the same directory.

Two caps, because the reported behaviour has two shapes:

``REMEMBER_MAX_CONCURRENT_SUMMARIZERS`` (default 4)
    Depth. A nested summarizer runs *inside* its parent's call, so the parent's
    record is still live while the child spawns; recursion therefore shows up as
    concurrency and stops at the cap. Also the cap on genuine parallelism, so it
    is not 1: several projects saving at once is normal and must not be refused.

``REMEMBER_MAX_SUMMARIZERS_PER_MIN`` (default 12)
    Rate. The serialized chain — save spawns summarizer, summarizer's session
    start recovers with ``--force``, that save spawns a summarizer — never runs
    two at once, so only a rolling window can see it. A store saves at most once
    per 120s cooldown, so 12/min leaves room for ~24 concurrently active
    projects before anything is refused.

Hitting a cap is REPORTED, never absorbed: ``SummarizerSpawnDeclined`` is a
third state beside a result and a failure. ``pipeline.shell`` gives it its own
exit code (``EXIT_SPAWN_DECLINED``) so ``save-session.sh`` can tell "the guard
refused" from "the model failed" — booking a decline as a failure would, after
``thresholds.max_summary_failures`` runs, advance the read cursor past a span
nobody ever summarized.

The guard itself fails OPEN. If it cannot read or write its own state the spawn
proceeds and says so, because a guard that turns an unwritable directory into a
permanent save outage is the #204 trap in a new costume.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

#: Exit code ``pipeline.shell`` uses for a declined spawn. Distinct from 0 (a
#: result) and 1 (a failure) so bash can act on the difference.
EXIT_SPAWN_DECLINED = 3

RUNTIME_DIR_ENV = "REMEMBER_RUNTIME_DIR"
MAX_CONCURRENT_ENV = "REMEMBER_MAX_CONCURRENT_SUMMARIZERS"
MAX_PER_MINUTE_ENV = "REMEMBER_MAX_SUMMARIZERS_PER_MIN"

DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_PER_MINUTE = 12

#: Rolling window for the rate cap, in seconds.
WINDOW_SECONDS = 60

#: Extra seconds past a call's own timeout before its record is treated as
#: abandoned. A summarizer killed mid-call (the reporter killed 19) leaves a
#: record behind; pid liveness retires it immediately on POSIX, and this is the
#: backstop for a pid that has been recycled or cannot be probed.
STALE_GRACE_SECONDS = 60

_SUFFIX = ".spawn"


class SummarizerSpawnDeclined(RuntimeError):
    """A cap was reached, so this summarizer was not spawned.

    A ``RuntimeError`` subclass so every existing caller keeps handling it, and
    a distinct type so the ones that care can separate "refused" from "failed".
    """


def record_dir() -> Path:
    """Where spawn records live.

    ``HOME`` and nothing else, so a child that inherited no plugin environment
    still finds the same directory. Deliberately NOT the memory store: the store
    is per project, and the runaway spans projects — a per-store bound would be
    lifted simply by resolving a different project, which is the shape of the
    bug.
    """
    base = os.environ.get(RUNTIME_DIR_ENV, "").strip()
    root = Path(base) if base else Path.home() / ".remember" / "run"
    return root / "summarizers"


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw.isdigit() and int(raw) >= 1:
        return int(raw)
    return default


#: Whether ``kill -0`` can be used to ask if a pid exists. False on Windows,
#: where ``os.kill`` only sends real signals.
#:
#: A named flag rather than an inline ``os.name`` check because that is what a
#: test can toggle: monkeypatching ``os.name`` mutates the real ``os`` module,
#: and ``pathlib`` reads it — so the patch silently turns every ``Path`` in the
#: process into a ``WindowsPath`` and the test dies somewhere unrelated.
_PIDS_ARE_PROBEABLE = os.name != "nt"


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` still exists.

    Where there is no cheap probe (Windows), say yes and let staleness retire
    the record instead. That errs toward declining for at most
    ``timeout + STALE_GRACE_SECONDS`` after a crash, which is bounded; erring
    the other way would hand out a slot per unprobeable record and uncap the
    spawn.
    """
    if pid <= 0:
        return False
    if not _PIDS_ARE_PROBEABLE:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM: alive, owned by someone else.
        return True
    return True


def _parse(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return fields


class _Slot:
    """One claimed spawn. ``release()`` is idempotent and never raises."""

    def __init__(self, path: Path | None, degraded: str = "") -> None:
        self._path = path
        self.degraded = degraded

    def release(self) -> None:
        if self._path is None:
            return
        path, self._path = self._path, None
        try:
            fields = _parse(path)
            path.write_text(
                f"pid={fields.get('pid', '0')}\n"
                f"started={fields.get('started', '0')}\n"
                "done=1\n",
                encoding="utf-8",
            )
        except OSError:
            # The record stays as it is; pid liveness and staleness retire it.
            pass


def _census(directory: Path, now: float, stale_after: float) -> tuple[int, int]:
    """(live spawns, spawns started inside the rate window), pruning as it goes."""
    live = 0
    recent = 0
    for path in directory.glob(f"*{_SUFFIX}"):
        try:
            fields = _parse(path)
        except OSError:
            continue
        try:
            started = float(fields.get("started", "0"))
            pid = int(fields.get("pid", "0"))
        except ValueError:
            started, pid = 0.0, 0
        age = now - started
        done = fields.get("done") == "1"

        if age < WINDOW_SECONDS:
            recent += 1

        if not done and age < stale_after and _pid_alive(pid):
            live += 1
            continue
        if age >= WINDOW_SECONDS:
            try:
                path.unlink()
            except OSError:
                pass
    return live, recent


def claim(timeout: float = 120.0) -> _Slot:
    """Reserve a summarizer spawn, or refuse it.

    Raises:
        SummarizerSpawnDeclined: a cap was reached. Nothing was spawned and
            nothing was billed; the caller must report this and leave the span
            alone rather than treat it as a failed summary.
    """
    max_concurrent = _positive_int(MAX_CONCURRENT_ENV, DEFAULT_MAX_CONCURRENT)
    max_per_minute = _positive_int(MAX_PER_MINUTE_ENV, DEFAULT_MAX_PER_MINUTE)
    stale_after = timeout + STALE_GRACE_SECONDS

    directory = record_dir()
    now = time.time()
    try:
        # mode on CREATION only: an existing directory keeps whatever permissions
        # its owner gave it. Forcing 0700 on every claim would quietly undo a
        # deliberate `chmod` — including one a user applied to stop this plugin
        # writing there.
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        live, recent = _census(directory, now, stale_after)
    except OSError as broken:
        return _Slot(None, degraded=f"{type(broken).__name__}: {broken}")

    if live >= max_concurrent:
        raise SummarizerSpawnDeclined(
            f"declined to spawn a summarizer: {live} already running and the "
            f"limit is {max_concurrent} ({MAX_CONCURRENT_ENV}). A summarizer "
            "that reaches this limit is nested inside another one — the guard "
            "that should have stopped it did not reach the child (#204). This "
            "save is skipped, not failed: the span is kept and summarized on a "
            "later run."
        )
    if recent >= max_per_minute:
        raise SummarizerSpawnDeclined(
            f"declined to spawn a summarizer: {recent} spawned in the last "
            f"{WINDOW_SECONDS}s and the limit is {max_per_minute} per minute "
            f"({MAX_PER_MINUTE_ENV}). This save is skipped, not failed: the "
            "span is kept and summarized on a later run."
        )

    path = directory / f"{now:.6f}-{os.getpid()}{_SUFFIX}"
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\nstarted={now:.6f}\n")
    except OSError as broken:
        return _Slot(None, degraded=f"{type(broken).__name__}: {broken}")
    return _Slot(path)
