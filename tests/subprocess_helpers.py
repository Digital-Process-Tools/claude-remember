"""Shared failure-message helper for tests that drive plugin scripts (#210).

`scripts/bootstrap-dirs.sh:112` redirects a driven script's stderr into
`logs/hook-errors.log` once REMEMBER_DIR exists, so `result.stderr` for any
subprocess started by these tests is *always* empty by design. The common

    assert result.returncode == 0, result.stderr

idiom therefore prints nothing on failure -- exactly when it is needed. This
cost about an hour on #208: the real diagnostic (`exec: command: not found`)
sat in hook-errors.log the whole time while the blank stderr sent the
investigation elsewhere.

Use ``subprocess_failure_detail`` in its place::

    assert result.returncode == 0, subprocess_failure_detail(result, remember_dir)

where ``remember_dir`` is the ``.remember`` directory the driven script was
pointed at (e.g. ``project / ".remember"``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# The log accumulates across the whole test run; only the most recent bytes
# are relevant to the failure just observed, so truncate from the end.
_TAIL_BYTES = 4000


def subprocess_failure_detail(result: subprocess.CompletedProcess, remember_dir: Path) -> str:
    """Assemble a useful assertion message: rc, stdout, and the log tail.

    Must never raise -- a missing directory, an unreadable file, or a
    permission error all degrade to a string. A helper that throws while
    building an assertion message turns one clear failure into a confusing
    one, which is worse than the blank string this replaces.
    """
    log_path = Path(remember_dir) / "logs" / "hook-errors.log"
    try:
        if not log_path.is_file():
            log_section = f"(no hook-errors.log at {log_path})"
        else:
            log_section = log_path.read_bytes()[-_TAIL_BYTES:].decode(
                "utf-8", errors="replace"
            )
    except Exception as exc:  # noqa: BLE001 - must degrade to a string, never raise
        log_section = f"(could not read {log_path}: {exc!r})"

    return (
        f"rc={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"hook-errors.log tail ({log_path}):\n{log_section}"
    )
