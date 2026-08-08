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

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_save_session_gates import _make_env, _run, _suppress_ndc  # noqa: E402


# These tests need NDC OFF so that now.md survives to be asserted on. They used
# to get that by writing `2**40` into tmp/last-ndc.ts — a marker ~34,000 years
# in the future, whose negative ELAPSED read as "inside the cooldown".
#
# That is #326's defect, used as an off switch. It stopped working the moment
# the range guard landed, which is the correct outcome twice over: the gate no
# longer accepts a marker ahead of now, and #159 already established that a
# hand-written marker was never the supported brake. `_suppress_ndc` writes a
# CURRENT timestamp against the 999999s cooldown `_make_env` configures, which
# suppresses NDC through the mechanism the product documents.
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
    _suppress_ndc(project)
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
    _suppress_ndc(project)
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
    _suppress_ndc(project)
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


def test_a_compression_spanning_midnight_restamps_with_the_new_day(tmp_path):
    """The kept bytes belong to the day they were appended, not to this run.

    Compression hands its work to a background subshell and the parent exits;
    the Haiku call can take 180s. $TODAY_DATE was computed once, before all of
    that. So a compression that started before midnight stamped the bytes a
    NEWER save appended with the previous day — not the day they were appended
    (which is all the documented residual concedes), and not their own day,
    but a third stale one belonging to an earlier run.

    The clock is shimmed to move between the parent's reads and the restamp,
    which is exactly what midnight does to a long-running compression.
    """
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    remember = project / ".remember"

    # A PATH shim only intercepts a real `date` process, so this depends on
    # REMEMBER_NO_PRINTF_T=1 — set for every test built from _make_env — keeping
    # lib-clock.sh off bash's spawn-free `printf '%(FMT)T'` builtin (#227).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    counter = tmp_path / "date-calls"
    shim = bindir / "date"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y-%m-%d" ]; then\n'
        f'  echo x >> "{counter}"\n'
        f'  if [ "$(wc -l < "{counter}" | tr -d " ")" -le 2 ]; then\n'
        "    echo 2026-07-24\n"
        "  else\n"
        "    echo 2026-07-26\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    shim.chmod(0o755)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["STUB_HAIKU_TEXT"] = "## 23:50 | main\n\n- evening work\n"
    # A newer save lands while this compression is in flight — the #142 window.
    env["STUB_APPEND_DURING_NDC"] = "\n## 00:05 | main\n\n- after midnight\n"
    env["STUB_MEMORY_FILE"] = str(remember / "now.md")

    _run(plugin, env, sid)
    for _ in range(40):
        if _day_stamp(project):
            break
        time.sleep(0.1)

    assert _day_stamp(project) == "2026-07-26", (
        "the bytes appended during compression were stamped with the day this "
        f"run started, not the day they arrived: {_day_stamp(project)!r}"
    )


# ── features.ndc_compression (#159) ──────────────────────────────────────────

def test_compression_can_be_switched_off(tmp_path):
    """The documented option was read nowhere; false did nothing.

    The only brake that worked was an unreachable pairing of
    cooldowns.ndc_seconds with a hand-written marker file — which is how this
    was found: a test asserting on now.md had to defeat compression by hand
    because the documented switch had no effect.
    """
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    cfg = json.loads(Path(env["REMEMBER_CONFIG"]).read_text())
    cfg["features"]["ndc_compression"] = False
    Path(env["REMEMBER_CONFIG"]).write_text(json.dumps(cfg))
    (plugin / "config.json").write_text(json.dumps(cfg))
    env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- work\n"

    _run(plugin, env, sid)

    remember = project / ".remember"
    assert not list(remember.glob("today-*.md")), "compression ran while disabled"
    assert "- work" in _read_now(project), "the entry should still be in now.md"


def test_compression_runs_when_the_option_is_absent(tmp_path):
    """Default is on — an install with no such key must be unaffected."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    cfg = json.loads(Path(env["REMEMBER_CONFIG"]).read_text())
    cfg["features"].pop("ndc_compression", None)
    Path(env["REMEMBER_CONFIG"]).write_text(json.dumps(cfg))
    (plugin / "config.json").write_text(json.dumps(cfg))
    env["STUB_HAIKU_TEXT"] = "## 12:00 | main\n\n- work\n"

    _run(plugin, env, sid)

    assert list((project / ".remember").glob("today-*.md")), (
        "compression did not run with the option absent"
    )


def test_a_false_boolean_option_is_readable_at_all(tmp_path):
    """jq's `//` treats false exactly like null.

    So `config ".key" true` returned true for a key explicitly set to false —
    every boolean option defaulting to true was impossible to switch off. Two
    shipped and documented options were affected: features.ndc_compression and
    features.recovery.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "features": {"ndc_compression": False, "recovery": False},
        "timezone": "UTC", "zero": 0, "empty": "",
        "null_string": "null", "real_null": None,
    }), encoding="utf-8")

    def read(key, default):
        # log.sh derives its log dir from REMEMBER_DIR and exits FATAL if it
        # cannot create it, so give it a real one.
        # _LIB_MEMORY_DIR_LOADED stops log.sh's lib-memory-dir.sh from
        # re-resolving REMEMBER_DIR (and REMEMBER_CONFIG) over the ones here.
        script = (f'export _LIB_MEMORY_DIR_LOADED=1\n'
                  f'export REMEMBER_DIR={tmp_path}\n'
                  f'export REMEMBER_CONFIG={cfg}\n'
                  f'source {REPO_ROOT}/scripts/log.sh\n'
                  f'config "{key}" "{default}"')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        return out.stdout.strip()

    assert read(".features.ndc_compression", "true") == "false", "false read back as the default"
    assert read(".features.recovery", "true") == "false", "false read back as the default"
    assert read(".timezone", "X") == "UTC", "a normal value regressed"
    assert read(".zero", "9") == "0", "0 is a value, not a missing key"
    assert read(".missing", "fallback") == "fallback", "a genuinely absent key must default"
    assert read(".deeply.missing", "fallback") == "fallback", "an absent path must default"
    # `jq -r` prints the bare word null for BOTH a JSON null and the string
    # "null", so comparing the printed value against "null" would discard a
    # legitimate value. The mapping is done inside jq, where the two differ.
    assert read(".null_string", "fallback") == "null", "the string \"null\" was discarded"
    assert read(".real_null", "fallback") == "fallback", "an explicit null must default"
