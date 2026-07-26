"""Compressed memory must be filed under the day it came from (issue #141).

NDC only fires on a save, and it carries an hour-long cooldown, so now.md
routinely still holds the evening's entries when the first save after midnight
arrives. The target filename was built from the date the run happened to fall
on, so that evening's work landed at the top of the NEW day's today-*.md — and
downstream consolidation then attributed it to the wrong day.

Entries carry `## HH:MM` and nothing else, so once now.md crosses midnight
there is nothing in the content that says which day it is from. The day is
recorded beside now.md, in tmp/now-day, when the file starts — beside it
rather than inside it, because a marker line would be the first thing
session-start injects into context and the first thing the summarizer reads,
for a fact only the pipeline needs.

The stamp is deliberately the day now.md STARTED. An entry written after
midnight, before the next compression, is filed with the day it was appended
to rather than its own — bounded by the NDC cooldown, and far narrower than
misfiling an entire evening. Fixing that residual needs a flush at the
boundary, which is a bigger change than this issue asked for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_save_session_gates import _make_env, _run  # noqa: E402


def _day_stamp(project: Path) -> str:
    f = project / ".remember" / "tmp" / "now-day"
    return f.read_text(encoding="utf-8").strip() if f.is_file() else ""


def _read_now(project: Path) -> str:
    f = project / ".remember" / "now.md"
    return f.read_text(encoding="utf-8") if f.is_file() else ""


def test_now_md_is_stamped_with_the_day_it_started(tmp_path):
    """Nothing else in the file records the day."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    env["STUB_HAIKU_TEXT"] = "## 23:50 | main\n\n- late evening work\n"
    (project / ".remember" / "tmp" / "last-ndc.ts").write_text(str(2**40))  # suppress NDC
    _run(plugin, env, sid)

    from datetime import datetime
    assert _day_stamp(project) == datetime.now().strftime("%Y-%m-%d"), (
        f"no day recorded for now.md: {_day_stamp(project)!r}"
    )
    assert "- late evening work" in _read_now(project)
    assert "remember-day" not in _read_now(project), (
        "the day leaked into now.md, which goes straight into context"
    )


def test_the_stamp_marks_the_file_not_each_entry(tmp_path):
    """Three entries, one day — the stamp is set when the file starts."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    (project / ".remember" / "tmp" / "last-ndc.ts").write_text(str(2**40))
    for i in range(3):
        env["STUB_HAIKU_TEXT"] = f"## 1{i}:00 | main\n\n- entry {i}\n"
        _run(plugin, env, sid)

    from datetime import datetime
    assert _day_stamp(project) == datetime.now().strftime("%Y-%m-%d")
    assert _read_now(project).count("entry ") == 3


def test_compression_files_content_under_the_stamped_day(tmp_path):
    """The reported bug: an evening's entries compressed after midnight.

    The stamp says the 25th; the run happens on whatever today is. The block
    must land in today-2026-07-25.md, not in the run day's file.
    """
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    remember = project / ".remember"
    remember.mkdir(parents=True, exist_ok=True)
    (remember / "now.md").write_text("## 23:50 | main\n\n- yesterday evening\n",
                                     encoding="utf-8")
    (remember / "tmp").mkdir(exist_ok=True)
    (remember / "tmp" / "now-day").write_text("2026-07-25\n", encoding="utf-8")
    env["STUB_HAIKU_TEXT"] = "## 00:10 | main\n\n- after midnight\n"
    _run(plugin, env, sid)

    stamped = remember / "today-2026-07-25.md"
    assert stamped.is_file(), (
        "the compressed block was not filed under the day it came from; "
        f"files present: {sorted(p.name for p in remember.glob('today-*.md'))}"
    )


def test_an_unstamped_now_md_still_compresses(tmp_path):
    """A now.md with no recorded day — written before this existed, or orphaned."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    remember = project / ".remember"
    remember.mkdir(parents=True, exist_ok=True)
    (remember / "now.md").write_text("## 09:00 | main\n\n- unstamped\n", encoding="utf-8")
    env["STUB_HAIKU_TEXT"] = "## 10:00 | main\n\n- new entry\n"
    _run(plugin, env, sid)

    produced = sorted(p.name for p in remember.glob("today-*.md"))
    assert produced, "no daily file produced for an unstamped now.md"


def test_a_forged_stamp_is_ignored(tmp_path):
    """The recorded day names a file path — it cannot be trusted unchecked."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    remember = project / ".remember"
    remember.mkdir(parents=True, exist_ok=True)
    (remember / "now.md").write_text("## 09:00 | main\n\n- x\n", encoding="utf-8")
    (remember / "tmp").mkdir(exist_ok=True)
    (remember / "tmp" / "now-day").write_text("../../escaped\n", encoding="utf-8")
    env["STUB_HAIKU_TEXT"] = "## 10:00 | main\n\n- y\n"
    _run(plugin, env, sid)

    assert not (remember.parent.parent / "escaped.md").exists(), "path escaped the store"
    produced = sorted(p.name for p in remember.glob("today-*.md"))
    assert produced, "a malformed stamp should fall back to today, not skip filing"


def test_the_day_is_not_written_into_now_md(tmp_path):
    """now.md goes verbatim into context and into the summarizer prompt.

    Keeping the day beside the file rather than in it means neither has to
    know about it, and no reader has to strip it back out.
    """
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    (project / ".remember" / "tmp" / "last-ndc.ts").write_text(str(2**40))
    env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- work\n"
    _run(plugin, env, sid)

    now = _read_now(project)
    # The entry's own time is rewritten to the script's clock (#139), so pin
    # the shape rather than the literal 12:00 the stub returned.
    assert now.lstrip().startswith("## "), f"now.md does not open with the entry:\n{now}"
    assert "| main" in now
    header = now.lstrip().splitlines()[0]
    assert "-" not in header.split("|")[0], f"a date leaked into the header: {header!r}"
    assert "remember-day" not in now, "the day marker is being written into now.md"
