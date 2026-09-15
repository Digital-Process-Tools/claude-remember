"""The capture-gap check must not block the session start (#660).

Lane A of #660: the SessionStart hook's only hard obligation is to produce
the injected context on stdout. The capture-gap check is not part of that --
its output is a file under `tmp/`, and the only thing that ever reads it is
`user-prompt-hook.sh`, on the NEXT user prompt. Until this change it ran in
the foreground anyway, and its `grep -q '"tool_use"'` reads the previous
session's transcript to EOF whenever the pattern is absent -- measured at
0.197s on a 100MB transcript on windows-latest, inside a start whose whole
budget is a couple of seconds.

The two tests here are a pair on purpose (CLAUDE.md: a negative assertion
needs a positive control):

  * `test_capture_gap_check_does_not_block_session_start` -- the hook must
    EXIT while the check is still running. A gate shim holds `grep` open, so
    this is deterministic rather than a timing threshold: before the change
    the hook cannot exit, and the run dies on the subprocess timeout.
  * `test_capture_gap_notice_still_written_after_release` -- the same run,
    continued: once the gate is released the notice must still appear. A
    "does not block" that passes because the work silently stopped happening
    would be the worse bug, and this is what tells them apart.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from .test_session_start_windows_benchmark_669 import (
    BASH,
    PREV_SESSION,
    SESSION,
    SESSION_START,
    _env,
    _payload,
    _slug,
    _store,
    _write_no_crlf,
)

pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash on this host (#432)"
)

# The unsaved previous session the check is meant to notice.
GAP_SESSION = "ffffffff-0000-4000-8000-00000000660a"

# Long enough that a foreground `grep` cannot finish inside the hook's own
# timeout below, short enough not to hang a suite if something goes wrong.
GATE_TIMEOUT = 120
HOOK_TIMEOUT = 45
NOTICE_TIMEOUT = 45


def _gate_shim(workdir: Path, started: Path, release: Path) -> Path:
    """A PATH front-end whose `grep` blocks until `release` exists.

    Only the capture-gap call is gated -- the shim looks for the hook's own
    `"tool_use"` pattern in its arguments and execs the real grep straight
    through for anything else, so nothing else in the hook is affected by
    the gate and the test cannot pass for the wrong reason.
    """
    shim_dir = workdir / "gate-bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    # Asked of bash, not of os.environ: on windows-latest the real binary is
    # `grep.exe` under Git's own usr/bin, which the Python process's PATH does
    # not necessarily carry -- a plain scan for a file literally named "grep"
    # found nothing and failed both tests on every Windows leg while passing
    # on macOS and Linux. `command -v` in the SAME bash the hook runs under
    # answers with the path that bash will actually resolve, msys form
    # included, which is the only form the shim's own `exec` can use.
    probe = subprocess.run(
        [BASH, "-c", "command -v grep"],
        capture_output=True, text=True, check=False,
    )
    real = probe.stdout.strip()
    assert real, f"no grep resolvable by {BASH}: {probe.stderr.strip()}"
    script = shim_dir / "grep"
    # newline="" throughout: a CRLF shebang is not a shebang on Git Bash,
    # and the shim then silently never fires (the class
    # tests/spawn_counting.py documents at length).
    _write_no_crlf(
        script,
        "#!/bin/bash\n"
        'case "$*" in\n'
        '    *tool_use*)\n'
        f'        printf gated > "{started.as_posix()}"\n'
        f'        _waited=0\n'
        f'        while [ ! -f "{release.as_posix()}" ]; do\n'
        "            sleep 0.2\n"
        "            _waited=$((_waited + 1))\n"
        f"            [ \"$_waited\" -gt {GATE_TIMEOUT * 5} ] && break\n"
        "        done\n"
        "        ;;\n"
        "esac\n"
        f'exec "{real}" "$@"\n',
    )
    script.chmod(0o755)
    return shim_dir


def _gap_store(tmp_path: Path):
    """A store whose newest-but-one transcript is an UNSAVED tool session.

    That is the exact signature the check fires on: a previous session that
    ran tools and was never captured.
    """
    home, project, remember = _store(tmp_path)
    session_dir = home / ".claude" / "projects" / _slug(str(project))

    gap = session_dir / f"{GAP_SESSION}.jsonl"
    _write_no_crlf(
        gap,
        '{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}\n' * 50,
    )
    # This session's own transcript has to be the newest, or the check reads
    # the gap session as the current one and says nothing.
    current = session_dir / f"{SESSION}.jsonl"
    _write_no_crlf(current, '{"type":"user","message":{"content":"x"}}\n')
    # Three-way ordering, and the middle one is the point: _store() ships its
    # own already-saved previous transcript, written AFTER this one, so
    # without backdating it `previous_transcript` returns that file instead
    # and the check correctly says nothing. Caught by running this fixture
    # against the UNCHANGED hook and watching the notice fail to appear --
    # a positive control that never fired would have made the deferral test
    # below pass for the wrong reason.
    now = time.time()
    os.utime(session_dir / f"{PREV_SESSION}.jsonl", (now - 120, now - 120))
    os.utime(gap, (now - 60, now - 60))
    os.utime(current, (now, now))

    # last-save.json records some OTHER session, so the gap session reads as
    # unsaved without last-save.json itself being absent (which would take a
    # different branch).
    _write_no_crlf(
        remember / "tmp" / "last-save.json",
        json.dumps({"sessions": {"ffffffff-0000-4000-8000-0000000000ff": 12}}),
    )
    return home, project, remember


def test_capture_gap_check_does_not_block_session_start(tmp_path):
    """The hook exits while the capture-gap grep is still running."""
    home, project, remember = _gap_store(tmp_path)
    started = tmp_path / "gate-started"
    release = tmp_path / "gate-release"
    shim = _gate_shim(tmp_path, started, release)

    env = _env(home, project, remember, os.environ["PATH"])
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"

    try:
        done = subprocess.run(
            [BASH, SESSION_START.as_posix()],
            input=_payload().encode(),
            capture_output=True,
            env=env,
            timeout=HOOK_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - the failure mode
        release.write_text("go")
        pytest.fail(
            f"session-start-hook.sh did not exit within {HOOK_TIMEOUT}s while "
            "the capture-gap grep was held open -- the check is still on the "
            "foreground path"
        )

    release.write_text("go")
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    # The context is the hook's actual obligation, and it still has to be
    # there: a hook that exits fast because it stopped injecting memory
    # passes the timing assertion and fails the user.
    assert b"=== REMEMBER ===" in done.stdout


def test_capture_gap_notice_still_written_after_release(tmp_path):
    """Deferred, not dropped: the notice appears once the gate opens."""
    home, project, remember = _gap_store(tmp_path)
    started = tmp_path / "gate-started"
    release = tmp_path / "gate-release"
    shim = _gate_shim(tmp_path, started, release)

    env = _env(home, project, remember, os.environ["PATH"])
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"

    notice = remember / "tmp" / "capture-gap-notice"
    try:
        done = subprocess.run(
            [BASH, SESSION_START.as_posix()],
            input=_payload().encode(),
            capture_output=True,
            env=env,
            timeout=HOOK_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - sibling covers it
        release.write_text("go")
        pytest.skip("hook blocked; the sibling test is the one that reports it")

    assert done.returncode == 0
    release.write_text("go")

    deadline = time.time() + NOTICE_TIMEOUT
    while time.time() < deadline:
        if notice.is_file() and notice.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.2)
    else:  # pragma: no cover - the regression this pairs against
        pytest.fail(
            "capture-gap notice was never written after the gate opened -- "
            "the check was deferred into nothing"
        )


def test_deferred_records_still_land_on_the_default_path(tmp_path):
    """The records are deferred, not dropped -- with nothing switched off.

    The slug record, the slug index and the case-divergence notice moved into
    the deferred phase. Every existing test that asserts one of them now runs
    that phase inline (REMEMBER_DEFER=0) for determinism, which leaves a gap:
    nothing would notice if the DEFAULT path stopped writing them at all. A
    plugin whose records silently never appear looks exactly like a fast one.
    """
    home, project, remember = _store(tmp_path)
    env = _env(home, project, remember, os.environ["PATH"])
    assert "REMEMBER_DEFER" not in env, "this test is about the default path"

    done = subprocess.run(
        [BASH, SESSION_START.as_posix()],
        input=_payload().encode(),
        capture_output=True,
        env=env,
        timeout=HOOK_TIMEOUT,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")

    # Both records this fixture's storage mode can produce, not just the
    # first (#695 round-1 audit). The docstring above names three writers;
    # polling only `session-slug` -- the FIRST of three consecutive calls at
    # session-start-hook.sh:874-876 -- left the other two uncovered on the
    # default path, while every test that asserts them runs with
    # REMEMBER_DEFER=0 and cannot see the default path at all. The live risk
    # is not a missing call but a slow one: _remember_write_slug_index blocks
    # on `lock_acquire`, and anything behind it in the deferred phase waits.
    #
    # `_remember_write_slug_index` is NOT polled here, and that is a
    # precondition rather than an omission: it returns early unless
    # REMEMBER_STORE_ROOT differs from REMEMBER_DIR (external storage), which
    # this fixture does not set up. It is covered by tests/test_slug_index_297.py,
    # which does -- with REMEMBER_DEFER=0, so the default path for THAT record
    # remains uncovered and is named here rather than left implied.
    expected = {
        "session-slug": remember / "tmp" / "session-slug",
        "case-divergence": remember / "tmp" / "case-divergence",
    }
    missing = dict(expected)
    deadline = time.time() + NOTICE_TIMEOUT
    while time.time() < deadline and missing:
        for name, path in list(missing.items()):
            if path.is_file() and path.read_text(encoding="utf-8").strip():
                del missing[name]
        if missing:
            time.sleep(0.1)
    assert not missing, (
        f"{sorted(missing)} never appeared on the default (deferred) path "
        f"within {NOTICE_TIMEOUT}s -- the deferred phase is not running, or "
        f"is dying part-way through its record writes"
    )


def test_dispatch_capture_files_are_namespaced_per_event():
    """Two dispatches running at once must not share a capture file (#660).

    `$$` is the SHELL's pid and is unchanged inside a subshell, so deferring
    the before_session_start dispatch put it on the same
    `tmp/dispatch-stdout.$$` as the foreground after_session_start dispatch.
    The background one's end-of-loop `rm -f` then deleted the file the
    foreground one was writing, and a plugin's injected context disappeared
    with no error on any channel -- the exact silent-absence class this repo
    keeps filing on.

    Pinned as a source shape rather than a race, because a race that
    reproduces reliably enough to assert on is a race you have already lost:
    the end-to-end proof is
    test_marketplace_hooks_d_dispatches_from_plugin, which failed against the
    unfixed version.
    """
    source = (Path(__file__).resolve().parent.parent
              / "scripts" / "log.sh").read_text(encoding="utf-8")
    for name in ("dispatch-stderr", "dispatch-stdout"):
        line = next(ln for ln in source.splitlines()
                    if f'/{name}.' in ln and "_file=" in ln)
        assert "$event" in line, (
            f"{name} capture file is not namespaced by event: {line.strip()}"
        )
