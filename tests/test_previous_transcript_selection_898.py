"""#898: pin WHICH transcript the session-start hook names as the previous
session -- PREV_JSONL and PREV_ID -- independently of how the lookup is
written.

The plugin directory's static scanner could not follow the previous-
transcript lookup in scripts/session-start-hook.sh, and the fix for that is a
rewrite of the lookup itself. A rewrite is only safe against a pin that does
not care about the old shape: these tests extract the lookup REGION of the
real shipped file (every definition between the exclusion-cap assignment and
the deferred-phase banner) plus the real call-site block inside
_remember_deferred_phase that turns it into PREV_JSONL / PREV_ID, and run
them together. They never name a lookup function, so they held on the code
before the rewrite and must hold, unchanged, after it.

Both call-site modes are pinned:

* with CURRENT_SESSION_ID -- the newest transcript that is not this
  session's own, by id;
* without it -- positional: the newest transcript is assumed to be this
  session's own, and the next one is the previous session.

Ordering is by mtime through bash's own `-nt`, and among EQUAL mtimes the
earlier file in glob order ranks first -- the tie fixtures below pin that,
including across a run of pluginless-SDK exclusions, so a rewrite cannot
quietly change which of two same-second transcripts is "the previous one".
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash on this host (#432)"
)

_PLUGINLESS_SDK_LINE = '{"entrypoint":"sdk-py"}\n'
_PLAIN_LINE = "{}\n"
_BACKSLASH = chr(92)


def _between(source: str, start: str, end: str) -> str:
    assert source.count(start) == 1, f"marker not unique in hook: {start!r}"
    i = source.index(start) + len(start)
    j = source.index(end, i)
    return source[i:j]


def _function_body(source: str, name: str) -> str:
    start = source.index(f"\n{name}() {{\n")
    end = source.index("\n}\n", start) + len("\n}")
    return source[start + 1 : end]


def _lookup_script() -> str:
    source = SESSION_START.read_text(encoding="utf-8")
    deps = "\n".join(
        _function_body(source, n)
        for n in ("_stdin_json_string", "_transcript_is_pluginless_sdk")
    )
    region = "_PREV_TRANSCRIPT_EXCLUDE_CAP=" + _between(
        source,
        "\n_PREV_TRANSCRIPT_EXCLUDE_CAP=",
        "\n# ── Deferred: previous-session recovery",
    )
    call_site = _between(
        source,
        "\n_remember_write_case_divergence\n",
        "\n# Asked ONCE, and before recovery forks",
    )
    return (
        "log() { printf 'LOG[%s]: %s\\n' \"$1\" \"$2\" >&2; }\n"
        + deps + "\n" + region + "\n"
        + 'SESSIONS_DIR="$1"\nCURRENT_SESSION_ID="$2"\n'
        + call_site + "\n"
        + "printf 'PREV_JSONL=%s\\n' \"$PREV_JSONL\"\n"
        + "printf 'PREV_ID=%s\\n' \"$PREV_ID\"\n"
    )


def _run(sessions: Path, current_id: str) -> subprocess.CompletedProcess:
    """Script on stdin, not `-c`: Git for Windows' bash re-parses its command
    line (#829)."""
    result = subprocess.run(
        [BASH, "-s", "--", str(sessions), current_id],
        input=_lookup_script(),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    # An extracted helper missing from the sandbox would print this and the
    # script would carry on to a plausible-looking empty answer.
    assert "command not found" not in result.stderr, result.stderr
    return result


def _lookup(sessions: Path, current_id: str = "") -> tuple[str, str]:
    result = _run(sessions, current_id)
    lines = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    return lines.get("PREV_JSONL", ""), lines.get("PREV_ID", "")


def _norm(path) -> str:
    return str(path).replace(_BACKSLASH, "/")


def _make(dir_: Path, spec: dict, pluginless=frozenset()) -> None:
    """spec maps an id to a relative mtime in seconds; bigger is newer."""
    base = int(time.time()) - 10_000
    for sid, age in spec.items():
        p = dir_ / f"{sid}.jsonl"
        p.write_text(_PLUGINLESS_SDK_LINE if sid in pluginless else _PLAIN_LINE)
        os.utime(p, (base + age, base + age))


@pytest.fixture
def sessions(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    return d


# -- nothing to find ----------------------------------------------------------


def test_missing_directory_names_no_previous_session(tmp_path):
    assert _lookup(tmp_path / "nope", "cur") == ("", "")
    assert _lookup(tmp_path / "nope") == ("", "")


def test_empty_directory_names_no_previous_session(sessions):
    assert _lookup(sessions, "cur") == ("", "")
    assert _lookup(sessions) == ("", "")


def test_non_transcript_files_are_ignored(sessions):
    (sessions / "notes.json").write_text("{}")
    (sessions / "old.jsonl.bak").write_text("{}")
    _make(sessions, {"real": 1})
    os.utime(sessions / "notes.json", None)
    os.utime(sessions / "old.jsonl.bak", None)
    prev_jsonl, prev_id = _lookup(sessions, "cur")
    assert prev_id == "real"
    assert _norm(prev_jsonl) == _norm(sessions / "real.jsonl")


# -- only one transcript -------------------------------------------------------


def test_only_transcript_is_current_names_nothing(sessions):
    _make(sessions, {"cur": 1})
    assert _lookup(sessions, "cur") == ("", "")


def test_only_transcript_is_not_current_names_it(sessions):
    """Startup: the current session's transcript does not exist yet."""
    _make(sessions, {"prev": 1})
    prev_jsonl, prev_id = _lookup(sessions, "cur")
    assert prev_id == "prev"
    assert _norm(prev_jsonl) == _norm(sessions / "prev.jsonl")


def test_only_transcript_without_id_names_nothing(sessions):
    """Positional mode treats the newest as this session's own."""
    _make(sessions, {"only": 1})
    assert _lookup(sessions) == ("", "")


# -- current session excluded --------------------------------------------------


def test_current_newest_is_skipped(sessions):
    _make(sessions, {"a": 1, "b": 2, "cur": 3})
    assert _lookup(sessions, "cur")[1] == "b"


def test_current_in_the_middle_does_not_hide_the_newest(sessions):
    _make(sessions, {"a": 1, "cur": 2, "c": 3})
    assert _lookup(sessions, "cur")[1] == "c"


def test_without_id_the_newest_is_skipped_positionally(sessions):
    _make(sessions, {"a": 1, "b": 2, "c": 3})
    assert _lookup(sessions)[1] == "b"


# -- ties -----------------------------------------------------------------------
#
# Interleaved fixture: glob order a b c d e, mtimes 150 200 150 200 100.
# Rank (newest first, equal mtimes in glob order): b d a c e.

_INTERLEAVED = {"a": 150, "b": 200, "c": 150, "d": 200, "e": 100}


@pytest.mark.parametrize(
    "pluginless, expected",
    [
        (set(), "b"),
        ({"b"}, "d"),
        ({"b", "d"}, "a"),
        ({"b", "d", "a"}, "c"),
        ({"b", "d", "a", "c"}, "e"),
        ({"b", "d", "a", "c", "e"}, ""),
    ],
)
def test_ties_with_id_follow_glob_order_across_exclusions(sessions, pluginless, expected):
    _make(sessions, _INTERLEAVED, pluginless)
    assert _lookup(sessions, "cur")[1] == expected


@pytest.mark.parametrize(
    "pluginless, expected",
    [
        (set(), "d"),
        ({"b"}, "d"),  # the positional "own" is never content-checked
        ({"d"}, "a"),
        ({"d", "a"}, "c"),
        ({"d", "a", "c"}, "e"),
        ({"d", "a", "c", "e"}, ""),
    ],
)
def test_ties_without_id_follow_glob_order_across_exclusions(sessions, pluginless, expected):
    _make(sessions, _INTERLEAVED, pluginless)
    assert _lookup(sessions)[1] == expected


def test_tie_with_current_excluded(sessions):
    _make(sessions, {"a": 5, "cur": 5, "z": 5})
    assert _lookup(sessions, "cur")[1] == "a"
    assert _lookup(sessions, "a")[1] == "cur"


# -- paths with spaces ----------------------------------------------------------


def test_spaces_in_directory_and_file_names(tmp_path):
    d = tmp_path / "my sessions dir"
    d.mkdir()
    _make(d, {"old one": 1, "prev one": 2, "cur one": 3})
    prev_jsonl, prev_id = _lookup(d, "cur one")
    assert prev_id == "prev one"
    assert _norm(prev_jsonl) == _norm(d / "prev one.jsonl")
    assert _lookup(d)[1] == "prev one"


# -- the exclusion cap -----------------------------------------------------------
# Must-fire / must-not-fire pair: past the cap the lookup gives up AND says so;
# below it, it walks the pluginless run and stays quiet.


def test_cap_gives_up_and_logs(sessions):
    spec = {f"p{i:02d}": 100 + i for i in range(25)}
    spec["real"] = 1
    _make(sessions, spec, pluginless={k for k in spec if k != "real"})
    result = _run(sessions, "cur")
    assert "PREV_ID=\n" in result.stdout
    assert "LOG[hook]: WARNING" in result.stderr


def test_below_the_cap_finds_the_real_one_without_logging(sessions):
    spec = {f"p{i:02d}": 100 + i for i in range(5)}
    spec["real"] = 1
    _make(sessions, spec, pluginless={k for k in spec if k != "real"})
    result = _run(sessions, "cur")
    assert "PREV_ID=real\n" in result.stdout
    assert "LOG[" not in result.stderr
