"""#1132: MSYS converts a /dev/null argv entry to `nul` before it reaches
lib-detach-pidwrap.sh, which then created a real file called `nul` (PID plus
all consolidation output). Fixed at the source (lib-detach.sh hands wscript an
empty string, which MSYS never converts), with a Windows-gated `nul` skip in
pidwrap as belt and braces, and an MSYS-only cleanup of an existing `nul`."""
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIDWRAP = os.path.join(ROOT, "scripts", "lib-detach-pidwrap.sh")
LIB = os.path.join(ROOT, "scripts", "lib-detach.sh")
LAUNCH = os.path.join(ROOT, "scripts", "consolidation-launch.sh")
SH = shutil.which("sh")
BASH = shutil.which("bash")
ON_WINDOWS = sys.platform == "win32"

needs_sh = pytest.mark.skipif(SH is None, reason="no sh on PATH")
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash on PATH")
posix_only = pytest.mark.skipif(
    ON_WINDOWS, reason="'nul' is the real null device here; non-Windows semantics untestable")


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k != "OS"}
    env.update(extra)
    return env


def _pidwrap(cwd, outfile, pidfile, **env):
    return subprocess.run(
        [SH, PIDWRAP, outfile, pidfile, "echo", "hi"],
        cwd=str(cwd), capture_output=True, text=True, timeout=30,
        env=_env(**env), check=False,
    )


@needs_sh
@pytest.mark.parametrize("spelling", ["/dev/null", ""])
def test_skip_spellings_skip_both_steps(tmp_path, spelling):
    r = _pidwrap(tmp_path, spelling, spelling)
    assert r.returncode == 0, r.stderr
    assert "hi" in r.stdout  # no redirect happened
    assert os.listdir(str(tmp_path)) == []


@needs_sh
@pytest.mark.parametrize("spelling", ["nul", "NUL"])
def test_msys_nul_skips_when_windows(tmp_path, spelling):
    r = _pidwrap(tmp_path, spelling, spelling, OS="Windows_NT")
    assert r.returncode == 0, r.stderr
    assert "hi" in r.stdout
    assert os.listdir(str(tmp_path)) == []


@needs_sh
@posix_only
def test_nul_is_a_legit_filename_off_windows(tmp_path):
    """Negative control: off Windows, `NUL` is just a filename and is honoured."""
    out = tmp_path / "NUL"
    r = _pidwrap(tmp_path, str(out), "/dev/null")
    assert r.returncode == 0, r.stderr
    assert "hi" in out.read_text()
    _pidwrap(tmp_path / ".", "/dev/null", "nul")
    assert (tmp_path / "nul").read_text().strip().isdigit()


@needs_sh
def test_real_paths_are_still_written(tmp_path):
    """Positive control: a real outfile/pidfile is honoured."""
    out = tmp_path / "out.log"
    pid = tmp_path / "pid.txt"
    r = _pidwrap(tmp_path, str(out), str(pid))
    assert r.returncode == 0, r.stderr
    assert "hi" in out.read_text()
    assert pid.read_text().strip().isdigit()


@needs_sh
@pytest.mark.parametrize("null", ["/dev/null", "", "nul"])
def test_mixed_real_outfile_null_pidfile(tmp_path, null):
    out = tmp_path / "out.log"
    r = _pidwrap(tmp_path, str(out), null, OS="Windows_NT")
    assert r.returncode == 0, r.stderr
    assert "hi" in out.read_text()
    assert sorted(os.listdir(str(tmp_path))) == ["out.log"]


@needs_sh
@pytest.mark.parametrize("null", ["/dev/null", "", "nul"])
def test_mixed_null_outfile_real_pidfile(tmp_path, null):
    pid = tmp_path / "pid.txt"
    r = _pidwrap(tmp_path, null, str(pid), OS="Windows_NT")
    assert r.returncode == 0, r.stderr
    assert "hi" in r.stdout
    assert pid.read_text().strip().isdigit()
    assert sorted(os.listdir(str(tmp_path))) == ["pid.txt"]


def _stub_wscript(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured = tmp_path / "captured.txt"
    for name, body in (
        ("wscript.exe", f'printf "%s\\n" "$@" > "{captured}"'),
        ("cygpath", 'shift; printf "%s" "$1"'),
    ):
        p = bindir / name
        with p.open("w", encoding="utf-8", newline="") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
    return bindir, captured


@needs_bash
@pytest.mark.skipif(ON_WINDOWS, reason="stub-wscript harness is POSIX-path based")
@pytest.mark.parametrize("out,pid,want_out,want_pid", [
    ("/dev/null", "/dev/null", "", ""),
    ("/dev/null", "/tmp/realpid", "", "/tmp/realpid"),
    ("/tmp/realout", "/dev/null", "/tmp/realout", ""),
    ("/tmp/realout", "/tmp/realpid", "/tmp/realout", "/tmp/realpid"),
])
def test_detach_windows_hands_wscript_empty_for_dev_null(
        tmp_path, out, pid, want_out, want_pid):
    bindir, captured = _stub_wscript(tmp_path)
    env = _env(TMPDIR=str(tmp_path), PATH=f"{bindir}:{os.environ['PATH']}")
    r = subprocess.run(
        [BASH, "-c", f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" echo hi; echo RC=$?'],
        env=env, capture_output=True, text=True, timeout=30, check=False)
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    lines = captured.read_text(encoding="utf-8").split("\n")
    assert lines[4] == want_out, lines
    assert lines[5] == want_pid, lines
    assert lines[6:8] == ["echo", "hi"], lines


def _launch(cwd, **env):
    return subprocess.run(
        [BASH, LAUNCH, "true"], cwd=str(cwd), capture_output=True, text=True,
        timeout=30, env=_env(**env), check=False)


@needs_bash
@posix_only
def test_launch_removes_stray_nul_under_msys(tmp_path):
    stray = tmp_path / "nul"
    stray.write_text("12345\nold output\n")
    r = _launch(tmp_path, OS="Windows_NT")
    assert r.returncode == 0, r.stderr
    assert not stray.exists()
    assert "nul" in r.stderr  # log line


@needs_bash
@posix_only
def test_launch_leaves_nul_alone_off_windows(tmp_path):
    """Negative + control: same file, same launcher, not Windows -> untouched."""
    stray = tmp_path / "nul"
    stray.write_text("keep me\n")
    r = _launch(tmp_path)
    assert r.returncode == 0, r.stderr
    assert stray.read_text() == "keep me\n"


@needs_bash
@posix_only
def test_launch_under_msys_with_no_nul_is_quiet(tmp_path):
    r = _launch(tmp_path, OS="Windows_NT")
    assert r.returncode == 0, r.stderr
    assert "nul" not in r.stderr
