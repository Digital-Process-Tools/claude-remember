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

# A non-UTF-8 locale that survives PEP 538 C-locale coercion — makes Python's
# stdin/subprocess codec ascii+surrogateescape on any OS, mimicking cp1252.
FORCED_NON_UTF8_ENV = {
    **os.environ,
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
