"""The star ask fires once the store has demonstrably done something for the
user, never at install time (#657).

The maintainer's decision on the issue (comment, 2026-09-11) settles the gate:

    features.plugin_promos on, host carries systemMessage, existing promo
    cooldown clear, and `recent.md` is a non-empty regular file. Fires once
    per store.

`recent.md` is written only once a past day's staging has been consolidated
(pipeline/consolidate.py), so its presence needs no new counter and cannot be
tripped by a fresh install -- the maintainer's own "did something for you by
construction" argument. This suite pins that gate directly against the
shipped `promos.json` "star" entry and the `gate = "recent_nonempty"` case
`_remember_compute_promo()` now understands, reusing the subprocess/fixture
harness `tests/test_plugin_promo_574.py` already established for the sibling
#574 promo mechanism the star ask is bolted onto (same cooldown marker, same
rotation, same `systemMessage`-only/off-switch/host-gate rules -- none of
that is re-asserted here since #574's own suite already pins it for every
entry in promos.json, this one included).

Every "must not fire" case here is paired with the "must fire" positive
control at the top of the class: a broken harness or an unresolved gate
condition passes a not-fire assertion for free, so the class only earns its
verdict once the harness is shown able to say yes at all.
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

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

SESSION = "eeeeeeee-0000-4000-8000-000000000657"
STAR_ID = "star"
STAR_URL = "https://github.com/Digital-Process-Tools/claude-remember/stargazers"


def _shipped_star_entry():
    promos = json.loads((REPO_ROOT / "promos.json").read_text(encoding="utf-8"))["promos"]
    for entry in promos:
        if entry.get("id") == STAR_ID:
            return entry
    raise AssertionError("promos.json does not ship a 'star' entry")


def _store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    return home, project, remember


def _write_installed(home, keyed=None, version="2"):
    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    body = {"version": version, "plugins": keyed or {}}
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(body), encoding="utf-8")


def _env(home, project, remember, extra=None):
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
    }
    if extra:
        env.update(extra)
    return env


def _payload():
    return json.dumps(
        {
            "session_id": SESSION,
            "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
            "hook_event_name": "SessionStart",
            "cwd": "/does/not/matter",
        }
    )


def _installed_everything_else():
    """The other promos.json entries, marked installed -- so the star entry
    is the only candidate rotation could ever reach; without this, an
    ungated sibling earlier in the file wins the candidate slot first and
    the star gate is never actually exercised."""
    promos = json.loads((REPO_ROOT / "promos.json").read_text(encoding="utf-8"))["promos"]
    return {
        p["installed_key"]: [{"name": p["id"]}]
        for p in promos
        if p.get("installed_key") and p["id"] != STAR_ID
    }


def _run(tmp_path, write_recent, recent_body="a day was consolidated\n", write_installed=True, env_extra=None, check=False):
    home, project, remember = _store(tmp_path)
    if write_installed:
        _write_installed(home, _installed_everything_else())
    if write_recent == "file":
        (remember / "recent.md").write_text(recent_body, encoding="utf-8")
    elif write_recent == "dir":
        (remember / "recent.md").mkdir()
    elif write_recent == "empty":
        (remember / "recent.md").write_text("", encoding="utf-8")
    # else "absent": nothing written -- the fresh-install state.
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        input=_payload(),
        env=_env(home, project, remember, env_extra),
        capture_output=True,
        text=True,
        timeout=60,
        check=check,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, home, remember


class TestStarAskGate:
    """One positive control, then each gate condition's own negative."""

    def test_positive_control_fires_when_recent_md_is_a_nonempty_file(self, tmp_path):
        out, _home, _remember = _run(tmp_path, write_recent="file")
        parsed = json.loads(out)
        assert parsed.get("systemMessage"), (
            "the star ask must fire once recent.md is a non-empty regular "
            "file -- without this positive control, every negative below "
            "would pass just as well against a mechanism that never speaks"
        )
        msg = parsed["systemMessage"]
        assert msg.startswith("claude-remember:")
        assert STAR_URL.replace("https://", "") in msg
        assert "(off: features.plugin_promos)" in msg

    def test_shipped_entry_matches_this_suite(self):
        """A copy-edit to promos.json that renames or re-keys the star entry
        must fail here loudly, not silently desync this suite's STAR_ID/URL
        constants from the file they are meant to pin."""
        entry = _shipped_star_entry()
        assert entry.get("gate") == "recent_nonempty"
        assert entry.get("url") == STAR_URL
        assert "installed_key" not in entry

    def test_absent_recent_md_does_not_fire(self, tmp_path):
        """Fresh install -- no recent.md at all yet. Must stay silent."""
        out, _home, _remember = _run(tmp_path, write_recent="absent")
        assert "systemMessage" not in out

    def test_empty_recent_md_does_not_fire(self, tmp_path):
        """recent.md exists (perhaps created but never populated) but is
        zero bytes -- `-s` must refuse it exactly like an absent file."""
        out, _home, _remember = _run(tmp_path, write_recent="empty")
        assert "systemMessage" not in out

    def test_non_regular_recent_md_does_not_fire(self, tmp_path):
        """A directory at recent.md's path -- never treated as evidence of
        work done, the same class of non-regular-file refusal #653/#654
        apply at every marker WRITE site, read-side here."""
        out, _home, _remember = _run(tmp_path, write_recent="dir")
        assert "systemMessage" not in out

    def test_off_switch_suppresses_even_with_a_populated_recent_md(self, tmp_path):
        home, project, remember = _store(tmp_path)
        _write_installed(home, _installed_everything_else())
        (remember / "recent.md").write_text("hi\n", encoding="utf-8")
        (remember / "config.json").write_text(
            json.dumps({"features": {"plugin_promos": False}}), encoding="utf-8"
        )
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            input=_payload(),
            env=_env(home, project, remember),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "systemMessage" not in result.stdout

    def test_cooldown_suppresses_a_second_session_on_the_same_machine(self, tmp_path):
        out1, home, remember = _run(tmp_path, write_recent="file")
        assert "systemMessage" in json.loads(out1)

        result2 = subprocess.run(
            ["bash", str(SESSION_START)],
            input=_payload(),
            env=_env(home, remember.parent, remember),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result2.returncode == 0, result2.stderr
        assert "systemMessage" not in result2.stdout

    def test_never_emitted_when_suppress_promo_flag_is_set(self, tmp_path):
        """REMEMBER_SUPPRESS_PROMO (#596): the same Antigravity-delegation
        guard the #574 promo already honours must cover the star ask too --
        it is the SAME `_remember_compute_promo()` gate, not a second one,
        but this is asserted directly rather than assumed from that fact."""
        out, _home, _remember = _run(
            tmp_path, write_recent="file", env_extra={"REMEMBER_SUPPRESS_PROMO": "1"}
        )
        assert "systemMessage" not in out
