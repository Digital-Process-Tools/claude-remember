"""NOW_DAY_FILE's read lacks the regular-file type check ndc_read_gen() and
ts_marker_read() were both given (#642).

scripts/save-session.sh reads $NOW_DAY_FILE via a bare
`cat "$NOW_DAY_FILE" 2>/dev/null | tr -d '[:space:]'`, guarded by nothing --
not even the `-e`/`-L` existence test ndc_read_gen() had before #634. A FIFO
or character device placed at this path hangs the whole script indefinitely
(cat on a FIFO with no writer blocks forever; a character device streams
unboundedly), the same defect #634 fixed for NDC_GEN_FILE and #625 fixed for
ts_marker_read()'s marker files.

Extracts the exact NDC_DAY read + case-fallback block out of
scripts/save-session.sh by content (not by line number, which the issue
itself flags as stale/pre-diff), mirroring the extraction technique
tests/test_ndc_read_gen_fifo_634.py already uses.
"""

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


def _extract_ndc_day_block() -> str:
    script = REPO_ROOT / "scripts" / "save-session.sh"
    text = script.read_text(encoding="utf-8")
    match = re.search(
        r"^if \[ -f \"\$NOW_DAY_FILE\" \].*?^esac\n",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, (
        "NDC_DAY read + case-fallback block not found in "
        "scripts/save-session.sh -- extraction regex is stale"
    )
    return match.group(0)


def _read_ndc_day(now_day_file: Path, today_date: str = "2026-01-01", timeout: float = 5) -> subprocess.CompletedProcess:
    block_src = _extract_ndc_day_block()
    script = f'NOW_DAY_FILE="{now_day_file}"\nTODAY_DATE="{today_date}"\n{block_src}\nprintf %s "$NDC_DAY"\n'
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        script_file = f.name
    try:
        cmd = ["bash", script_file]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    finally:
        os.unlink(script_file)


class TestNowDayFileDoesNotHangOnNonRegularFiles:

    def test_a_fifo_now_day_file_does_not_hang(self, tmp_path):
        now_day_file = tmp_path / "now-day"
        os.mkfifo(now_day_file)
        # No writer is ever opened on this FIFO. Before the fix, `cat` on it
        # blocks forever. The timeout is the assertion: a hang fails via
        # TimeoutExpired, which a bare equality check cannot express.
        proc = _read_ndc_day(now_day_file, timeout=5)
        assert proc.returncode == 0 and proc.stdout == "2026-01-01", (
            f"a FIFO now-day marker must fall back to today's date rather "
            f"than hanging, got returncode={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

    def test_a_regular_file_now_day_is_unaffected(self, tmp_path):
        """Positive control: the new type check must not break the ordinary case."""
        now_day_file = tmp_path / "now-day"
        now_day_file.write_text("2025-06-15")
        proc = _read_ndc_day(now_day_file, timeout=5)
        assert proc.stdout == "2025-06-15", (
            f"a regular file holding a valid date must still read as that "
            f"date, got: {proc.stdout!r} / {proc.stderr!r}"
        )

    def test_an_absent_now_day_file_falls_back_to_today(self, tmp_path):
        """Positive control: the absence path (never created) is untouched."""
        now_day_file = tmp_path / "never-created"
        proc = _read_ndc_day(now_day_file, timeout=5)
        assert proc.stdout == "2026-01-01", (
            f"a now-day file that was never created must fall back to "
            f"today's date, got: {proc.stdout!r} / {proc.stderr!r}"
        )
