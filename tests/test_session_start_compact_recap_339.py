"""At `source=compact` the recap must not be re-injected wholesale (#339).

`SessionStart` fires again at every auto-compaction. The hook read `session_id`
out of the payload (#206/#270) and discarded `source`, so a compaction received
the same full recap a cold start does — every memory file cat'd into a context
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
unrecognised `source` must produce today's output exactly — an absence read as
`compact` would silently stop injecting memory for anyone whose payload shape
differs from the one this heuristic was written against, which is the failure
this plugin exists to prevent rather than to cause.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

SESSION = "dddddddd-0000-4000-8000-000000000339"

# One sentinel per memory file. Asserting on the BODY, not on the section
# header: the header survives either way, so a test that looked for it would
# pass against a hook that still cat'd everything.
# Today's staging file is named for the date, so it is built the way the hook
# builds it: system-local, because this repo ships no config.json and an unset
# REMEMBER_TZ falls back to local rather than UTC (#99). Naming it matters —
# it is the sixth entry of MEMORY_FILES, and a fixture that omitted it would
# leave the largest of the deferred files untested.
TODAY_FILE = "today-" + time.strftime("%Y-%m-%d") + ".md"

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


def _env(home, project, remember):
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


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
    kwargs = {
        "env": _env(home, project, remember),
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

    The assertion is on the RESOLVED PATH, not the basename — a full recap
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
