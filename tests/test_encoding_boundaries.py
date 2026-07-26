"""Real process-boundary encoding tests (#91 mojibake, #97 lone-surrogate crash).

These bugs live where raw bytes become Python ``str``: the stdin pipe feeding
``parse-haiku`` and the ``claude`` subprocess decode in ``call_haiku``. The
existing suite mocks both boundaries (``StringIO`` stdin, ``MagicMock``
subprocess), so the decode never runs and a green Windows matrix proved
nothing about encoding.

To reproduce on ANY OS (incl. the Linux/macOS CI legs whose default locale is
UTF-8), we force a non-UTF-8 locale on the child process:
``PYTHONUTF8=0 PYTHONCOERCECLOCALE=0 LC_ALL=C`` → Python decodes pipes/subprocess
output as ascii+surrogateescape, exactly as a legacy Windows cp1252 box does.
Under that locale, UTF-8 input must still round-trip byte-identical.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

def _ambient_env() -> dict[str, str]:
    """os.environ minus every GIT_* var.

    conftest's _sanitize_ambient_git_env strips those per test, but the dicts
    below are built at import time — before any fixture runs — so a GIT_DIR
    leaked into the launching shell would be baked in and handed to every
    subprocess these constants feed. Harmless while pipeline.shell never shells
    out to git; a silent reopening of the bug the moment it does. Filter on the
    prefix rather than restating conftest's list, so the two cannot drift.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


# A non-UTF-8 locale that survives PEP 538 C-locale coercion — makes Python's
# stdin/subprocess codec ascii+surrogateescape on any OS, mimicking cp1252.
FORCED_NON_UTF8_ENV = {
    **_ambient_env(),
    "PYTHONUTF8": "0",
    "PYTHONCOERCECLOCALE": "0",
    "LC_ALL": "C",
    "LANG": "C",
    "LC_CTYPE": "C",
}
FORCED_NON_UTF8_ENV.pop("PYTHONIOENCODING", None)  # would otherwise force utf-8

ARROW = "→"   # → : E2 86 92 — the canonical mojibake/crash trigger
DASH = "—"    # — : E2 80 94


def _run_shell(args: list[str], stdin_bytes: bytes) -> subprocess.CompletedProcess:
    """Run `python -m pipeline.shell <args>` as a real child under the forced
    non-UTF-8 locale, piping raw bytes to its stdin."""
    return subprocess.run(
        [sys.executable, "-m", "pipeline.shell", *args],
        input=stdin_bytes,
        capture_output=True,
        cwd=str(REPO_ROOT),
        env=FORCED_NON_UTF8_ENV,
        timeout=30,
    )


# ── Boundary 1: the stdin pipe into parse-haiku (#91 #1, #97 the observed crash)

def test_parse_haiku_pipe_roundtrips_utf8_under_non_utf8_locale(tmp_path):
    """UTF-8 bytes piped into parse-haiku must reach the output file
    byte-identical — not mojibake (#91) and not a crash (#97)."""
    out = tmp_path / "haiku.txt"
    payload = json.dumps(
        {"result": f"arrow {ARROW} dash {DASH}",
         "input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0},
        ensure_ascii=False,
    ).encode("utf-8")

    result = _run_shell(["parse-haiku", str(out)], payload)

    assert result.returncode == 0, (
        f"parse-haiku crashed under non-UTF-8 locale (#97).\n"
        f"stderr:\n{result.stderr.decode('utf-8', 'replace')}"
    )
    assert out.read_text(encoding="utf-8") == f"arrow {ARROW} dash {DASH}"


def test_parse_haiku_pipe_handles_cjk_under_non_utf8_locale(tmp_path):
    """Non-Latin UTF-8 (CJK) must not be silently dropped — cp1252's unmapped
    bytes would raise UnicodeDecodeError and lose the save entirely (#91)."""
    out = tmp_path / "haiku.txt"
    payload = json.dumps(
        {"result": "日本語 test", "input_tokens": 1, "output_tokens": 1,
         "cache_read_input_tokens": 0},
        ensure_ascii=False,
    ).encode("utf-8")

    result = _run_shell(["parse-haiku", str(out)], payload)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert out.read_text(encoding="utf-8") == "日本語 test"


# ── Boundary 2: the claude subprocess stdout decode in call_haiku (#91 #2)

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake `claude` is a shebang+chmod script; Windows CreateProcess only "
    "runs .exe, not a bare executable script. The encoding kwarg is verified "
    "cross-platform by test_call_haiku_passes_utf8_encoding below; the real "
    "decode under a forced locale runs on the POSIX CI legs.",
)
def test_call_haiku_decodes_utf8_stdout_under_non_utf8_locale(tmp_path):
    """call_haiku must decode the claude CLI's UTF-8 stdout as UTF-8 regardless
    of locale — `subprocess.run(text=True)` without encoding uses cp1252/ascii."""
    # Stub `claude` that emits UTF-8 JSON containing the arrow.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "claude"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.buffer.write("
        "'{\"result\":\"arrow \\u2192\",\"input_tokens\":1,"
        "\"output_tokens\":1,\"cache_read_input_tokens\":0}'.encode('utf-8'))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    env = {**FORCED_NON_UTF8_ENV, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    driver = (
        "from pipeline.haiku import call_haiku\n"
        "r = call_haiku('go')\n"
        "import sys; sys.stdout.buffer.write(r.text.encode('utf-8'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True, cwd=str(REPO_ROOT), env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.decode("utf-8") == f"arrow {ARROW}"


def test_call_haiku_passes_utf8_encoding_to_subprocess():
    """Cross-platform guard (incl. Windows): call_haiku must hand subprocess.run
    an explicit UTF-8 decode, never relying on the locale codec (#91)."""
    sys.path.insert(0, str(REPO_ROOT))
    from unittest.mock import MagicMock, patch
    import pipeline.haiku as haiku

    with patch("pipeline.haiku.subprocess.run") as run:
        run.return_value = MagicMock(
            returncode=0,
            stdout='{"result":"x","input_tokens":1,"output_tokens":1,'
                   '"cache_read_input_tokens":0}',
            stderr="",
        )
        haiku.call_haiku("go")
    assert run.call_args.kwargs.get("encoding") == "utf-8"
    assert run.call_args.kwargs.get("errors") == "replace"


# ── Write resilience: a lone surrogate in text must never crash a save (#97)

def test_emit_haiku_result_survives_lone_surrogate(tmp_path, capsys):
    """Even if a lone surrogate slips into the text, the write must not raise
    UnicodeEncodeError (which would kill the save and stall rotation)."""
    sys.path.insert(0, str(REPO_ROOT))
    from pipeline.shell import _emit_haiku_result
    from pipeline.types import HaikuResult, TokenUsage

    r = HaikuResult(
        text="summary \udc8f with \udc9d lone surrogates",
        tokens=TokenUsage(input=1, output=1, cache=0, cost_usd=0.0),
        is_skip=False,
    )
    out = tmp_path / "out.md"
    _emit_haiku_result(r, str(out))  # must not raise

    captured = capsys.readouterr().out
    text_file = next(
        line.split("=", 1)[1]
        for line in captured.splitlines()
        if line.startswith("HAIKU_TEXT_FILE=")
    )
    # Both the temp file and the explicit output file must exist and be readable.
    assert Path(text_file).read_text(encoding="utf-8")
    assert "summary" in out.read_text(encoding="utf-8")


def test_consolidate_survives_lone_surrogate(tmp_path, capsys):
    """The consolidation write path must also tolerate a lone surrogate."""
    sys.path.insert(0, str(REPO_ROOT))
    from unittest.mock import patch
    from pipeline.shell import cmd_consolidate
    from pipeline.types import ConsolidationResult, TokenUsage

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "today-2020-01-01.md").write_text("old", encoding="utf-8")

    bad = ConsolidationResult(
        recent="recent \udc8f text",
        archive="archive \udc9d text",
        tokens=TokenUsage(input=1, output=1, cache=0, cost_usd=0.0),
    )
    with patch("pipeline.consolidate.consolidate", return_value=bad):
        cmd_consolidate(
            staging_dir=str(staging),
            recent_file=str(tmp_path / "recent.md"),
            archive_file=str(tmp_path / "archive.md"),
        )  # must not raise
    out = capsys.readouterr().out
    assert "RECENT_OUT=" in out  # produced output instead of crashing


# ── Read boundary: session JSONL (external) may carry non-UTF-8 bytes (extract.py)

def test_count_lines_tolerates_non_utf8_jsonl(tmp_path):
    """A non-UTF-8 byte in the transcript must not crash line counting."""
    sys.path.insert(0, str(REPO_ROOT))
    from pipeline.extract import count_lines
    p = tmp_path / "session.jsonl"
    p.write_bytes(b'{"type":"user"}\nbad \x80\x81 byte line\n{"type":"assistant"}\n')
    assert count_lines(str(p)) == 3  # must not raise UnicodeDecodeError


def test_extract_messages_tolerates_non_utf8_jsonl(tmp_path):
    """A corrupt (non-UTF-8) line must be skipped, not crash the whole extract —
    the good messages still come through."""
    sys.path.insert(0, str(REPO_ROOT))
    from pipeline.extract import extract_messages
    p = tmp_path / "session.jsonl"
    good = b'{"type":"user","message":{"content":"hello there"}}\n'
    bad = b"garbage \x80\x81\x8f bytes\n"
    p.write_bytes(good + bad)
    msgs = extract_messages(str(p))  # must not raise
    assert ("HUMAN", "hello there") in msgs


# ── Structured machine-JSON stays STRICT: a corrupt last-save.json must fail to
#    a clean 0, never an errors="replace"-patched wrong line number.

def test_get_last_save_line_returns_zero_on_corrupt_json(tmp_path):
    """A non-UTF-8 byte in last-save.json must yield 0 (re-extract from start),
    not a U+FFFD-corrupted line number that silently skips/re-processes."""
    sys.path.insert(0, str(REPO_ROOT))
    from pipeline.extract import get_last_save_line

    save = tmp_path / "tmp" / "last-save.json"
    save.parent.mkdir(parents=True)
    # Corrupt the integer field with a raw non-UTF-8 byte.
    save.write_bytes(b'{"session":"abc-123","line":12\x8034}')
    assert get_last_save_line("abc-123", remember_dir=str(tmp_path)) == 0


# ── Read boundary: user-editable memory files may be saved in a non-UTF-8 editor

def test_build_ndc_prompt_tolerates_non_utf8_now_md(tmp_path, monkeypatch):
    """now.md is user-editable; a non-UTF-8 byte must not crash NDC prompt build."""
    sys.path.insert(0, str(REPO_ROOT))
    import pipeline.prompts as prompts_mod
    from pipeline.shell import cmd_build_ndc_prompt

    templates = tmp_path / "prompts"
    templates.mkdir()
    (templates / "compress-ndc.prompt.txt").write_text(
        "Compress:\n{{NOW_CONTENT}}", encoding="utf-8")
    monkeypatch.setattr(prompts_mod, "PROMPTS_DIR", str(templates))

    now = tmp_path / "now.md"
    now.write_bytes(b"## entry\nuser-edited bad byte \x80\x81 here\n")
    out = tmp_path / "prompt.txt"
    cmd_build_ndc_prompt(str(now), str(out))  # must not raise
    assert "## entry" in out.read_text(encoding="utf-8")


def test_consolidate_tolerates_non_utf8_staging(tmp_path, capsys):
    """Staging memory files are user-editable; a non-UTF-8 byte must not crash
    the consolidation read."""
    sys.path.insert(0, str(REPO_ROOT))
    from unittest.mock import patch
    from pipeline.shell import cmd_consolidate
    from pipeline.types import ConsolidationResult, TokenUsage

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "today-2020-01-01.md").write_bytes(b"old \x80\x81 entry")
    res = ConsolidationResult(
        recent="r", archive="a", tokens=TokenUsage(input=1, output=1, cache=0, cost_usd=0.0))
    with patch("pipeline.consolidate.consolidate", return_value=res):
        cmd_consolidate(
            staging_dir=str(staging),
            recent_file=str(tmp_path / "r.md"),
            archive_file=str(tmp_path / "a.md"),
        )  # must not raise
    assert "RECENT_OUT=" in capsys.readouterr().out


def test_consolidate_staging_consumed_bytes_match_real_file_size(tmp_path, capsys):
    """The consumed-byte count recorded for run-consolidation.sh must describe
    the FILE on disk, not the decoded-and-re-encoded string (review of 8d2cdab).

    The staging file used to be read with ``open(..., errors="replace")`` and
    the consumed count taken from ``len(content.encode("utf-8"))``. Each
    undecodable byte becomes one U+FFFD, which re-encodes to three bytes, so a
    file with two stray non-UTF-8 bytes measured four bytes bigger than it
    actually is (a 61-byte file measured 65). Overstated, run-consolidation.sh's
    ``staging_now -gt staging_consumed`` reads false and it falls through to the
    blind ``mv`` rename — sealing anything appended during consolidation inside
    ``.done.md``, unreachable, which is exactly the loss this count exists to
    prevent (#142's shape, one layer over).
    """
    sys.path.insert(0, str(REPO_ROOT))
    from unittest.mock import patch
    from pipeline.shell import cmd_consolidate
    from pipeline.types import ConsolidationResult, TokenUsage

    staging = tmp_path / "staging"
    staging.mkdir()
    staging_file = staging / "today-2020-01-01.md"
    # \xff\xfe: two bytes, neither valid UTF-8 on its own. errors="replace"
    # turns them into two U+FFFD (6 bytes when re-encoded) instead of 2.
    raw_bytes = b"entry with two stray bytes \xff\xfe right here, then more text\n"
    staging_file.write_bytes(raw_bytes)
    real_size = os.path.getsize(staging_file)

    res = ConsolidationResult(
        recent="r", archive="a", tokens=TokenUsage(input=1, output=1, cache=0, cost_usd=0.0))
    with patch("pipeline.consolidate.consolidate", return_value=res):
        cmd_consolidate(
            staging_dir=str(staging),
            recent_file=str(tmp_path / "r.md"),
            archive_file=str(tmp_path / "a.md"),
        )
    output = capsys.readouterr().out
    staging_paths_file = next(
        line.split("=", 1)[1]
        for line in output.splitlines()
        if line.startswith("STAGING_PATHS_FILE=")
    )
    raw = Path(staging_paths_file).read_bytes()
    fields = [p for p in raw.split(b"\x00") if p]
    paths_from_file, counts = fields[0::2], fields[1::2]
    idx = next(i for i, p in enumerate(paths_from_file)
               if p.decode().endswith("today-2020-01-01.md"))
    consumed = int(counts[idx].decode())
    assert consumed == real_size, (
        f"consumed count {consumed} != real file size {real_size} bytes — "
        "measured from the decoded-and-re-encoded string instead of the file, "
        "so run-consolidation.sh's staging_now > staging_consumed check goes "
        "false and seals appended entries inside .done.md"
    )


# ── Boundary 3: shell.py's own stdout, read back as a path by bash (#145)
#
# Every cmd_* prints KEY=value lines that bash captures by command substitution
# and passes on as argv to the NEXT python call. On Windows that print() used
# the console's ANSI codepage, so a temp path under a non-ASCII profile came
# back mojibake and build-prompt died with FileNotFoundError on a file that was
# right there on disk.
#
# Note this boundary needs a DIFFERENT stand-in from the two above. Forcing
# LC_ALL=C would also make Python's filesystem encoding ascii, which mangles
# the path before stdout is ever reached — that is not the bug, and it is not
# what Windows does: there the filesystem is UTF-8 (PEP 529) and only the
# console codec is wrong. So keep the locale alone and force just the io codec
# to a real ANSI codepage, which is exactly the shape #145 reported.
ANSI_STDOUT_ENV = {**_ambient_env(), "PYTHONIOENCODING": "cp1252"}
ANSI_STDOUT_ENV.pop("PYTHONUTF8", None)


def _run_shell_ansi_stdout(args: list[str], stdin_bytes: bytes, tmpdir: Path):
    """Run pipeline.shell with an ANSI stdout codec and a non-ASCII TMPDIR."""
    env = {**ANSI_STDOUT_ENV, "TMPDIR": str(tmpdir), "TEMP": str(tmpdir),
           "TMP": str(tmpdir)}
    return subprocess.run(
        [sys.executable, "-m", "pipeline.shell", *args],
        input=stdin_bytes, capture_output=True, cwd=str(REPO_ROOT),
        env=env, timeout=30,
    )


def _haiku_payload(result: str) -> bytes:
    return json.dumps(
        {"result": result, "input_tokens": 1, "output_tokens": 1,
         "cache_read_input_tokens": 0},
        ensure_ascii=False,
    ).encode("utf-8")


# The two tests below put a non-ASCII directory on disk, so they need a
# filesystem codec that can carry one. A runner under LANG=C cannot, and there
# the path is mangled before stdout is ever involved — a different bug from
# #145, and not one this stand-in can show. The unit guard below covers those
# runners.
requires_utf8_fs = pytest.mark.skipif(
    sys.getfilesystemencoding().lower().replace("-", "") != "utf8",
    reason="needs a UTF-8 filesystem codec to put a non-ASCII dir on disk; "
           "test_main_reconfigures_both_output_streams covers this elsewhere",
)


@requires_utf8_fs
def test_shell_stdout_emits_utf8_paths_under_ansi_codepage(tmp_path):
    """A path printed for bash to re-consume must survive as UTF-8 (#145)."""
    tmpdir = tmp_path / "ユーザー"
    tmpdir.mkdir()
    out = tmp_path / "haiku.txt"

    result = _run_shell_ansi_stdout(["parse-haiku", str(out)], _haiku_payload("x"), tmpdir)

    assert result.returncode == 0, (
        "shell.py crashed printing a non-ASCII path under an ANSI stdout codec "
        f"(#145).\nstderr:\n{result.stderr.decode('utf-8', 'replace')}"
    )
    line = next(
        ln for ln in result.stdout.decode("utf-8").splitlines()
        if ln.startswith("HAIKU_TEXT_FILE=")
    )
    assert "ユーザー" in line, f"path came back mangled: {line!r}"


@requires_utf8_fs
def test_shell_stdout_path_round_trips_into_a_second_process(tmp_path):
    """The reporter's own check: the captured path must still open (#145).

    Printing valid UTF-8 is only half of it — bash hands the captured string to
    the next `python -m pipeline.shell` call as argv, so the path has to survive
    the whole loop, not merely look right in a terminal.
    """
    tmpdir = tmp_path / "プロジェクト"
    tmpdir.mkdir()
    out = tmp_path / "haiku.txt"

    captured = _run_shell_ansi_stdout(["parse-haiku", str(out)], _haiku_payload("y"), tmpdir)
    assert captured.returncode == 0, captured.stderr.decode("utf-8", "replace")
    path = next(
        ln.split("=", 1)[1]
        for ln in captured.stdout.decode("utf-8").splitlines()
        if ln.startswith("HAIKU_TEXT_FILE=")
    ).strip("'\"")

    probe = subprocess.run(
        [sys.executable, "-c",
         "import os, sys; print(os.path.exists(sys.argv[1]))", path],
        capture_output=True, cwd=str(REPO_ROOT), timeout=30,
    )
    assert probe.stdout.decode("utf-8").strip() == "True", (
        f"captured path does not resolve in a second process: {path!r}"
    )


def test_main_reconfigures_both_output_streams():
    """Cross-platform guard: the reconfigure must cover stderr too, not just
    stdout — error text carries paths as well, and a crash there is what made
    #145 undiagnosable in the first place."""
    sys.path.insert(0, str(REPO_ROOT))
    from unittest.mock import patch
    import pipeline.shell as shell

    class Recorder:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

        def write(self, *a, **k):
            pass

        def flush(self):
            pass

    out, err = Recorder(), Recorder()
    with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err), \
            patch.object(sys, "argv", ["shell"]):
        with pytest.raises(SystemExit):
            shell.main()

    for name, rec in (("stdout", out), ("stderr", err)):
        assert rec.calls == [{"encoding": "utf-8", "errors": "replace"}], (
            f"{name} was not reconfigured to UTF-8: {rec.calls!r}"
        )
