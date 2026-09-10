"""SessionEnd must leave a trace, and must keep the budget it was given (#647).

The reporter's Windows sessions fail on every exit with

    SessionEnd hook [bash "${CLAUDE_PLUGIN_ROOT}/scripts/session-end-hook.sh"] failed: Hook cancelled

and `.remember/logs/autonomous/` holds not a single `session-end-*.log` across the
whole project history. `/remember:doctor` then reports "SessionEnd has never fired
for this project" and attributes it to hook registration -- sending the user to
debug a registration that is fine.

**The `Hook cancelled` itself is #560, already diagnosed and already fixed.**
Claude Code gives SessionEnd a 1.5-second budget shared across every hook on the
event, and this hook's synchronous preamble (`lib-clock.sh`, `resolve-paths.sh`,
`detect-tools.sh`, `bootstrap-dirs.sh`, `log.sh`) was measured at ~3.4s on a slow
Windows/Git-Bash machine. #560's fix declares `"timeout": 10` to raise that
ceiling, and `docs/hooks.md` carries the rest, including the
`CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` fallback for the plugin-install case
where Claude Code's own carve-out means a declared timeout does not raise it. The
#647 reporter is on 0.29.1 and predates that fix; nothing in this file re-fixes
it. What is here is the two things #560 left standing.

**1. That `timeout: 10` reads exactly like an arbitrary leftover.** It is the only
timed hook in `hooks.json` -- `SessionStart`, `UserPromptSubmit` and `PostToolUse`
all run untimed -- and #647's first suggestion is to drop or raise it on that
reading. Dropping it puts the ceiling back to 1.5s and fails silently: no error,
just sessions quietly losing their tails on slower machines. The test below pins
it so the next reader who finds it odd finds the reason with it. (An earlier
version of this very file asserted the opposite, on exactly that misreading,
before `docs/hooks.md` was checked.)

**2. A hook that exits early leaves no trace at all.** The `logs/autonomous/` seed
write sat at the very bottom of the script, after `resolve-paths.sh`,
`detect-tools.sh`, `bootstrap-dirs.sh`, `log.sh`, the `$REMEMBER_DIR` check and
the `$SAVE_SCRIPT` existence check. Every one of those early exits left the store
in precisely the state an unregistered hook leaves it in, which is the only state
doctor can read. A missing `save-session.sh` -- a half-finished install, the case
the hook's own `report_error` text says "Reinstall the plugin" for -- was
indistinguishable, to doctor, from a SessionEnd that never fired.

The fixture drives that through the missing-`save-session.sh` path because it is
deterministic and needs no timing: the trace has to be on disk by the time the
hook returns, whatever it decided to do afterwards. The paired positive control
runs the same fixture with the script present, so "a file exists" is not passing
against a harness that writes it unconditionally.

**Scope, stated rather than implied.** Moving the seed up does not rescue a hook
cancelled during the preamble -- the seed needs `$REMEMBER_DIR`, and resolving it
is part of what spends the budget. On the #647 reporter's machine this changes
nothing by itself; #560's timeout is what does. It closes the narrower gap: every
exit path *after* resolution now leaves evidence, so "fired and gave up" stops
looking like "never fired". Making the preamble itself fit inside the default 1.5s
is the larger change `docs/hooks.md` already tracks separately.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Deliberately NOT a module-level blanket win32 skip. The hooks.json contract
# test below reads a JSON file and spawns nothing, and it is the assertion that
# most needs to keep running on the platform the issue was reported from -- a
# whole-file skip would take it out on exactly that leg. The skip goes on the
# two subprocess tests instead (docs/windows-skip-triage.md's own preferred
# route: no blanket skip, so no triage row).
_needs_posix_bash = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_END = REPO_ROOT / "scripts" / "session-end-hook.sh"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

SESSION = "ffffffff-0000-4000-8000-000000000647"


def test_session_end_keeps_its_declared_timeout():
    """SessionEnd's `timeout` must stay, and must stay 10s.

    It reads like an arbitrary leftover -- the only timed hook in the file,
    while `SessionStart`, `UserPromptSubmit` and `PostToolUse` all run untimed
    -- and #647 proposes dropping or raising it on exactly that reading. It is
    the opposite. #560 ADDED it: Claude Code gives SessionEnd a **1.5-second
    budget shared across every hook registered for the event**, and a `timeout`
    declared on the hook is what raises that ceiling. The untimed siblings are
    untimed because their events carry no such shared budget to raise.

    So removing it is a regression, and a silent one -- nothing fails, sessions
    just start losing their tails on slower machines. That is what this test is
    for. The value is pinned to 10s because `docs/hooks.md` tells a user whose
    logs are still missing to set
    `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS=10000` "matching the `timeout` this
    hook declares"; if one moves, the doc is wrong about the other.

    What this does NOT assert is that the declaration is sufficient. Claude
    Code's own reference says "Timeouts set on plugin-provided hooks don't raise
    the budget", and `hooks/hooks.json` is exactly that location -- so on a
    plugin install the ceiling can stay 1.5s no matter what this file says. That
    gap is #560's, documented in `docs/hooks.md`, and is not something a test in
    this repo can close.
    """
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    declared = [
        hook.get("timeout")
        for matcher in hooks.get("SessionEnd", [])
        for hook in matcher.get("hooks", [])
    ]
    assert declared == [10], (
        f"SessionEnd's declared timeout is {declared}, expected [10]. This is not "
        "a stray value to tidy away: SessionEnd runs under a 1.5s budget shared "
        "across every hook on the event, and this declaration is what raises it "
        "(#560). Dropping it costs the last-chance flush its budget on any "
        "machine whose hook preamble runs longer than 1.5s -- measured at ~3.4s "
        "on a slow Windows/Git-Bash box -- and fails silently, as `Hook "
        "cancelled` with no session-end-*.log. Changing the number desynchronises "
        "docs/hooks.md, which tells users to set "
        "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS to match it."
    )


def _store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    return home, project, remember


def _plugin_root(tmp_path, *, with_save_script: bool):
    """The real plugin root, optionally missing scripts/save-session.sh."""
    root = tmp_path / "plugin-root"
    (root / "scripts").mkdir(parents=True)
    for entry in REPO_ROOT.iterdir():
        if entry.name != "scripts":
            (root / entry.name).symlink_to(entry)
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.name == "save-session.sh" and not with_save_script:
            continue
        (root / "scripts" / entry.name).symlink_to(entry)
    if with_save_script:
        # Replace the real flush with a no-op: this fixture is about the trace,
        # not about what a save does, and a real save would make a model call.
        (root / "scripts" / "save-session.sh").unlink()
        stub = root / "scripts" / "save-session.sh"
        stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    return root


def _fire(tmp_path, *, with_save_script: bool):
    home, project, remember = _store(tmp_path)
    root = _plugin_root(tmp_path, with_save_script=with_save_script)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(root),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    payload = json.dumps(
        {
            "session_id": SESSION,
            "reason": "other",
            "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
            "cwd": str(project),
        }
    )
    result = subprocess.run(
        ["bash", str(SESSION_END)],
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    logs = sorted((remember / "logs" / "autonomous").glob("session-end-*.log"))
    return result, logs


@_needs_posix_bash
def test_trace_is_written_even_when_the_flush_cannot_run(tmp_path):
    """A hook that gives up early must still say it fired.

    `save-session.sh` missing is the hook's own documented "Reinstall the
    plugin" path. Today it exits before the seed write, so doctor sees exactly
    what an unregistered hook leaves behind: nothing.
    """
    result, logs = _fire(tmp_path, with_save_script=False)
    assert logs, (
        "SessionEnd ran and returned 0, but wrote no "
        "logs/autonomous/session-end-*.log, so nothing on disk distinguishes "
        "this run from a hook that never fired -- which is what "
        "/remember:doctor reports, blaming hook registration. The seed write "
        "sits below the early exits it needs to survive. This is #647.\n"
        "stderr: " + result.stderr[:800]
    )


@_needs_posix_bash
def test_trace_is_written_on_an_ordinary_run(tmp_path):
    """Positive control for the assertion above.

    Without this, a harness that could never produce a log file at all -- wrong
    store, wrong glob, a hook that died before doing anything -- would make the
    first test's failure mean nothing.
    """
    result, logs = _fire(tmp_path, with_save_script=True)
    assert logs, (
        "SessionEnd wrote no logs/autonomous/session-end-*.log on an ORDINARY "
        "run, so this fixture cannot observe the trace at all and the "
        "early-exit assertion above proves nothing.\nstderr: " + result.stderr[:800]
    )
