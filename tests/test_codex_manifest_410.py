"""Codex plugin manifest, marketplace entry, and Codex-side hooks.json (#410).

Scaffolding only. No Codex binary is installed anywhere this repo's CI or a
maintainer's machine runs, so these tests can prove the three JSON files are
well-formed, name only events Codex documents, and reference scripts that
actually exist in the tree -- and nothing more. They cannot prove Codex loads
the plugin, discovers the marketplace entry, or fires a single hook. See the
`#410` changelog fragment and README section for that limit stated in prose.

Modelled on tests/test_hooks_json.py (the Claude Code manifest's own lint),
narrowed to structural checks -- there is no live Codex process to dry-parse
against, unlike bash -n for the Claude-side manifest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEX_MANIFEST = REPO_ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
CODEX_HOOKS = REPO_ROOT / "hooks" / "hooks.codex.json"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# https://learn.chatgpt.com/docs/hooks -- verified 2026-08-28 against the raw
# page text, not from memory. Codex documents exactly these eleven lifecycle
# events; SessionEnd does not fire for subagents.
CODEX_DOCUMENTED_EVENTS = {
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "UserPromptSubmit",
    "Stop",
}


def test_codex_plugin_manifest_exists_and_is_valid_json():
    assert CODEX_MANIFEST.is_file(), f"missing {CODEX_MANIFEST}"
    data = json.loads(CODEX_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_codex_plugin_manifest_has_required_fields():
    """https://developers.openai.com/codex/plugins/build -- name, version,
    description identify the plugin; verified 2026-08-28 against the raw
    page's minimal and complete manifest examples."""
    data = json.loads(CODEX_MANIFEST.read_text(encoding="utf-8"))
    for key in ("name", "version", "description"):
        assert key in data and str(data[key]).strip(), f"plugin.json missing {key!r}"
    assert data["name"] == "remember"


def test_codex_plugin_manifest_version_matches_claude_manifest():
    """One plugin, one version number -- a second manifest that drifts from
    the first would silently ship stale metadata the way #133 did for the
    Claude-side manifest alone (see test_version_manifest.py)."""
    claude_manifest = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    codex_manifest = json.loads(CODEX_MANIFEST.read_text(encoding="utf-8"))
    assert codex_manifest["version"] == claude_manifest["version"], (
        f".codex-plugin/plugin.json declares {codex_manifest['version']!r} but "
        f".claude-plugin/plugin.json declares {claude_manifest['version']!r} -- "
        "these must move together at release time."
    )


def test_codex_plugin_manifest_hooks_path_is_relative_and_resolves():
    """Per the docs: hook paths 'start with ./, resolve relative to the
    plugin root, and stay inside the plugin root.' Verified 2026-08-28
    against the raw page text (not the default hooks/hooks.json -- that
    path is already claimed by the Claude Code manifest's own convention,
    per #410's own scope: 'a second manifest pointing at the same scripts',
    i.e. a distinct file, not a shared one)."""
    data = json.loads(CODEX_MANIFEST.read_text(encoding="utf-8"))
    hooks_field = data.get("hooks")
    assert isinstance(hooks_field, str) and hooks_field.startswith("./"), (
        f"plugin.json 'hooks' field must be a './'-relative path, got {hooks_field!r}"
    )
    resolved = (REPO_ROOT / hooks_field[2:]).resolve()
    assert REPO_ROOT in resolved.parents or resolved == REPO_ROOT, (
        f"resolved hooks path {resolved} escapes the plugin root {REPO_ROOT}"
    )
    assert resolved == CODEX_HOOKS.resolve(), (
        f"plugin.json 'hooks' field resolves to {resolved}, expected {CODEX_HOOKS}"
    )
    assert resolved != (REPO_ROOT / "hooks" / "hooks.json").resolve(), (
        "Codex manifest must not point at the same hooks.json the Claude Code "
        "manifest already uses by default -- #410 asks for a second manifest, "
        "not a shared one"
    )


def test_marketplace_entry_exists_and_is_valid_json():
    assert MARKETPLACE.is_file(), f"missing {MARKETPLACE}"
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_marketplace_entry_lists_remember_with_required_fields():
    """https://developers.openai.com/codex/plugins/build -- verified
    2026-08-28: each plugins[] entry requires name, source,
    policy.installation, policy.authentication, category."""
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    plugins = data.get("plugins")
    assert isinstance(plugins, list) and plugins, "marketplace.json has no plugins[] entries"
    entries = {p.get("name"): p for p in plugins}
    assert "remember" in entries, f"marketplace.json does not list 'remember': {sorted(entries)}"
    entry = entries["remember"]
    source = entry.get("source")
    assert isinstance(source, dict) and source.get("source"), "remember entry missing source"
    policy = entry.get("policy")
    assert isinstance(policy, dict), "remember entry missing policy"
    assert policy.get("installation"), "remember entry missing policy.installation"
    assert policy.get("authentication"), "remember entry missing policy.authentication"
    assert entry.get("category"), "remember entry missing category"


def test_marketplace_local_source_path_resolves_inside_repo():
    """A 'local' source path must actually resolve somewhere in this repo,
    or 'codex plugin marketplace add' finds a plugin.json that isn't there."""
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    entries = {p.get("name"): p for p in data.get("plugins", [])}
    source = entries["remember"]["source"]
    if source.get("source") == "local":
        path = source.get("path", "")
        assert path.startswith("./"), f"local source path must be './'-relative: {path!r}"
        resolved = (REPO_ROOT / path).resolve()
        assert (resolved / ".codex-plugin" / "plugin.json").is_file(), (
            f"marketplace local source {resolved} has no .codex-plugin/plugin.json"
        )


def test_codex_hooks_json_exists_and_is_valid_json():
    assert CODEX_HOOKS.is_file(), f"missing {CODEX_HOOKS}"
    data = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("hooks"), dict)


def _iter_codex_commands():
    data = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))
    for event, groups in data.get("hooks", {}).items():
        for gi, group in enumerate(groups):
            for hi, hook in enumerate(group.get("hooks", [])):
                yield event, gi, hi, hook


def test_codex_hooks_json_names_only_documented_events():
    """A hook bound to an event Codex does not document is dead configuration
    at best -- Codex would presumably ignore an unrecognised key, but nothing
    here can verify that against a live install, so the manifest must not
    claim an event outside the published set in the first place."""
    data = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))
    named = set(data.get("hooks", {}).keys())
    assert named, "hooks.codex.json declares no events"
    unknown = named - CODEX_DOCUMENTED_EVENTS
    assert not unknown, (
        f"hooks.codex.json names events Codex does not document: {sorted(unknown)}"
    )


def test_codex_hooks_json_commands_are_type_command():
    for event, gi, hi, hook in _iter_codex_commands():
        assert hook.get("type") == "command", (
            f"{event}[{gi}].hooks[{hi}]: unsupported type {hook.get('type')!r}"
        )
        cmd = hook.get("command", "")
        assert isinstance(cmd, str) and cmd.strip(), (
            f"{event}[{gi}].hooks[{hi}]: empty/missing command"
        )


def test_every_codex_hooks_json_script_reference_exists():
    """Every script path the Codex manifest names must be a real file in the
    tree -- the same contract test_hooks_json.py already holds the Claude
    Code manifest to."""
    pat = re.compile(r"\$\{?PLUGIN_ROOT\}?/(scripts/[A-Za-z0-9_./-]+\.sh)")
    found_any = False
    for event, gi, hi, hook in _iter_codex_commands():
        cmd = hook.get("command", "")
        for rel in pat.findall(cmd):
            found_any = True
            path = REPO_ROOT / rel
            assert path.is_file(), (
                f"{event}[{gi}].hooks[{hi}]: references missing script {rel}"
            )
    assert found_any, "no script references found in hooks.codex.json -- regex drift?"


def test_codex_hooks_json_reuses_existing_scripts_only():
    """#410's scope is explicit: bind Codex events to the EXISTING
    scripts/*-hook.sh, not new hook code. Every command in the Codex
    manifest must reference a script that also ships for Claude Code --
    a script that exists only here would be new hook code in disguise."""
    claude_hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    claude_commands = "\n".join(
        h.get("command", "")
        for groups in claude_hooks.get("hooks", {}).values()
        for group in groups
        for h in group.get("hooks", [])
    )
    claude_scripts = set(re.findall(r"scripts/[A-Za-z0-9_./-]+\.sh", claude_commands))
    assert claude_scripts, "could not extract any script names from hooks/hooks.json -- regex drift?"

    for event, gi, hi, hook in _iter_codex_commands():
        cmd = hook.get("command", "")
        for rel in re.findall(r"scripts/[A-Za-z0-9_./-]+\.sh", cmd):
            assert rel in claude_scripts, (
                f"{event}[{gi}].hooks[{hi}]: references {rel}, which the Claude Code "
                "manifest does not use -- #410 scope is EXISTING scripts only"
            )


def test_codex_hooks_json_plugin_root_var_is_double_quoted():
    """Mirrors test_hooks_json.py's guard against 4d50166: an unquoted
    ${PLUGIN_ROOT} breaks on install paths with spaces."""
    for event, gi, hi, hook in _iter_codex_commands():
        cmd = hook.get("command", "")
        for m in re.finditer(r"\$\{?PLUGIN_ROOT\}?", cmd):
            before = cmd[: m.start()]
            after = cmd[m.end() :]
            opening = before.rfind('"')
            closing = after.find('"')
            assert opening != -1 and closing != -1, (
                f"{event}[{gi}].hooks[{hi}]: ${{PLUGIN_ROOT}} not inside double quotes"
            )
