"""`_stdin_json_string source` takes the FIRST `"source"` occurrence (#344).

Not a live bug: no payload shape Claude Code sends today nests a `"source"`
key ahead of the top-level one. The round-1 security audit gating 0.19.0
threw every hostile shape it could at the extractor and everything degraded
to the safe empty string EXCEPT this one direction — a payload whose FIRST
`"source"` occurrence in the joined stdin string is a nested one, e.g.
`{"tool":{"source":"compact"},"source":"startup"}`, extracts `compact` at a
genuine `startup` and defers the recap when it should be injected in full.

This is filed to PIN the asymmetry, not to fix it (#344 is explicit: do not
reach for a JSON parser — #340 avoided that dependency deliberately, and a
hook that must survive a broken install is the wrong place to acquire one).
Two tests, both characterizing exactly what `_stdin_json_string` does today:

  - a nested key AFTER the top-level one is the safe, common-shape case —
    first-occurrence-scanning finds the top-level key first, so it wins.
    This is the property that must keep holding.
  - a nested key BEFORE the top-level one is the documented, currently-real
    gap: first-occurrence-scanning finds the nested key first, so IT wins.
    This test pins that this is what happens today, so it fails loudly the
    day someone "fixes" the scan without noticing which direction it moved,
    and stops being invisible the day a payload shaped like this actually
    arrives.

Recap injection is the observable: full body text for every non-identity
memory file means the hook resolved `source` as something other than
`compact`; the "not re-injected at compact" marker with no bodies means it
resolved `source` as `compact`. Same detection method
test_session_start_compact_recap_339.py already uses.
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

SESSION = "aaaaaaaa-0000-4000-8000-000000000344"
TODAY_FILE = "today-" + time.strftime("%Y-%m-%d") + ".md"
BODIES = {
    "identity.md": "IDENTITY-BODY-344",
    "core-memories.md": "CORE-BODY-344",
    TODAY_FILE: "TODAY-BODY-344",
    "now.md": "NOW-BODY-344",
    "recent.md": "RECENT-BODY-344",
    "archive.md": "ARCHIVE-BODY-344",
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


def _fire(home, project, remember, raw_payload):
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        env=_env(home, project, remember),
        input=raw_payload,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _base_fields():
    return {
        "session_id": SESSION,
        "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
        "hook_event_name": "SessionStart",
        "cwd": "/does/not/matter",
    }


def test_top_level_source_wins_when_the_nested_key_comes_after(tmp_path):
    """Common shape: top-level `source` is written first, some other field
    that happens to nest a `source` key comes later. First-occurrence
    scanning finds the top-level one first and this must keep resolving to
    a full recap."""
    home, project, remember = _store(tmp_path)
    payload = _base_fields()
    payload["source"] = "startup"
    payload["tool"] = {"source": "compact"}
    out = _fire(home, project, remember, json.dumps(payload))

    assert "=== MEMORY ===" in out
    for name, body in BODIES.items():
        assert body in out, name + " body missing — nested-after-top-level stopped winning"


def test_nested_source_ahead_of_top_level_is_the_documented_344_gap(tmp_path):
    """The one direction #344 says is not covered: a nested `source` key
    appearing in the raw stdin BEFORE the top-level one. This is a
    characterization of TODAY's mechanism, not a requirement — it pins the
    gap so a change to the scan is a visible, deliberate decision rather
    than an accidental shift in which direction is unsafe."""
    home, project, remember = _store(tmp_path)
    payload = {"tool": {"source": "compact"}}
    payload.update(_base_fields())
    payload["source"] = "startup"
    out = _fire(home, project, remember, json.dumps(payload))

    assert "=== MEMORY ===" in out
    assert BODIES["identity.md"] in out
    marker = 'source" occurrence, which is the #344 mechanism this test pins'
    for name in DEFERRABLE:
        assert BODIES[name] not in out, (
            name + " was injected — the extractor stopped taking the first "
            + marker
        )
