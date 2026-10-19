"""#1040: every hook read its stdin payload one byte per syscall.

All four hooks read the host's JSON payload with

    while IFS= read -r -t 1 _line || [ -n "$_line" ]; do HOOK_STDIN="$HOOK_STDIN$_line"; ...

bash reads a pipe unbuffered (it cannot seek back over what it over-read), so
that loop costs one read() per payload byte, and with `-t` one select() too.
On Cygwin/MSYS every non-tty fd counts as a pipe and every one of those calls
goes through the Cygwin layer: @gwangyong-kim measured 3.97 s for a 100 KB
payload and 24.7 s for 1 MB on the Windows MSYS bash 5.2.37, against 0.45 s
for a chunked `read -N` of the same bytes. The fix reads in `-N` chunks where
bash has it (>= 4.1) and keeps the line loop as the bash 3.2 fallback (stock
macOS).

Two kinds of test, and what each is evidence of:

* CORRECTNESS, every platform. Each hook's own extractor (pulled verbatim out
  of the hook, as tests/test_stdin_extractor_top_level_wins_447.py does) is fed
  the string the shared reader produced from a REAL PIPE -- never a file
  redirect: bash switches the `-t` timeout off for a regular file, which is
  how a redirect hides this bug. Every reader path (the chunked one, the
  REMEMBER_NO_READ_N=1 line-loop fallback, and /bin/bash 3.2 where the host has
  it) must hand the extractors exactly what the old loop did: the payload with
  its newlines removed. post-tool-hook.sh is also run end to end through a
  pipe, its session_id / transcript_path / cwd observed through the files it
  writes. session-start-hook.sh keeps the line loop (no room under the release
  size budget, and its payload is a few hundred bytes); its own loop, cut out
  of the hook, gets the same small-payload checks.
* TIMING, meaningful on windows-latest only. A ~1 MB payload piped into
  post-tool-hook.sh must finish under a bound the old loop cannot meet there
  (the reporter's whole-hook figure was 34-43 s). On macOS and Linux the old
  loop was already about 1 s for 1 MB, so the same test is only a smoke check
  there and is no evidence about Windows. A small-payload run under the same
  bound is the positive control: if it fails, the bound or the harness is
  wrong, not the reader.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash
from ._compiled_hooks import skip_if_dropped
from .env_cache import write_config
from .test_stdin_extractor_top_level_wins_447 import _extract_function

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash found (see tests/_bash_runner.py)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib-stdin.sh"
POST_TOOL = REPO_ROOT / "scripts" / "post-tool-hook.sh"
NL = chr(10)
BS = chr(92)
DQ = chr(34)
SESSION_ID = "1040abcd-0000-4000-8000-000000001040"

# (script, the variable its payload lands in, extractor, extractor style)
HOOKS = [
    pytest.param("scripts/post-tool-hook.sh", "HOOK_STDIN",
                 "_stdin_json_string_into", "into", id="post-tool"),
    pytest.param("scripts/session-start-hook.sh", "HOOK_STDIN",
                 "_stdin_json_string_into", "into", id="session-start"),
    pytest.param("scripts/session-end-hook.sh", "HOOK_STDIN",
                 "_stdin_json_string", "stdout", id="session-end"),
    pytest.param("scripts/user-prompt-hook.sh", "_HOOK_STDIN",
                 "_stdin_cwd_into", "cwd-only", id="user-prompt"),
]


def _fwd(p) -> str:
    return str(p).replace(BS, "/")


def _q(s: str) -> str:
    """A double-quoted bash word for a path with no quote, dollar or backslash."""
    return DQ + s + DQ


def _bash32():
    """/bin/bash when it is a 3.x bash (stock macOS), else None."""
    cand = Path("/bin/bash")
    if sys.platform == "win32" or not cand.is_file():
        return None
    out = subprocess.run([str(cand), "-c", "echo $BASH_VERSINFO"],
                         capture_output=True, text=True, check=False).stdout.strip()
    return str(cand) if out == "3" else None


BASH32 = _bash32()

# The line loop is the bash 3.2 path. On Windows it is the bug itself (25 s
# per MB), and its `-t 1` then fires every ~40 KB in the middle of a line, so
# it is neither run there nor meaningful there.
READERS = [
    pytest.param("chunked", id="chunked"),
    pytest.param("fallback", id="line-loop-fallback", marks=pytest.mark.skipif(
        sys.platform == "win32",
        reason="the line loop is the bash 3.2 fallback; on MSYS bash it is the #1040 bug")),
    pytest.param("bash32", id="bin-bash-3.2", marks=pytest.mark.skipif(
        BASH32 is None, reason="no /bin/bash 3.x on this host (stock macOS has one)")),
]


def _reader_env(reader: str, locale: str | None = None):
    env = dict(os.environ)
    env.pop("REMEMBER_NO_READ_N", None)
    if reader == "fallback":
        env["REMEMBER_NO_READ_N"] = "1"
    if locale:
        env["LC_ALL"] = locale
    return (BASH32 if reader == "bash32" else BASH), env


def _utf8_locale() -> str:
    return "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"


# --- payloads -----------------------------------------------------------------

CWD = "/work/proj-1040"
MB_TEXT = "é漢字\U0001f642 "


def _obj(content: str, cwd: str = CWD) -> dict:
    return {
        "session_id": SESSION_ID,
        "transcript_path": "/home/u/.claude/projects/p/" + SESSION_ID + ".jsonl",
        "cwd": cwd,
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "x.py"},
        "tool_response": {"content": content},
    }


def _long_lines(total: int) -> str:
    """Several hundred KB of tool output made of long lines (escaped in JSON)."""
    line = "L" * 4000 + NL
    return line * (total // len(line))


PAYLOADS = {
    "small": json.dumps(_obj("ok")),
    "large-long-lines": json.dumps(_obj(_long_lines(400 * 1024))),
    # Raw newlines in the payload itself: one JSON document over many lines.
    "large-pretty-printed": json.dumps(_obj(_long_lines(300 * 1024)), indent=2),
    "no-trailing-newline": json.dumps(_obj("ok")),
    "trailing-newline": json.dumps(_obj("ok")) + NL,
    "multibyte": json.dumps(_obj(MB_TEXT * 20000, cwd="/work/proj-é漢"),
                            ensure_ascii=False),
    "multibyte-small": json.dumps(_obj(MB_TEXT * 20, cwd="/work/proj-é漢"),
                                  ensure_ascii=False),
}
PAYLOAD_IDS = sorted(PAYLOADS)


def _expected_seen(payload: str) -> str:
    """What the old line loop handed the extractors: every newline dropped.

    `read` strips the delimiter and the loop concatenated the lines bare, so
    a trailing newline and the line breaks of a pretty-printed payload never
    reached the extraction. The new reader must keep that exactly."""
    return payload.replace(NL, "")


# --- the composition: shared reader + each hook's own extractor ----------------

CMDSUB_OPEN = "$" + "("


# session-start-hook.sh keeps the line loop (the comment at its read says
# why: a tiny payload, and no room under the 120 KiB release budget). The
# other three read through lib-stdin.sh.
LINE_LOOP_HOOKS = {"scripts/session-start-hook.sh"}
# A SessionStart payload is ids, paths and a source word: a few hundred bytes.
LINE_LOOP_MAX_PAYLOAD = 16 * 1024


def _read_site(script: str, var: str) -> str:
    """The hook's own stdin read, as the harness should run it.

    A hook on the shared reader gets the real library. A line-loop hook gets
    its own block, cut verbatim out of its text, so the test runs what
    ships rather than a copy that could drift from it."""
    text = (REPO_ROOT / script).read_text(encoding="utf-8")
    if "_remember_read_stdin_into " + var in text:
        return "source " + _q(_fwd(LIB)) + NL + "_remember_read_stdin_into " + var
    start = text.index(var + '=""' + NL + "if [ ! -t 0 ]; then")
    end = text.index(NL + "fi" + NL, start) + len(NL + "fi")
    return text[start:end]


def _harness(script: str, var: str, func: str, style: str, out: Path) -> str:
    body = _extract_function(script, func)
    o = _fwd(out)
    v = _q("$" + var)
    lines = [
        body,
        _read_site(script, var),
        "printf %s " + v + " > " + _q(o + "/seen"),
    ]
    fields = ["cwd"] if style == "cwd-only" else ["session_id", "transcript_path", "cwd"]
    for f in fields:
        dest = _q(o + "/" + f)
        if style == "into":
            lines.append(func + " _V " + f + " " + v + "; printf %s " + _q("$_V") + " > " + dest)
        elif style == "stdout":
            lines.append("_V=" + CMDSUB_OPEN + func + " " + f + " " + v + "); printf %s "
                         + _q("$_V") + " > " + dest)
        else:
            lines.append(func + " _V " + v + "; printf %s " + _q("$_V") + " > " + dest)
    return NL.join(lines) + NL


def _pipe_through(bash, env, script_text: str, stdin: bytes, tmp: Path, timeout=120):
    """Run SCRIPT_TEXT from a file with STDIN on a real pipe (subprocess.PIPE)."""
    path = tmp / "harness.sh"
    path.write_bytes(script_text.encode("utf-8"))
    return subprocess.run([bash, _fwd(path)], input=stdin, capture_output=True,
                          env=env, timeout=timeout, check=False)


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8") if p.exists() else "<missing>"


def _err(r) -> str:
    return r.stderr.decode("utf-8", "replace")[-1500:]


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("name", PAYLOAD_IDS)
@pytest.mark.parametrize("script, var, func, style", HOOKS)
def test_each_hook_extracts_the_same_fields_from_a_piped_payload(
        tmp_path, script, var, func, style, name, reader):
    skip_if_dropped(REPO_ROOT / script, func)
    payload = PAYLOADS[name]
    if script in LINE_LOOP_HOOKS and len(payload.encode("utf-8")) > LINE_LOOP_MAX_PAYLOAD:
        pytest.skip(script + " keeps the line loop and never gets a payload this large")
    obj = json.loads(payload)
    out = tmp_path / "out"
    out.mkdir()
    bash, env = _reader_env(reader, _utf8_locale())
    r = _pipe_through(bash, env, _harness(script, var, func, style, out),
                      payload.encode("utf-8"), tmp_path)
    assert r.returncode == 0, _err(r)
    seen = _read(out / "seen")
    want = _expected_seen(payload)
    assert seen == want, (
        "the payload the extractors saw differs from what the old line loop "
        "produced (len " + str(len(seen)) + " vs " + str(len(want)) + "); " + _err(r)
    )
    fields = ["cwd"] if style == "cwd-only" else ["session_id", "transcript_path", "cwd"]
    for f in fields:
        assert _read(out / f) == obj[f], (script, f, _err(r))


def _reader_only(out: Path, pre: str = "") -> str:
    return (pre + "source " + _q(_fwd(LIB)) + NL + "_remember_read_stdin_into GOT" + NL
            + "printf %s " + _q("[$GOT]") + " > " + _q(_fwd(out / "seen")) + NL)


@pytest.mark.parametrize("locale", ["C", "utf8"])
def test_a_multibyte_payload_over_one_chunk_survives_byte_for_byte(tmp_path, locale):
    """Over 1 MiB of multibyte text: under LC_ALL=C `-N` counts bytes, so a
    chunk boundary falls inside a character; the bytes must still join up.

    Chunked reader only. The line loop takes about a second per MB even on
    macOS, so its `-t 1` can fire inside this one long line, and a timeout
    that lands inside a multibyte character was seen to change a byte there
    (bash 5.3, UTF-8 locale, a loaded machine): a flaw of the old loop that
    this test would only report as noise."""
    reader = "chunked"
    payload = json.dumps(_obj("é漢" * 250000), ensure_ascii=False)
    raw = payload.encode("utf-8")
    assert len(raw) > 1048576
    out = tmp_path / "out"
    out.mkdir()
    bash, env = _reader_env(reader, "C" if locale == "C" else _utf8_locale())
    r = _pipe_through(bash, env, _reader_only(out), raw, tmp_path)
    assert r.returncode == 0, _err(r)
    assert (out / "seen").read_bytes() == b"[" + raw + b"]"


# Shapes that each once cost the chunked reader a quadratic tail on bash 5.3,
# found while writing the fix: a suffix strip of the trailing newline on a
# payload WITHOUT one (165 s for 1 MB), and a pattern substitution of the
# newlines of a pretty-printed payload (8 s for 840 KB, UTF-8 locale).
READ_SHAPES = {
    "1mb-no-trailing-newline": "x" * (1024 * 1024),
    "1mb-trailing-newline": "x" * (1024 * 1024) + NL,
    "pretty-printed-many-lines": json.dumps(
        {"c": ["ab" * 10] * 20000, "d": "L" * 300000}, indent=2),
}
READ_BOUND_SECONDS = 10.0


def _has_read_n(bash) -> bool:
    out = subprocess.run([bash, "-c", "echo $BASH_VERSION"], capture_output=True,
                         text=True, check=False).stdout.strip().split(".")
    try:
        return (int(out[0]), int(out[1])) >= (4, 1)
    except (ValueError, IndexError):
        return False


@pytest.mark.parametrize("shape", sorted(READ_SHAPES))
def test_the_chunked_reader_has_no_quadratic_tail(tmp_path, shape):
    """Not a claim about the bash 3.2 line loop: its concatenation is itself
    quadratic in the number of lines (108 s for this pretty-printed shape on
    /bin/bash 3.2, measured), as it always was. Claude Code sends one line."""
    if not _has_read_n(BASH):
        pytest.skip(str(BASH) + " has no read -N, so the chunked reader never runs there")
    payload = READ_SHAPES[shape]
    out = tmp_path / "out"
    out.mkdir()
    bash, env = _reader_env("chunked", _utf8_locale())
    t0 = time.monotonic()
    r = _pipe_through(bash, env, _reader_only(out), payload.encode("utf-8"), tmp_path)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, _err(r)
    assert (out / "seen").read_text(encoding="utf-8") == "[" + _expected_seen(payload) + "]"
    assert elapsed < READ_BOUND_SECONDS, f"{shape}: {elapsed:.2f} s on {sys.platform}"


@pytest.mark.parametrize("reader", READERS)
def test_an_empty_stdin_reads_as_empty(tmp_path, reader):
    """And the variable is cleared, not left holding a stale value."""
    out = tmp_path / "out"
    out.mkdir()
    bash, env = _reader_env(reader)
    r = _pipe_through(bash, env, _reader_only(out, pre="GOT=stale" + NL), b"", tmp_path)
    assert r.returncode == 0, _err(r)
    assert (out / "seen").read_bytes() == b"[]"


@pytest.mark.parametrize("reader", READERS)
def test_a_pipe_held_open_with_nothing_in_it_still_costs_about_a_second(tmp_path, reader):
    """The `-t 1` bound survives: a host that never writes or closes stdin
    must not hang the hook. The positive half: the reader does return, with
    an empty value, while the writer still holds the pipe open."""
    out = tmp_path / "out"
    out.mkdir()
    bash, env = _reader_env(reader)
    path = tmp_path / "held.sh"
    path.write_bytes(_reader_only(out).encode("utf-8"))
    proc = subprocess.Popen([bash, _fwd(path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    t0 = time.monotonic()
    try:
        while proc.poll() is None and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        elapsed = time.monotonic() - t0
        finished = proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdin.close()
        proc.wait()
    assert finished, "the reader blocked on an open, silent pipe for 20 s"
    assert proc.returncode == 0, proc.stderr.read().decode("utf-8", "replace")[-800:]
    assert (out / "seen").read_bytes() == b"[]"
    assert elapsed < 10, elapsed


# --- the hooks call the shared reader --------------------------------------------

@pytest.mark.parametrize("script, var, func, style", HOOKS)
def test_every_hook_reads_stdin_through_the_shared_reader(script, var, func, style):
    """No hook keeps a private copy of the byte-per-syscall loop -- except
    session-start-hook.sh, deliberately, and that exception is pinned too:
    moving it onto the library has to go past the release size budget."""
    text = (REPO_ROOT / script).read_text(encoding="utf-8")
    if script in LINE_LOOP_HOOKS:
        assert "_remember_read_stdin_into" not in text, script
        assert "read -r -t 1 _line" in text, script
        return
    assert "_remember_read_stdin_into " + var in text, script
    assert "read -r -t 1 _line" not in text, script


# --- post-tool-hook.sh end to end, through a pipe -------------------------------

LINE = json.dumps({"type": "assistant", "message": {"content": "x"}}) + NL


def _fixture(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    # A threshold no 10-line transcript can reach: no save fork afterwards.
    write_config(remember / "config.json",
                 dict(thresholds=dict(delta_lines_trigger=100000)))  # noqa: C408
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    transcript = tdir / (SESSION_ID + ".jsonl")
    transcript.write_text(LINE * 10, encoding="utf-8")
    return home, project, remember, transcript


def _post_tool_payload(project, transcript, content, *, pretty=False, nl=False):
    obj = _obj(content, cwd=_fwd(project))
    obj["transcript_path"] = _fwd(transcript)
    text = json.dumps(obj, indent=2 if pretty else None, ensure_ascii=False)
    return text + (NL if nl else "")


def _run_post_tool(tmp_path, home, payload: str):
    systmp = tmp_path / "systmp"
    systmp.mkdir(exist_ok=True)
    bash, env = _reader_env("chunked")
    env.update(HOME=_fwd(home), CLAUDE_PLUGIN_ROOT=_fwd(REPO_ROOT), TMPDIR=_fwd(systmp))
    # cwd must come from the payload, so the project is not handed over by env.
    for stale in ("CLAUDE_PROJECT_DIR", "REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED",
                  "REMEMBER_TZ", "REMEMBER_NESTED_SUMMARIZER", "REMEMBER_HOOK_CWD"):
        env.pop(stale, None)
    t0 = time.monotonic()
    r = subprocess.run([bash, _fwd(POST_TOOL)], env=env, input=payload.encode("utf-8"),
                       capture_output=True, timeout=300, check=False)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, _err(r)
    return r, elapsed


def _assert_post_tool_parsed(remember: Path, transcript: Path, r):
    alive = remember / "tmp" / "capture-alive.d" / SESSION_ID
    sidecar = remember / "tmp" / ("transcript-scan." + SESSION_ID)
    detail = (" tmp/: " + repr(sorted(p.name for p in (remember / "tmp").iterdir()))
              + " stderr: " + _err(r))
    # cwd: the store is the payload's cwd (CLAUDE_PROJECT_DIR is unset), and
    # session_id: the capture-alive marker there is named after it.
    assert alive.exists(), "no capture-alive marker for the payload's session_id;" + detail
    # transcript_path: the #913 scan sidecar was written for that transcript.
    assert sidecar.is_file(), "no transcript-scan sidecar for transcript_path;" + detail
    assert int(sidecar.read_text(encoding="utf-8").split()[0]) == len(transcript.read_bytes())


E2E = {
    "small": dict(content="ok"),  # noqa: C408
    "large-long-lines": dict(content=_long_lines(400 * 1024)),  # noqa: C408
    "pretty-printed": dict(content=_long_lines(100 * 1024), pretty=True),  # noqa: C408
    "trailing-newline": dict(content="ok", nl=True),  # noqa: C408
    "multibyte": dict(content=MB_TEXT * 5000),  # noqa: C408
}


@pytest.mark.parametrize("name", sorted(E2E))
def test_post_tool_hook_end_to_end_through_a_pipe(tmp_path, name):
    home, project, remember, transcript = _fixture(tmp_path)
    payload = _post_tool_payload(project, transcript, **E2E[name])
    r, _ = _run_post_tool(tmp_path, home, payload)
    _assert_post_tool_parsed(remember, transcript, r)


def test_post_tool_hook_with_an_empty_stdin_exits_cleanly(tmp_path):
    """No payload, so no cwd and no session: exit 0 and write no marker."""
    home, _project, remember, _t = _fixture(tmp_path)
    _run_post_tool(tmp_path, home, "")
    assert not (remember / "tmp" / "capture-alive.d").exists()


# --- timing: the #1040 symptom ----------------------------------------------------

# Generous on purpose. The old loop took 34-43 s for the whole hook with a 1 MB
# payload on the reporter's machine, 24.7 s of it the read alone; the chunked
# read took 0.45 s there. A shared CI runner is slower than a desktop and the
# rest of the hook still costs a few seconds on Windows, so the bound sits well
# above that and well below the old loop's cost.
BOUND_SECONDS = 15.0


def _timed(tmp_path, content):
    home, project, remember, transcript = _fixture(tmp_path)
    payload = _post_tool_payload(project, transcript, content)
    r, elapsed = _run_post_tool(tmp_path, home, payload)
    _assert_post_tool_parsed(remember, transcript, r)
    return len(payload.encode("utf-8")), elapsed


def test_timing_control_a_small_payload_is_under_the_bound(tmp_path):
    """POSITIVE CONTROL for the bound below: if this fails, the bound is wrong
    for this runner, and the 1 MB verdict means nothing."""
    size, elapsed = _timed(tmp_path, "ok")
    assert elapsed < BOUND_SECONDS, f"a small payload of {size} B took {elapsed:.2f} s"


def test_a_one_megabyte_payload_through_a_pipe_is_under_the_bound(tmp_path):
    """THE #1040 CASE on windows-latest; a smoke check on macOS and Linux."""
    size, elapsed = _timed(tmp_path, "x" * (1024 * 1024))
    assert size > 1024 * 1024
    assert elapsed < BOUND_SECONDS, (
        f"a {size} B payload piped to post-tool-hook.sh took {elapsed:.2f} s, bound "
        f"{BOUND_SECONDS:.0f} s, on {sys.platform}: the stdin read is paying per byte "
        "again, #1040"
    )
