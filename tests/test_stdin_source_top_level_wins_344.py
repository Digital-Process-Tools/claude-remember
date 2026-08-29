"""`_stdin_json_string source` takes the FIRST `"source"` occurrence (#344).

Not a live bug: no payload shape Claude Code sends today nests a `"source"`
key ahead of the top-level one. The round-1 security audit gating 0.19.0
threw every hostile shape it could at the extractor and everything degraded
to the safe empty string EXCEPT this one direction -- a payload whose FIRST
`"source"` occurrence in the joined stdin string is a nested one, e.g.
`{"tool":{"source":"compact"},"source":"startup"}`, extracts `compact` at a
genuine `startup` and defers the recap when it should be injected in full.

This is filed to PIN the asymmetry, not to fix it (#344 is explicit: do not
reach for a JSON parser -- #340 avoided that dependency deliberately, and a
hook that must survive a broken install is the wrong place to acquire one).
Two tests, both characterizing exactly what `_stdin_json_string` does today:

  - a nested key AFTER the top-level one is the safe, common-shape case --
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

── The #422 fixture bug, and how the fixture is built now ──────────────────
This file used to name its own "today" file with `time.strftime(...)` at
IMPORT time (pytest collects every module before running any test), while
`scripts/session-start-hook.sh` computes its own `$TODAY` at RUN time --
minutes later on this suite's slower legs. A run that straddles UTC midnight
between those two reads names a "today" file the hook no longer recognises
as today's: `REMEMBER_TODAY_FILE="$REMEMBER_DIR/today-${TODAY}.md"`
(session-start-hook.sh:866) then points at a file that does not exist, so
its body drops out of the injected recap, AND the fixture's now-stale file
gets counted as staging and forks a background consolidation
(session-start-hook.sh:1395-1399) -- both observed on PR #421's
`pytest (macos-latest, 3.9)` leg, timestamped 57 seconds past UTC midnight.

Reproduced deterministically (no waiting for midnight) by shimming `date` on
PATH so the fixture's "today" and the hook's runtime "today" can be set
independently, then driven back into agreement: `_shim_date` freezes
`+%Y-%m-%d` to one fixed, arbitrary value for both the fixture's filename
and the hook's own clock, so the two can never drift apart again regardless
of when the suite actually runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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

SESSION = "aaaaaaaa-0000-4000-8000-000000000344"

# Fixed, arbitrary "today" -- never the real wall-clock date, so nothing here
# can ever again straddle a real UTC midnight (#422). Two distinct fixed
# values give the drift tests below a genuine day boundary without touching
# a clock.
FROZEN_TODAY = "2099-03-17"
DAY_BEFORE_FROZEN_TODAY = "2099-03-16"


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
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y-%m-%d" ]; then\n'
        f'  echo {today}\n'
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def _bodies_for(today_file):
    return {
        "identity.md": "IDENTITY-BODY-344",
        "core-memories.md": "CORE-BODY-344",
        today_file: "TODAY-BODY-344",
        "now.md": "NOW-BODY-344",
        "recent.md": "RECENT-BODY-344",
        "archive.md": "ARCHIVE-BODY-344",
    }


def _store(tmp_path, today_file, bodies):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    for name, body in bodies.items():
        (remember / name).write_text(body + "\n", encoding="utf-8")
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


def _fire(home, project, remember, raw_payload, bindir):
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        env=_env(home, project, remember, bindir),
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
    today_file = "today-" + FROZEN_TODAY + ".md"
    bodies = _bodies_for(today_file)
    home, project, remember = _store(tmp_path, today_file, bodies)
    bindir = tmp_path / "bin"
    _shim_date(bindir, FROZEN_TODAY)

    payload = _base_fields()
    payload["source"] = "startup"
    payload["tool"] = {"source": "compact"}
    out = _fire(home, project, remember, json.dumps(payload), bindir)

    assert "=== MEMORY ===" in out
    for name, body in bodies.items():
        assert body in out, name + " body missing -- nested-after-top-level stopped winning"


def test_nested_source_ahead_of_top_level_is_the_documented_344_gap(tmp_path):
    """The one direction #344 says is not covered: a nested `source` key
    appearing in the raw stdin BEFORE the top-level one. This is a
    characterization of TODAY's mechanism, not a requirement -- it pins the
    gap so a change to the scan is a visible, deliberate decision rather
    than an accidental shift in which direction is unsafe."""
    today_file = "today-" + FROZEN_TODAY + ".md"
    bodies = _bodies_for(today_file)
    deferrable = [n for n in bodies if n != "identity.md"]
    home, project, remember = _store(tmp_path, today_file, bodies)
    bindir = tmp_path / "bin"
    _shim_date(bindir, FROZEN_TODAY)

    payload = {"tool": {"source": "compact"}}
    payload.update(_base_fields())
    payload["source"] = "startup"
    out = _fire(home, project, remember, json.dumps(payload), bindir)

    assert "=== MEMORY ===" in out
    assert bodies["identity.md"] in out
    marker = 'source" occurrence, which is the #344 mechanism this test pins'
    for name in deferrable:
        assert bodies[name] not in out, (
            name + " was injected -- the extractor stopped taking the first "
            + marker
        )


def test_a_stale_today_file_does_not_blank_the_rest_of_the_recap(tmp_path):
    """Pins #422 without waiting for midnight.

    A real UTC-midnight crossing between when a fixture names its "today"
    file and when the hook resolves its own `$TODAY` at runtime leaves the
    fixture's file genuinely stale by the time the hook looks for it --
    that exclusion is correct once the two clocks truly disagree, not a
    bug. What must NOT happen is the rest of the recap going missing with
    it: identity, core-memories, now.md, recent.md and archive.md carry no
    $TODAY in their path at all, so a stale reading of "today" must never
    blank them out the way it took the whole recap down in the observed
    CI failure.

    The two dates are driven apart deliberately, on separate shims, rather
    than hoping to catch a real boundary.
    """
    stale_today_file = "today-" + DAY_BEFORE_FROZEN_TODAY + ".md"
    bodies = _bodies_for(stale_today_file)
    home, project, remember = _store(tmp_path, stale_today_file, bodies)
    bindir = tmp_path / "bin"
    _shim_date(bindir, FROZEN_TODAY)  # the hook's clock, a day AHEAD of the fixture's

    payload = _base_fields()
    payload["source"] = "startup"
    out = _fire(home, project, remember, json.dumps(payload), bindir)

    assert "=== MEMORY ===" in out
    for name in ("identity.md", "core-memories.md", "now.md", "recent.md", "archive.md"):
        assert bodies[name] in out, (
            name + " body missing under a genuine TODAY mismatch -- the whole "
            "recap must not go down with one stale file"
        )
    # Documenting the correct, expected half: the genuinely stale file is
    # excluded from the daily slot and, correctly, triggers consolidation --
    # this is not the #422 defect, the missing OTHER bodies were.
    assert "=== MEMORY CONSOLIDATION ===" in out
    assert bodies[stale_today_file] not in out


def test_agreeing_dates_trigger_no_background_consolidation(tmp_path):
    """Must-not-fire control for the test above: when the fixture's "today"
    and the hook's runtime "today" agree (the normal case, and the only
    case after the #422 freeze fix), there is no stray staging file lying
    around and nothing should fire a background consolidation."""
    today_file = "today-" + FROZEN_TODAY + ".md"
    bodies = _bodies_for(today_file)
    home, project, remember = _store(tmp_path, today_file, bodies)
    bindir = tmp_path / "bin"
    _shim_date(bindir, FROZEN_TODAY)

    payload = _base_fields()
    payload["source"] = "startup"
    out = _fire(home, project, remember, json.dumps(payload), bindir)

    assert "=== MEMORY CONSOLIDATION ===" not in out, (
        "agreeing dates still triggered a background consolidation:\n" + out
    )
