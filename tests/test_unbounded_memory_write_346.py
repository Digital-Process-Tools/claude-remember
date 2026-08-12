"""recent.md / archive.md can be written without bound (#346).

The reporter's store reached 6.4 GB (`recent.md`) and 1.8 GB (`archive.md`),
and every `claude` launch in that project froze because the SessionStart hook
`cat`s both into context. Their diagnosis was "unlike now.md / today-*.md,
these two have no rotation — they only ever get appended to".

That diagnosis does not survive the code. Nothing appends to either file. The
only writer in the tree is ``cp "$RECENT_OUT" "$RECENT_FILE"`` at
scripts/run-consolidation.sh:159, which REPLACES the file wholesale with the
consolidation's output. The observation behind the diagnosis was still exactly
right — the files only ever grew and never shrank — but the route there is the
opposite of an append:

**The consolidation caps its input and does not cap its output.**

``consolidate()`` refuses to SEND a prompt over ``max_prompt_bytes``
(pipeline/consolidate.py:315) — staging + recent.md + archive.md, default
600000. Past that it raises and nothing is written. But between ``call_haiku``
returning and the ``cp``, no byte count is taken: ``cmd_consolidate`` writes
``result.recent`` to a temp file verbatim (pipeline/shell.py:517-523) and the
shell copies it over recent.md. ``capture_output=True`` on the CLI subprocess
(pipeline/haiku.py:686) is bounded by a wall clock, not by bytes, so the size
of that response is not a quantity this pipeline has ever measured.

The two halves of the defect are one mechanism, and the second half is what
makes it permanent:

1. **A single response of any size is written.** Not "a few KB per session" —
   one round, arbitrarily many bytes, straight into the permanent record.
2. **That same write disables the only thing that could repair it.** The cap
   is measured on the INPUT, and recent.md is part of the input. So the round
   after an oversized write assembles an oversized prompt, raises
   ``ConsolidationTooLarge``, and skips. Forever. ``_rotate_archive`` is no
   escape: it can only shrink archive.md, and the bulk here is recent.md.
   The file cannot grow again and cannot shrink either — frozen at its worst
   size, which is precisely "only ever gets appended to" as a user sees it.

There is a second, much slower grower, and it is deliberately NOT this bug:
consolidation is told to keep recent under 600 tokens but nothing enforces it,
so a model that faithfully re-emits the file plus one day per round grows it
monotonically — measured at ~2 KB/round, reaching the 600000 cap after ~298
rounds and then freezing. That one is bounded by the cap by construction and
cannot reach a gigabyte. Distinguishing them matters: capping the output is
what closes the multi-GB path; the slow path is a compression-compliance
question, filed separately.

The last defence has to hold on a store that is ALREADY broken, because the
fix above does nothing for the 6.4 GB file the reporter already had. Two
readers walk into it unguarded: the SessionStart hook `cat`s the file into
every session (which is what froze `claude` and took iTerm2 to ~56 GB), and
``cmd_consolidate`` reads the whole file into memory and assembles a prompt
around it BEFORE the cap gets to say it is too large — spending several times
the file's size in RAM to discover it should not have been read.

Direction of every guard here: refusing is non-destructive. A skipped
consolidation leaves staging and memory intact and the next run retries; a
memory file named instead of injected stays on disk and greppable, and the
store already has the vocabulary for that (#124, rotated archives). Writing
an unbounded response, or reading one into a hook, is what costs the machine.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))

from pipeline import consolidate as consolidate_mod
from pipeline import shell as shell_mod
from pipeline.consolidate import ConsolidationSkipped, consolidate
from pipeline.slug import session_dir_slug as _slug
from pipeline.types import HaikuResult, TokenUsage

CAP = 600000
STAGING = {"today-2026-01-01.md": "## 10:00 | main\n\nA day of work.\n"}
RECENT_BEFORE = "# Recent\n\n## 2025-12-31\n\nSmall and healthy.\n"
ARCHIVE_BEFORE = "# Archive\n\n## Week of 2025-12-22\n\nSmall and healthy.\n"


def _response(recent_body: str, archive_body: str = "fine") -> HaikuResult:
    """A well-formed consolidation envelope. Only its SIZE is under test.

    Deliberately valid on every other axis: it carries the ``===RECENT===``
    envelope and a real ``## YYYY-MM-DD`` entry header, and none of the
    template's instruction lines, so it passes ``_is_valid_consolidation`` and
    the #202 echo guard. A test that leaned on those would pass against a
    pipeline that still had no size guard at all.
    """
    return HaikuResult(
        text=(f"===RECENT===\n# Recent\n\n## 2026-01-01\n\n{recent_body}\n\n"
              f"===ARCHIVE===\n# Archive\n\n## Week of 2025-12-29\n\n{archive_body}\n"),
        is_skip=False,
        is_rejected=False,
        tokens=TokenUsage(input=0, output=0, cache=0, cost_usd=0.0),
    )


# ── 1. The write itself ────────────────────────────────────────────────────


def test_oversized_response_is_refused_instead_of_written():
    """A response larger than the cap must not become the permanent record.

    The pipeline already knows this number: it is the same cap that made it
    refuse to SEND a prompt this size one function call earlier. Accepting
    back what it would not send is the whole defect.
    """
    huge = _response("y" * (CAP * 3))

    with patch.object(consolidate_mod, "call_haiku", lambda p, timeout=180: huge):
        with pytest.raises(ConsolidationSkipped):
            consolidate(STAGING, RECENT_BEFORE, ARCHIVE_BEFORE, max_prompt_bytes=CAP)


def test_oversized_archive_section_is_refused_too():
    """archive.md reached 1.8 GB in the report — both halves are written."""
    huge = _response("small", "z" * (CAP * 3))

    with patch.object(consolidate_mod, "call_haiku", lambda p, timeout=180: huge):
        with pytest.raises(ConsolidationSkipped):
            consolidate(STAGING, RECENT_BEFORE, ARCHIVE_BEFORE, max_prompt_bytes=CAP)


def test_a_normal_response_is_still_written():
    """The guard must not be a cap on consolidation working at all.

    Paired with the two above on purpose: a size guard that rejected
    everything would satisfy them and silently stop the plugin from ever
    consolidating, which is a worse bug than the one being fixed.
    """
    ok = _response("a compressed day, a few hundred bytes long")

    with patch.object(consolidate_mod, "call_haiku", lambda p, timeout=180: ok):
        result = consolidate(STAGING, RECENT_BEFORE, ARCHIVE_BEFORE, max_prompt_bytes=CAP)

    assert result.recent.startswith("# Recent")
    assert "a compressed day" in result.recent


def test_refusal_leaves_memory_and_staging_untouched(tmp_path):
    """The refusal has to take the established non-destructive path.

    ``CONSOLIDATION_STATUS=skip`` with no ``RECENT_OUT`` is the contract
    run-consolidation.sh reads: it means do not overwrite memory and do not
    retire the staging files to ``.done.md``. A refusal that instead wrote an
    empty file, or that let the shell retire staging, would lose the day it
    was protecting.
    """
    recent_f = tmp_path / "recent.md"
    archive_f = tmp_path / "archive.md"
    recent_f.write_text(RECENT_BEFORE, encoding="utf-8")
    archive_f.write_text(ARCHIVE_BEFORE, encoding="utf-8")
    (tmp_path / "today-2026-01-01.md").write_text("## 10:00 | main\n\nWork.\n", encoding="utf-8")

    huge = _response("y" * (CAP * 3))
    buf = io.StringIO()
    with patch.object(consolidate_mod, "call_haiku", lambda p, timeout=180: huge):
        with redirect_stdout(buf):
            shell_mod.cmd_consolidate(str(tmp_path), str(recent_f), str(archive_f), CAP, "")

    out = buf.getvalue()
    assert "CONSOLIDATION_STATUS=skip" in out, out
    assert "RECENT_OUT=" not in out, out
    assert recent_f.read_text(encoding="utf-8") == RECENT_BEFORE
    assert archive_f.read_text(encoding="utf-8") == ARCHIVE_BEFORE


# ── 2. The ratchet: an already-oversized store must not be read whole ──────


def test_oversized_store_is_refused_by_size_not_by_reading_it(tmp_path):
    """Discovering the store is too large must not cost the store's size in RAM.

    Today the order is: read recent.md whole, read archive.md whole, build a
    prompt string around both, encode it to count bytes, and only then raise
    ``ConsolidationTooLarge``. On the reporter's 6.4 GB file that is several
    times 6.4 GB of allocation to reach a decision that ``os.path.getsize``
    answers for free — and it runs disowned in the background, next to a live
    session, on a machine that then needed a restart.

    Asserted by watching the reads rather than the timing: the file is a real
    file of the right size, and the test fails if anything opens it.
    """
    recent_f = tmp_path / "recent.md"
    archive_f = tmp_path / "archive.md"
    # Sparse: the size is the point, the bytes are not.
    with open(recent_f, "wb") as f:
        f.truncate(CAP * 4)
    archive_f.write_text(ARCHIVE_BEFORE, encoding="utf-8")
    (tmp_path / "today-2026-01-01.md").write_text("## 10:00 | main\n\nWork.\n", encoding="utf-8")

    real_open = io.open
    opened: list[str] = []

    def spy(file, *a, **kw):
        opened.append(str(file))
        return real_open(file, *a, **kw)

    called = []
    buf = io.StringIO()
    with patch.object(consolidate_mod, "call_haiku",
                      lambda p, timeout=180: called.append(1) or _response("x")):
        with patch("builtins.open", spy):
            with redirect_stdout(buf):
                shell_mod.cmd_consolidate(str(tmp_path), str(recent_f), str(archive_f), CAP, "")

    assert "CONSOLIDATION_STATUS=skip" in buf.getvalue(), buf.getvalue()
    assert not called, "an oversized store must not reach the model call"
    assert str(recent_f) not in opened, (
        "recent.md was read into memory to discover it is too large to read — "
        f"opened: {opened}"
    )


# ── 3. The last defence: a broken store must not freeze the session ───────


pytestmark_bash = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

SESSION = "dddddddd-0000-4000-8000-000000000346"


@pytestmark_bash
def test_session_start_names_an_oversized_memory_file_instead_of_injecting_it(tmp_path):
    """The symptom the reporter actually hit: every `claude` launch froze.

    A memory file past the injection threshold is named with its size and left
    on disk, exactly as rotated archives already are (#124) — kept, greppable,
    and not poured into a context window. Injecting it is what hung the launch
    and took iTerm2 to ~56 GB, and no fix to the writer helps a store that is
    already in that state.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)

    (remember / "now.md").write_text("NOW-BODY-346\n", encoding="utf-8")
    # Big, and with a sentinel at the very front so a truncating implementation
    # (rather than a refusing one) would still be caught by the size line.
    with open(remember / "recent.md", "w", encoding="utf-8") as f:
        f.write("RECENT-BODY-346\n")
        f.write("q" * (CAP * 4))
    oversized_bytes = (remember / "recent.md").stat().st_size

    payload = json.dumps({
        "session_id": SESSION,
        "transcript_path": f"/does/not/matter/{SESSION}.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": "/does/not/matter",
    })

    proc = subprocess.run(
        ["bash", str(SESSION_START)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "HOME": str(home),
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "REMEMBER_DIR": str(remember),
            "_LIB_MEMORY_DIR_LOADED": "1",
        },
    )

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout

    assert "NOW-BODY-346" in out, "healthy memory files must still be injected"
    assert "RECENT-BODY-346" not in out, (
        f"an oversized recent.md was cat'd into the session ({len(out)} bytes of hook output)"
    )
    assert "recent.md" in out, "an oversized file must still be NAMED, not silently dropped"
    assert str(oversized_bytes) in out, "the size must be reported so the state is diagnosable"
