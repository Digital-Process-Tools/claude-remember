"""#913: the transcript line count becomes incremental, not O(session length).

Each test here pins one edge case the issue's own suggested fix named:
truncation, rotation ("its head changed"), and a half-written last line --
plus the one thing that must NOT have changed: which saves fire, and at
what threshold, has to stay identical to the whole-file `wc -l` this
replaces. `tests/spawn_counting.py` carries `tail` in its COUNTED list as
of this change -- previously absent, so a `tail`-based fix would have
passed every existing fork-count assertion while the harness itself stayed
blind to the very fork it claims to measure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pipeline.slug import session_dir_slug as _slug
from tests.env_cache import write_config
from tests.spawn_counting import make_shim_dir
from tests.spawn_counting import spawns as _spawn_lines

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"

TRANSCRIPT_LINE = chr(123) + chr(34) + "type" + chr(34) + ":" + chr(34) + "assistant" + chr(34) + chr(44) + chr(34) + "message" + chr(34) + ":" + chr(123) + chr(34) + "content" + chr(34) + ":" + chr(34) + "x" + chr(34) + chr(125) + chr(125) + chr(10)


def _project(tmp_path, *, jsonl_lines=10, config=None, cooldown_ts=None,
             session_id="sess-1"):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    transcript = session_dir / (session_id + ".jsonl")
    transcript.write_text(TRANSCRIPT_LINE * jsonl_lines, encoding="utf-8")
    cfg = {"thresholds": {"delta_lines_trigger": 50}} if config is None else config
    write_config(remember / "config.json", cfg)
    if cooldown_ts is not None:
        (remember / "tmp" / "last-save-ts").write_text(str(cooldown_ts), encoding="utf-8")
    return home, project, remember, transcript


def _env(tmp_path, home, project, extra=None):
    tmpdir = tmp_path / "systmp"
    tmpdir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "TMPDIR": str(tmpdir),
    }
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED", "REMEMBER_TZ",
                  "REMEMBER_NESTED_SUMMARIZER"):
        env.pop(stale, None)
    if extra:
        env.update(extra)
    return env


def _run(env, *, count_into=None, shims=None):
    if count_into is not None:
        count_into.write_text("", encoding="utf-8")
        env = {**env, "SPAWN_LOG": str(count_into),
               "PATH": str(shims) + os.pathsep + env["PATH"]}
    return subprocess.run(["bash", str(HOOK)], capture_output=True, env=env,
                           timeout=120, input=b"", check=False)


def _cmds(lines):
    return [line.split(" ", 1)[0] for line in lines]


def _sidecar(remember, session_id="sess-1"):
    return remember / "tmp" / ("transcript-scan." + session_id)


def _reap(remember):
    pid_file = remember / "tmp" / "save-session.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


# -- The common case: only the appended bytes are read -----------------------

def test_a_warm_call_reads_only_the_appended_bytes(tmp_path):
    """POSITIVE CONTROL first: the cold run must still spawn something that
    reads the transcript (`tail`, from offset 0, same as a fresh file), or
    the absence of `wc` below proves nothing. Then the real assertion: a
    warm call, after more lines were appended, must fork `tail` and must
    NOT fork `wc` at all -- the whole-file scan #913 complains about."""
    home, project, remember, transcript = _project(tmp_path, jsonl_lines=10)
    env = _env(tmp_path, home, project)
    log = tmp_path / "spawns.log"
    shims = make_shim_dir(tmp_path)

    cold = _run(env, count_into=log, shims=shims)
    assert cold.returncode == 0, cold.stderr[:400]
    cold_lines = _spawn_lines(log)
    assert "tail" in _cmds(cold_lines), (
        "the cold run never read the transcript at all: " + repr(cold_lines)
    )

    with transcript.open("a", encoding="utf-8") as f:
        f.write(TRANSCRIPT_LINE * 5)

    warm = _run(env, count_into=log, shims=shims)
    assert warm.returncode == 0, warm.stderr[:400]
    warm_lines = _spawn_lines(log)
    _reap(remember)

    assert "tail" in _cmds(warm_lines), (
        "the warm run did not re-scan the transcript at all: " + repr(warm_lines)
    )
    assert "wc" not in _cmds(warm_lines), (
        "the warm run still forked `wc` -- the whole-file scan #913 exists "
        "to remove: " + repr(warm_lines)
    )

    sidecar = _sidecar(remember)
    assert sidecar.exists(), "no transcript-scan sidecar was written"
    offset, lines = sidecar.read_text(encoding="utf-8").split()
    assert int(lines) == 15, (
        f"expected 15 lines counted (10 + 5 appended), sidecar says {lines}"
    )
    assert int(offset) == len(transcript.read_bytes()), (
        "the sidecar's OFFSET does not point at the end of the file it just "
        f"scanned: offset={offset}, actual size={len(transcript.read_bytes())}"
    )


def test_the_save_decision_is_unchanged_by_the_incremental_rewrite(tmp_path):
    """The one thing that must survive byte-for-byte: WHICH saves fire, and
    at what threshold. Three warm calls, lines appended between each, a
    threshold of 12 -- the fork must land on exactly the call that pushes
    the cumulative delta past it, never one call early or late."""
    home, project, remember, transcript = _project(
        tmp_path, jsonl_lines=10,
        config={"thresholds": {"delta_lines_trigger": 12}},
    )
    env = _env(tmp_path, home, project)

    first = _run(env)
    assert first.returncode == 0, first.stderr[:400]
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "a 10-line transcript against a threshold of 12 fired a save on the "
        "very first call"
    )

    with transcript.open("a", encoding="utf-8") as f:
        f.write(TRANSCRIPT_LINE)  # 11 lines total -- still under 12

    second = _run(env)
    assert second.returncode == 0, second.stderr[:400]
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "11 lines against a threshold of 12 fired a save one call early -- "
        "the incremental count has drifted ahead of the real total"
    )

    with transcript.open("a", encoding="utf-8") as f:
        f.write(TRANSCRIPT_LINE * 2)  # 13 lines total -- now past 12

    third = _run(env)
    assert third.returncode == 0, third.stderr[:400]
    assert (remember / "tmp" / "save-session.pid").exists(), (
        "13 lines against a threshold of 12 never fired -- the incremental "
        "count has drifted behind the real total"
    )
    _reap(remember)


# -- #913's named fallbacks ---------------------------------------------------

def test_a_half_written_last_line_is_not_counted_until_complete(tmp_path):
    home, project, remember, transcript = _project(
        tmp_path, jsonl_lines=10,
        config={"thresholds": {"delta_lines_trigger": 10}},
    )
    env = _env(tmp_path, home, project)
    first = _run(env)
    assert first.returncode == 0, first.stderr[:400]
    sidecar = _sidecar(remember)
    offset_before, lines_before = sidecar.read_text(encoding="utf-8").split()
    assert lines_before == "10"

    partial = chr(123) + chr(34) + "type" + chr(34) + ":" + chr(34) + "x"
    with transcript.open("a", encoding="utf-8") as f:
        f.write(partial)

    second = _run(env)
    assert second.returncode == 0, second.stderr[:400]
    offset_mid, lines_mid = sidecar.read_text(encoding="utf-8").split()
    assert lines_mid == "10"
    assert offset_mid == offset_before
    assert not (remember / "tmp" / "save-session.pid").exists()

    closer = chr(34) + chr(125) + chr(10)
    with transcript.open("a", encoding="utf-8") as f:
        f.write(closer)

    third = _run(env)
    assert third.returncode == 0, third.stderr[:400]
    _, lines_after = sidecar.read_text(encoding="utf-8").split()
    assert lines_after == "11"
    assert (remember / "tmp" / "save-session.pid").exists()
    _reap(remember)


def test_a_shrunk_transcript_falls_back_to_a_full_recount(tmp_path):
    home, project, remember, transcript = _project(tmp_path, jsonl_lines=20)
    env = _env(tmp_path, home, project)

    first = _run(env)
    assert first.returncode == 0, first.stderr[:400]
    sidecar = _sidecar(remember)
    offset_before, _ = sidecar.read_text(encoding="utf-8").split()
    assert int(offset_before) == len(transcript.read_bytes())

    transcript.write_text(TRANSCRIPT_LINE * 3, encoding="utf-8")

    second = _run(env)
    assert second.returncode == 0, second.stderr[:400]
    offset_after, lines_after = sidecar.read_text(encoding="utf-8").split()
    assert lines_after == "3"
    assert int(offset_after) == len(transcript.read_bytes())


def test_a_rewritten_transcript_falls_back_rather_than_trusting_a_stale_offset(tmp_path):
    home, project, remember, transcript = _project(tmp_path, jsonl_lines=20)
    env = _env(tmp_path, home, project)

    first = _run(env)
    assert first.returncode == 0, first.stderr[:400]

    pad = "y" * 400
    rewritten = chr(123) + chr(34) + "type" + chr(34) + ":" + chr(34) + pad + chr(34) + chr(125) + chr(10)
    transcript.write_text(rewritten, encoding="utf-8")

    second = _run(env)
    assert second.returncode == 0, second.stderr[:400]
    sidecar = _sidecar(remember)
    offset_after, lines_after = sidecar.read_text(encoding="utf-8").split()
    assert lines_after == "1"
    assert int(offset_after) == len(transcript.read_bytes())


def test_a_partial_sidecar_write_is_rejected_as_a_pair_not_per_field(tmp_path):
    """A write interrupted between the two fields can leave a syntactically
    valid OFFSET (a real prior scan position) next to a missing LINES field.
    Each field alone passes the digits-only check -- the pair must be
    rejected together, or OFFSET's own newline-validation in _pt_scan_lines
    passes too, silently pairing a real position with LINES=0."""
    home, project, remember, transcript = _project(tmp_path, jsonl_lines=20)
    env = _env(tmp_path, home, project)

    first = _run(env)
    assert first.returncode == 0, first.stderr[:400]
    sidecar = _sidecar(remember)
    real_offset, real_lines = sidecar.read_text(encoding="utf-8").split()
    assert real_lines == "20"

    sidecar.write_text(real_offset + " ", encoding="utf-8")

    second = _run(env)
    assert second.returncode == 0, second.stderr[:400]
    offset_after, lines_after = sidecar.read_text(encoding="utf-8").split()
    assert lines_after == "20", (
        f"a partial sidecar write (valid OFFSET, missing LINES) undercounted "
        f"to {lines_after} lines instead of recounting the real 20"
    )
    assert int(offset_after) == len(transcript.read_bytes())


# -- The other named cost: no scan at all when a save cannot fire -----------

def test_cooldown_active_skips_the_scan_entirely(tmp_path):
    home, project, remember, _transcript = _project(
        tmp_path, jsonl_lines=10, cooldown_ts=int(time.time()),
    )
    env = _env(tmp_path, home, project)
    log = tmp_path / "spawns.log"
    shims = make_shim_dir(tmp_path)

    result = _run(env, count_into=log, shims=shims)
    assert result.returncode == 0, result.stderr[:400]
    cmds = _cmds(_spawn_lines(log))

    assert "ls" in cmds, "the hook never looked for a transcript"
    assert "tail" not in cmds, "the cooldown-active run still scanned the transcript"
    assert "wc" not in cmds, "the cooldown-active run still forked wc"
    assert not (remember / "tmp" / "transcript-scan.sess-1").exists()

