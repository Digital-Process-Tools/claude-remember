"""Gemini CLI distributable extension packaging (#533).

Follow-up to #456. `.gemini/settings.json` (checked in by #456/PR 531) spells
every hook command with `${PLUGIN_ROOT}`. That variable cannot resolve there:
per the installed `@google/gemini-cli` 0.57.0 package's own bundled docs
(`bundle/docs/hooks/index.md` `### Environment variables`), a plain
project-scope `.gemini/settings.json` only gets ordinary shell expansion of
`GEMINI_PROJECT_DIR`, `GEMINI_PLANS_DIR`, `GEMINI_SESSION_ID`, `GEMINI_CWD`
and `CLAUDE_PROJECT_DIR` -- no plugin/extension-root alias exists there.
Gemini CLI's actual plugin-root template variable, `${extensionPath}`, is
substituted only inside an installed *extension*'s own `gemini-extension.json`
and `hooks/hooks.json` (`bundle/docs/extensions/reference.md` `## Variables`),
never inside a plain settings.json.

The fix has two halves, both pinned here:

1. `.gemini/settings.json` is kept, for the narrow case of developing inside
   this repo's own checkout, but every `${PLUGIN_ROOT}` is rewritten to
   `${GEMINI_PROJECT_DIR}` -- the one project-path variable a plain
   settings.json actually receives (`hooks/index.md`'s own configuration
   schema example uses exactly this: `$GEMINI_PROJECT_DIR/.gemini/hooks/...`).
2. A real, distributable Gemini extension is added at `.gemini/` --
   `.gemini/gemini-extension.json` (the manifest) and
   `.gemini/hooks/hooks.json` (the hook bindings, mirroring the shape of the
   existing Claude Code `hooks/hooks.json` this repo ships at the top level)
   -- with every hook command spelled using `${extensionPath}`, the variable
   that is actually documented to resolve there. Installed via
   `gemini extensions link <path-to-this-checkout>/.gemini`.

Same limit as `tests/test_gemini_manifest_456.py` and
`tests/test_codex_manifest_410.py`: no `gemini` binary runs in CI, and #532
means none of this has been driven against a live session either -- these
are manifest lints (valid JSON, documented event names, the right template
variable, existing scripts only), never proof that Gemini CLI loads any of
it or fires a hook.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GEMINI_SETTINGS = REPO_ROOT / ".gemini" / "settings.json"
EXTENSION_MANIFEST = REPO_ROOT / ".gemini" / "gemini-extension.json"
EXTENSION_HOOKS = REPO_ROOT / ".gemini" / "hooks" / "hooks.json"

# Same table #456 already pinned for .gemini/settings.json -- the extension's
# own hooks/hooks.json binds the identical set of our events to Gemini's.
CLAUDE_TO_GEMINI_EVENT = {
    "SessionStart": "SessionStart",
    "SessionEnd": "SessionEnd",
    "UserPromptSubmit": "BeforeAgent",
    "PostToolUse": "AfterTool",
}

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


def _iter_commands(manifest_path):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for event, groups in data.get("hooks", {}).items():
        for gi, group in enumerate(groups):
            for hi, hook in enumerate(group.get("hooks", [])):
                yield event, gi, hi, hook


# --- .gemini/settings.json: kept, but must never spell ${PLUGIN_ROOT} ---


def test_settings_json_never_spells_plugin_root():
    """${PLUGIN_ROOT} cannot resolve inside a plain project-scope
    settings.json on a live Gemini CLI install (#533) -- it must be gone."""
    found_any = False
    for event, gi, hi, hook in _iter_commands(GEMINI_SETTINGS):
        found_any = True
        cmd = hook.get("command", "")
        assert "PLUGIN_ROOT" not in cmd, (
            f"{event}[{gi}].hooks[{hi}]: still spells a PLUGIN_ROOT variant, "
            f"which cannot resolve inside settings.json: {cmd!r}"
        )
    assert found_any, "no hook commands found in .gemini/settings.json -- would pass vacuously"


def test_settings_json_commands_use_gemini_project_dir_var():
    """The one variable a plain settings.json hook command actually
    receives for a project-rooted path, per hooks/index.md's own
    configuration schema example."""
    found_any = False
    for event, gi, hi, hook in _iter_commands(GEMINI_SETTINGS):
        found_any = True
        cmd = hook.get("command", "")
        assert re.search(r"\$\{?GEMINI_PROJECT_DIR\}?", cmd), (
            f"{event}[{gi}].hooks[{hi}]: command does not reference "
            f"GEMINI_PROJECT_DIR: {cmd!r}"
        )
    assert found_any, "no hook commands found in .gemini/settings.json -- would pass vacuously"


# --- .gemini/gemini-extension.json: the manifest ---


def test_extension_manifest_exists_and_is_valid_json():
    assert EXTENSION_MANIFEST.is_file(), f"missing {EXTENSION_MANIFEST}"
    data = json.loads(EXTENSION_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_extension_manifest_has_name_and_version():
    data = json.loads(EXTENSION_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data.get("name"), str) and data["name"], "manifest needs a non-empty name"
    assert isinstance(data.get("version"), str) and data["version"], (
        "manifest needs a non-empty version"
    )


def test_extension_manifest_declares_no_hooks_key():
    """bundle/docs/extensions/reference.md: 'Note that hooks are not
    defined in the gemini-extension.json manifest' -- they live in the
    sibling hooks/hooks.json instead. A "hooks" key here would be silently
    ignored by a real install and is a sign this was copied from the wrong
    file."""
    data = json.loads(EXTENSION_MANIFEST.read_text(encoding="utf-8"))
    assert "hooks" not in data, "hooks belong in hooks/hooks.json, not gemini-extension.json"


# --- .gemini/hooks/hooks.json: the extension's own hook bindings ---


def test_extension_hooks_exists_and_is_valid_json():
    assert EXTENSION_HOOKS.is_file(), f"missing {EXTENSION_HOOKS}"
    data = json.loads(EXTENSION_HOOKS.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("hooks"), dict)


def test_extension_hooks_names_only_documented_events():
    data = json.loads(EXTENSION_HOOKS.read_text(encoding="utf-8"))
    named = set(data.get("hooks", {}).keys())
    assert named, ".gemini/hooks/hooks.json declares no events"
    unknown = named - GEMINI_DOCUMENTED_EVENTS
    assert not unknown, (
        f".gemini/hooks/hooks.json names events Gemini CLI does not document: {sorted(unknown)}"
    )


def test_extension_hooks_maps_every_claude_event():
    data = json.loads(EXTENSION_HOOKS.read_text(encoding="utf-8"))
    hooks = data.get("hooks", {})
    for claude_event, gemini_event in CLAUDE_TO_GEMINI_EVENT.items():
        groups = hooks.get(gemini_event)
        assert groups, (
            f"{claude_event!r} maps to {gemini_event!r}, but .gemini/hooks/hooks.json "
            f"has no non-empty entry for it"
        )


def test_extension_hooks_commands_use_extension_path_var():
    """The one variable documented to resolve inside hooks/hooks.json
    (bundle/docs/extensions/reference.md ## Variables) -- never
    ${PLUGIN_ROOT}, which is this repo's own vendor-neutral name for a
    Claude-plugin-shaped root and is not a Gemini CLI concept at all."""
    found_any = False
    for event, gi, hi, hook in _iter_commands(EXTENSION_HOOKS):
        found_any = True
        cmd = hook.get("command", "")
        assert re.search(r"\$\{extensionPath\}", cmd), (
            f"{event}[{gi}].hooks[{hi}]: command does not reference "
            f"${{extensionPath}}: {cmd!r}"
        )
        assert "PLUGIN_ROOT" not in cmd, (
            f"{event}[{gi}].hooks[{hi}]: spells a PLUGIN_ROOT variant, which "
            f"is not a Gemini CLI extension variable: {cmd!r}"
        )
    assert found_any, "no hook commands found in .gemini/hooks/hooks.json -- would pass vacuously"


def test_extension_hooks_reuses_existing_scripts_only():
    """Same limit #410/#456 already state for the other manifests: this
    binds Gemini events to the EXISTING scripts/*-hook.sh, not new hook
    code, and every script it references must actually exist relative to
    the extension root (.gemini/../scripts == repo-root scripts/)."""
    claude_hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    claude_scripts = set(
        re.findall(
            r"scripts/[A-Za-z0-9_./-]+\.sh",
            "\n".join(
                h.get("command", "")
                for groups in claude_hooks.get("hooks", {}).values()
                for group in groups
                for h in group.get("hooks", [])
            ),
        )
    )
    assert claude_scripts, "could not extract any script names from hooks/hooks.json -- regex drift?"

    pat = re.compile(r"\$\{extensionPath\}/\.\./(scripts/[A-Za-z0-9_./-]+\.sh)")
    found_any = False
    for event, gi, hi, hook in _iter_commands(EXTENSION_HOOKS):
        cmd = hook.get("command", "")
        for rel in pat.findall(cmd):
            found_any = True
            assert rel in claude_scripts, (
                f"{event}[{gi}].hooks[{hi}]: references {rel}, which the Claude "
                "Code manifest does not use"
            )
            assert (REPO_ROOT / rel).is_file(), (
                f"{event}[{gi}].hooks[{hi}]: references missing script {rel}"
            )
    assert found_any, "no scripts/*.sh references found via ${extensionPath}/.. -- regex drift?"
