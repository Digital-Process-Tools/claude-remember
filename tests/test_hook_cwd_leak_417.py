"""Regression pin for #417: post-tool-hook.sh and user-prompt-hook.sh must not
silently inherit REMEMBER_HOOK_CWD from the process environment.

resolve-paths.sh falls back to REMEMBER_HOOK_CWD (#411) when CLAUDE_PROJECT_DIR
is unset. Only session-start-hook.sh and session-end-hook.sh ever set that
variable -- each from its own stdin `cwd`, freshly validated on every run.
post-tool-hook.sh and user-prompt-hook.sh source resolve-paths.sh (on their
slow path) without ever managing the variable themselves, so before this fix
they would silently consult whatever the process environment already held --
on a host that reuses one environment across separate hook invocations, that
could be a value a DIFFERENT session's SessionStart exported for a DIFFERENT
project.

Reachability of that host behaviour was never established (see the issue), so
the fix is not a reachability check -- it is an unconditional `unset
REMEMBER_HOOK_CWD` near the top of each hook, before either hook does
anything else. This is correct regardless of whether the leak is reachable on
any real host: neither hook has a stdin `cwd` of its own to offer, so the
variable is never theirs to use.

Two tests, one fixture family:

* test_resolve_paths_sh_still_honours_the_variable_when_asked -- the POSITIVE
  control. If resolve-paths.sh's own REMEMBER_HOOK_CWD arm were silently
  broken or dead code, the "must not fire" tests below would pass for the
  wrong reason (nothing propagates a leaked value because the underlying
  mechanism never worked in the first place, not because the hooks correctly
  clear it). This proves the mechanism this fix defends against is real.

* test_post_tool_hook_clears_the_variable_before_sourcing_anything and its
  user-prompt-hook.sh sibling -- the MUST NOT FIRE half: each hook's own
  preamble (from BASH_SOURCE resolution through the new `unset` line) is
  extracted verbatim and executed with REMEMBER_HOOK_CWD pre-set to a
  directory that is very much NOT this hook's own project. If the `unset`
  line were ever removed or reordered, the variable would still hold that
  directory afterwards and this test would fail.
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
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"
POST_TOOL_HOOK = REPO_ROOT / "scripts" / "post-tool-hook.sh"
USER_PROMPT_HOOK = REPO_ROOT / "scripts" / "user-prompt-hook.sh"

# Both hooks manage the variable in an identical preamble block, ending in
# this exact line. Splitting on it (rather than a hardcoded line number)
# survives the file growing or shrinking elsewhere.
_UNSET_LINE = "unset REMEMBER_HOOK_CWD"


def _preamble(hook: Path) -> str:
    """Return everything in `hook` up to and including the line that clears
    REMEMBER_HOOK_CWD. Fails loudly (not silently returning the whole file
    or an empty string) if that line is ever removed."""
    text = hook.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == _UNSET_LINE:
            return "\n".join(lines[: i + 1])
    raise AssertionError(
        f"{hook} no longer contains {_UNSET_LINE!r} -- either the #417 fix "
        "was reverted, or this test's split point needs to move with it"
    )


def test_resolve_paths_sh_still_honours_the_variable_when_asked(tmp_path):
    """POSITIVE CONTROL: without this, the tests below could pass because the
    fallback they are guarding against is already dead code, not because the
    hooks correctly clear it."""
    real_project = tmp_path / "real-project"
    real_project.mkdir()

    script = (
        f'source "{RESOLVE_PATHS}"; '
        'echo "PROJECT_DIR=${PROJECT_DIR:-unset}"'
    )
    env = {
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_PATHS_SOFT_FAIL": "1",
        "REMEMBER_HOOK_CWD": str(real_project),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    result = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30,
    )
    assert f"PROJECT_DIR={real_project}" in result.stdout, (
        "resolve-paths.sh no longer honours REMEMBER_HOOK_CWD at all -- the "
        "mechanism this fix defends against may already be dead code:\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("hook", [POST_TOOL_HOOK, USER_PROMPT_HOOK], ids=["post-tool-hook", "user-prompt-hook"])
def test_hook_clears_the_variable_before_sourcing_anything(tmp_path, hook):
    """MUST NOT FIRE: a value left over from a different session's
    SessionStart must not survive past this hook's own preamble."""
    leaked_project = tmp_path / "someone-elses-project"
    leaked_project.mkdir()

    preamble = _preamble(hook)
    script = preamble + "\necho \"REMEMBER_HOOK_CWD=${REMEMBER_HOOK_CWD:-cleared}\"\n"
    env = {
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_HOOK_CWD": str(leaked_project),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    result = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30,
    )
    assert "REMEMBER_HOOK_CWD=cleared" in result.stdout, (
        f"{hook.name} did not clear a leaked REMEMBER_HOOK_CWD before "
        f"sourcing anything else:\n{result.stdout}\n{result.stderr}"
    )
    assert str(leaked_project) not in result.stdout, (
        f"{hook.name}'s preamble still surfaced the leaked project path:\n"
        f"{result.stdout}"
    )
