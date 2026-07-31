"""Tests for ``rotate_logs`` in scripts/log.sh — issue #252.

Two defects, one visible and one not.

**The visible one.** ``rotate_logs`` passed an *absolute* archive path to
``tar -czf``. On Windows that path begins with a drive letter, and GNU tar
parses an ``-f`` argument whose colon precedes the first slash as ``host:path``
— so it tries to resolve a machine named ``C`` and exits 2. Verified against
GNU tar 1.35 locally::

    $ gtar -czf "C:/tmp/x/out.tar.gz" -C /tmp/x a.log
    tar (child): Cannot connect to C: resolve failed
    exit=2
    $ gtar -czf "/tmp/x/we:ird.tar.gz" -C /tmp/x a.log   # colon AFTER a slash
    exit=0

That second line is the whole rule: it is the colon's *position*, not its
presence, that triggers the remote parse. So an archive name with no directory
prefix at all cannot trip it on any tar, on any platform — which is why the fix
is a relative name and not ``--force-local``. bsdtar 3.5.3 (``/usr/bin/tar`` on
macOS, the maintainers' own platform) rejects ``--force-local`` outright with
exit 1 and writes no archive, so hardcoding that flag would have fixed Windows
by silently disabling macOS.

``test_archive_argument_carries_no_directory_prefix`` is the pin, and it is
deliberately an assertion about the *constructed argv* rather than about tar's
behaviour on the machine running the suite: a POSIX test box has no drive
letter to reproduce with, but "the ``-f`` argument contains no ``/``" is
checkable anywhere and is strictly stronger than "no colon" — a name with no
directory prefix has nowhere to put one.

**The invisible one, which is why it lasted five weeks.** ``2>/dev/null``
discarded tar's diagnostic, the failure branch logged one line without it, and
``rotate_logs`` returned 0 either way. So "there was nothing to rotate" and
"rotation could not run" were the same event to every caller, and the reporter
watched ``ERROR: tar failed for 19 logs`` scroll past daily for over a month
with no way to learn *why* from anything the plugin wrote down. The tests below
pin the three states separately, because the second defect is the one that will
still matter after tar is fixed.

Real artifacts throughout: a real temp log directory, real aged files, and a
real ``tar`` for the success path. The two shims that stand in for tar exist to
make it *fail* on demand and to record argv — both assert they actually ran,
because a shim that cannot spawn makes a test pass while testing nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

DAY = 86400


def _project(tmp_path: Path, aged: int = 3, age_days: int = 20):
    """A project whose log dir holds ``aged`` genuinely old memory logs.

    The files are real and their mtimes are really backdated — ``find -mtime
    +7`` is what selects them in the code under test, so faking that selection
    would be testing the fixture.
    """
    project = tmp_path / "proj"
    (project / ".claude" / "remember").mkdir(parents=True)
    logs = project / ".remember" / "logs"
    logs.mkdir(parents=True)

    old = time.time() - age_days * DAY
    names = []
    for i in range(aged):
        stamp = time.strftime("%Y-%m-%d", time.localtime(old + i * DAY))
        f = logs / f"memory-{stamp}.log"
        f.write_text(f"08:33:14 [save] entry {i}\n", encoding="utf-8")
        os.utime(f, (old, old))
        names.append(f.name)
    return project, logs, names


def _rotate(project: Path, path_prefix: Path | None = None, env_extra: dict | None = None):
    """Source log.sh, run rotate_logs, report its exit status.

    ``rotate_logs`` is the unit under test, so its status is echoed rather than
    inherited: the script must keep running afterwards to print it.
    """
    script = f'''
    export PROJECT_DIR="{project}"
    source "{LOG_SH}"
    rotate_logs
    echo "ROTATE_STATUS=$?"
    '''
    env = {**os.environ}
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True, timeout=120)


def _status(result) -> int:
    for line in result.stdout.splitlines():
        if line.startswith("ROTATE_STATUS="):
            return int(line.split("=", 1)[1])
    raise AssertionError(f"rotate_logs never returned:\n{result.stdout}\n{result.stderr}")


def _log_text(logs: Path) -> str:
    """Everything rotate_logs wrote to today's log file."""
    return "\n".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in sorted(logs.glob("memory-*.log"))
    )


def _tar_shim(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """A ``tar`` earlier on PATH than the real one, recording its argv.

    One argument per line, so an argument containing a space cannot be mistaken
    for two — the ``-f`` value is exactly what this test is about.
    """
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    argv = tmp_path / "tar-argv.txt"
    shim = d / "tar"
    shim.write_text(
        "#!/bin/bash\n"
        f'for a in "$@"; do printf "%s\\n" "$a" >> "{argv}"; done\n'
        f"{body}\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return d, argv


def _recorded_argv(argv: Path) -> list[str]:
    """The shim's argv — and proof the shim actually executed.

    A fixture that silently fails to spawn turns every assertion downstream
    into a vacuous truth. This is the assertion that says it ran.
    """
    assert argv.exists(), (
        "the tar shim never executed — PATH did not pick it up, so nothing "
        "below tested anything"
    )
    lines = [l for l in argv.read_text(encoding="utf-8").splitlines() if l]
    assert lines, "the tar shim executed but recorded no arguments"
    return lines


def _f_argument(argv_lines: list[str]) -> str:
    """The value tar would open as its archive — the only argument subject to
    the ``host:path`` parse."""
    for i, a in enumerate(argv_lines):
        if a == "-f":
            return argv_lines[i + 1]
        if a.startswith("-") and not a.startswith("--") and a.endswith("f"):
            # bundled short flags: -czf <archive>
            return argv_lines[i + 1]
    raise AssertionError(f"no -f argument in tar argv: {argv_lines}")


# ── The tar invocation ───────────────────────────────────────────────────────


def test_archive_argument_carries_no_directory_prefix(tmp_path):
    """#252: the archive name tar opens must have nowhere to hide a drive letter.

    GNU tar treats an ``-f`` argument as ``host:path`` when a colon precedes the
    first slash. An argument with no slash at all therefore cannot be parsed as
    remote by any tar implementation — no flag, no platform detection, no
    ``--force-local`` (which bsdtar rejects outright).

    Before the fix this argument is ``<REMEMBER_LOG_DIR>/logs-YYYY-MM.tar.gz``,
    an absolute path, which is a drive-letter path on Windows and is exactly
    what the reporter's tar refused to open.
    """
    project, logs, _ = _project(tmp_path)
    shim_dir, argv = _tar_shim(tmp_path, "exit 0")

    _rotate(project, path_prefix=shim_dir)
    f_arg = _f_argument(_recorded_argv(argv))

    assert "/" not in f_arg, (
        f"tar -f got {f_arg!r}, which carries a directory prefix. On Windows "
        "that prefix is 'C:', and GNU tar reads a colon before the first slash "
        "as a remote host — 'Cannot connect to C: resolve failed' (#252)."
    )
    # The consequence, stated directly: nothing before a slash means nothing
    # before a colon either.
    assert ":" not in f_arg.split("/")[0], (
        f"tar -f got {f_arg!r} — GNU tar would parse this as host:path"
    )


def test_rotation_archives_aged_logs_with_the_real_tar(tmp_path):
    """The happy path, end to end, against whichever tar this machine has.

    This is the guard on the fix rather than on the bug: a relative archive name
    must still produce the same archive, in the same directory, with the same
    member names, and must still delete the originals. Run against bsdtar 3.5.3
    and GNU tar 1.35 during development; both produce identical members.
    """
    project, logs, names = _project(tmp_path)

    result = _rotate(project)

    assert _status(result) == 0, f"rotation failed:\n{result.stdout}\n{result.stderr}"
    archives = list(logs.glob("logs-*.tar.gz"))
    assert len(archives) == 1, f"expected one archive, found {[a.name for a in archives]}"
    assert archives[0].parent == logs, "archive landed outside the log directory"

    with tarfile.open(archives[0], "r:gz") as tf:
        # `._name` entries are AppleDouble sidecars: bsdtar on macOS stores the
        # extended attributes pytest's tmp files carry. They are an artifact of
        # the machine running the suite, not of the code under test, and they
        # appear identically before and after this fix — so they are excluded
        # rather than asserted on, which would make the test pass or fail on
        # filesystem xattrs.
        members = sorted(n for n in tf.getnames() if not Path(n).name.startswith("._"))
    assert members == sorted(names), f"archive members {members} != aged logs {sorted(names)}"

    for n in names:
        assert not (logs / n).exists(), f"{n} was archived but not removed"


# ── The three states ─────────────────────────────────────────────────────────


def test_nothing_to_rotate_is_silent_and_successful(tmp_path):
    """State one. No aged logs is not an error and is not worth a line."""
    project, logs, _ = _project(tmp_path, aged=0)

    result = _rotate(project)

    assert _status(result) == 0
    assert "[rotate]" not in _log_text(logs), (
        "an empty rotation announced itself — the quiet state must stay quiet, "
        "or the noisy state stops meaning anything"
    )


def test_failed_rotation_reports_tars_own_reason(tmp_path):
    """State three, and the reason #252 went five weeks undiagnosed.

    ``2>/dev/null`` threw away the one sentence that identified the cause. The
    log line must carry tar's own diagnostic, or the next failure is just as
    unreadable as this one was.
    """
    project, logs, names = _project(tmp_path)
    shim_dir, argv = _tar_shim(
        tmp_path,
        'echo "tar (child): Cannot connect to C: resolve failed" >&2\nexit 2',
    )

    result = _rotate(project, path_prefix=shim_dir)
    _recorded_argv(argv)
    text = _log_text(logs)

    assert "Cannot connect to C" in text, (
        "the failure was logged without tar's diagnostic, so the log says "
        f"something broke but not what:\n{text}"
    )
    assert "ERROR" in text, f"a failed rotation was not logged as an error:\n{text}"


def test_failed_rotation_is_distinguishable_from_nothing_to_rotate(tmp_path):
    """State three must not look like state one *to a caller*, not only in a log.

    ``rotate_logs`` returned 0 on both. A caller that wanted to know whether
    rotation is working had no way to ask.
    """
    project, logs, _ = _project(tmp_path)
    shim_dir, argv = _tar_shim(tmp_path, 'echo "boom" >&2\nexit 2')

    failed = _status(_rotate(project, path_prefix=shim_dir))
    _recorded_argv(argv)

    empty_project, _, _ = _project(tmp_path / "empty", aged=0)
    nothing = _status(_rotate(empty_project))

    assert nothing == 0, "nothing to rotate is not a failure"
    assert failed != 0, (
        "a rotation that could not run returned success — indistinguishable "
        "from one that had nothing to do (#252)"
    )


def test_failed_rotation_keeps_the_logs_it_could_not_archive(tmp_path):
    """The originals are the only copy. A failed archive must not delete them."""
    project, logs, names = _project(tmp_path)
    shim_dir, argv = _tar_shim(tmp_path, 'echo "boom" >&2\nexit 2')

    _rotate(project, path_prefix=shim_dir)
    _recorded_argv(argv)

    for n in names:
        assert (logs / n).exists(), f"{n} was deleted although archiving failed"


# ── Escalation ───────────────────────────────────────────────────────────────


def test_repeated_failure_escalates_beyond_one_line_a_day(tmp_path):
    """The reporter saw one identical line per day for five weeks.

    One line a day is demonstrably not enough to prompt an investigation, so a
    rotation that keeps failing has to say something *different* once it is
    clearly stuck rather than repeating the same sentence forever.
    """
    project, logs, _ = _project(tmp_path)
    shim_dir, argv = _tar_shim(tmp_path, 'echo "boom" >&2\nexit 2')

    for _ in range(3):
        _rotate(project, path_prefix=shim_dir)
    _recorded_argv(argv)

    text = _log_text(logs)
    assert "3 times" in text or "3 consecutive" in text, (
        "three consecutive failures produced three identical lines — nothing "
        f"escalated:\n{text}"
    )
    assert "doctor" in text, (
        "the escalated line does not tell the user where to look for the "
        f"diagnosis:\n{text}"
    )


def test_a_recovered_rotation_clears_the_failure_state(tmp_path):
    """A stuck-rotation warning that outlives the problem is a false alarm."""
    project, logs, _ = _project(tmp_path)
    shim_dir, argv = _tar_shim(tmp_path, 'echo "boom" >&2\nexit 2')

    for _ in range(3):
        _rotate(project, path_prefix=shim_dir)
    _recorded_argv(argv)

    # Without this the test is vacuous — a build that never writes a breadcrumb
    # would "clear" one trivially and this would pass while checking nothing.
    assert (logs / ".rotate-failed").exists(), (
        "no failure breadcrumb was written, so the assertion below would pass "
        "no matter what the code did"
    )

    result = _rotate(project)  # real tar, succeeds
    assert _status(result) == 0

    assert not (logs / ".rotate-failed").exists(), (
        "the failure breadcrumb survived a successful rotation — doctor would "
        "keep reporting a problem that is over"
    )


def test_doctor_reports_a_stuck_rotation(tmp_path):
    """The escalation surface. ``/remember:doctor`` exists to answer 'is this
    silently broken?', and until now a permanently-failing rotation was exactly
    the kind of silence it could not see."""
    project, logs, _ = _project(tmp_path)
    shim_dir, argv = _tar_shim(tmp_path, 'echo "Cannot connect to C" >&2\nexit 2')

    for _ in range(3):
        _rotate(project, path_prefix=shim_dir)
    _recorded_argv(argv)

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(project / ".remember"),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    out = subprocess.run(["bash", str(DOCTOR)], env=env,
                         capture_output=True, text=True, timeout=120).stdout

    assert "rotation" in out.lower(), f"doctor never mentions log rotation:\n{out}"
    assert "Cannot connect to C" in out, (
        f"doctor reports the rotation failure without its cause:\n{out}"
    )


# ── The archive is not a scratch file (#255) ─────────────────────────────────


def _age_more_logs(logs: Path, count: int, age_days: int) -> list[str]:
    """Add ``count`` more genuinely-aged logs, with names no earlier batch used.

    Real files, really backdated — ``find -mtime +7`` is the selector in the
    code under test, so a fixture that faked the selection would be testing
    itself.
    """
    old = time.time() - age_days * DAY
    names = []
    for i in range(count):
        stamp = time.strftime("%Y-%m-%d", time.localtime(old + i * DAY))
        f = logs / f"memory-{stamp}.log"
        f.write_text(f"08:33:14 [save] batch {age_days} entry {i}\n", encoding="utf-8")
        os.utime(f, (old, old))
        names.append(f.name)
    return names


def _archived_members(logs: Path) -> set[str]:
    """Every log recoverable from every archive in the directory.

    Deliberately a union over ``logs-*.tar.gz`` rather than a check on one
    filename: the post-condition of #255 is that the data survives *somewhere*,
    which is a weaker and more honest requirement than any particular naming
    scheme. A fix that scatters, one that merges and one that renames all pass
    this if and only if nothing was lost.
    """
    found: set[str] = set()
    for a in sorted(logs.glob("logs-*.tar.gz")):
        with tarfile.open(a, "r:gz") as tf:
            found |= {
                n for n in tf.getnames() if not Path(n).name.startswith("._")
            }
    return found


def test_a_second_rotation_in_the_same_month_keeps_the_first_batch(tmp_path):
    """#255: rotation deletes the originals, so the archive is the only copy.

    The archive name carried only a month and ``tar -czf`` truncates, so the
    second rotation of a calendar month replaced the first one's archive with
    its own contents — and the logs inside it had already been deleted from
    disk when it was written. Nothing survived anywhere.

    The assertion is about the *data*, not the naming: after two rotations,
    every log either still sits in the directory or is readable out of some
    archive in it. Anything weaker would be a test of the naming scheme.
    """
    project, logs, first = _project(tmp_path, aged=2, age_days=40)

    assert _status(_rotate(project)) == 0, "first rotation failed"
    for n in first:
        assert not (logs / n).exists(), (
            f"{n} survived on disk, so this test would pass without an archive"
        )
    assert set(first) <= _archived_members(logs), (
        "the first rotation did not archive what it deleted — the setup for "
        "this test never happened"
    )

    second = _age_more_logs(logs, 2, age_days=20)
    assert not set(second) & set(first), "batches must not share filenames"

    assert _status(_rotate(project)) == 0, "second rotation failed"

    recoverable = _archived_members(logs) | {
        p.name for p in logs.glob("memory-*.log")
    }
    lost = sorted(set(first) - recoverable)
    assert not lost, (
        f"{lost} exist nowhere: deleted from disk by the first rotation and "
        "then overwritten in the archive by the second (#255)"
    )
    assert set(second) <= recoverable, "the second batch was lost instead"


def test_rotation_never_truncates_an_existing_archive(tmp_path):
    """The mechanism, pinned directly.

    ``tar -czf`` opens its archive with O_TRUNC. Whatever this rotation writes,
    it must not be a file that already held the only copy of something — so no
    pre-existing ``logs-*.tar.gz`` may lose bytes across a rotation.
    """
    project, logs, first = _project(tmp_path, aged=2, age_days=40)
    assert _status(_rotate(project)) == 0

    before = {
        p.name: p.read_bytes() for p in logs.glob("logs-*.tar.gz")
    }
    assert before, "no archive was produced, so there is nothing to protect"

    _age_more_logs(logs, 2, age_days=20)
    assert _status(_rotate(project)) == 0

    for name, blob in before.items():
        after = logs / name
        assert after.exists(), f"{name} was removed by a later rotation"
        assert after.read_bytes() == blob, (
            f"{name} was rewritten by a later rotation — the logs it held were "
            "already deleted from disk (#255)"
        )


def test_originals_survive_a_tar_that_exits_zero_without_archiving(tmp_path):
    """Deletion must be gated on the archive being complete, not on exit 0.

    A tar that reports success and writes an archive missing members is the
    general case behind #255: the success branch trusted the exit status and
    removed the originals on the strength of it. Here tar produces a valid
    archive containing only *one* of the aged logs and exits 0.
    """
    project, logs, names = _project(tmp_path, aged=3, age_days=20)
    # A real tar, invoked with only the first member — a truthful exit 0 over
    # an archive that is missing two files.
    shim_dir, argv = _tar_shim(
        tmp_path,
        'args=(); for a in "$@"; do args+=("$a"); done\n'
        'exec /usr/bin/tar "${args[0]}" "${args[1]}" "${args[2]}"',
    )

    _rotate(project, path_prefix=shim_dir)
    _recorded_argv(argv)

    recoverable = _archived_members(logs) | {
        p.name for p in logs.glob("memory-*.log")
    }
    lost = sorted(set(names) - recoverable)
    assert not lost, (
        f"{lost} were deleted although the archive does not contain them — "
        "tar exited 0 and that was taken as proof"
    )
