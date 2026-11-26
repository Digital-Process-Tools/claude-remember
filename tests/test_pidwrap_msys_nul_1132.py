"""#1132: MSYS converts a /dev/null argv entry to `nul` before it reaches
lib-detach-pidwrap.sh; the script must treat that as "skip" instead of
creating a file called nul (holding a PID) in the cwd."""
import os
import shutil
import subprocess

import pytest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "lib-detach-pidwrap.sh",
)
SH = shutil.which("sh")


def _run(cwd, outfile, pidfile):
    return subprocess.run(
        [SH, SCRIPT, outfile, pidfile, "echo", "hi"],
        cwd=str(cwd), capture_output=True, text=True, timeout=30, check=False,
    )


@pytest.mark.skipif(SH is None, reason="no sh on PATH")
@pytest.mark.parametrize("spelling", ["/dev/null", "nul", "NUL", "Nul"])
def test_null_device_spellings_skip_both_steps(tmp_path, spelling):
    r = _run(tmp_path, spelling, spelling)
    assert r.returncode == 0, r.stderr
    assert "hi" in r.stdout  # no redirect happened
    assert os.listdir(str(tmp_path)) == []  # no stray nul/NUL file


@pytest.mark.skipif(SH is None, reason="no sh on PATH")
def test_real_paths_are_still_written(tmp_path):
    """Positive control: a real outfile/pidfile is honoured."""
    out = tmp_path / "out.log"
    pid = tmp_path / "pid.txt"
    r = _run(tmp_path, str(out), str(pid))
    assert r.returncode == 0, r.stderr
    assert "hi" in out.read_text()
    assert pid.read_text().strip().isdigit()
