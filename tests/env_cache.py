"""Say which path a hook run took, instead of hoping it took the right one.

`scripts/lib-env-cache.sh` lets a hook replay a resolution somebody already
paid for — ~19 processes on macOS, 27 on the Windows/QEMU box in #227 — and it
refuses that cache unless the cache file is `-nt` **every** config layer. Bash's
`-nt` compares whole SECONDS. A config written in the same second as the cache
therefore reads as "not newer", the cache is rejected, and the next run resolves
from scratch.

That direction fails safe: one extra re-resolution, never a stale answer, and
nothing here asks to change it. The hazard is in the tests. A spawn-budget test
that writes a config and then counts processes on the "warm" run is measuring
one of two different things depending on which side of a second boundary the two
writes landed on — cold and expensive, or warm and cheap. Both are correct
product behaviour; only one is the thing the test claims to measure, and when it
passes there is nothing in the output saying which it was (#303).

Two halves, and the second is the one that matters:

* :func:`write_config` removes the coin flip — a config written through it is
  unambiguously older than any cache published afterwards.
* :class:`EnvCacheProbe` removes the *silence*. It brackets a run and reports
  ``warm`` / ``cold`` / ``unknown``, the same three answers this repo insists on
  from every other checker: yes, no, and cannot tell. Backdating alone leaves a
  measurement that is merely likely to be right; a hook change that stops the
  cache loading at all would leave a green budget test quietly measuring the
  cold path forever.

How the verdict is reached, and why it needs no clock: a warm run reads the
cache and writes nothing. A cold run ends in ``_remember_env_cache_publish``,
which writes a private temp file and ``mv``s it over the cache — a rename, so
the cache file's **inode** changes. Comparing inode and mtime across the run
distinguishes the two by causality rather than by timing, at any duration,
including one shorter than any clock on this platform can name.

Usage::

    from tests.env_cache import EnvCacheProbe, write_config

    write_config(home / ".remember" / "config.json", {"timezone": "UTC"})
    _run(env)                       # cold; publishes the resolution

    probe = EnvCacheProbe(env["TMPDIR"])
    probe.snapshot()
    _run(env)                       # the run being measured
    probe.assert_warm("the spawn budget")

Pinned by ``tests/test_env_cache_probe_303.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

WARM = "warm"
COLD = "cold"
UNKNOWN = "unknown"

# The name `_remember_env_cache_path` builds, minus the project key.
CACHE_GLOB = "remember-env-*"

# Far enough that no second boundary, and no plausible slowness between the
# write and the run that follows it, can put the config on the wrong side of the
# cache. A config the user edited before this session started is also what a
# warm-path measurement is supposed to be about.
OLDER_BY_SECONDS = 60

# The other direction, for a test that wants the cache invalidated on purpose.
# Must exceed one whole second for the same reason.
NEWER_BY_SECONDS = 5


def write_config(path, data: dict, *, older_by: float = OLDER_BY_SECONDS) -> Path:
    """Write a config layer that is unambiguously older than any later cache."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    age_back(path, seconds=older_by)
    return path


def age_back(path, *, seconds: float = OLDER_BY_SECONDS) -> None:
    """Backdate a file so `-nt` cannot call it newer than a cache written now."""
    when = time.time() - seconds
    os.utime(path, (when, when))


def invalidate(path, *, seconds: float = NEWER_BY_SECONDS) -> None:
    """Post-date a config so an existing cache is refused on the next run.

    The mirror of :func:`age_back`, and needed for the same reason: a config
    rewritten in the same second as the cache that must lose to it reads as
    "not newer" and the edit is invisible until the next second.
    """
    when = time.time() + seconds
    os.utime(path, (when, when))


_NT_GRANULARITY: Optional[str] = None


def nt_granularity() -> str:
    """Measure — never assume — how finely bash's `-nt` resolves here.

    Returns ``"second"`` when a file 0.8s newer than another, inside the same
    whole second, is *not* reported newer; ``"sub-second"`` when it is;
    ``"unknown"`` when bash could not be run at all. The same-second cache
    rejection this module exists for is only reachable on a ``"second"``
    platform, so a test reproducing it can say what went unchecked elsewhere
    rather than failing on a premise that does not hold.
    """
    global _NT_GRANULARITY
    if _NT_GRANULARITY is not None:
        return _NT_GRANULARITY
    with tempfile.TemporaryDirectory() as tmp:
        older = Path(tmp) / "older"
        newer = Path(tmp) / "newer"
        older.write_text("", encoding="utf-8")
        newer.write_text("", encoding="utf-8")
        second = int(time.time())
        os.utime(older, ns=((second * 1_000_000_000) + 100_000_000,) * 2)
        os.utime(newer, ns=((second * 1_000_000_000) + 900_000_000,) * 2)
        try:
            result = subprocess.run(
                ["bash", "-c", f'[ "{newer}" -nt "{older}" ]'], timeout=15
            )
        except (OSError, subprocess.SubprocessError):
            _NT_GRANULARITY = "unknown"
        else:
            _NT_GRANULARITY = "sub-second" if result.returncode == 0 else "second"
    return _NT_GRANULARITY


@dataclass(frozen=True)
class CacheVerdict:
    """One bracketed run's answer, with the reason it reached it.

    Three answers, not two. ``unknown`` is not a softer ``cold``: a cold run
    means the setup raced and the number is the wrong number, while an unknown
    one means nothing was published or replayed and the number cannot be
    attributed at all. They have different repairs, so they are different words.
    """

    verdict: str
    detail: str

    @property
    def is_warm(self) -> bool:
        return self.verdict == WARM


_Identity = Dict[str, Tuple[int, int, int]]


def _identities(tmpdir: Path) -> _Identity:
    found: _Identity = {}
    for path in sorted(Path(tmpdir).glob(CACHE_GLOB)):
        try:
            stat = path.stat()
        except OSError:
            continue
        found[path.name] = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    return found


class EnvCacheProbe:
    """Brackets a hook run and reports whether it replayed or re-resolved."""

    def __init__(self, tmpdir) -> None:
        self.tmpdir = Path(tmpdir)
        self._before: Optional[_Identity] = None

    def snapshot(self) -> None:
        """Record the published resolutions as they stand before the run."""
        self._before = _identities(self.tmpdir)

    def verdict(self) -> CacheVerdict:
        """Attribute what happened between :meth:`snapshot` and now."""
        if self._before is None:
            raise RuntimeError(
                "EnvCacheProbe.verdict() without a preceding snapshot(): with no "
                "baseline, 'nothing changed' and 'nothing was looked at' are the "
                "same observation, and reporting warm for the second is exactly "
                "the misreport this probe exists to prevent."
            )
        before, after = self._before, _identities(self.tmpdir)

        if not after:
            return CacheVerdict(
                UNKNOWN,
                f"no resolution is published under {self.tmpdir} — the env cache is "
                f"disabled, unwritable, or keyed on a different project, so nothing "
                f"here can say whether the run replayed or resolved",
            )

        republished = sorted(
            name for name, identity in after.items() if before.get(name) != identity
        )
        if republished:
            was_new = [name for name in republished if name not in before]
            return CacheVerdict(
                COLD,
                f"the run re-resolved and published: {', '.join(republished)}"
                + (f" (published for the first time: {', '.join(was_new)})" if was_new else ""),
            )
        return CacheVerdict(
            WARM,
            f"the published resolution was replayed untouched: {', '.join(sorted(after))}",
        )

    def assert_warm(self, what: str) -> None:
        """Fail with the path that was measured, not just the number.

        "3 spawns, expected 2" sends a reader looking for a new fork in the
        hook. "this run was cold" sends them to the two lines of setup that
        caused it — which is where the bug is, every time this fires.
        """
        verdict = self.verdict()
        if verdict.is_warm:
            return
        raise AssertionError(
            f"{what} did not measure the warm path: {verdict.verdict} — {verdict.detail}. "
            f"The number this produced answers a different question than the one asked. "
            f"Write config layers through tests.env_cache.write_config, which backdates "
            f"them past bash's whole-second `-nt` granularity (#303)."
        )
