"""`_jq_fallback` drops `--arg`, so a jq-less install reports every previous
session as unsaved and spawns `save-session.sh --force` on every startup (#667).

`session_was_saved()` (scripts/session-start-hook.sh:345-348) calls::

    $JQ -r --arg id "$1" "$SAVED_QUERY" "$LAST_SAVE_FILE"

When jq is on PATH this is ordinary jq. When it is not, `$JQ` is
`_jq_fallback` (scripts/detect-tools.sh:61), whose shim consumes every
leading `-*` token into a flags variable IT NEVER READS, then treats its
next positional argument as the query and the one after that as the file.
Fed `--arg id "$1" "$SAVED_QUERY" "$LAST_SAVE_FILE"`:

  * `--arg` is swallowed as a flag (starts with `-`);
  * `id` does NOT start with `-`, so the flag-eating loop stops there and
    `id` becomes `_jq_query`;
  * `"$1"` (the session id) becomes `_jq_file` -- not a real file, so
    `json.load` raises and the shim's `except Exception: sys.exit(0)` prints
    nothing;
  * `$SAVED_QUERY` and `$LAST_SAVE_FILE` are silently dropped.

`session_was_saved` compares the shim's stdout against the literal string
"saved" (session-start-hook.sh:347), so any of those failure shapes reads as
"unsaved" -- a session that genuinely WAS saved is judged not to have been,
and the recovery block (session-start-hook.sh:705-724) force-spawns
`save-session.sh "$PREV_ID" --force` in the background on every single
startup, jq-less or not.

Fix shape taken: `session_was_saved` gets a jq-free branch (checked via
`[ "$JQ" = "_jq_fallback" ]`) that reads `last-save.json` with ONE direct
Python call carrying the session id as argv, rather than teaching the
generic `_jq_fallback` shim to parse `--arg` pairs -- grepping every `$JQ`
and `$JQ_BIN` call site under scripts/ (session-start-hook.sh:1710,
user-prompt-hook.sh:469,502) shows every other `--arg` caller already guards
itself with `command -v jq` first, so `session_was_saved` is the only
call site that ever reaches the fallback with `--arg` -- teaching the shim a
`--arg` parser it would be the sole caller of is strictly more code for the
same coverage.

Two assertions, same fixture shape, pulling in opposite directions (a
"must not fire" test alone would also pass if the harness never ran
anything at all):

  * negative -- PREV really was saved (an integer line count in
    last-save.json, matching `session_was_saved`'s own type-checked jq
    query): no `save-session.sh` spawn, hook exits 0;
  * positive control -- PREV was NOT recorded as saved: the
    `save-session.sh ... --force` spawn IS observed. A gate that
    unconditionally skipped recovery (e.g. a stray early return) would pass
    the negative test above and fail this one.

Both run twice: once with jq masked off PATH (the bug's own reproduction --
Git for Windows ships no jq, named in the issue and in #660), and once with
jq left on PATH as a control that the existing jq-based behaviour is
unchanged by the fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

from ._bash_runner import resolve_bash
from .spawn_counting import make_shim_dir
from .subprocess_helpers import subprocess_failure_detail

# #432's own narrowing, not a platform skip: run under whatever real,
# non-WSL bash this platform has (Git Bash on Windows), rather than
# blanket-skipping the whole file on win32 -- this issue's own reproduction
# is specifically the Git-for-Windows, jq-less path, so that leg is exactly
# the one this file must not skip.
BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install "
    "locations) -- #667's jq-fallback --arg regression cannot be reproduced "
    "without one",
)

PREV_SESSION = "bbbbbbbb-0000-4000-8000-000000000667"
CURRENT_SESSION = "cccccccc-0000-4000-8000-000000000667"
PREV_SESSION_LINES = 42  # arbitrary; only its type (an integer) matters


def _path_without_jq(path_value: str) -> str:
    """PATH with every directory holding a `jq` (or `jq.exe` etc.) removed --
    a real "jq absent" PATH, mirroring
    tests/test_session_start_windows_benchmark_669.py's own helper of the
    same name (not imported from there: that file's helper is private to
    its own module, and the native-suffix reasoning is documented in
    tests/spawn_counting.py, which both copies read from)."""
    suffixes = [""] if os.name != "nt" else ["", ".exe", ".cmd", ".bat"]
    kept = []
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        has_jq = any((Path(entry) / ("jq" + suf)).is_file() for suf in suffixes)
        if not has_jq:
            kept.append(entry)
    return os.pathsep.join(kept)


def _plugin_root_with_stub_save(tmp_path: Path, record: Path) -> Path:
    """A plugin root identical to the real one except that save-session.sh
    records its argv instead of saving -- lifted from #270's own
    `_plugin_root_with_stub_save` (tests/test_session_start_prev_session_270.py).

    A PATH-shim spawn log (tests/spawn_counting.py) CANNOT see this call:
    the recovery block invokes `"$PLUGIN_ROOT/scripts/save-session.sh"` by
    absolute path (session-start-hook.sh:722), which never goes through PATH
    resolution, and `save-session.sh` is not in spawn_counting.py's own
    COUNTED list regardless (self-review of this file's first draft: a
    `spawns(log)` assertion for "save-session.sh" silently never matches,
    passing the negative case for the wrong reason -- no jq-fallback fix
    needed to make it pass, only a harness that cannot see the call at all).
    The argv record is the only place the choice is actually observable.
    """
    root = tmp_path / "plugin"
    root.mkdir()
    for entry in REPO_ROOT.iterdir():
        if entry.name == "scripts":
            continue
        (root / entry.name).symlink_to(entry)
    scripts = root / "scripts"
    scripts.mkdir()
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.name == "save-session.sh":
            continue
        (scripts / entry.name).symlink_to(entry)
    stub = scripts / "save-session.sh"
    # newline="" (not write_text's default universal-newline translation,
    # which turns every \n into \r\n on Windows): a shebang line ending in
    # \r is a broken interpreter directive under Git Bash/MSYS -- the exact
    # defect class this test's own `make_shim_dir` import already exists to
    # avoid (tests/spawn_counting.py, #669/#670), and unlike a shimmed
    # COUNTED tool this stub has no same-named real binary on PATH to
    # silently fall through to: a corrupted shebang here would fail loudly
    # with a "bad interpreter" error instead, breaking the positive-control
    # test on windows-latest for a reason unrelated to #667 (self-review
    # finding).
    with open(stub, "w", encoding="utf-8", newline="") as f:
        f.write(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$*" >> "{record}"\n'
            "exit 0\n"
        )
    stub.chmod(0o755)
    return root


def _store(tmp_path: Path, *, prev_saved: bool, last_save_body: dict | None = None):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)

    # CURRENT's own transcript does not exist yet at real startup -- Claude
    # Code creates it after the hook runs -- so PREV sorts newest and
    # `previous_transcript()` (session-start-hook.sh:670) correctly picks it
    # without needing #270's older/newer distinction here.
    prev = session_dir / f"{PREV_SESSION}.jsonl"
    prev.write_text('{"type":"assistant","message":{"content":"x"}}\n')

    if last_save_body is not None:
        body = last_save_body
    elif prev_saved:
        body = {"sessions": {PREV_SESSION: PREV_SESSION_LINES}}
    else:
        # Recorded, but for a DIFFERENT session -- a real "not saved" state,
        # not merely a missing file (which session_was_saved also treats as
        # unsaved, but that would not distinguish "the check ran and said no"
        # from "the check never looked").
        body = {"sessions": {"ffffffff-0000-4000-8000-000000000000": 1}}
    (remember / "tmp" / "last-save.json").write_text(json.dumps(body))

    return home, project, remember


def _payload() -> str:
    return json.dumps({
        "session_id": CURRENT_SESSION,
        "transcript_path": f"/does/not/matter/{CURRENT_SESSION}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })


def _run(tmp_path: Path, *, prev_saved: bool, jq_present: bool, last_save_body: dict | None = None):
    home, project, remember = _store(tmp_path, prev_saved=prev_saved, last_save_body=last_save_body)
    record = tmp_path / "save-session-argv.log"
    plugin_root = _plugin_root_with_stub_save(tmp_path, record)

    # Removing every directory that HOLDS a jq from PATH (`_path_without_jq`)
    # also removes whatever else lives in that same directory -- on this
    # machine that is /usr/bin, taking sed/tr/id/dirname down with it and
    # making the WHOLE hook fail long before it reaches recovery, which
    # would make both scenarios below pass for the wrong reason (nothing
    # ran). `make_shim_dir` rebuilds every COUNTED tool as its own shim,
    # resolved off the ambient (jq-having) PATH, then the "jq" shim alone is
    # deleted for the jq-absent scenario -- mirroring
    # tests/test_session_start_windows_benchmark_669.py's own fix for the
    # identical self-review finding ("shims onto the real ambient PATH ...
    # defeating _path_without_jq entirely").
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    if not jq_present:
        jq_shim = shims / "jq"
        if jq_shim.exists():
            jq_shim.unlink()
    base_path = os.environ["PATH"] if jq_present else _path_without_jq(os.environ["PATH"])
    base_path = f"{shims}{os.pathsep}{base_path}"

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "REMEMBER_DIR": str(remember),
        "SPAWN_LOG": str(log),
        "PATH": base_path,
    }
    result = subprocess.run(
        [BASH, (plugin_root / "scripts" / "session-start-hook.sh").as_posix()],
        input=_payload(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, remember, record


def _await_record(record: Path, timeout: float = 10.0) -> str:
    """Recovery forks in the background (disowned), so its argv can land
    after the hook process itself has already exited -- poll rather than
    reading the file exactly once at that instant, or an absence measured
    too early looks the same as a real absence. Lifted from #270's own
    `_await_record`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if record.exists() and record.read_text().strip():
            return record.read_text().strip()
        time.sleep(0.05)
    return record.read_text().strip() if record.exists() else ""


@pytest.mark.parametrize("jq_present", [True, False], ids=["jq-present", "jq-absent"])
def test_a_saved_previous_session_does_not_spawn_a_forced_resave(tmp_path, jq_present):
    """Negative: PREV genuinely saved -> no save-session.sh invocation, ever."""
    result, remember, record = _run(tmp_path, prev_saved=True, jq_present=jq_present)
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    # save-session.sh is only ever invoked by recovery on this path -- give
    # any background fork a moment to land, then assert its ABSENCE, not
    # just that none had landed yet.
    time.sleep(0.5)
    seen = record.read_text().strip() if record.exists() else ""
    assert not seen, (
        f"a session already recorded as saved triggered a forced re-save "
        f"anyway (jq_present={jq_present}); save-session.sh argv: {seen!r}"
    )


@pytest.mark.parametrize("jq_present", [True, False], ids=["jq-present", "jq-absent"])
def test_an_unsaved_previous_session_does_spawn_a_forced_resave(tmp_path, jq_present):
    """Positive control for the test above: PREV genuinely NOT saved -> the
    force-resave invocation IS observed. Without this, a `session_was_saved`
    that always returned true (or a recovery block that never ran at all)
    would pass the negative test above for the wrong reason."""
    result, remember, record = _run(tmp_path, prev_saved=False, jq_present=jq_present)
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    seen = _await_record(record)
    assert seen, (
        f"a genuinely unsaved previous session did NOT trigger recovery "
        f"(jq_present={jq_present}); save-session.sh argv: {seen!r}"
    )
    assert seen == f"{PREV_SESSION} --force", (
        f"recovery invoked save-session.sh with unexpected argv: {seen!r}"
    )


@pytest.mark.parametrize("jq_present", [True, False], ids=["jq-present", "jq-absent"])
def test_a_malformed_sessions_shape_reads_as_unsaved_on_both_paths(tmp_path, jq_present):
    """Parity check (self-review finding, Explore pass on this diff): real
    jq's `(.sessions // {})[$id]` throws a hard runtime error -- aborting
    the WHOLE `$SAVED_QUERY` program with no fallback to the legacy
    `.session`/`.line` shape below it -- the instant `.sessions` is present
    but is not an object (or null). The jq-free Python branch must diverge
    from that on stdout text (jq: error to stderr, empty stdout; Python:
    an explicit "unsaved") but must NOT diverge on the observable outcome:
    a `sessions` value corrupted into a list, with an otherwise-valid
    legacy `session`/`line` pair sitting right next to it, must still read
    as unsaved and still trigger recovery -- on jq and on the fallback
    alike. Before the fix's `isinstance(sessions, dict)` short-circuit,
    the Python branch fell through to the legacy check and read this
    exact shape as "saved" (no recovery), diverging from jq on the
    identical file.
    """
    result, remember, record = _run(
        tmp_path,
        prev_saved=False,
        jq_present=jq_present,
        last_save_body={
            "sessions": ["not", "a", "dict"],
            "session": PREV_SESSION,
            "line": PREV_SESSION_LINES,
        },
    )
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    seen = _await_record(record)
    assert seen == f"{PREV_SESSION} --force", (
        f"a corrupted `sessions` shape (jq: whole query errors -> "
        f"unsaved) was read as saved instead (jq_present={jq_present}); "
        f"save-session.sh argv: {seen!r}"
    )
