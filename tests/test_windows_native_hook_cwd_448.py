"""#448: resolve-paths.sh tested REMEMBER_HOOK_CWD for directory existence
BEFORE the Windows backslash/drive-letter normalization block that would have
made it resolvable, so a `cwd` arriving in Windows-native spelling was tested
in the form the shell cannot resolve and the branch was skipped before the
normalization that would have fixed it ever ran. On a host with no
CLAUDE_PROJECT_DIR (Codex, Gemini CLI) that meant every hook silently no-oped
via its own `|| exit 0`.

REASONED, not fully observed (the issue's own caveat): found by reading, in a
session with no Windows or Git Bash shell. This test is what would settle it
-- it needs the Windows CI leg to be meaningful, which is exactly why it uses
tests/_bash_runner.py's resolve_bash() (#432/#439) instead of a blanket
skipif(win32) -- a skip here would reproduce the #442 defect (a Windows-only
regression test skipped on the one platform that could catch it) in the very
lane meant to fix a Windows bug.

On Windows, pathlib.Path renders its own separators natively -- str(project)
for a Windows tmp_path already comes back with backslash separators, i.e.
genuine Windows-native spelling, with no synthetic construction needed. On
macOS/Linux this collapses to the ordinary POSIX-path case every other
REMEMBER_HOOK_CWD test already covers, so the four assertions below are not
redundant with the existing suite only on the Windows leg -- which is the
platform this issue is about.

That native-backslash spelling is deliberately used ONLY for the stdin
`cwd` payload field under test -- see `_win_bash_path` below for why every
OTHER path handed to bash (the script argv, HOME, CLAUDE_PLUGIN_ROOT) is
forced to forward slashes instead. A first version of this file used
`str(...)` unnormalized for those too and broke session-start-hook.sh's own
self-location on the real windows-latest CI leg before ever reaching
resolve-paths.sh's candidate logic -- a harness bug, not a finding about
the fix, caught by CI and fixed here rather than papered over: this repo's
own tests/test_hooks_json.py already established forward-slash-always as
the convention for exactly this reason, and this file now follows it.

All four hooks that read the #411/#444 REMEMBER_HOOK_CWD fallback are covered:
session-start-hook.sh and session-end-hook.sh (#411), user-prompt-hook.sh and
post-tool-hook.sh (#444).

Positive control: test_windows_hook_cwd_alone_would_fail_before_the_448_fix
below runs a fabricated Windows-native path directly against a bare `[ -d ]`
test on the resolved bash, proving that form is one bash genuinely refuses to
resolve on this platform -- so the fixture is demonstrated capable of failing
and the green results above are not an artifact of a harness that never
really exercised the code path. Its sibling,
test_session_start_scaffolds_nothing_when_cwd_is_genuinely_unusable, is the
existing "must not fire" half moved into this module for the same reason
test_hook_cwd_end_to_end_411.py pairs its own positive case with one.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash

BASH = resolve_bash()

# Applied per-function below, NOT as a module-level `pytestmark` (#448 self-
# review): test_remember_hook_cwd_is_normalized_before_its_existence_test is
# a pure source-text pin with no bash dependency at all -- a module-level
# skip would silence the one test in this file its own docstring calls
# "checkable identically on every platform" for a reason that has nothing to
# do with what it checks, which is exactly the "misreports what always runs"
# shape the rest of this suite exists to catch.
_needs_bash = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
SESSION_END = REPO_ROOT / "scripts" / "session-end-hook.sh"
USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"
POST_TOOL = REPO_ROOT / "scripts" / "post-tool-hook.sh"
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"

SESSION_ID = "aaaaaaaa-0000-4000-8000-000000000002"


def _win_bash_path(p: Path) -> str:
    """A path spelled the way this repo's OWN Windows-CI convention spells
    everything handed to bash -- forward slashes, unconditionally, matching
    tests/test_hooks_json.py's `str(REPO_ROOT).replace("\\\\", "/")` and
    hooks/hooks.json's own `"${CLAUDE_PLUGIN_ROOT}/scripts/..."` template.
    Real hook dispatch NEVER hands a hook script a backslash-spelled $0: the
    registered command is a fixed forward-slash template, and CLAUDE_PLUGIN_ROOT
    itself arrives POSIX-style on Windows (this file's own header comment on
    CLAUDE_PROJECT_DIR says the same). `pathlib.Path.__str__` on Windows
    renders native backslashes, which is the right spelling for an OS-level
    call (subprocess's own `cwd=` argument, for instance) and the WRONG one
    for a string bash will itself parse as a path -- session-start-hook.sh's
    self-location (`_HOOK_DIR="${BASH_SOURCE[0]%/*}"`) cannot derive a
    directory from a path with no `/` in it, so an un-normalized $0 breaks
    hook bootstrap before resolve-paths.sh is ever sourced, regardless of
    what this issue's fix does. This is ONLY for strings bash will parse as
    paths (the script argv and the env vars below); the REMEMBER_HOOK_CWD
    *payload* value under test is deliberately left native-spelled -- see
    the payload builders, which pass str(project) through untouched.
    """
    return str(p).replace("\\", "/")


def _env(home: Path, plugin_root: Path) -> dict:
    env = {
        **os.environ,
        "HOME": _win_bash_path(home),
        "CLAUDE_PLUGIN_ROOT": _win_bash_path(plugin_root),
    }
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("REMEMBER_HOOK_CWD", None)
    return env


def _run(script: Path, env: dict, payload: str, cwd: Path):
    return subprocess.run(
        [BASH, _win_bash_path(script)], env=env, input=payload,
        capture_output=True, text=True, timeout=60, cwd=str(cwd), check=False,
    )


def _session_start_payload(cwd: str) -> str:
    return json.dumps({
        "session_id": SESSION_ID,
        "transcript_path": "/does/not/matter.jsonl",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": cwd,
    })


def _session_end_payload(cwd: str) -> str:
    return json.dumps({
        "session_id": SESSION_ID,
        "transcript_path": "/does/not/matter.jsonl",
        "hook_event_name": "SessionEnd",
        "reason": "other",
        "cwd": cwd,
    })


def _user_prompt_payload(cwd: str) -> str:
    return json.dumps({
        "session_id": SESSION_ID,
        "transcript_path": "/does/not/matter.jsonl",
        "hook_event_name": "UserPromptSubmit",
        "cwd": cwd,
        "prompt": "hello",
    })


def _post_tool_payload(cwd: str) -> str:
    return json.dumps({
        "session_id": SESSION_ID,
        "transcript_path": "/does/not/matter.jsonl",
        "hook_event_name": "PostToolUse",
        "cwd": cwd,
        "tool_name": "Read",
        "tool_input": {"file_path": "/does/not/matter.py"},
        "tool_response": {"content": "ok"},
    })


# --- the gap itself: a Windows-native-spelled cwd, no CLAUDE_PROJECT_DIR ---

@_needs_bash
def test_session_start_resolves_windows_native_stdin_cwd(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "win-project"
    project.mkdir()
    env = _env(home, REPO_ROOT)

    result = _run(SESSION_START, env, _session_start_payload(str(project)), cwd=project)

    assert result.returncode == 0, result.stderr
    assert (project / ".remember").is_dir(), (
        "session-start-hook.sh did not resolve PROJECT_DIR from a "
        f"Windows-native-spelled stdin cwd; stderr:\n{result.stderr}"
    )
    assert "FATAL: Cannot resolve project root" not in result.stderr


@_needs_bash
def test_session_end_resolves_windows_native_stdin_cwd(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "win-project"
    project.mkdir()
    env = _env(home, REPO_ROOT)

    result = _run(SESSION_END, env, _session_end_payload(str(project)), cwd=project)

    assert result.returncode == 0, result.stderr
    assert (project / ".remember").is_dir(), (
        "session-end-hook.sh did not resolve PROJECT_DIR from a "
        f"Windows-native-spelled stdin cwd; stderr:\n{result.stderr}"
    )
    assert "FATAL: Cannot resolve project root" not in result.stderr


@_needs_bash
def test_user_prompt_hook_resolves_windows_native_stdin_cwd(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "win-project"
    project.mkdir()
    env = _env(home, REPO_ROOT)

    result = _run(USER_PROMPT, env, _user_prompt_payload(str(project)), cwd=project)

    assert result.returncode == 0, result.stderr
    assert (project / ".remember").is_dir(), (
        "user-prompt-hook.sh did not resolve PROJECT_DIR from a "
        f"Windows-native-spelled stdin cwd; stderr:\n{result.stderr}"
    )
    assert "FATAL: Cannot resolve project root" not in result.stderr


@_needs_bash
def test_post_tool_hook_resolves_windows_native_stdin_cwd(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "win-project"
    project.mkdir()
    env = _env(home, REPO_ROOT)

    result = _run(POST_TOOL, env, _post_tool_payload(str(project)), cwd=project)

    assert result.returncode == 0, result.stderr
    assert (project / ".remember").is_dir(), (
        "post-tool-hook.sh did not resolve PROJECT_DIR from a "
        f"Windows-native-spelled stdin cwd; stderr:\n{result.stderr}"
    )
    assert "FATAL: Cannot resolve project root" not in result.stderr


# --- positive control: the fixture itself is capable of failing ------------

@_needs_bash
def test_windows_hook_cwd_alone_would_fail_before_the_448_fix():
    r"""Proves the fixture below is capable of failing: a fabricated
    Windows-native path (drive letter + backslash separators, e.g.
    Z:\this\does\not\exist) fed straight to `[ -d ]` on the resolved bash,
    with no normalization applied, must NOT resolve. This is the exact
    pre-#448 shape -- test resolve-paths.sh line 168 used to run -- so if
    this assertion ever failed, the four "resolves" tests above would be
    green for the wrong reason: not because #448's normalize-before-test
    fix works, but because this bash resolves backslash paths unaided and
    the defect was never reachable here to begin with.
    """
    fake_native = r"Z:\this\does\not\exist\as\written"
    script = (
        'if [ -d "$1" ]; then echo WOULD_HAVE_RESOLVED; '
        'else echo WOULD_HAVE_FAILED; fi'
    )
    result = subprocess.run(
        [BASH, "-c", script, "_", fake_native],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert "WOULD_HAVE_FAILED" in result.stdout, (
        "the pre-normalization existence test unexpectedly resolved a "
        "fabricated Windows-native path on this bash -- the #448 defect is "
        f"not reachable here, so the tests above prove nothing; stdout:\n{result.stdout}"
    )


# --- the actual TDD gate: source order, not runtime resolution -------------
#
# The four "resolves" tests above and the bash-mechanism check below are
# platform-dependent in opposite directions -- Windows-meaningful and
# POSIX-meaningless, or POSIX-provable and unable to speak to Windows at all
# -- neither one goes red against the pre-#448 source on THIS machine. This
# one does: it is a structural pin on execution order, checkable identically
# on every platform, and it is what "test first, watch it fail" actually
# means for this issue -- see the report for the literal red/green pair.

def test_remember_hook_cwd_is_normalized_before_its_existence_test():
    """#448's actual defect was ORDER: `-d` on REMEMBER_HOOK_CWD ran before
    the Windows-path normalizer that would have made a backslash/drive-letter
    spelling resolvable. Whether that ordering fix changes what actually
    resolves is only observable on Git Bash (the hook-level tests above,
    Windows-CI-only in practice) -- but the ORDERING ITSELF is a fact about
    the source text, true or false on every platform. Pins that resolve-paths.sh's
    REMEMBER_HOOK_CWD existence-test line routes through the normalizer,
    rather than testing the raw value.
    """
    source = RESOLVE_PATHS.read_text(encoding="utf-8")
    candidates = [
        line for line in source.splitlines()
        if "REMEMBER_HOOK_CWD" in line
        and "-d" in line
        and not line.lstrip().startswith("#")
    ]
    assert candidates, (
        "no line testing REMEMBER_HOOK_CWD for directory existence was found "
        "in resolve-paths.sh -- the file's shape changed enough that this "
        "pin can no longer locate what it is meant to check"
    )
    for line in candidates:
        assert "_remember_normalize_win_path" in line, (
            "REMEMBER_HOOK_CWD's existence test does not route through the "
            "Windows-path normalizer before deciding whether the candidate "
            f"is a real directory: {line!r}"
        )


# --- positive control: without a usable cwd, nothing is scaffolded ---------

@_needs_bash
def test_session_start_scaffolds_nothing_when_cwd_is_genuinely_unusable(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "win-project"
    project.mkdir()
    env = _env(home, REPO_ROOT)

    result = _run(
        SESSION_START, env,
        _session_start_payload(str(tmp_path / "never-created")), cwd=project,
    )

    assert result.returncode == 0, (
        "session-start-hook.sh must exit 0 even on resolution failure "
        f"(soft-fail contract); stderr:\n{result.stderr}"
    )
    assert not (project / ".remember").exists()
    assert not (tmp_path / "never-created").exists()
