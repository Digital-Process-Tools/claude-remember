from __future__ import annotations
import os
import time
from pathlib import Path
EXIT_SPAWN_DECLINED = 3
MAX_CONCURRENT_ENV = "REMEMBER_MAX_CONCURRENT_SUMMARIZERS"
MAX_PER_MINUTE_ENV = "REMEMBER_MAX_SUMMARIZERS_PER_MIN"
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_PER_MINUTE = 12
WINDOW_SECONDS = 60
STALE_GRACE_SECONDS = 60
_SUFFIX = ".spawn"
class SummarizerSpawnDeclined(RuntimeError):
    pass
def record_dir() -> Path:
    base = os.environ.get("REMEMBER_RUNTIME_DIR", "").strip()
    root = Path(base) if base else Path.home() / ".remember" / "run"
    return root / "summarizers"
def _positive_int(value: str, default: int) -> int:
    raw = value.strip()
    if raw.isdigit() and int(raw) >= 1:
        return int(raw)
    return default
_PIDS_ARE_PROBEABLE = os.name != "nt"
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if not _PIDS_ARE_PROBEABLE:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True
def _parse(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, _, value = line.partition("=")
        fields[name.strip()] = value.strip()
    return fields
class _Slot:
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
            pass
def _census(directory: Path, now: float, stale_after: float) -> tuple[int, int]:
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
    max_concurrent = _positive_int(
        os.environ.get("REMEMBER_MAX_CONCURRENT_SUMMARIZERS", ""), DEFAULT_MAX_CONCURRENT)
    max_per_minute = _positive_int(
        os.environ.get("REMEMBER_MAX_SUMMARIZERS_PER_MIN", ""), DEFAULT_MAX_PER_MINUTE)
    stale_after = timeout + STALE_GRACE_SECONDS
    directory = record_dir()
    now = time.time()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        live, recent = _census(directory, now, stale_after)
    except OSError as broken:
        return _Slot(None, degraded=f"{type(broken).__name__}: {broken}")
    if live >= max_concurrent:
        raise SummarizerSpawnDeclined(
            f"declined to spawn a summarizer: {live} already running and the "
            f"limit is {max_concurrent} ({MAX_CONCURRENT_ENV}). A summarizer "
            "that reaches this limit is nested inside another one -- the guard "
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
