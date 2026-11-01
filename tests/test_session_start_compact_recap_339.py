"""At `source=compact` the recap must not be re-injected wholesale (#339).

`SessionStart` fires again at every auto-compaction. The hook read `session_id`
out of the payload (#206/#270) and discarded `source`, so a compaction received
the same full recap a cold start does -- every memory file cat'd into a context
that was just replaced by a summary of a conversation which already contained
them.

What a compaction is NOT is a fresh session, and that asymmetry is the whole
design here:

* **Identity is presence-required.** A pointer to `identity.md` does not make
  the agent behave as that persona, and nothing else in the session-start
  output even names the file. It stays injected in full.
* **Everything else is recall-on-demand**, and already addressable: the
  unconditional `=== REMEMBER ===` hint names `now.md`, `today-*.md`,
  `recent.md`, `archive.md` and `core-memories.md` on every fire. Naming them
  again with their sizes costs a line each and loses nothing that cannot be
  read back.

The direction of the default is the other half. A missing, empty or
unrecognised `source` must produce today's output exactly -- an absence read as
`compact` would silently stop injecting memory for anyone whose payload shape
differs from the one this heuristic was written against, which is the failure
this plugin exists to prevent rather than to cause.

── The #422 fixture bug, and how "today" is pinned now ─────────────────────
This file used to name its own "today" staging file with
`time.strftime("%Y-%m-%d")` at IMPORT time, while
`scripts/session-start-hook.sh` computes its own `$TODAY` at RUN time --
minutes later on this suite's slower legs, since pytest collects every module
before running any test. A run that straddles a real clock boundary between
those two reads names a "today" file the hook no longer recognises as
today's, dropping its body from the recap and forking a stray background
consolidation (session-start-hook.sh:866, :1395-1399) -- the exact #422
mechanism pinned in tests/test_stdin_source_top_level_wins_344.py, which
shares this file's fixture shape closely enough to share its exposure.

Fixed by shimming `date` on PATH so the fixture's "today" and the hook's own
runtime clock read from the same frozen, arbitrary value -- never the real
wall clock, so the two can never drift apart regardless of when the suite
actually runs. This intentionally departs from the file's previous
system-local rationale (matching an unconfigured REMEMBER_TZ's local-time
fallback, #99): that only matched TIMEZONE conventions, not TIMING, and a
frozen shim value removes the timing dependency the timezone match never
addressed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest.mock
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

SESSION = "dddddddd-0000-4000-8000-000000000339"

# Fixed, arbitrary "today" -- never the real wall-clock date (#422), so a
# suite that straddles a real day boundary can no longer make the fixture's
# "today" file disagree with the hook's own runtime notion of today.
FROZEN_TODAY = "2099-04-08"
TODAY_FILE = "today-" + FROZEN_TODAY + ".md"


def _write_shell_script(path: Path, text: str) -> None:
    r"""Writes a `#!/bin/sh` script via `open(..., newline="\n")`, never
    `Path.write_text()` (#994/#999): `write_text()`'s default
    `newline=None` applies universal-newline translation on write, turning
    every `\n` into `os.linesep` -- `\r\n` on Windows -- which corrupts
    the shebang line into a "bad interpreter" exec failure. This file
    carries a win32 skip (above), so that breakage is inert here, but the
    write is still fixed to the safe pattern rather than left as a trap
    for whoever narrows or removes that skip."""
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        f.write(text)


def _shim_date(bindir, today):
    """PATH shim for `date`, freezing `+%Y-%m-%d` to a fixed value.

    A PATH shim only intercepts a real `date` process -- on bash >= 4.2,
    lib-clock.sh's `_remember_date` prefers the spawn-free
    `printf '%(FMT)T'` builtin and never touches PATH at all, silently
    ignoring this shim (the seam tests/test_prompt_hook_spawns.py's own
    guard exists to catch). REMEMBER_NO_PRINTF_T=1, set in `_env` below,
    forces the `date` path so the shim actually intercepts. That guard also
    requires any file that puts `date` on PATH to name the variable, hence
    this comment: REMEMBER_NO_PRINTF_T.
    """
    bindir.mkdir(exist_ok=True)
    shim = bindir / "date"
    _write_shell_script(
        shim,
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y-%m-%d" ]; then\n'
        f'  echo {today}\n'
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def test_write_shell_script_never_uses_path_write_text(tmp_path):
    r"""Regression pin for #994/#999: `_write_shell_script` must go through
    `open(..., newline="\n")`, never `Path.write_text()`, whose default
    `newline=None` applies universal-newline translation and can corrupt
    the shebang line on Windows. Pins the *call*, not the output bytes --
    `os.linesep` is "\n" on every platform this suite can exercise
    locally, so the corrupted bytes this bug produces on Windows would
    not appear here even without the fix, which is exactly why the call
    itself has to be pinned instead."""
    target = tmp_path / "stub"
    with unittest.mock.patch.object(
        Path,
        "write_text",
        side_effect=AssertionError(
            r"shell script written via Path.write_text(), not open(newline='\n')"
        ),
    ):
        _write_shell_script(target, "#!/bin/sh\nexit 0\n")
    assert target.read_bytes() == b"#!/bin/sh\nexit 0\n"


# One sentinel per memory file. Asserting on the BODY, not on the section
# header: the header survives either way, so a test that looked for it would
# pass against a hook that still cat'd everything.
BODIES = {
    "identity.md": "IDENTITY-BODY-339",
    "core-memories.md": "CORE-BODY-339",
    TODAY_FILE: "TODAY-BODY-339",
    "now.md": "NOW-BODY-339",
    "recent.md": "RECENT-BODY-339",
    "archive.md": "ARCHIVE-BODY-339",
}
DEFERRABLE = [n for n in BODIES if n != "identity.md"]


def _store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    for name, body in BODIES.items():
        (remember / name).write_text(body + "\n", encoding="utf-8")
    (remember / "remember.md").write_text("HANDOFF-BODY-339\n", encoding="utf-8")
    return home, project, remember


def _env(home, project, remember, bindir):
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
        "REMEMBER_NO_PRINTF_T": "1",
    }
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    return env


def _payload(source=None, extra=None):
    """A SessionStart payload. `source=None` omits the field entirely."""
    p = {
        "session_id": SESSION,
        "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
        "hook_event_name": "SessionStart",
        "cwd": "/does/not/matter",
    }
    if source is not None:
        p["source"] = source
    if extra:
        p.update(extra)
    return json.dumps(p)


def _run(tmp_path, payload):
    home, project, remember = _store(tmp_path)
    bindir = tmp_path / "bin"
    _shim_date(bindir, FROZEN_TODAY)
    kwargs = {
        "env": _env(home, project, remember, bindir),
        "capture_output": True,
        "text": True,
        "timeout": 60,
    }
    if payload is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = payload
    result = subprocess.run(["bash", str(SESSION_START)], check=False, **kwargs)
    assert result.returncode == 0, result.stderr
    return result.stdout, remember


def _assert_full_recap(out):
    assert "=== MEMORY ===" in out
    for name, body in BODIES.items():
        assert body in out, name + " body missing from a full recap"


# ── compact: identity injected, the rest named ────────────────────────────

def test_compact_still_injects_identity(tmp_path):
    out, _ = _run(tmp_path, _payload("compact"))
    assert "=== MEMORY ===" in out
    assert BODIES["identity.md"] in out


def test_compact_does_not_reinject_the_recap_bodies(tmp_path):
    out, _ = _run(tmp_path, _payload("compact"))
    for name in DEFERRABLE:
        assert BODIES[name] not in out, name + " was re-injected at source=compact"


def test_compact_names_every_file_it_withheld_by_full_path(tmp_path):
    """Withholding silently would be the same defect in the other direction:
    the agent cannot grep a file it was never told is there.

    The assertion is on the RESOLVED PATH, not the basename -- a full recap
    already prints `--- recent.md ---` as its body header, so a basename check
    would pass against a hook that changed nothing at all.
    """
    out, remember = _run(tmp_path, _payload("compact"))
    for name in DEFERRABLE:
        assert str(remember / name) in out, name + " was withheld without a path"


def test_compact_states_the_size_of_what_it_withheld(tmp_path):
    """Named-not-injected is the #124 vocabulary, and #124 prints sizes: the
    agent has to be able to tell a 300-byte now.md from a 300kB archive before
    deciding to read it."""
    out, remember = _run(tmp_path, _payload("compact"))
    size = (remember / "recent.md").stat().st_size
    assert str(size) + " bytes" in out


def test_compact_keeps_the_other_sections(tmp_path):
    """Only the recap bodies change. The handoff and the history hint are not
    in scope and must not be narrowed by the same commit."""
    out, _ = _run(tmp_path, _payload("compact"))
    assert "=== LAST HANDOFF ===" in out
    assert "HANDOFF-BODY-339" in out
    assert "=== REMEMBER ===" in out


# ── every other source is unchanged ───────────────────────────────────────

@pytest.mark.parametrize("source", ["startup", "resume", "clear", "fork"])
def test_known_sources_other_than_compact_get_the_full_recap(tmp_path, source):
    _assert_full_recap(_run(tmp_path, _payload(source))[0])


def test_absent_source_field_gets_the_full_recap(tmp_path):
    """An absence is not a compaction. A payload shape this heuristic does not
    recognise must lose nothing."""
    _assert_full_recap(_run(tmp_path, _payload(None))[0])


def test_no_stdin_at_all_gets_the_full_recap(tmp_path):
    _assert_full_recap(_run(tmp_path, None)[0])


@pytest.mark.parametrize("source", ["", "Compact", "COMPACT", "compaction",
                                    "compact ", "startup,compact"])
def test_unrecognised_source_values_get_the_full_recap(tmp_path, source):
    _assert_full_recap(_run(tmp_path, _payload(source))[0])


def test_a_similarly_named_key_is_not_read_as_source(tmp_path):
    """The extractor is deliberately narrow: the key must be `"source"`, not a
    field that merely ends in it."""
    out, _ = _run(tmp_path, _payload(None, {"hook_source": "compact",
                                            "data_source": "compact"}))
    _assert_full_recap(out)
