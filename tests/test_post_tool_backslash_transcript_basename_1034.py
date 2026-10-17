r"""#1034: a backslash-separated transcript_path must still name its sidecar.

Claude Code on Windows sends `transcript_path` as a native path,
`C:\Users\<user>\.claude\projects\<slug>\<session-id>.jsonl`. After #829
decodes the JSON escapes, TRANSCRIPT holds single backslashes and not one
`/`. post-tool-hook.sh took every basename with `${TRANSCRIPT##*/}`, so the
WHOLE path survived, failed the `[A-Za-z0-9._-]` sanitiser and came out
empty: the #913 `tmp/transcript-scan.<id>` sidecar was never read or
written, and every PostToolUse call cold-rescanned the entire transcript
(minutes on a 100+ MB one -- reported and diagnosed by @RidoMr). Silent:
nothing was logged.

What each case is evidence of, per platform. The native-backslash cases are
only real on Windows: str(Path) there renders backslash separators with no
synthetic construction, the exact shape Claude Code sends. They are skipped
elsewhere, because on POSIX a backslash is an ordinary filename byte and a
fixture built from one is a different file, not the Windows path. The
forward-slash cases are the positive controls and run everywhere, same
fixture and same assertions; if they fail, the harness is broken and the
backslash verdict means nothing. The POSIX literal-backslash cases pin the
one behaviour change the fix makes there (a backslash in a transcript
basename now splits it, as a slash does; Claude Code and Codex transcript
names never contain one). They are not Windows evidence; they exist so the
change is written down and so a POSIX run of this module fails without the
fix. Paths bash itself parses (script argv, HOME, CLAUDE_PLUGIN_ROOT,
CLAUDE_PROJECT_DIR) are forced to forward slashes, the convention of
test_windows_native_hook_cwd_448.py; only the transcript_path payload field
under test keeps its native spelling.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash
from .env_cache import write_config

BASH = resolve_bash()
_needs_bash = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (see tests/_bash_runner.py)",
)
_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="a native backslash transcript_path only exists on Windows; on POSIX "
    "a backslash is a filename byte, so the fixture would be a different file "
    "(the literal-backslash cases in this module cover POSIX separately)",
)
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="a filename containing a literal backslash cannot exist on Windows",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"
SESSION_ID = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
NL = chr(10)
BS = chr(92)
LINE = json.dumps(dict(type="assistant", message=dict(content="x"))) + NL  # noqa: C408
WARNING = "transcript-scan cache disabled"
POSIX_BACKSLASH_NAME = "C:" + BS + "Users" + BS + "me" + BS + SESSION_ID + ".jsonl"


def _fwd(p: Path) -> str:
    return str(p).replace(BS, "/")


def _fixture(tmp_path: Path, name: str = SESSION_ID + ".jsonl"):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    # A threshold no 10-line transcript can reach: no save fork, so nothing
    # runs after the hook returns and the sidecar is the only effect.
    write_config(remember / "config.json",
                 dict(thresholds=dict(delta_lines_trigger=100000)))  # noqa: C408
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    transcript = tdir / name
    transcript.write_text(LINE * 10, encoding="utf-8")
    return home, project, remember, transcript


def _run(tmp_path, home, project, transcript_path: str, *, session_id=SESSION_ID):
    systmp = tmp_path / "systmp"
    systmp.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        HOME=_fwd(home),
        CLAUDE_PROJECT_DIR=_fwd(project),
        CLAUDE_PLUGIN_ROOT=_fwd(REPO_ROOT),
        TMPDIR=_fwd(systmp),
    )
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED", "REMEMBER_TZ",
                  "REMEMBER_NESTED_SUMMARIZER", "REMEMBER_HOOK_CWD"):
        env.pop(stale, None)
    payload = dict(  # noqa: C408
        transcript_path=transcript_path,
        hook_event_name="PostToolUse",
        tool_name="Read",
        tool_input=dict(file_path="x.py"),  # noqa: C408
        tool_response=dict(content="ok"),  # noqa: C408
    )
    if session_id is not None:
        payload["session_id"] = session_id
    result = subprocess.run(
        [BASH, _fwd(HOOK)], env=env, input=json.dumps(payload).encode("utf-8"),
        capture_output=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return result


def _logs(remember: Path) -> str:
    return NL.join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted(remember.rglob("*.log")))


def _assert_sidecar_written(remember, transcript, result):
    sidecar = remember / "tmp" / ("transcript-scan." + SESSION_ID)
    assert sidecar.is_file(), (
        "no tmp/transcript-scan." + SESSION_ID + " after one call -- the "
        "#913 cache was never written, so every call rescans from offset 0. "
        "tmp/ holds: " + repr(sorted(p.name for p in (remember / "tmp").iterdir()))
        + NL + "logs: " + _logs(remember)[-800:]
        + NL + "stderr: " + result.stderr.decode("utf-8", "replace")[-800:]
    )
    offset, lines = sidecar.read_text(encoding="utf-8").split()
    assert int(lines) == 10
    assert int(offset) == len(transcript.read_bytes())


# --- the #913 sidecar --------------------------------------------------------

@_needs_bash
def test_forward_slash_transcript_path_writes_the_scan_sidecar(tmp_path):
    """POSITIVE CONTROL, every platform: same fixture, forward slashes."""
    home, project, remember, transcript = _fixture(tmp_path)
    result = _run(tmp_path, home, project, _fwd(transcript))
    _assert_sidecar_written(remember, transcript, result)


@_needs_bash
@_windows_only
def test_native_backslash_transcript_path_writes_the_scan_sidecar(tmp_path):
    """THE #1034 CASE: a native Windows path, exactly as Claude Code sends it."""
    home, project, remember, transcript = _fixture(tmp_path)
    native = str(transcript)
    assert BS in native and "/" not in native, native
    result = _run(tmp_path, home, project, native)
    _assert_sidecar_written(remember, transcript, result)


@_needs_bash
@_posix_only
def test_literal_backslash_in_a_posix_basename_now_splits_there(tmp_path):
    """Pins the POSIX consequence of the fix (not Windows evidence): the
    part after the last backslash names the sidecar, as it would after a slash."""
    home, project, remember, transcript = _fixture(tmp_path, name=POSIX_BACKSLASH_NAME)
    result = _run(tmp_path, home, project, str(transcript))
    _assert_sidecar_written(remember, transcript, result)


# --- the SESSION_ID basename fallback (transcript_path, no session_id) -------

def _capture_alive(remember: Path):
    d = remember / "tmp" / "capture-alive.d"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


@_needs_bash
def test_session_id_fallback_from_a_forward_slash_transcript_path(tmp_path):
    """POSITIVE CONTROL for the basename-derived SESSION_ID."""
    home, project, remember, transcript = _fixture(tmp_path)
    _run(tmp_path, home, project, _fwd(transcript), session_id=None)
    assert _capture_alive(remember) == [SESSION_ID]


@_needs_bash
@_windows_only
def test_session_id_fallback_from_a_native_backslash_transcript_path(tmp_path):
    home, project, remember, transcript = _fixture(tmp_path)
    _run(tmp_path, home, project, str(transcript), session_id=None)
    assert _capture_alive(remember) == [SESSION_ID], (
        "SESSION_ID derived from a backslash transcript_path came out wrong "
        "or empty: " + repr(_capture_alive(remember))
    )


@_needs_bash
@_posix_only
def test_session_id_fallback_splits_a_literal_backslash_on_posix(tmp_path):
    home, project, remember, transcript = _fixture(tmp_path, name=POSIX_BACKSLASH_NAME)
    _run(tmp_path, home, project, str(transcript), session_id=None)
    assert _capture_alive(remember) == [SESSION_ID]


# --- the silent-failure guard ------------------------------------------------

@_needs_bash
def test_an_unusable_scan_name_is_logged_once_per_transcript(tmp_path):
    """MUST FIRE: a basename the sanitiser rejects (a plus sign is outside the
    class, and legal in a filename on every platform) used to disable the
    cache without a word."""
    home, project, remember, transcript = _fixture(tmp_path, name="bad+name.jsonl")
    _run(tmp_path, home, project, _fwd(transcript))
    assert not list((remember / "tmp").glob("transcript-scan.*"))
    logs = _logs(remember)
    assert logs.count(WARNING) == 1, logs[-1500:]
    _run(tmp_path, home, project, _fwd(transcript))
    assert _logs(remember).count(WARNING) == 1, (
        "the warning repeated on the second call for the same transcript -- "
        "it must be once per transcript, not once per tool call"
    )


@_needs_bash
def test_a_usable_scan_name_logs_no_warning(tmp_path):
    """MUST NOT FIRE, paired with the case above."""
    home, project, remember, transcript = _fixture(tmp_path)
    result = _run(tmp_path, home, project, _fwd(transcript))
    _assert_sidecar_written(remember, transcript, result)
    assert WARNING not in _logs(remember)
