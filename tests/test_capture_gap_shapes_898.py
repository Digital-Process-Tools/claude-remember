"""#898 round 20: the capture-gap part of session-start-hook.sh, rewritten
so the directory's scanner can follow the file -- with its behaviour pinned.

The portal kept listing scripts/session-start-hook.sh as "a script it could
not follow" after every other hook cleared. Bisection of the compiled hook
narrowed it to three neighbouring shapes, which only cleared when ALL of
them changed at once (the scanner carries parse state from one to the next):

  1. session_was_saved()'s jq-less branch fed a multi-line Python program
     to `python -` as a single-quoted here-string, with '"'"' quote
     splices in its comments. The program now lives in
     scripts/session_saved.py and is called by path.
  2. capture_was_seen()'s id-shape tests used a literal `.`/`..` strip and a
     negated bracket class. They now compare against a dot built with
     printf and match a positive ERE.
  3. The capture-seen prune globbed into an array and piped
     `ls -t | tail | while read`. It is now a counted glob loop that removes
     the oldest entry by mtime (bash's own `-nt`), one at a time.

The behaviour tests here run the functions' own text, extracted from the
hook, under every bash this machine has (the default one and /bin/bash, 3.2
on macOS). They are characterisation tests: they pass on the old shapes and
must still pass on the new ones. The shape tests at the bottom, and the
prune tests (which call the new helper by name), are the ones that went
red first.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
HOOK = SCRIPTS / "session-start-hook.sh"
DETECT = SCRIPTS / "detect-tools.sh"
NOT_SHIPPED = {"bench-slug.sh", "run-tests.sh"}
SHIPPED_SH = sorted(p for p in SCRIPTS.glob("*.sh") if p.name not in NOT_SHIPPED) + sorted(
    (REPO_ROOT / "hooks.d").rglob("*.sh"))


def _bashes() -> list:
    found = []
    default = resolve_bash()
    if default:
        found.append(default)
    if os.name != "nt" and Path("/bin/bash").is_file() and (
            not default
            or os.path.realpath(shutil.which(default) or default) != os.path.realpath("/bin/bash")):
        found.append("/bin/bash")
    return found


BASHES = _bashes()
pytestmark = pytest.mark.skipif(not BASHES, reason="no usable bash found")
PYTHON_NAME = "python3" if shutil.which("python3") else ("python" if shutil.which("python") else None)


def _function(text: str, name: str) -> str:
    m = re.search(r"^" + re.escape(name) + r"\(\) \{\n.*?^\}\n", text, re.DOTALL | re.MULTILINE)
    assert m, f"{name}() not found in source"
    return m.group(0)


def _saved_query(text: str) -> str:
    m = re.search(r"^SAVED_QUERY=.*\n", text, re.MULTILINE)
    assert m, "SAVED_QUERY not found"
    return m.group(0)


def _run(bash: str, script: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([bash, "-c", script], capture_output=True, text=True, env=env, timeout=60, check=False)


# -- session_was_saved -----------------------------------------------------

SAVED_CASES = [
    # (id, file content -- None for a missing file, a str for raw bytes -- expected)
    ("S", {"sessions": {"S": 5}}, "saved"),
    ("S", {"sessions": {"S": 5.0}}, "saved"),
    ("S", {"sessions": {"S": True}}, "unsaved"),
    ("S", {"sessions": {"S": None}}, "unsaved"),
    ("S", {"sessions": {"S": 1.5}}, "unsaved"),
    ("S", {"sessions": {"S": "5"}}, "unsaved"),
    ("S", {"sessions": {"T": 5}}, "unsaved"),
    ("S", {"session": "S", "line": 3}, "saved"),
    ("S", {"session": "S", "line": "3"}, "unsaved"),
    ("S", {"session": "S", "line": False}, "unsaved"),
    ("S", {"session": "T", "line": 3}, "unsaved"),
    ("S", {"sessions": None, "session": "S", "line": 3}, "saved"),
    # .sessions present but not an object: jq raises, so the legacy pair
    # is never reached -- unsaved, and the fallback mirrors that.
    ("S", {"sessions": [1], "session": "S", "line": 3}, "unsaved"),
    ("S", [1, 2], "unsaved"),
    ("S", "not json {", "unsaved"),
    ("S", None, "unsaved"),
    ("", {"sessions": {"": 5}}, "unsaved"),
]


def _saved_harness(mode: str) -> str:
    text = HOOK.read_text(encoding="utf-8")
    detect = DETECT.read_text(encoding="utf-8")
    parts = [
        _function(detect, "_remember_run_python"),
        _function(detect, "_remember_run_jq"),
        '_remember_python() { [ -n "${PYTHON:-}" ]; }\n',
        "JQ=" + ("jq" if mode == "jq" else "_jq_fallback") + "\n",
        _saved_query(text),
        _function(text, "session_was_saved"),
        'if session_was_saved "$SID"; then echo saved; else echo unsaved; fi\n',
    ]
    return "".join(parts)


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("mode", ["jq", "python"])
def test_session_was_saved_table(tmp_path, bash, mode):
    if mode == "jq" and not shutil.which("jq"):
        pytest.skip("jq not on PATH")
    if mode == "python" and not PYTHON_NAME:
        pytest.skip("no python on PATH under a name _remember_run_python knows")
    script = _saved_harness(mode)
    got = []
    for i, (sid, content, _want) in enumerate(SAVED_CASES):
        f = tmp_path / f"last-save-{i}.json"
        if isinstance(content, str):
            f.write_text(content, encoding="utf-8")
        elif content is not None:
            f.write_text(json.dumps(content), encoding="utf-8")
        r = _run(bash, script, {
            "LAST_SAVE_FILE": f.as_posix(), "SID": sid,
            "PYTHON": PYTHON_NAME or "", "_HOOK_DIR": SCRIPTS.as_posix(),
        })
        got.append(r.stdout.strip() or f"<no output; stderr={r.stderr!r}>")
    want = [w for _, _, w in SAVED_CASES]
    assert got == want, list(zip(SAVED_CASES, got))


@pytest.mark.parametrize("bash", BASHES)
def test_session_was_saved_python_path_without_python_is_unsaved(tmp_path, bash):
    """The jq-less branch with no python at all answers unsaved, never errors.
    (The table above is this test's positive control: same file, saved.)"""
    f = tmp_path / "last-save.json"
    f.write_text(json.dumps({"sessions": {"S": 5}}), encoding="utf-8")
    r = _run(bash, _saved_harness("python"), {
        "LAST_SAVE_FILE": f.as_posix(), "SID": "S", "PYTHON": "", "_HOOK_DIR": SCRIPTS.as_posix()})
    assert r.stdout.strip() == "unsaved", r.stderr


def test_session_saved_program_is_a_file_called_by_path():
    """The jq-less program is scripts/session_saved.py, run by path."""
    body = _function(HOOK.read_text(encoding="utf-8"), "session_was_saved")
    assert (SCRIPTS / "session_saved.py").is_file()
    assert "session_saved.py" in body
    assert "<<<" not in body and "_remember_run_python -" not in body, body


# -- capture_was_seen ------------------------------------------------------

# id -> must the marker store vouch for it (when a marker by that name exists)?
ID_TABLE = {
    ".": False, "..": False, "...": True, "abc": True, "a.b": True,
    "a/b": False, "a b": False, "-": True, "._-": True, "é": False,
}


def _seen_harness() -> str:
    text = HOOK.read_text(encoding="utf-8")
    return (
        'SEEN_ID=""; PREV_ID=""; PREV_WAS_SAVED=""\n'
        "session_was_saved() { return 1; }\n"
        + _function(text, "capture_was_seen")
        + 'if capture_was_seen "$ID"; then echo seen; else echo unseen; fi\n'
    )


@pytest.mark.parametrize("bash", BASHES)
def test_capture_was_seen_id_table(tmp_path, bash):
    store = tmp_path / "capture-alive.d"
    store.mkdir()
    (store / "a").mkdir()
    for name in ID_TABLE:
        if name in (".", ".."):
            continue  # store/. and store/.. exist by themselves
        try:
            (store / name).write_text("x", encoding="utf-8")
        except OSError:
            pytest.skip(f"filesystem cannot hold a marker named {name!r}")
    script = _seen_harness()
    got = {}
    for name in ID_TABLE:
        r = _run(bash, script, {"CAPTURE_SEEN_DIR": store.as_posix(), "ID": name,
                                "LANG": "en_US.UTF-8"})
        got[name] = r.stdout.strip() == "seen"
    assert got == ID_TABLE


@pytest.mark.parametrize("bash", BASHES)
def test_capture_was_seen_needs_a_marker(tmp_path, bash):
    """Positive control for the table: a well-formed id with no marker, no
    legacy slot and no save record is not vouched for -- nor is an empty id."""
    store = tmp_path / "capture-alive.d"
    store.mkdir()
    script = _seen_harness()
    for name in ("abc", ""):
        r = _run(bash, script, {"CAPTURE_SEEN_DIR": store.as_posix(), "ID": name})
        assert r.stdout.strip() == "unseen", (name, r.stderr)


# -- capture-seen prune ----------------------------------------------------

def _prune(bash: str, store: Path, keep: int) -> subprocess.CompletedProcess:
    text = HOOK.read_text(encoding="utf-8")
    script = _function(text, "_remember_prune_keep_newest") + '_remember_prune_keep_newest "$D" "$K"\n'
    return _run(bash, script, {"D": store.as_posix(), "K": str(keep)})


def _seed(store: Path, names_oldest_first: list) -> None:
    now = int(time.time())
    n = len(names_oldest_first)
    for i, name in enumerate(names_oldest_first):
        p = store / name
        if not p.exists():
            p.write_text("x", encoding="utf-8")
        os.utime(p, (now - 10 * (n - i), now - 10 * (n - i)))


@pytest.mark.parametrize("bash", BASHES)
def test_prune_keeps_the_newest_by_mtime(tmp_path, bash):
    store = tmp_path / "capture-alive.d"
    store.mkdir()
    # Name order deliberately disagrees with mtime order.
    oldest_first = ["m", "c", "x", "a", "q", "b", "z", "d", "k", "e", "y", "f"]
    _seed(store, oldest_first)
    r = _prune(bash, store, 5)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in store.iterdir()) == sorted(oldest_first[-5:])


@pytest.mark.parametrize("bash", BASHES)
def test_prune_leaves_an_at_or_under_threshold_store_alone(tmp_path, bash):
    store = tmp_path / "capture-alive.d"
    store.mkdir()
    _seed(store, ["c", "a", "b"])
    assert _prune(bash, store, 3).returncode == 0
    assert _prune(bash, store, 5).returncode == 0
    assert sorted(p.name for p in store.iterdir()) == ["a", "b", "c"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _prune(bash, empty, 0).returncode == 0
    assert _prune(bash, tmp_path / "absent", 0).returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="rm -f on a directory is the unremovable entry used here")
@pytest.mark.parametrize("bash", BASHES)
def test_prune_skips_an_unremovable_entry_and_terminates(tmp_path, bash):
    """An entry rm -f cannot remove (a directory) is skipped, as the old
    `ls -t | tail | while rm` skipped it: the newest KEEP stay, every other
    file goes, and the loop ends."""
    store = tmp_path / "capture-alive.d"
    store.mkdir()
    (store / "dir").mkdir()
    oldest_first = ["dir", "o1", "o2", "n1", "n2", "n3"]
    _seed(store, oldest_first)
    r = _prune(bash, store, 3)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in store.iterdir()) == ["dir", "n1", "n2", "n3"]


def test_the_hook_prunes_with_the_helper():
    text = HOOK.read_text(encoding="utf-8")
    assert re.search(r'^_remember_prune_keep_newest "\$CAPTURE_SEEN_DIR" "\$CAPTURE_SEEN_KEEP"$', text, re.MULTILINE)


# -- scanner shapes (red first) --------------------------------------------

def _multiline_here_string_programs(text: str) -> list:
    """Lines where a `<<< '` single-quoted here-string opens and does not
    close on the same line -- a multi-line program in a shell string."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        i = line.find("<<< '")
        if i >= 0 and line[i + 5:].count("'") % 2 == 0:
            hits.append(n)
    return hits


SPLICE = "'" + '"' + "'" + '"' + "'"


def test_shape_detectors_fire_on_their_own_shapes():
    """Positive controls for the shipped-file assertions below."""
    assert _multiline_here_string_programs("python3 - <<< 'import sys\nprint(1)\n'\n") == [1]
    assert _multiline_here_string_programs("x <<< 'one line'\n") == []
    assert SPLICE in "echo 'it" + SPLICE + "s'"


@pytest.mark.parametrize("path", SHIPPED_SH, ids=lambda p: p.name)
def test_no_multiline_here_string_program(path):
    hits = _multiline_here_string_programs(path.read_text(encoding="utf-8"))
    assert not hits, f"{path.name}: multi-line `<<< '` program at lines {hits}; move it to a .py file"


@pytest.mark.parametrize("path", SHIPPED_SH, ids=lambda p: p.name)
def test_no_quote_splice(path):
    lines = [n for n, l in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if SPLICE in l]
    assert not lines, f"{path.name}: quote splice at lines {lines}"


def test_capture_was_seen_has_no_dot_strip_or_negated_class():
    body = _function(HOOK.read_text(encoding="utf-8"), "capture_was_seen")
    assert "${1#.}" not in body and "${1#..}" not in body, body
    assert "[!" not in body, body
    assert "local LC_ALL=C" in body


def test_capture_seen_prune_has_no_ls_pipeline():
    text = HOOK.read_text(encoding="utf-8")
    assert 'ls -t "$CAPTURE_SEEN_DIR"' not in text
    assert '("$CAPTURE_SEEN_DIR"/*)' not in text
