#!/usr/bin/env python3
"""Smoke-test a built release tree before it is published (#851).

Two parts:

1. `claude plugin validate --strict TREE` -- the same validator the directory
   documents. `--validate auto` skips it, out loud, when no `claude` CLI is on
   PATH; `--validate require` fails instead; `--validate skip` never runs it.
2. Every command hooks/hooks.json names is run once, in file order, with a
   minimal JSON payload for its event on stdin (session_id, transcript_path,
   cwd, hook_event_name, plus source/prompt/tool_*/reason as the event has
   them). Each hook must exit 0.

Isolation -- nothing here may touch real memory or the tree that ships:

- the hooks run against a COPY of the tree (CLAUDE_PLUGIN_ROOT), so a
  `__pycache__` or a log written beside the scripts never reaches the commit;
- HOME, CLAUDE_PROJECT_DIR, TMPDIR and XDG_* all point inside one temp
  directory; inherited CLAUDE_*/REMEMBER_*/GIT_* variables are dropped;
- a fake `claude` (and `codex`) is first on PATH and named by
  REMEMBER_CLAUDE_BIN/REMEMBER_CODEX_BIN: it records its argv and exits 1, so no
  hook can reach a model, bill anyone, or hang on the network;
- each hook runs as its own process group. Anything still alive in those groups
  (or naming the temp directory in its argv) after the last hook is waited for
  up to --linger seconds, then killed and reported. The temp directory is
  removed afterwards unless --keep is given.

Usage:
    smoke_release_tree.py TREE [--validate auto|require|skip] [--linger 30]
                               [--keep DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DROP_PREFIXES = ("CLAUDE", "REMEMBER_", "GIT_", "CODEX", "GEMINI", "ANTIGRAVITY")

FAKE_BIN = """#!/bin/sh
printf '%s\\n' "$0 $*" >> "${SMOKE_FAKE_CALLS:-/dev/null}"
echo "smoke: fake $(basename "$0") -- no model is reachable from the release smoke test" >&2
exit 1
"""


@dataclass
class HookRun:
    event: str
    command: str
    returncode: int
    seconds: float
    stdout: str
    stderr: str


@dataclass
class SmokeResult:
    hooks: list = field(default_factory=list)
    killed: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    after: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(h.returncode == 0 for h in self.hooks)

    def report(self) -> str:
        lines = list(self.notes)
        for h in self.hooks:
            verdict = "PASS" if h.returncode == 0 else "FAIL"
            lines.append(f"{verdict} {h.event}: exit {h.returncode} in {h.seconds:.1f}s, "
                         f"stdout {len(h.stdout)} bytes -- {h.command}")
            tail = h.stderr.strip().splitlines()[-5:]
            lines.extend(f"    stderr: {t}" for t in tail)
        for k in self.killed:
            lines.append(f"NOTE killed a process still running after the linger window: {k}")
        lines.extend(self.after)
        lines.extend(f"FAIL {e}" for e in self.errors)
        lines.append("smoke: OK" if self.ok else "smoke: FAILED")
        return "\n".join(lines)


def hook_commands(tree: Path) -> list:
    """(event, command, timeout) for every command hook in hooks/hooks.json."""
    doc = json.loads((Path(tree) / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    out = []
    for event, groups in doc.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                if hook.get("type") == "command":
                    out.append((event, hook["command"], hook.get("timeout")))
    return out


def payload_for(event: str, session_id: str, transcript: Path, cwd: Path) -> dict:
    base = {"session_id": session_id, "transcript_path": str(transcript),
            "cwd": str(cwd), "hook_event_name": event}
    extra = {
        "SessionStart": {"source": "startup"},
        "UserPromptSubmit": {"prompt": "hello from the release smoke test"},
        "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": "true"}},
        "PostToolUse": {"tool_name": "Bash", "tool_input": {"command": "true"},
                        "tool_response": {"stdout": "", "stderr": "", "interrupted": False}},
        "Stop": {"stop_hook_active": False},
        "SessionEnd": {"reason": "other"},
    }.get(event, {})
    base.update(extra)
    return base


def _transcript(home: Path, project: Path, session_id: str) -> Path:
    slug = "".join(c if c.isalnum() else "-" for c in str(project))
    d = home / ".claude" / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    rows = []
    for i in range(3):
        rows.append({"type": "user", "sessionId": session_id, "cwd": str(project),
                     "message": {"role": "user", "content": f"smoke prompt {i}"}})
        rows.append({"type": "assistant", "sessionId": session_id, "cwd": str(project),
                     "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": f"smoke reply {i}"}]}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _env(work: Path, plugin: Path, project: Path, home: Path, fakebin: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(DROP_PREFIXES)}
    tmp = work / "tmp"
    tmp.mkdir(exist_ok=True)
    env.update({
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "CLAUDECODE": "1",
        "TMPDIR": str(tmp),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "REMEMBER_CLAUDE_BIN": str(fakebin / "claude"),
        "REMEMBER_CODEX_BIN": str(fakebin / "codex"),
        "SMOKE_FAKE_CALLS": str(work / "fake-calls.log"),
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def _survivors(pgids: set, marker: str) -> list:
    """(pid, pgid, args) of live processes in one of PGIDS or naming MARKER."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,args="], capture_output=True,
                             text=True, check=False).stdout
    except OSError:
        return []
    me = os.getpid()
    found = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, pgid = int(parts[0]), int(parts[1])
        args = parts[2] if len(parts) > 2 else ""
        if pid == me or args.startswith("ps "):
            continue
        if pgid in pgids or marker in args:
            found.append((pid, pgid, args))
    return found


def _reap(pgids: set, marker: str, linger: float, result: SmokeResult) -> None:
    deadline = time.monotonic() + linger
    while time.monotonic() < deadline:
        if not _survivors(pgids, marker):
            return
        time.sleep(0.25)
    left = _survivors(pgids, marker)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, _pgid, args in left:
            try:
                os.kill(pid, sig)
                if sig == signal.SIGTERM:
                    result.killed.append(f"pid {pid}: {args}")
            except (ProcessLookupError, PermissionError):
                pass
        if sig == signal.SIGTERM:
            time.sleep(2)
            left = _survivors(pgids, marker)


def run_validate(tree: Path, mode: str, claude_bin: str | None, home: Path,
                 result: SmokeResult) -> None:
    if mode == "skip":
        result.notes.append("validate: SKIPPED (--validate skip)")
        return
    binary = claude_bin or shutil.which("claude")
    if not binary or not Path(binary).exists():
        msg = f"claude CLI not found ({binary or 'not on PATH'})"
        if mode == "require":
            result.errors.append(f"validate: {msg}")
        else:
            result.notes.append(f"validate: SKIPPED -- {msg}; "
                                "`claude plugin validate --strict` did not run")
        return
    env = {k: v for k, v in os.environ.items() if not k.startswith(DROP_PREFIXES)}
    env["HOME"] = str(home)
    r = subprocess.run([binary, "plugin", "validate", "--strict", str(tree)],
                       capture_output=True, text=True, env=env, check=False, timeout=300)
    output = (r.stdout + r.stderr).strip()
    result.notes.append(f"validate: `claude plugin validate --strict` exit {r.returncode}")
    result.notes.extend(f"    {line}" for line in output.splitlines())
    if r.returncode != 0:
        result.errors.append(f"validate: claude plugin validate --strict exited {r.returncode}")


def run_smoke(tree: Path, validate: str = "auto", claude_bin: str | None = None,
              linger_seconds: float = 30, keep_dir: Path | None = None) -> SmokeResult:
    tree = Path(tree).resolve()
    result = SmokeResult()
    if keep_dir:
        work = Path(keep_dir).resolve()
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="release-smoke-")).resolve()
    try:
        home, project, fakebin = work / "home", work / "project", work / "bin"
        for d in (home, project, fakebin):
            d.mkdir(parents=True, exist_ok=True)
        run_validate(tree, validate, claude_bin, home, result)

        if not (tree / "hooks" / "hooks.json").is_file():
            result.notes.append("hooks: no hooks/hooks.json in the tree, nothing to run")
            return result

        plugin = work / "plugin"
        shutil.copytree(tree, plugin, symlinks=True)
        for name in ("claude", "codex"):
            p = fakebin / name
            p.write_text(FAKE_BIN, encoding="utf-8")
            p.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(project)], check=False,
                       capture_output=True)
        session_id = str(uuid.uuid4())
        transcript = _transcript(home, project, session_id)
        env = _env(work, plugin, project, home, fakebin)

        pgids: set = set()
        for event, command, timeout in hook_commands(plugin):
            payload = json.dumps(payload_for(event, session_id, transcript, project))
            start = time.monotonic()
            proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=str(project), env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
            pgids.add(proc.pid)
            try:
                out, err = proc.communicate(payload, timeout=timeout or 60)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                rc = -9
                err += f"\nsmoke: killed after the hook's {timeout or 60}s timeout"
            result.hooks.append(HookRun(event, command, rc, time.monotonic() - start, out, err))

        _reap(pgids, str(work), linger_seconds, result)

        calls = work / "fake-calls.log"
        if calls.exists():
            n = len(calls.read_text(encoding="utf-8").splitlines())
            result.after.append(f"hooks: the fake claude/codex was called {n} time(s) "
                                "(and refused each one)")
        written = sum(1 for p in project.rglob("*") if p.is_file() and ".git" not in p.parts)
        result.after.append(f"hooks: {written} file(s) written under the temp project "
                            "(HOME, TMPDIR and the project all sat inside the temp directory)")
        # Not a verdict: with the model refused, error lines are expected. Shown so a
        # human reading the run sees anything else that went wrong behind an exit 0.
        for log in sorted(project.rglob("hook-errors.log")):
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                result.after.append(f"    hook-errors.log: {line}")
        return result
    finally:
        if not keep_dir:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tree")
    ap.add_argument("--validate", choices=("auto", "require", "skip"), default="auto")
    ap.add_argument("--claude-bin", default=None)
    ap.add_argument("--linger", type=float, default=30.0)
    ap.add_argument("--keep", default=None, help="keep the temp HOME/project here")
    args = ap.parse_args(argv)
    result = run_smoke(Path(args.tree), args.validate, args.claude_bin, args.linger,
                       Path(args.keep) if args.keep else None)
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
