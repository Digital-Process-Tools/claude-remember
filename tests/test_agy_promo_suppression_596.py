"""The Antigravity (agy) SessionStart delegation path discards
session-start-hook.sh's stdout (`>/dev/null`, protojson mismatch, #563), so
a promo composed on that path never reaches any human -- yet the emitting
script committed the machine-global throttle/rotation marker on any
successful `printf`, and `printf` to `/dev/null` succeeds (#596). One
Antigravity session start burned the whole `cooldowns.promo_seconds`
window for every Claude Code session on the same machine, for a promo
nobody ever saw.

Fixed at the point stdout is known to be discarded (the agy delegation
call itself), not by having session-start-hook.sh try to detect its own
stdout target: a real caller's pipe (Claude Code capturing this hook's
stdout for hookSpecificOutput) is *also* not a tty, so `[ -t 1 ]` cannot
tell "discarded" from "captured normally" -- there is no reliable
in-script signal to gate on. agy-session-start-hook.sh is the one place
that KNOWS its delegate's stdout goes nowhere, so it is the one place that
suppresses the promo, via `REMEMBER_SUPPRESS_PROMO=1` read by
session-start-hook.sh before `_remember_compute_promo` ever runs -- so the
marker is never armed and the whole cooldown/rotation window is left
untouched for a real Claude Code session to actually show the promo in.

Two fixtures, same promo-eligible setup (a well-formed promos.json entry,
installed_plugins.json confirming the plugin genuinely absent):
must-not-fire through the agy delegation path, must-still-fire on the
direct Claude Code path -- without the positive control, a change that
disabled the marker/promo everywhere would pass the first test just as
well while breaking the feature entirely.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
AGY_SESSION_START = REPO_ROOT / "scripts" / "agy-session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

SESSION = "eeeeeeee-0000-4000-8000-000000000596"

# The one promo this repo actually ships that has no installed_key
# collision risk -- read from the real promos.json elsewhere in the suite
# (test_plugin_promo_574.py); reused here by key rather than duplicated.
SUPERTOOL_KEY = "supertool@dpt-plugins"


def _store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    return home, project, remember


def _write_installed_empty(home):
    """installed_plugins.json, version 2, with NOTHING installed -- the
    genuinely-not-installed case that MUST make the promo fire (#574
    decision 3), so a suite that only tests suppression could not be
    passed by an emitter that is silently broken and never speaks."""
    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    body = {"version": "2", "plugins": {}}
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(body), encoding="utf-8")


def _env(home, project, remember):
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        # Deliberately NOT "_LIB_MEMORY_DIR_LOADED" -- see test_plugin_promo_574.py's
        # own comment: this suite needs config.json actually merged and read,
        # not the hardcoded-default shortcut some other suites use.
    }


def _marker(home) -> Path:
    return home / ".remember" / "tmp" / "promo-notice"


def _cc_payload():
    return json.dumps({
        "session_id": SESSION,
        "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
        "hook_event_name": "SessionStart",
        "cwd": "/does/not/matter",
    })


def _agy_payload():
    return json.dumps({
        "conversationId": SESSION,
        "transcriptPath": "/does/not/matter/" + SESSION + ".jsonl",
        "workspacePaths": ["/does/not/matter"],
    })


def test_positive_control_direct_claude_code_path_still_arms_marker(tmp_path):
    """Must-fire control: invoked the ordinary way (stdout not discarded),
    a genuinely-not-installed promo still shows AND commits the marker --
    proving the #596 fix does not disable the feature everywhere, only on
    the delegation path that cannot deliver it."""
    home, project, remember = _store(tmp_path)
    _write_installed_empty(home)
    marker = _marker(home)

    result = subprocess.run(
        ["bash", str(SESSION_START)],
        input=_cc_payload(),
        env=_env(home, project, remember),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed.get("systemMessage")
    assert marker.exists(), (
        "the direct Claude Code path must still commit the throttle marker "
        "when the promo actually reached stdout"
    )


def test_agy_delegation_path_must_not_arm_marker(tmp_path):
    """Must-not-fire: the same genuinely-not-installed promo, driven through
    agy-session-start-hook.sh (stdout discarded to /dev/null) must NOT
    commit the marker -- nothing was ever shown, so nothing should be
    throttled."""
    home, project, remember = _store(tmp_path)
    _write_installed_empty(home)
    marker = _marker(home)

    result = subprocess.run(
        ["bash", str(AGY_SESSION_START)],
        input=_agy_payload(),
        env=_env(home, project, remember),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not marker.exists(), (
        "agy-session-start-hook.sh discards its delegate's stdout, so a "
        "promo composed on this path is never shown to anyone -- the "
        "marker must not be committed either (#596)"
    )

    # And the cooldown/rotation window must be genuinely untouched: a real
    # Claude Code session on the same machine, right after, must still get
    # to show the promo.
    result2 = subprocess.run(
        ["bash", str(SESSION_START)],
        input=_cc_payload(),
        env=_env(home, project, remember),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result2.returncode == 0, result2.stderr
    parsed2 = json.loads(result2.stdout)
    assert parsed2.get("systemMessage"), (
        "the agy delegation path must not have burned the cooldown for a "
        "promo it could never show"
    )
    assert marker.exists()


