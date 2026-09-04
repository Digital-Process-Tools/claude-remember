"""Gemini CLI hook manifest (#456).

`.gemini/settings.json` is the manifest `gemini hooks migrate --from-claude`
produces when fed this repo's real `hooks/hooks.json` (observed against a
live `gemini-cli 0.57.0` install, not reconstructed from the issue's prose --
see the #456 changelog fragment and README section). The migration renames
event keys only; it carries every command string through verbatim, including
`${CLAUDE_PLUGIN_ROOT}`, which Gemini CLI never sets. #407 already fixed this
for our own scripts by reading `${PLUGIN_ROOT}` first
(`scripts/resolve-paths.sh`, `pipeline/host.PLUGIN_ROOT_VARS`), so the
checked-in manifest must spell it the vendor-neutral way rather than ship the
raw migration output unedited.

This is a manifest lint only, the same limit test_codex_manifest_410.py
states for its own file: no `gemini` binary runs in CI, so these tests prove
the file is well-formed JSON, uses Gemini's own documented event names, and
never spells the Claude-only variable -- not that Gemini actually loads it,
fires a hook, or expands `${PLUGIN_ROOT}` in a hook command at all (that is
explicitly out of scope per the #456 issue itself: "not yet observed").
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GEMINI_SETTINGS = REPO_ROOT / ".gemini" / "settings.json"

# https://github.com/google-gemini/gemini-cli bundled docs, hooks/index.md
# and hooks/reference.md, verified against the installed
# @google/gemini-cli 0.57.0 package's own bundle/docs/hooks/*.md -- the
# full set of lifecycle events Gemini CLI's hook engine recognises.
GEMINI_DOCUMENTED_EVENTS = {
    "BeforeTool",
    "AfterTool",
    "BeforeAgent",
    "AfterAgent",
    "Notification",
    "SessionStart",
    "SessionEnd",
    "PreCompress",
    "BeforeModel",
    "AfterModel",
    "BeforeToolSelection",
}

# Our own event -> Gemini's event, per `gemini hooks migrate --from-claude`
# run against this repo's real hooks/hooks.json (#456).
CLAUDE_TO_GEMINI_EVENT = {
    "SessionStart": "SessionStart",
    "SessionEnd": "SessionEnd",
    "UserPromptSubmit": "BeforeAgent",
    "PostToolUse": "AfterTool",
}


def _iter_commands():
    data = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
    for event, groups in data.get("hooks", {}).items():
        for gi, group in enumerate(groups):
            for hi, hook in enumerate(group.get("hooks", [])):
                yield event, gi, hi, hook


def test_gemini_settings_exists_and_is_valid_json():
    assert GEMINI_SETTINGS.is_file(), f"missing {GEMINI_SETTINGS}"
    data = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("hooks"), dict)


def test_gemini_settings_names_only_documented_events():
    data = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
    named = set(data.get("hooks", {}).keys())
    assert named, ".gemini/settings.json declares no events"
    unknown = named - GEMINI_DOCUMENTED_EVENTS
    assert not unknown, (
        f".gemini/settings.json names events Gemini CLI does not document: {sorted(unknown)}"
    )


def test_gemini_settings_maps_every_claude_event_per_migrate_table():
    """Every one of our own bound events (hooks/hooks.json) must appear
    under its migrated Gemini name, non-empty."""
    data = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
    hooks = data.get("hooks", {})
    for claude_event, gemini_event in CLAUDE_TO_GEMINI_EVENT.items():
        groups = hooks.get(gemini_event)
        assert groups, (
            f"{claude_event!r} maps to {gemini_event!r} per `gemini hooks migrate "
            f"--from-claude`, but .gemini/settings.json has no non-empty entry for it"
        )


def test_gemini_settings_unbound_events_are_empty_arrays():
    """`gemini hooks migrate` emits empty arrays for events we bind nothing
    to, rather than omitting the key -- the manifest should match that
    shape rather than silently drop them."""
    data = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
    hooks = data.get("hooks", {})
    bound_gemini_events = set(CLAUDE_TO_GEMINI_EVENT.values())
    for event in GEMINI_DOCUMENTED_EVENTS - bound_gemini_events:
        assert event in hooks, f"{event!r} missing from .gemini/settings.json"
        assert hooks[event] == [], (
            f"{event!r} should be an empty array (unbound), got {hooks[event]!r}"
        )


def test_gemini_settings_commands_are_type_command_and_non_empty():
    for event, gi, hi, hook in _iter_commands():
        assert hook.get("type") == "command", (
            f"{event}[{gi}].hooks[{hi}]: unsupported type {hook.get('type')!r}"
        )
        cmd = hook.get("command", "")
        assert isinstance(cmd, str) and cmd.strip(), (
            f"{event}[{gi}].hooks[{hi}]: empty/missing command"
        )


def test_gemini_settings_never_spells_claude_plugin_root():
    """The one substantive edit this manifest makes over the raw migration
    output: CLAUDE_PLUGIN_ROOT -> PLUGIN_ROOT, since Gemini CLI never sets
    the Claude-only name (#407)."""
    for event, gi, hi, hook in _iter_commands():
        cmd = hook.get("command", "")
        assert "CLAUDE_PLUGIN_ROOT" not in cmd, (
            f"{event}[{gi}].hooks[{hi}]: still spells CLAUDE_PLUGIN_ROOT: {cmd!r}"
        )


def test_gemini_settings_commands_use_plugin_root_var():
    for event, gi, hi, hook in _iter_commands():
        cmd = hook.get("command", "")
        assert re.search(r"\$\{PLUGIN_ROOT\}", cmd), (
            f"{event}[{gi}].hooks[{hi}]: command does not reference ${{PLUGIN_ROOT}}: {cmd!r}"
        )


def test_every_gemini_settings_script_reference_exists():
    pat = re.compile(r"\$\{PLUGIN_ROOT\}/(scripts/[A-Za-z0-9_./-]+\.sh)")
    found_any = False
    for event, gi, hi, hook in _iter_commands():
        cmd = hook.get("command", "")
        for rel in pat.findall(cmd):
            found_any = True
            path = REPO_ROOT / rel
            assert path.is_file(), (
                f"{event}[{gi}].hooks[{hi}]: references missing script {rel}"
            )
    assert found_any, "no script references found in .gemini/settings.json -- regex drift?"


def test_gemini_settings_reuses_existing_scripts_only():
    """Same limit as #410's Codex manifest: the migrated manifest binds
    Gemini events to the EXISTING scripts/*-hook.sh, not new hook code."""
    claude_hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    claude_commands = "\n".join(
        h.get("command", "")
        for groups in claude_hooks.get("hooks", {}).values()
        for group in groups
        for h in group.get("hooks", [])
    )
    claude_scripts = set(re.findall(r"scripts/[A-Za-z0-9_./-]+\.sh", claude_commands))
    assert claude_scripts, "could not extract any script names from hooks/hooks.json -- regex drift?"

    for event, gi, hi, hook in _iter_commands():
        cmd = hook.get("command", "")
        for rel in re.findall(r"scripts/[A-Za-z0-9_./-]+\.sh", cmd):
            assert rel in claude_scripts, (
                f"{event}[{gi}].hooks[{hi}]: references {rel}, which the Claude Code "
                "manifest does not use"
            )
