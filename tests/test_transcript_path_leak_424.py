"""Regression pin for #424: post-tool-hook.sh and user-prompt-hook.sh must not
silently inherit REMEMBER_TRANSCRIPT_PATH from the process environment.

pipeline/host.transcript_path() gates on os.path.isfile() alone, and
pipeline/extract.py::find_session() returns that value BEFORE
_validate_session_id() ever runs -- so a REMEMBER_TRANSCRIPT_PATH set anywhere
in the ambient environment (a .envrc, a modified dotfile) bypasses the
traversal validator entirely and is read, summarized, then committed and
pushed by hooks.d/after_save/50-git-backup.sh.

Only session-start-hook.sh and session-end-hook.sh have a legitimate reason to
set this variable -- each extracts `transcript_path` from its own stdin
payload, freshly validated on every run (see tests/test_transcript_path_407.py,
which is this fix's positive control: it proves host.transcript_path() still
honours a LEGITIMATELY supplied value, so the "must not fire" tests below
cannot pass by testing a mechanism that is already dead).

post-tool-hook.sh and user-prompt-hook.sh have no stdin `transcript_path` of
their own to offer and must not silently consult whatever the process
environment already holds -- on a host that reuses one process environment
across hook invocations, that could be a path exported by a DIFFERENT
session's SessionStart (or worse, an ambient dotfile value naming an arbitrary
file). Reachability of that host behaviour was never established (see the
issue), so the fix is not a reachability check -- it is an unconditional
`unset REMEMBER_TRANSCRIPT_PATH` near the top of each hook, the same pattern
#417 already applied to REMEMBER_HOOK_CWD in the same two files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
POST_TOOL_HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"
USER_PROMPT_HOOK = REPO_ROOT / "scripts" / "user-prompt-hook.sh"

# Both hooks clear REMEMBER_TRANSCRIPT_PATH (#424) right after the
# REMEMBER_HOOK_CWD block (#417), before sourcing anything else. Split on the
# new line so this test survives the file growing or shrinking elsewhere.
_TRANSCRIPT_UNSET_LINE = "unset REMEMBER_TRANSCRIPT_PATH"


def _preamble(hook: Path) -> str:
    """Return everything in `hook` up to and including the line that clears
    REMEMBER_TRANSCRIPT_PATH. Fails loudly (not silently returning the whole
    file or an empty string) if that anchor line is ever removed."""
    text = hook.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == _TRANSCRIPT_UNSET_LINE:
            return "\n".join(lines[: i + 1])
    raise AssertionError(
        f"{hook} no longer contains {_TRANSCRIPT_UNSET_LINE!r} -- either the "
        "#424 fix was reverted, or this test's split point needs to move "
        "with it"
    )


@pytest.mark.parametrize(
    "hook", [POST_TOOL_HOOK, USER_PROMPT_HOOK], ids=["post-tool-hook", "user-prompt-hook"]
)
def test_hook_clears_transcript_path_before_sourcing_anything(tmp_path, hook):
    """MUST NOT FIRE: a REMEMBER_TRANSCRIPT_PATH left over in the ambient
    environment -- from a different session's SessionStart, or an .envrc --
    must not survive past this hook's own preamble."""
    victim = tmp_path / "victim.jsonl"
    victim.write_text("not your business", encoding="utf-8")

    preamble = _preamble(hook)
    script = (
        preamble
        + "\necho \"REMEMBER_TRANSCRIPT_PATH=${REMEMBER_TRANSCRIPT_PATH:-cleared}\"\n"
    )
    env = {
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_TRANSCRIPT_PATH": str(victim),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    result = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert "REMEMBER_TRANSCRIPT_PATH=cleared" in result.stdout, (
        f"{hook.name} did not clear a leaked REMEMBER_TRANSCRIPT_PATH before "
        f"sourcing anything else:\n{result.stdout}\n{result.stderr}"
    )
    assert str(victim) not in result.stdout, (
        f"{hook.name}'s preamble still surfaced the leaked transcript path:\n"
        f"{result.stdout}"
    )
