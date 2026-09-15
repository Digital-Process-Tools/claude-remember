"""THROWAWAY end-to-end init benchmark for #660 -- delete with the branch.

Not a pytest test and not collected by the suite: it needs the `claude` CLI
on PATH, which the normal matrix does not have. It is run by
.github/workflows/diag-660-init.yml, and by hand the same way:

    npm i -g @anthropic-ai/claude-code
    python tests/diag660_init_bench.py

WHAT THIS MEASURES THAT NOTHING ELSE IN THIS REPO DOES
------------------------------------------------------
Every existing #660 instrument -- the #669 benchmark, the round-3 trace in
test_zzz_diag_660_where_time_goes.py -- invokes scripts/session-start-hook.sh
directly. They are therefore structurally blind to what the reporter measured
on 2026-09-15: a full Claude Code cold start at 18.3s median with the plugin
enabled against 6.8s disabled, an 11.5s gap, of which the hook's own p50
accounts for about 2.2s. Their words: "most of the gap sits outside the
hook's own path."

This runs the real `claude` binary with the real plugin installed the real
way (a local marketplace), against a local stub endpoint, and reports three
numbers per run:

  wall            -- launch to process exit
  first-request   -- launch to the first API call reaching the stub. This is
                     the init cost: plugin load, SessionStart hook, context
                     ingestion -- everything before the first token is asked
                     for.
  hook durationMs -- Claude Code's own hook record, read back out of the
                     transcript. The SAME field #660 was opened on.

POSITIVE CONTROL
----------------
The no-plugin arm must report a dash for hook durationMs and every plugin arm
must report a number. An arm where the plugin silently failed to install
would otherwise look exactly like a fast one (CLAUDE.md: a negative assertion
needs a positive control). The runner exits non-zero if that inversion holds.

WHAT CI CANNOT SEE, STATED SO NOBODY READS IT BACK IN
-----------------------------------------------------
  * windows-latest reports RealTimeProtectionEnabled: False with both drives
    already on the exclusion list. No antivirus tax is measurable here, at
    any effort.
  * The runner is x86-64. The original #660 reporter is on ARM64, where x64
    jq/git/coreutils under emulation is a per-fork multiplier this cannot
    reproduce.
  * This measures the CLI. The hard 60,000ms ceiling that actually breaks
    sessions belongs to the VS Code extension, which CI cannot launch.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.diag660_stub_api import StubServer
from tests.test_session_start_windows_benchmark_669 import (
    _write_no_crlf,
)
from tests.test_zzz_diag_660_where_time_goes import (
    _fatten,
    _litter_transcripts,
)

RUNS = int(os.environ.get("DIAG660_RUNS", "5"))

# name, install plugin?, store_kb, previous transcript MB, slices, staging
# The store shapes are the round-3 scenarios, so this table and the hook-only
# table in test_zzz_diag_660_where_time_goes.py describe the same stores and
# can be read against each other.
ARMS = (
    ("no-plugin", False, 0, 0.0, 0, 0),
    ("plugin-fresh-store", True, 0, 0.0, 0, 0),
    ("plugin-year-old-store", True, 6_000, 20.0, 60, 3),
    ("plugin-abandoned-store", True, 20_000, 100.0, 250, 10),
)


def _claude():
    exe = shutil.which("claude")
    if not exe:
        sys.exit("no `claude` on PATH -- npm i -g @anthropic-ai/claude-code")
    return exe


def _marketplace(root):
    """A local marketplace whose one entry is this checkout.

    The `source` must be a path RELATIVE to the marketplace root; an absolute
    one is rejected with `source: Invalid input`. A symlink is what keeps the
    checkout where it is.
    """
    mkt = root / "marketplace"
    (mkt / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    link = mkt / "remember"
    if not link.exists():
        try:
            link.symlink_to(REPO_ROOT, target_is_directory=True)
        except OSError:
            # Windows without developer mode has no symlink privilege. A copy
            # costs a second and measures the same plugin.
            shutil.copytree(REPO_ROOT, link,
                            ignore=shutil.ignore_patterns(".git"))
    _write_no_crlf(
        mkt / ".claude-plugin" / "marketplace.json",
        json.dumps({
            "name": "local660",
            "owner": {"name": "local"},
            "plugins": [{"name": "remember", "source": "./remember",
                         "description": "this checkout"}],
        }),
    )
    return mkt


def _arm_dirs(root, name):
    home = root / name / "home"
    project = root / name / "project"
    (project / ".remember" / "tmp").mkdir(parents=True, exist_ok=True)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    # Onboarding and trust, or the run stops on a prompt instead of measuring.
    _write_no_crlf(home / ".claude.json",
                   json.dumps({"hasCompletedOnboarding": True,
                               "hasTrustDialogAccepted": True}))
    _write_no_crlf(home / ".claude" / "settings.json", json.dumps({}))
    return home, project


def _env(home, project, port):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        # Windows resolves the user directory from USERPROFILE, not HOME.
        "USERPROFILE": str(home),
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:" + str(port),
        "ANTHROPIC_API_KEY": "sk-ant-stub",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    for leak in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "ANTHROPIC_MODEL", "REMEMBER_DIR"):
        env.pop(leak, None)
    return env


def _install_plugin(exe, env, mkt):
    for args in (["plugin", "marketplace", "add", str(mkt)],
                 ["plugin", "install", "remember@local660"]):
        done = subprocess.run([exe] + args, env=env, capture_output=True,
                              text=True, check=False)
        if done.returncode != 0:
            sys.exit("plugin setup failed: " + " ".join(args) + "\n"
                     + done.stdout + done.stderr)


def _hook_ms(home):
    """SessionStart durationMs from the newest transcript that has one.

    Newest-FIRST across every transcript, not "read the newest file and
    accept whatever it holds": the fixture litters the same directory with
    synthetic past transcripts that carry no hook record, and one of those
    winning the mtime race made a real plugin arm report a dash -- which is
    also what a plugin that failed to load reports. The positive control has
    to be unambiguous, so the read cannot have a second way to say nothing.
    """
    projects = home / ".claude" / "projects"
    files = sorted(projects.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    for path in files:
        found = None
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                attachment = record.get("attachment") or {}
                name = str(attachment.get("hookName", ""))
                # Only hook_success carries durationMs. The same session also
                # writes hook_system_message (the star-ask) and
                # hook_additional_context (the injected memory) under the same
                # hookName with durationMs absent -- reading the LAST match
                # therefore reported None for a hook that had just been timed
                # at 1536ms, which the positive control could not tell from a
                # plugin that never loaded.
                is_success = attachment.get("type") == "hook_success"
                if name.startswith("SessionStart") and is_success:
                    found = attachment.get("durationMs")
        if found is not None:
            return found
    return None


def _run_once(exe, env, project, stub):
    t0 = time.time()
    done = subprocess.run([exe, "-p", "ok"], env=env, cwd=str(project),
                          capture_output=True, text=True, check=False)
    wall = time.time() - t0
    first = stub.first_hit_after(t0)
    # A run that failed fast is the cheapest way to produce a flattering
    # number, so it is never allowed to pass as a measurement: the stub
    # answers "ok", and anything else means the start did not complete.
    ok = done.returncode == 0 and done.stdout.strip() == "ok"
    if not ok:
        print(f"    RUN FAILED rc={done.returncode} stdout={done.stdout[:200]!r} stderr={done.stderr[:200]!r}")
    return wall, (None if first is None else first - t0), ok


def _hook_ms_settled(home, deadline=3.0):
    """_hook_ms, retried until the transcript has been flushed.

    Claude Code writes the hook record around process exit, not before it,
    so a read immediately after wait() can miss it -- observed locally as a
    dash on run 1 of an arm and a number on run 2 of the same arm.
    """
    end = time.time() + deadline
    while True:
        found = _hook_ms(home)
        if found is not None or time.time() >= end:
            return found
        time.sleep(0.1)


def main():
    exe = _claude()
    root = Path(os.environ.get("DIAG660_ROOT")
                or (REPO_ROOT / ".diag660")).resolve()
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    mkt = _marketplace(root)

    stub = StubServer().start()
    rows = []
    failures = []
    try:
        for name, with_plugin, store_kb, previous_mb, slices, staging in ARMS:
            home, project = _arm_dirs(root, name)
            if store_kb:
                _fatten(project / ".remember", store_kb, slices, staging)
                _litter_transcripts(home, project, 50, previous_mb)
            env = _env(home, project, stub.port)
            if with_plugin:
                _install_plugin(exe, env, mkt)
            for i in range(RUNS):
                wall, first, ok = _run_once(exe, env, project, stub)
                hook = _hook_ms_settled(home)
                if not ok:
                    failures.append(f"{name} run {i + 1}")
                rows.append((name, i + 1, wall, first, hook))
                print("  {:<24} run {}  wall {:.2f}s  first-request {}  "
                      "hook {}".format(
                          name, i + 1, wall,
                          "n/a" if first is None else f"+{first:.2f}s",
                          "-" if hook is None else str(hook) + "ms"),
                      flush=True)
    finally:
        stub.stop()

    print()
    print("| arm | runs | median wall | median first-request | "
          "median SessionStart hook |")
    print("|---|---|---|---|---|")
    medians = {}
    for name, with_plugin, store_kb, previous_mb, slices, staging in ARMS:
        mine = [r for r in rows if r[0] == name]
        if not mine:
            continue
        walls = [r[2] for r in mine]
        firsts = [r[3] for r in mine if r[3] is not None]
        hooks = [r[4] for r in mine if r[4] is not None]
        medians[name] = (statistics.median(walls),
                         statistics.median(firsts) if firsts else None,
                         statistics.median(hooks) if hooks else None)
        print("| {} | {} | {:.2f}s | {} | {} |".format(
            name, len(mine), medians[name][0],
            "n/a" if medians[name][1] is None
            else f"{medians[name][1]:.2f}s",
            "-" if medians[name][2] is None
            else f"{medians[name][2]:.0f}ms"))

    # Positive control: the no-plugin arm must have NO hook record and every
    # plugin arm must have one. Without this, an arm whose plugin silently
    # failed to install reads as a fast arm.
    bad = []
    if failures:
        bad.append("runs that did not complete: " + ", ".join(failures))
    if medians.get("no-plugin", (0, 0, None))[2] is not None:
        bad.append("no-plugin arm recorded a SessionStart hook")
    for name, with_plugin, store_kb, previous_mb, slices, staging in ARMS:
        if with_plugin and medians.get(name, (0, 0, None))[2] is None:
            bad.append(name + " recorded no SessionStart hook -- the plugin "
                       "did not load, so its timings mean nothing")
    if bad:
        print()
        for line in bad:
            print("POSITIVE CONTROL FAILED: " + line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
