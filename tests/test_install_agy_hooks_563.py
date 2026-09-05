"""scripts/install_agy_hooks.py -- merges Remember's Antigravity hooks into
the shared ~/.gemini/config/hooks.json (#563).

Antigravity has no per-plugin manifest and no working variable substitution
in a shared hooks.json (no host-set plugin-root or project-dir variable --
pipeline/host.py's ANTIGRAVITY host declares neither, #563): every command
in the manifest has to be a literal, resolved absolute path, embedded at
install time. And the target file is SHARED across every plugin on the
machine -- unlike Claude Code's or Codex's own per-plugin hooks.json, an agy
install must not clobber another tool's own top-level hook name. Both of
those are what this installer -- and this test file -- exist to get right;
no live `agy` binary is needed to test either.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import install_agy_hooks as installer

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _plugin_root():
    return os.path.abspath(REPO_ROOT)


def test_build_entry_uses_literal_absolute_script_paths():
    """No ${VAR} placeholder survives -- Antigravity sets no variable this
    manifest could rely on (see the module docstring), so every command must
    already be a real, resolved path."""
    entry = installer.build_remember_entry(_plugin_root())
    for event in ("SessionStart", "PreInvocation", "Stop"):
        assert event in entry
        for handler in entry[event]:
            command = handler["command"]
            assert "${" not in command, f"{event} command still carries a placeholder: {command}"
            assert os.path.isabs(_plugin_root())
            assert _plugin_root() in command


def test_every_shipped_agy_hook_script_is_wired_into_the_installer():
    """The reverse direction of test_build_entry_references_scripts_that_exist
    (#563 review finding): that test only proves every script the installer
    NAMES exists on disk. This proves the other half -- every
    scripts/agy-*-hook.sh file that SHIPS is named somewhere in the built
    entry's commands. Without this, tests/test_hooks_json.py's own
    "every shipped *-hook.sh is wired" guard (which this repo's
    test_every_shipped_hook_script_is_wired carves an exception out of for
    the whole `agy-` family, on the strength of this file's coverage) would
    have nothing standing behind it for a script added later and never
    registered in _EVENT_SCRIPTS -- exactly the class of regression that
    guard exists to catch in the first place."""
    scripts_dir = os.path.join(REPO_ROOT, "scripts")
    entry = installer.build_remember_entry(_plugin_root())
    all_commands = "\n".join(
        h["command"] for handlers in entry.values() if isinstance(handlers, list) for h in handlers
    )
    shipped = sorted(
        name for name in os.listdir(scripts_dir)
        if name.startswith("agy-") and name.endswith("-hook.sh")
    )
    assert shipped, "no scripts/agy-*-hook.sh found -- did the port move/rename them?"
    for name in shipped:
        assert name in all_commands, (
            f"scripts/{name} ships with the plugin but install_agy_hooks.py's "
            f"build_remember_entry() never references it"
        )


def test_build_entry_only_names_confirmed_events():
    """#563's own scope: SessionStart, PreInvocation, Stop -- the events
    confirmed loading AND firing. PostInvocation is confirmed too but this
    plugin has no PostToolUse-shaped analogue for it (#563 non-goals), so it
    must not appear here just because it is available."""
    entry = installer.build_remember_entry(_plugin_root())
    named = set(entry.keys()) - {"enabled"}
    assert named == {"SessionStart", "PreInvocation", "Stop"}


def test_build_entry_references_scripts_that_exist():
    entry = installer.build_remember_entry(_plugin_root())
    for handlers in (entry["SessionStart"], entry["PreInvocation"], entry["Stop"]):
        for handler in handlers:
            assert handler["type"] == "command"
            # "bash \"/abs/path/scripts/whatever.sh\"" -- pull the path out.
            command = handler["command"]
            path = command.split('"')[1]
            assert os.path.isfile(path), f"references missing script: {path}"


def test_build_entry_command_survives_windows_shell_quoting():
    """#569: the test just above (`command.split('"')[1]; assert
    os.path.isfile(path)`) validates the emitted path with Python's OWN
    os.path resolver -- never with a shell -- so it passes on Windows
    whether or not the emitted command is actually executable there. This
    asserts the string-building contract directly instead: a
    backslash-bearing, drive-lettered plugin_root (the shape a real
    Windows install path takes) must not leave a raw backslash anywhere in
    the emitted `command` string. A raw Windows-style separator mixed with
    the forward slashes os.path.join already inserts on this (POSIX) test
    host is exactly the kind of value whose meaning is not guaranteed to
    survive the shell-string encoding it is placed into before agy
    executes it -- forward slashes are unambiguous under every quoting
    model in play (POSIX double-quote rules, cmd.exe's own quoting) and
    are accepted by every path API bash and Windows itself expose.

    REASONED, not observed: no Windows `agy` install was available to run
    the emitted command end to end (see docs/install-antigravity.md's own
    "Everything above is macOS ... Nothing here is claimed for ... Windows"
    caveat) -- this proves the command *string* is unambiguous, not that a
    live Windows `agy` executes it, which stays out of reach in this
    environment.
    """
    windows_root = r"C:\Users\Test User\AppData\Roaming\plugin"
    entry = installer.build_remember_entry(windows_root)
    event_scripts = {
        "SessionStart": "agy-session-start-hook.sh",
        "PreInvocation": "agy-pre-invocation-hook.sh",
        "Stop": "agy-stop-hook.sh",
    }
    for event, handlers in entry.items():
        if not isinstance(handlers, list):
            continue
        for handler in handlers:
            command = handler["command"]
            assert "\\" not in command, (
                f"{event} command still carries a raw backslash, ambiguous "
                f"under Windows shell-quoting rules: {command}"
            )
            path = command.split('"')[1]
            assert path.endswith(f"plugin/scripts/{event_scripts[event]}")


def test_merge_creates_file_and_dir_when_absent(tmp_path):
    target = tmp_path / "nested" / "hooks.json"
    installer.install(plugin_root=_plugin_root(), target=str(target))
    assert target.is_file()
    data = json.loads(target.read_text())
    assert "remember" in data
    assert set(data["remember"].keys()) - {"enabled"} == {"SessionStart", "PreInvocation", "Stop"}


def test_merge_preserves_other_top_level_hook_names(tmp_path):
    """The shared file is not this plugin's alone -- another tool's own
    top-level hook name must survive an install untouched (#563's own
    scope note: 'a plugin's own hooks.json is copied in ... and never
    loaded -- only the shared ... path loads', so THIS is the path real
    installs land on, alongside whatever else is already there)."""
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps({"someone_elses_hook": {"enabled": True, "Stop": []}}))
    installer.install(plugin_root=_plugin_root(), target=str(target))
    data = json.loads(target.read_text())
    assert "someone_elses_hook" in data
    assert data["someone_elses_hook"] == {"enabled": True, "Stop": []}
    assert "remember" in data


def test_merge_is_idempotent(tmp_path):
    target = tmp_path / "hooks.json"
    installer.install(plugin_root=_plugin_root(), target=str(target))
    first = json.loads(target.read_text())
    installer.install(plugin_root=_plugin_root(), target=str(target))
    second = json.loads(target.read_text())
    assert first == second


def test_merge_overwrites_only_the_remember_key(tmp_path):
    """A second install (say, from a different checkout path) must replace
    THIS plugin's own entry cleanly, not accumulate stale handlers beside
    the new ones."""
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps({
        "remember": {"enabled": True, "SessionStart": [{"type": "command", "command": "stale"}]},
    }))
    installer.install(plugin_root=_plugin_root(), target=str(target))
    data = json.loads(target.read_text())
    for handler in data["remember"]["SessionStart"]:
        assert handler["command"] != "stale"


def test_install_refuses_a_corrupt_existing_file_rather_than_wiping_it(tmp_path):
    """A hand-edited or half-written hooks.json must not be silently treated
    as empty and overwritten -- this file is shared across every plugin on
    the machine (see the module docstring), so "cannot parse it" and
    "genuinely nothing here" are different states with different correct
    actions: an absent file is safe to create fresh (see
    test_merge_creates_file_and_dir_when_absent, the paired must-succeed
    case), but a CORRUPT one may still hold another tool's real hook
    entries the parser just could not reach, and silently discarding them
    is data loss with no warning. install() must refuse and leave the file
    exactly as it was, rather than coercing "corrupt" into "empty"."""
    target = tmp_path / "hooks.json"
    original = "{not valid json"
    target.write_text(original)
    with pytest.raises(installer.CorruptHooksFile):
        installer.install(plugin_root=_plugin_root(), target=str(target))
    assert target.read_text() == original, "a refused install must not touch the existing file"


def test_install_creates_fresh_file_when_genuinely_absent(tmp_path):
    """Paired with the refusal above: a target that does not exist at all
    is the "nothing to lose" case and installs cleanly -- proven here
    again, explicitly beside the corrupt-file refusal, so the two are not
    confused with each other."""
    target = tmp_path / "hooks.json"
    installer.install(plugin_root=_plugin_root(), target=str(target))
    data = json.loads(target.read_text())
    assert "remember" in data
