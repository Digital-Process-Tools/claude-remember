"""Nested summarizer spawns must be bounded by something the host cannot strip (#204).

Two defences kept the nested `claude -p` from re-entering the plugin, and both
depend on a signal that has to *arrive*:

* `--setting-sources ''` (#202) registers none of the user's hooks in the child —
  defeated by a CLI that rejects the flag, because `call_haiku` then fails open
  and retries without it, with the user's hooks live.
* `REMEMBER_NESTED_SUMMARIZER` (#205) makes the plugin's own hooks no-op in the
  child — defeated by any host that redacts env into spawned hook subprocesses,
  the way one was observed to redact `CLAUDE_CODE_OAUTH_TOKEN` in #131.

@ehutchinsonSFDC reported the shape that follows when both fail on 0.9.0: ~19
live `claude` processes, +7 new summarizer transcripts during an 8-second idle
with no tool calls, and summarizer prompts whose transcript embedded a previous
summarizer prompt. Nothing in the pipeline bounded it — the save lock serializes
saves within one store but never limits how many run, and `session-start-hook.sh`
recovers with `--force`, which bypasses the cooldown that would otherwise be the
only brake.

So the bound tested here must hold with **every** one of those signals absent.
It is enforced in the spawner (`call_haiku`), recorded on the filesystem under
the user's home, and the child in the recursion test below is re-entered with a
deliberately scrubbed environment — no `REMEMBER_NESTED_SUMMARIZER`, no
`CLAUDE*` — because that is precisely the reported condition.

A cap that fires must SAY so: `SummarizerSpawnDeclined` is a third state next to
`ok` and a finding, never a silent second-best, and `pipeline.shell call-haiku`
reports it with its own exit code so `save-session.sh` does not book it as a
summarization failure and drop the span.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.haiku import call_haiku  # noqa: E402

# Spelled out rather than imported from the module under test: these two numbers
# are the contract a user reads in the CHANGELOG, and a test that imports them
# would pass against any value the code happened to hold.
EXPECTED_MAX_CONCURRENT = 4
EXPECTED_MAX_PER_MINUTE = 12

# The recursion harness stops itself here so a missing bound fails the test
# instead of the machine. Any count at or near this means nothing bounded it.
HARNESS_CEILING = 25

_JSON_REPLY = (
    '{"result":"## 10:00 | main\\n\\n- did some work",'
    '"usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0}}'
)


def _fake_claude(tmp_path: Path, ledger: Path) -> Path:
    """A stand-in `claude` that answers like the real one and records the call."""
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/bash\n"
        "cat > /dev/null\n"
        f'echo spawn >> "{ledger}"\n'
        f"printf '%s' '{_JSON_REPLY}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _recursive_fake_claude(tmp_path: Path, ledger: Path) -> Path:
    """A stand-in `claude` that re-enters `call_haiku` the way the report does.

    This is the whole point of the test: the child is launched with `env -i`,
    keeping only PATH and HOME (and the override that locates this script). The
    marker `_child_env()` sets, and every CLAUDE* variable, are gone — so any
    containment that depends on them is absent by construction, and only a bound
    the spawner enforces for itself can hold.
    """
    snippet = (
        "import sys;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        "from pipeline.haiku import call_haiku\n"
        "try:\n"
        "    call_haiku('summarize this', timeout=20)\n"
        "except Exception as exc:\n"
        f"    open({str(ledger)!r}, 'a').write('refused %s\\n' % exc)\n"
    )
    snippet_file = tmp_path / "reenter.py"
    snippet_file.write_text(snippet, encoding="utf-8")

    script = tmp_path / "recursive-fake-claude"
    script.write_text(
        "#!/bin/bash\n"
        "cat > /dev/null\n"
        f'echo spawn >> "{ledger}"\n'
        f'if [ "$(grep -c \'^spawn$\' "{ledger}")" -lt {HARNESS_CEILING} ]; then\n'
        '  env -i PATH="$PATH" HOME="$HOME" REMEMBER_CLAUDE_BIN="$0" '
        f'{sys.executable} "{snippet_file}" >/dev/null 2>&1\n'
        "fi\n"
        f"printf '%s' '{_JSON_REPLY}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _spawns(ledger: Path) -> int:
    if not ledger.is_file():
        return 0
    return sum(1 for line in ledger.read_text(encoding="utf-8").splitlines()
               if line == "spawn")


def _refusals(ledger: Path) -> list[str]:
    if not ledger.is_file():
        return []
    return [line for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.startswith("refused ")]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash harness — not portable to Windows runners (#79)")
def test_nested_spawns_are_bounded_with_every_env_signal_absent(tmp_path, monkeypatch):
    """The reported failure, run for real: a summarizer that spawns a summarizer.

    No `REMEMBER_NESTED_SUMMARIZER` reaches the child, no `--setting-sources`
    support is assumed, and the runtime state is found through HOME alone. The
    recursion must stop on its own, well below the harness ceiling.
    """
    ledger = tmp_path / "ledger.txt"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("REMEMBER_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("REMEMBER_CLAUDE_BIN",
                       str(_recursive_fake_claude(tmp_path, ledger)))

    call_haiku("summarize this", timeout=30)

    spawned = _spawns(ledger)
    assert spawned < HARNESS_CEILING, (
        f"the recursion never stopped: {spawned} nested summarizers spawned "
        f"before the harness cut it off at {HARNESS_CEILING}"
    )
    assert spawned <= EXPECTED_MAX_CONCURRENT, (
        f"{spawned} concurrent summarizers, bound is {EXPECTED_MAX_CONCURRENT}"
    )
    assert _refusals(ledger), (
        "the bound must be reported, not applied in silence — nothing raised "
        "when the recursion was cut off"
    )


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash harness — not portable to Windows runners (#79)")
def test_serialized_spawns_are_rate_bounded(tmp_path, monkeypatch):
    """The other reported shape: not nested, just relentless.

    `session-start-hook.sh` recovers with `--force`, which bypasses the save
    cooldown, and the save lock only serializes — so a chain where each save
    spawns a summarizer whose session starts the next save is unbounded without
    ever running two at once. Concurrency cannot see that; a rate window can.
    """
    ledger = tmp_path / "ledger.txt"
    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("REMEMBER_CLAUDE_BIN", str(_fake_claude(tmp_path, ledger)))

    declined = 0
    for _ in range(EXPECTED_MAX_PER_MINUTE + 6):
        try:
            call_haiku("summarize this", timeout=20)
        except RuntimeError as exc:
            declined += 1
            assert "per minute" in str(exc).lower(), str(exc)

    assert _spawns(ledger) == EXPECTED_MAX_PER_MINUTE, (
        f"{_spawns(ledger)} spawns in one window, bound is "
        f"{EXPECTED_MAX_PER_MINUTE}"
    )
    assert declined == 6


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash harness — not portable to Windows runners (#79)")
def test_a_crashed_summarizer_does_not_wedge_saves_forever(tmp_path, monkeypatch):
    """Crash recovery. A killed summarizer leaves its record behind — @ehutchinsonSFDC
    killed 19 of them at once — and if that record counted forever the fix would
    be a permanent save outage, which is the #204 trap this repo already names.
    """
    ledger = tmp_path / "ledger.txt"
    runtime = tmp_path / "run"
    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("REMEMBER_CLAUDE_BIN", str(_fake_claude(tmp_path, ledger)))

    from pipeline import spawn_guard

    # Records from processes that no longer exist: dead pids, at the cap.
    for i, pid in enumerate(_dead_pids(EXPECTED_MAX_CONCURRENT + 2)):
        _write_record(spawn_guard.record_dir(), f"{i}-{pid}", pid)

    call_haiku("summarize this", timeout=20)

    assert _spawns(ledger) == 1, "a dead holder's record blocked a legitimate save"


def _write_record(directory: Path, name: str, pid: int) -> None:
    """A spawn record as `claim()` writes one: pid plus a start time inside the
    rate window, so only pid liveness or staleness can retire it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.spawn").write_text(
        f"pid={pid}\nstarted={time.time():.6f}\n", encoding="utf-8"
    )


def _dead_pids(count: int) -> list[int]:
    """Pids of processes that have certainly exited."""
    pids = []
    for _ in range(count):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        pids.append(proc.pid)
    return pids


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash harness — not portable to Windows runners (#79)")
def test_a_declined_spawn_has_its_own_exit_code_and_says_why(tmp_path, monkeypatch):
    """The shell bridge. A decline is not a summarization failure: booking it as
    one advances the read cursor past an unsummarized span after
    `thresholds.max_summary_failures` runs, which loses memory to a bound that
    was working correctly. It gets its own exit code so bash can tell them apart.
    """
    runtime = tmp_path / "run"
    ledger = tmp_path / "ledger.txt"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("summarize this", encoding="utf-8")

    from pipeline import spawn_guard

    env = dict(os.environ)
    env["REMEMBER_RUNTIME_DIR"] = str(runtime)
    env["REMEMBER_CLAUDE_BIN"] = str(_fake_claude(tmp_path, ledger))
    env.pop("REMEMBER_DIR", None)

    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(runtime))
    for i in range(EXPECTED_MAX_CONCURRENT):
        _write_record(spawn_guard.record_dir(), f"{i}-{os.getpid()}", os.getpid())

    result = subprocess.run(
        [sys.executable, "-m", "pipeline.shell", "call-haiku", str(prompt)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=60,
    )

    assert result.returncode == spawn_guard.EXIT_SPAWN_DECLINED, (
        f"exit {result.returncode}, stderr: {result.stderr}"
    )
    assert result.returncode not in (0, 1), "declined must not read as ok or as failed"
    assert _spawns(ledger) == 0, "declined, yet it spawned anyway"
    assert re.search(r"declin", result.stderr, re.I), result.stderr
    assert "HAIKU_TEXT_FILE" not in result.stdout, (
        "a declined call must not emit a result contract"
    )


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash harness — not portable to Windows runners (#79)")
def test_save_session_does_not_book_a_decline_as_a_summary_failure(tmp_path):
    """The consequence of getting the exit code wrong, pinned end to end.

    `record_summary_failure` exists so a span that fails three times stops
    wedging memory forever (#147) — it advances the read cursor past it. Feed it
    a decline and the guard destroys spans on a busy machine. So: exit 0, no
    failure marker, position untouched, and the word DECLINED in the log.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_save_session_gates import _make_env, _run, _saved_position

    env, project, plugin, _calls, session_id = _make_env(
        tmp_path, exchanges=40, humans=5
    )
    env["STUB_HAIKU_DECLINE"] = "1"

    result = _run(plugin, env, session_id)

    assert result.returncode == 0, result.stderr
    assert not (project / ".remember" / "tmp" / "last-summary-failure").exists(), (
        "a declined spawn was counted against the span"
    )
    assert _saved_position(project) is None, (
        "the position advanced past a span that was never summarized"
    )
    logs = sorted((project / ".remember" / "logs").glob("memory-*.log"))
    assert logs, "no daily log was written"
    assert "DECLINED" in logs[-1].read_text(encoding="utf-8")


def test_the_child_env_does_not_carry_the_parents_project_dir(monkeypatch):
    """`CLAUDE_PROJECT_DIR` is not in the `CLAUDE_CODE_*` family `_child_env()`
    strips, so it was inherited by the nested session — while both the #204
    report and `resolve-paths.sh`'s own comments describe the child as having
    none. Claude Code 2.1.219 overwrites it from the sandbox cwd and the leak is
    inert there; a CLI that honoured the inherited value would instead point the
    summarizer at the real project, which is the failure @ehutchinsonSFDC saw.
    """
    from pipeline.haiku import _child_env

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/Users/dev/real-project")
    assert "CLAUDE_PROJECT_DIR" not in _child_env()


def test_a_released_slot_stops_counting_and_releasing_twice_is_harmless(monkeypatch, tmp_path):
    """The record has to be retired on the way out, or the first four saves of a
    session would use the concurrency budget up permanently.
    """
    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "run"))
    from pipeline import spawn_guard

    slot = spawn_guard.claim(timeout=30)
    live, _ = spawn_guard._census(spawn_guard.record_dir(), time.time(), 90)
    assert live == 1

    slot.release()
    live, recent = spawn_guard._census(spawn_guard.record_dir(), time.time(), 90)
    assert live == 0
    assert recent == 1, "a completed spawn still counts toward the rate window"

    slot.release()  # idempotent — the EXIT path can reach it twice


def test_records_outside_the_window_are_pruned(monkeypatch, tmp_path):
    """Otherwise the directory grows forever, which is the unbounded-log half of
    what #204 originally reported, moved to a new file.
    """
    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "run"))
    from pipeline import spawn_guard

    records = spawn_guard.record_dir()
    records.mkdir(parents=True)
    stale = records / "old.spawn"
    stale.write_text(
        f"pid={os.getpid()}\nstarted={time.time() - 3600:.6f}\ndone=1\n",
        encoding="utf-8",
    )

    spawn_guard._census(records, time.time(), 90)

    assert not stale.exists()


def test_an_unreadable_record_is_ignored_rather_than_fatal(monkeypatch, tmp_path):
    """A truncated or garbled record must not take the save path down with it —
    this file is diagnostic state, not memory.
    """
    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "run"))
    from pipeline import spawn_guard

    records = spawn_guard.record_dir()
    records.mkdir(parents=True)
    (records / "garbage.spawn").write_text("not\x00even=fields\n", encoding="utf-8")
    (records / "half.spawn").write_text("pid=abc\nstarted=soon\n", encoding="utf-8")

    live, recent = spawn_guard._census(records, time.time(), 90)

    assert (live, recent) == (0, 0)


@pytest.mark.parametrize("value", ["0", "-3", "abc", "", "  ", "1.5"])
def test_a_nonsense_cap_falls_back_to_the_default(monkeypatch, value):
    """Same shape as `_resolve_max_turns`: a typo in a knob must not disable the
    bound (0 would refuse every spawn; a non-number would crash the save).
    """
    from pipeline import spawn_guard

    monkeypatch.setenv(spawn_guard.MAX_CONCURRENT_ENV, value)
    assert spawn_guard._positive_int(
        spawn_guard.MAX_CONCURRENT_ENV, spawn_guard.DEFAULT_MAX_CONCURRENT
    ) == spawn_guard.DEFAULT_MAX_CONCURRENT


def _nt_liveness(monkeypatch) -> None:
    """Make `_pid_alive` behave as it does on Windows, wherever this runs.

    Through the module's own flag, never `os.name`: patching that mutates the
    real `os` module, `pathlib` reads it, and every `Path` in the process becomes
    a `WindowsPath` that cannot be instantiated on a POSIX host — which is how
    this helper first announced itself.
    """
    from pipeline import spawn_guard

    monkeypatch.setattr(spawn_guard, "_PIDS_ARE_PROBEABLE", False)


def test_liveness_answers_the_two_questions_that_are_the_same_everywhere():
    """A pid we are running as exists; pid 0 is not a process anywhere.

    The interesting case — a pid that has *exited* — has two different correct
    answers, one per platform, and gets a test each below.
    """
    from pipeline import spawn_guard

    assert spawn_guard._pid_alive(os.getpid()) is True
    assert spawn_guard._pid_alive(0) is False


@pytest.mark.skipif(sys.platform == "win32",
                    reason="`kill -0` is the POSIX probe; Windows has no cheap "
                           "equivalent and retires records by staleness instead "
                           "— covered by the two tests below")
def test_on_posix_a_dead_holder_is_retired_the_instant_it_dies():
    """`kill -0` answers cheaply and exactly, so a crashed summarizer frees its
    slot immediately and no deferral is ever observed here.
    """
    from pipeline import spawn_guard

    assert spawn_guard._pid_alive(_dead_pids(1)[0]) is False


def test_where_liveness_cannot_be_asked_the_answer_is_yes(monkeypatch):
    """Windows: `os.kill` there only sends real signals, so there is no probe to
    make. Saying "alive" defers a save for at most `timeout + grace` after a
    crash; saying "dead" would hand out a slot per unprobeable record and uncap
    the spawn. Bounded wrongness beats unbounded wrongness.

    Forced rather than skipped, so a POSIX-only runner still covers the branch.
    """
    from pipeline import spawn_guard

    _nt_liveness(monkeypatch)
    assert spawn_guard._pid_alive(_dead_pids(1)[0]) is True


def test_staleness_retires_a_crashed_holder_on_the_promised_schedule(monkeypatch,
                                                                    tmp_path):
    """The Windows contract, which is the one liveness does NOT cover.

    On the platform where a dead holder reads as alive, staleness is the only
    thing that ever frees its slot — so this is the path that decides whether the
    guard defers saves for a bounded `timeout + grace` or wedges forever. The two
    halves bracket that boundary, which is what a regression in
    `STALE_GRACE_SECONDS` or in the census arithmetic would move.
    """
    from pipeline import spawn_guard

    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "run"))
    _nt_liveness(monkeypatch)

    records = spawn_guard.record_dir()
    records.mkdir(parents=True)
    dead = _dead_pids(1)[0]
    timeout = 10.0
    stale_after = timeout + spawn_guard.STALE_GRACE_SECONDS
    now = time.time()
    record = records / "crashed.spawn"

    record.write_text(f"pid={dead}\nstarted={now - (stale_after - 5):.6f}\n",
                      encoding="utf-8")
    live, _ = spawn_guard._census(records, now, stale_after)
    assert live == 1, (
        "inside the grace a crashed holder still consumes its slot — deliberate, "
        "and the reason the deferral is bounded rather than absent"
    )
    assert record.exists(), "retired before its own grace had elapsed"

    record.write_text(f"pid={dead}\nstarted={now - (stale_after + 1):.6f}\n",
                      encoding="utf-8")
    live, recent = spawn_guard._census(records, now, stale_after)
    assert (live, recent) == (0, 0), "staleness never retired the record"
    assert not record.exists(), "the record was left behind to accumulate"


def test_a_batch_of_crashed_holders_defers_saves_and_then_stops(monkeypatch,
                                                               tmp_path):
    """Four crashes in quick succession, end to end through `claim()`.

    @ehutchinsonSFDC killed 19 summarizers at once, so a batch is the realistic
    shape here. On Windows semantics every one of those records reads as live:
    saves decline until the grace elapses, then resume on their own. Pinned
    rather than reasoned about — if the arithmetic is wrong the guard declines
    every save forever, which is the #204 trap in the fix's own clothes.
    """
    from pipeline import spawn_guard

    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "run"))
    _nt_liveness(monkeypatch)

    records = spawn_guard.record_dir()
    records.mkdir(parents=True)
    timeout = 10.0
    stale_after = timeout + spawn_guard.STALE_GRACE_SECONDS
    now = time.time()
    dead = _dead_pids(EXPECTED_MAX_CONCURRENT)

    def date_records(age: float) -> None:
        for i, pid in enumerate(dead):
            (records / f"{i}.spawn").write_text(
                f"pid={pid}\nstarted={now - age:.6f}\n", encoding="utf-8"
            )

    date_records(stale_after - 5)
    with pytest.raises(spawn_guard.SummarizerSpawnDeclined) as declined:
        spawn_guard.claim(timeout=timeout)
    assert "already running" in str(declined.value)
    assert str(EXPECTED_MAX_CONCURRENT) in str(declined.value)

    date_records(stale_after + 1)
    slot = spawn_guard.claim(timeout=timeout)
    try:
        assert slot.degraded == "", (
            "the guard degraded instead of recovering from the crash"
        )
        live, _ = spawn_guard._census(records, time.time(), stale_after)
        assert live == 1, "only the new claim should be live once the grace passed"
    finally:
        slot.release()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash harness — not portable to Windows runners (#79)")
def test_an_unusable_runtime_dir_says_unbounded_and_still_saves(tmp_path, monkeypatch):
    """The guard fails OPEN, loudly. A bound that turns an unwritable directory
    into "no saves ever again" is the #204 trap wearing the fix's clothes — so
    the spawn proceeds, and the daily log records that nothing is counting it.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    remember_dir = tmp_path / "store"
    (remember_dir / "logs").mkdir(parents=True)
    ledger = tmp_path / "ledger.txt"

    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(blocker / "run"))
    monkeypatch.setenv("REMEMBER_DIR", str(remember_dir))
    monkeypatch.setenv("REMEMBER_CLAUDE_BIN", str(_fake_claude(tmp_path, ledger)))

    call_haiku("summarize this", timeout=20)

    assert _spawns(ledger) == 1, "the spawn was suppressed instead of failing open"
    logged = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (remember_dir / "logs").glob("memory-*.log")
    )
    assert "UNBOUNDED" in logged, logged


def test_release_survives_its_record_disappearing(monkeypatch, tmp_path):
    """`release()` runs from an exit path. It must not raise there, whatever
    happened to the file — a tmp reaper, a `rm -rf`, an unmounted home.
    """
    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "run"))
    from pipeline import spawn_guard

    slot = spawn_guard.claim(timeout=30)
    for path in spawn_guard.record_dir().glob("*.spawn"):
        path.unlink()

    slot.release()  # must not raise


def test_an_unreadable_entry_is_skipped_rather_than_fatal(monkeypatch, tmp_path):
    """Something that is not a readable file where a record is expected."""
    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "run"))
    from pipeline import spawn_guard

    records = spawn_guard.record_dir()
    records.mkdir(parents=True)
    (records / "impostor.spawn").mkdir()

    assert spawn_guard._census(records, time.time(), 90) == (0, 0)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_read_only_record_dir_degrades_rather_than_refuses(monkeypatch, tmp_path):
    """The dir exists and can be listed but not written. Same verdict as an
    unusable one: unbounded and loud, never a refused save.
    """
    monkeypatch.setenv("REMEMBER_RUNTIME_DIR", str(tmp_path / "run"))
    from pipeline import spawn_guard

    records = spawn_guard.record_dir()
    records.mkdir(parents=True)
    records.chmod(0o500)
    try:
        slot = spawn_guard.claim(timeout=30)
    finally:
        records.chmod(0o700)

    assert slot.degraded, "an unwritable record dir must be reported, not ignored"
    slot.release()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pid semantics")
def test_a_holder_owned_by_another_user_counts_as_alive():
    """EPERM from `kill -0` means the process exists and is someone else's — on a
    shared box that is still a live summarizer, not a free slot.
    """
    from pipeline import spawn_guard

    assert spawn_guard._pid_alive(1) is True
