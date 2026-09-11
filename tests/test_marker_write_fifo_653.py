"""The marker WRITE sites must refuse a planted FIFO the way the reads do (#653, #654).

#625/#634/#642 gave every marker READ a regular-file type check, because `cat` on
a FIFO with no writer blocks forever. The WRITE side of the same paths was
rewritten in #635/#643 as `{ … > "$FILE"; } 2>/dev/null || true` -- and a `>` on
a FIFO with no reader blocks in `open(2)` before any redirection error exists
for the `2>/dev/null` or the `|| true` to catch. Release gate 3 (v0.31.0, round
1) exercised it: the write never returned. So a FIFO at `$COOLDOWN_MARKER`
passes the read as `unreadable`, is reported, and then hangs the post-save write
holding `LOCK_DIR` -- every later save blocks on the lock.

Two things pinned here:

  - `marker_write_ok()` (extracted from save-session.sh by content, the way
    tests/test_now_day_file_fifo_642.py extracts its block) refuses a FIFO and
    reports it, and allows a regular file and an absent path. The FIFO case is
    the timeout-as-assertion shape: a hang fails via TimeoutExpired.
  - every write to the four marker paths in save-session.sh sits behind that
    helper -- a static check, so a new write site added without the guard fails
    here rather than in a user's session.

#654 is the read-side sibling: a non-regular `NOW_DAY_FILE` used to fall
through to today's date with no log line. That assertion lives in
tests/test_now_day_file_fifo_642.py beside the read it describes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX FIFO -- not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE = REPO_ROOT / "scripts" / "save-session.sh"


def _extract_helper() -> str:
    text = SAVE.read_text(encoding="utf-8")
    m = re.search(r"^marker_write_ok\(\) \{.*?^\}\n", text, re.MULTILINE | re.DOTALL)
    assert m, "marker_write_ok() not found in scripts/save-session.sh"
    return m.group(0)


def _run(path: Path, timeout: float = 5) -> subprocess.CompletedProcess:
    script = (
        'report_error() { printf "REPORTED [%s] %s\\n" "$1" "$2" >&2; }\n'
        + _extract_helper()
        + f'\nif marker_write_ok "{path}" probe; then echo allowed; else echo refused; fi\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as f:
        f.write(script)
        name = f.name
    try:
        return subprocess.run(["bash", name], capture_output=True, text=True, timeout=timeout, check=False)
    finally:
        os.unlink(name)


class TestMarkerWriteOk:
    def test_a_fifo_is_refused_and_reported(self, tmp_path):
        p = tmp_path / "marker"
        os.mkfifo(p)
        proc = _run(p)
        assert proc.stdout.strip() == "refused", proc
        assert "REPORTED [probe]" in proc.stderr and "WARNING" in proc.stderr, proc.stderr

    def test_a_regular_file_is_allowed(self, tmp_path):
        p = tmp_path / "marker"
        p.write_text("1")
        proc = _run(p)
        assert proc.stdout.strip() == "allowed", proc
        assert proc.stderr == ""

    def test_an_absent_path_is_allowed(self, tmp_path):
        proc = _run(tmp_path / "never")
        assert proc.stdout.strip() == "allowed", proc
        assert proc.stderr == ""


_MARKERS = ("COOLDOWN_MARKER", "NDC_MARKER", "NOW_DAY_FILE", "NDC_GEN_FILE")


def test_every_marker_write_site_is_guarded():
    """Static: each `> "$MARKER"` write in save-session.sh is on a line that
    also invokes marker_write_ok for that marker, or sits inside an `if
    marker_write_ok "$MARKER"` block opened within the previous three lines.
    Read-side `<` redirects and `[ -f ]` tests are not writes and not counted."""
    lines = SAVE.read_text(encoding="utf-8").splitlines()
    unguarded = []
    for i, line in enumerate(lines):
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        for m in _MARKERS:
            if re.search(rf'(?<![<0-9])>\s*"\${m}"', code):
                window = "\n".join(lines[max(0, i - 3): i + 1])
                if f'marker_write_ok "${m}"' not in window:
                    unguarded.append(f"line {i + 1}: {line.strip()}")
    assert not unguarded, (
        "marker write site(s) not behind marker_write_ok -- a FIFO planted there "
        "blocks open(2) before any redirection error exists to suppress (#653):\n  "
        + "\n  ".join(unguarded)
    )
