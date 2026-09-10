"""ndc_read_gen() lacks the regular-file type check ts_marker_read() was
given (#634).

scripts/save-session.sh:113-130 defines ndc_read_gen(), which reaches
`cat "$NDC_GEN_FILE"` guarded only by an `-e`/`-L` existence test. A later
sibling function in the same file, ts_marker_read() (added by #625), does
the same job for a sibling marker and was given a regular-file type check
(see ts_marker_read()'s own comment: "`cat` on a FIFO with no writer present
BLOCKS forever (no O_NONBLOCK, no timeout anywhere in this script)"). That
check was never backported to ndc_read_gen -- so a FIFO or character device
at $REMEMBER_DIR/tmp/ndc-generation hangs the whole script indefinitely
while holding LOCK_DIR, or streams unboundedly for a character device.

This test proves the hang (and lack of type check) directly against
ndc_read_gen() in isolation, mirroring the extraction technique
TestTsMarkerReadDoesNotHangOnNonRegularFiles in
tests/test_save_session_marker_unreadable_625.py already uses for
ts_marker_read() -- source just the one function's definition out of a real
temp file (not process substitution -- #621/#627's macOS-CI-only failure
mode), call it directly, with a hard timeout as the actual assertion since a
hang is not a value a plain equality check can see.
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


def _extract_ndc_read_gen_source() -> str:
    script = REPO_ROOT / "scripts" / "save-session.sh"
    text = script.read_text(encoding="utf-8")
    match = re.search(r"^ndc_read_gen\(\).*?^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "ndc_read_gen() not found in scripts/save-session.sh -- extraction regex is stale"
    return match.group(0) + "\n"


def _call_ndc_read_gen(gen_file: Path, timeout: float = 5) -> subprocess.CompletedProcess:
    """Call ndc_read_gen() in isolation against a real NDC_GEN_FILE path.

    ndc_read_gen() reads the global $NDC_GEN_FILE rather than taking an
    argument, so the target path is passed through the environment, the same
    variable the real script exports.
    """
    func_src = _extract_ndc_read_gen_source()
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8"
    ) as f:
        f.write(func_src)
        func_file = f.name
    try:
        env = dict(os.environ)
        env["NDC_GEN_FILE"] = str(gen_file)
        cmd = ["bash", "-c", f'source "{func_file}"; ndc_read_gen']
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    finally:
        os.unlink(func_file)


class TestNdcReadGenDoesNotHangOnNonRegularFiles:

    def test_a_fifo_gen_file_returns_unreadable_without_blocking(self, tmp_path):
        gen_file = tmp_path / "ndc-generation"
        os.mkfifo(gen_file)
        # No writer is ever opened on this FIFO. Before the fix, `cat` on it
        # blocks forever -- i.e. for the life of this test process, since
        # nothing here or in the script ever opens a writer either. The
        # timeout is the assertion: a hang fails the test via
        # TimeoutExpired, which a bare pytest.raises cannot express for "did
        # not hang".
        proc = _call_ndc_read_gen(gen_file, timeout=5)
        assert proc.stdout.strip() == "unreadable", (
            f"a FIFO generation marker must read as unreadable (it can never "
            f"legitimately hold a generation number), got: {proc.stdout!r} / "
            f"{proc.stderr!r}"
        )

    def test_a_regular_file_gen_is_unaffected(self, tmp_path):
        """Positive control: the new type check must not break the ordinary case."""
        gen_file = tmp_path / "ndc-generation"
        gen_file.write_text("3")
        proc = _call_ndc_read_gen(gen_file, timeout=5)
        assert proc.stdout.strip() == "3", (
            f"a regular file holding a valid generation must still read as "
            f"that generation, got: {proc.stdout!r} / {proc.stderr!r}"
        )

    def test_an_absent_gen_file_still_reads_as_zero(self, tmp_path):
        """Positive control: the absence path (never created) is untouched."""
        gen_file = tmp_path / "never-created"
        proc = _call_ndc_read_gen(gen_file, timeout=5)
        assert proc.stdout.strip() == "0", (
            f"a generation file that was never created must still read as "
            f"0, got: {proc.stdout!r} / {proc.stderr!r}"
        )

    def test_a_dangling_symlink_reads_as_unreadable_not_zero(self, tmp_path):
        gen_file = tmp_path / "dangling-symlink-gen"
        gen_file.symlink_to(tmp_path / "nonexistent-target")
        proc = _call_ndc_read_gen(gen_file, timeout=5)
        assert proc.stdout.strip() == "unreadable", (
            f"a dangling symlink was created here even though its target is "
            f"gone -- it must read as unreadable, not as the fresh-0 a "
            f"generation file never created at all would, got: "
            f"{proc.stdout!r} / {proc.stderr!r}"
        )
