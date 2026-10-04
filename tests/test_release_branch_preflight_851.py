"""#851 -- the directory pre-submission checklist, row by row, on a built tree.

The Anthropic directory (https://claude.com/docs/plugins/pre-submission-checklist)
blocks or holds a version on far more than size: the manifest, a 40-word README, a
licence, portable file names, symlinks/submodules/LFS pointers, a 5 MiB hard cap,
hooks.json shape and command form, skill/command/agent front matter, real
credentials, referenced images, launchers and package installs in hooks, a root
package.json with a lockfile, package-manager config files.
`.github/scripts/check_release_tree.py` checks every row it can decide mechanically.

Each failing case is paired with a passing twin that differs only in the property
under test; the base tree passes everything (that is the first test).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_tree.py"

MIB = 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
README_40 = "# Example\n\n" + " ".join(f"word{i}" for i in range(40)) + "\n"
GOOD_CMD = 'bash "${CLAUDE_PLUGIN_ROOT}/scripts/hook.sh"'


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#851)"
    spec = importlib.util.spec_from_file_location("check_release_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


def _manifest(**over) -> bytes:
    m = {"name": "example-plugin", "description": "d", "version": "1.0.0",
         "author": {"name": "A"}}
    m.update(over)
    return json.dumps({k: v for k, v in m.items() if v is not None}).encode()


def _hooks(command: str = GOOD_CMD, event: str = "SessionStart", type_: str = "command",
           raw: bytes | None = None) -> bytes:
    if raw is not None:
        return raw
    return json.dumps({"hooks": {event: [{"hooks": [{"type": type_, "command": command}]}]}}).encode()


SKILL = b"---\nname: s\ndescription: Does a thing.\n---\n\nBody.\n"


def _tree(tmp_path: Path, files: dict, drop: tuple = ()) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    base = {
        ".claude-plugin/plugin.json": _manifest(),
        "README.md": README_40.encode(),
        "LICENSE": b"license text\n",
        "hooks/hooks.json": _hooks(),
        "scripts/hook.sh": b"#!/bin/sh\nexit 0\n",
        "skills/s/SKILL.md": SKILL,
        "commands/c.md": b"---\ndescription: A command.\n---\n\nBody.\n",
    }
    base.update(files)
    for rel in drop:
        base.pop(rel)
    for rel, data in base.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def _offenders(root: Path) -> list:
    return _load().check_tree(root, {}).offenders


def _fails(root: Path, *needles: str) -> None:
    offenders = _offenders(root)
    assert any(all(n in o for n in needles) for o in offenders), (needles, offenders)


def _passes(root: Path) -> None:
    assert _offenders(root) == []


def test_the_base_tree_passes_every_row(tmp_path):
    _passes(_tree(tmp_path, {}))


# -- manifest -------------------------------------------------------------------

def test_missing_manifest_fails(tmp_path):
    _fails(_tree(tmp_path, {}, drop=(".claude-plugin/plugin.json",)), "plugin.json", "missing")


@pytest.mark.parametrize("name", ["Example", "ex_ample", "ex ample", "a" * 65, ""])
def test_bad_plugin_names_fail(tmp_path, name):
    _fails(_tree(tmp_path, {".claude-plugin/plugin.json": _manifest(name=name)}), "name")


@pytest.mark.parametrize("name", ["a" * 64, "remember", "claude-jit-context2"])
def test_good_plugin_names_pass(tmp_path, name):
    _passes(_tree(tmp_path, {".claude-plugin/plugin.json": _manifest(name=name)}))


@pytest.mark.parametrize("key", ["description", "version", "author"])
def test_manifest_without_a_required_field_fails(tmp_path, key):
    _fails(_tree(tmp_path, {".claude-plugin/plugin.json": _manifest(**{key: None})}), key)


def test_manifest_that_is_not_json_fails(tmp_path):
    _fails(_tree(tmp_path, {".claude-plugin/plugin.json": b"{nope"}), "plugin.json")


# -- README ---------------------------------------------------------------------

def test_readme_under_40_words_fails(tmp_path):
    short = "# Example\n\n" + " ".join(f"w{i}" for i in range(38)) + "\n"
    _fails(_tree(tmp_path, {"README.md": short.encode()}), "README.md", "40")


def test_code_blocks_do_not_count_toward_the_readme_words(tmp_path):
    text = ("# Example\n\n" + " ".join(f"w{i}" for i in range(30)) + "\n\n```\n"
            + " ".join(f"code{i}" for i in range(100)) + "\n```\n")
    _fails(_tree(tmp_path, {"README.md": text.encode()}), "README.md", "40")


def test_missing_readme_fails(tmp_path):
    _fails(_tree(tmp_path, {}, drop=("README.md",)), "README")


# -- licence --------------------------------------------------------------------

def test_no_licence_anywhere_fails(tmp_path):
    _fails(_tree(tmp_path, {}, drop=("LICENSE",)), "licen")


def test_a_licence_field_alone_passes(tmp_path):
    _passes(_tree(tmp_path, {".claude-plugin/plugin.json": _manifest(license="MIT")},
                  drop=("LICENSE",)))


# -- junk and names -------------------------------------------------------------

def test_macosx_folder_fails(tmp_path):
    _fails(_tree(tmp_path, {"__MACOSX/x.txt": b"x"}), "__MACOSX")


@pytest.mark.parametrize("name", ["CON", "con.txt", "nul.md", "Com1.json", "lpt9", "aux.tar.gz",
                                  "dir/prn/x.txt"])
def test_windows_device_names_fail(tmp_path, name):
    _fails(_tree(tmp_path, {name: b"x"}), "Windows")


@pytest.mark.parametrize("name", ["console.md", "com10.md", "nullable.txt", "auxiliary/x.txt"])
def test_names_that_only_start_like_a_device_pass(tmp_path, name):
    _passes(_tree(tmp_path, {name: b"x"}))


@pytest.mark.skipif(os.name == "nt", reason="cannot create these names on Windows at all")
@pytest.mark.parametrize("name", ["a:b.txt", "trailing.", "trailing ", "dir./x.txt"])
def test_names_invalid_on_windows_fail(tmp_path, name):
    _fails(_tree(tmp_path, {name: b"x"}), "Windows")


def _case_sensitive(tmp_path: Path) -> bool:
    (tmp_path / "probe").write_text("x")
    return not (tmp_path / "PROBE").exists()


def test_paths_differing_only_by_case_fail(tmp_path):
    if not _case_sensitive(tmp_path):
        pytest.skip("case-insensitive filesystem: two such paths cannot coexist here")
    _fails(_tree(tmp_path, {"docs/Guide.md": b"x", "docs/guide.md": b"y"}), "case")


# -- symlinks, submodules, LFS ---------------------------------------------------

def test_lfs_pointer_fails(tmp_path):
    ptr = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 12\n"
    _fails(_tree(tmp_path, {"img/big.txt": ptr}), "LFS")


def test_a_file_mentioning_lfs_later_passes(tmp_path):
    """Must not fire the LFS-pointer FAIL -- unrelated to #898's own
    scheme-literal guard, which this fixture's own URL legitimately also
    fires; assert no LFS offender specifically, not a wholly offender-free
    tree."""
    offenders = _offenders(_tree(tmp_path, {
        "notes.md": b"# n\n\nversion https://git-lfs.github.com/spec/v1\n",
    }))
    assert not any("LFS" in o for o in offenders), offenders


def test_a_nested_git_file_marks_a_submodule_and_fails(tmp_path):
    _fails(_tree(tmp_path, {"vendor/lib/.git": b"gitdir: ../../.git/modules/lib\n"}), ".git")


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_a_symlink_fails(tmp_path):
    root = _tree(tmp_path, {})
    os.symlink("LICENSE", root / "LICENSE.link")
    _fails(root, "LICENSE.link", "symlink")


# -- 5 MiB hard cap --------------------------------------------------------------

def test_an_image_at_5_mib_fails(tmp_path):
    _fails(_tree(tmp_path, {"img/a.png": PNG + b"\x00" * (5 * MIB - len(PNG))}), "img/a.png", "5 MiB")


def test_an_image_just_under_5_mib_passes_the_hard_cap(tmp_path):
    root = _tree(tmp_path, {"img/a.png": PNG + b"\x00" * (5 * MIB - len(PNG) - 1),
                            "README.md": (README_40 + "\n![a](img/a.png)\n").encode()})
    result = _load().check_tree(root, {"max_total_bytes": 10 * MIB})
    assert result.offenders == []


# -- hooks.json -----------------------------------------------------------------

@pytest.mark.parametrize("raw,needle", [
    (b"{not json", "JSON"),
    (b'{"SessionStart": []}', "hooks"),
    (b'{"hooks": []}', "hooks"),
])
def test_malformed_hooks_json_fails(tmp_path, raw, needle):
    _fails(_tree(tmp_path, {"hooks/hooks.json": _hooks(raw=raw)}), "hooks.json", needle)


def test_unknown_event_fails(tmp_path):
    _fails(_tree(tmp_path, {"hooks/hooks.json": _hooks(event="OnBoot")}), "OnBoot")


def test_unknown_type_fails(tmp_path):
    _fails(_tree(tmp_path, {"hooks/hooks.json": _hooks(type_="webhook")}), "webhook")


def test_hooks_also_listed_in_the_manifest_fails(tmp_path):
    root = _tree(tmp_path, {".claude-plugin/plugin.json": _manifest(hooks="./hooks/hooks.json")})
    _fails(root, "plugin.json", "hooks")


@pytest.mark.parametrize("command", [
    'bash "$HOME/scripts/hook.sh"',
    'bash "${CLAUDE_PLUGIN_ROOT}/scripts/$(whoami).sh"',
    "python3 -c 'print(1)'",
    "bash scripts/hook.sh",
    'bash "${CLAUDE_PLUGIN_ROOT}/scripts/hook.sh" "${CLAUDE_PROJECT_DIR}"',
])
def test_hook_commands_must_spell_paths_from_the_plugin_root(tmp_path, command):
    _fails(_tree(tmp_path, {"hooks/hooks.json": _hooks(command=command)}), "command")


@pytest.mark.parametrize("command", [
    GOOD_CMD,
    "bash ${CLAUDE_PLUGIN_ROOT}/scripts/hook.sh",
    'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook.sh" --flag',
])
def test_well_formed_hook_commands_pass(tmp_path, command):
    _passes(_tree(tmp_path, {"hooks/hooks.json": _hooks(command=command)}))


# -- front matter ---------------------------------------------------------------

@pytest.mark.parametrize("rel", ["skills/s/SKILL.md", "commands/c.md", "agents/a.md"])
@pytest.mark.parametrize("body,needle", [
    (b"# no front matter\n", "front matter"),
    (b"---\nname: s\n---\n\nBody.\n", "description"),
    (b"---\ndescription: [a, b]\n---\n", "description"),
    (b"---\ndescription: ''\n---\n", "description"),
    (b"---\ndescription: x\n", "front matter"),
])
def test_bad_front_matter_fails(tmp_path, rel, body, needle):
    _fails(_tree(tmp_path, {rel: body}), rel, needle)


def test_good_front_matter_in_an_agent_passes(tmp_path):
    _passes(_tree(tmp_path, {"agents/a.md": b"---\nname: a\ndescription: \"Quoted: fine\"\n---\nx\n"}))


# -- allowed-tools unrestricted shell (#859) -------------------------------------

@pytest.mark.parametrize("rel", ["skills/s/SKILL.md", "commands/c.md", "agents/a.md"])
@pytest.mark.parametrize("value", [
    "Bash",
    "Bash(*)",
    "Bash(:*)",
    "Bash(bash:*)",
    "Bash(sh:*)",
    "Bash(zsh:*)",
    "Bash(env:*)",
    "Bash(python*:*)",
    "Bash(python3:*)",
])
def test_unrestricted_bash_grant_as_a_string_fails(tmp_path, rel, value):
    body = f"---\ndescription: d\nallowed-tools: {value}\n---\n\nx\n".encode()
    _fails(_tree(tmp_path, {rel: body}), rel, "allowed-tools")


def test_unrestricted_bash_grant_as_a_yaml_list_fails(tmp_path):
    body = b"---\ndescription: d\nallowed-tools:\n  - Bash\n  - Read\n---\n\nx\n"
    _fails(_tree(tmp_path, {"commands/c.md": body}), "commands/c.md", "allowed-tools")


@pytest.mark.parametrize("value", [
    "Bash(${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh:*)",
    "Bash(${CLAUDE_PLUGIN_ROOT}/scripts/write-handoff.sh:*)",
])
def test_narrow_bash_grant_scoped_to_a_file_passes(tmp_path, value):
    body = f"---\ndescription: d\nallowed-tools: {value}\n---\n\nx\n".encode()
    _passes(_tree(tmp_path, {"commands/c.md": body}))


# -- credentials ----------------------------------------------------------------

@pytest.mark.parametrize("secret", [
    "sk-ant-api03-" + "A1b2C3d4E5" * 5,
    "ghp_" + "a1B2c3D4e5" * 3 + "a1B2c3",
    "github_pat_" + "11ABCDEFG0" * 6,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "xoxb-" + "1234567890-1234567890-" + "abcdefghij" * 2,
])
def test_a_real_looking_credential_fails(tmp_path, secret):
    _fails(_tree(tmp_path, {"config.json": f'{{"token": "{secret}"}}\n'.encode()}),
           "config.json", "credential")


@pytest.mark.parametrize("text", [
    "pattern = r'sk-ant-[A-Za-z0-9_-]{20,}'",
    "export ANTHROPIC_API_KEY=sk-ant-...",
    "token: ghp_xxx (example)",
])
def test_patterns_and_placeholders_pass(tmp_path, text):
    _passes(_tree(tmp_path, {"notes.md": f"# n\n\n{text}\n".encode()}))


def test_reading_a_credential_is_reported_for_review_not_failed(tmp_path):
    root = _tree(tmp_path, {"pipeline/x.py": b'import os\nkey = os.environ.get("ANTHROPIC_API_KEY")\n'})
    result = _load().check_tree(root, {})
    assert result.offenders == []
    assert any("pipeline/x.py" in r and "ANTHROPIC_API_KEY" in r for r in result.reviews), result.reviews


# -- images ---------------------------------------------------------------------

def test_an_image_referenced_from_a_script_fails(tmp_path):
    root = _tree(tmp_path, {"img/a.png": PNG,
                            "scripts/hook.sh": b"#!/bin/sh\ncat \"$X/img/a.png\"\n"})
    _fails(root, "img/a.png", "scripts/hook.sh")


def test_an_image_path_in_backticks_fails(tmp_path):
    root = _tree(tmp_path, {"img/a.png": PNG,
                            "README.md": (README_40 + "\nSee `img/a.png`.\n").encode()})
    _fails(root, "img/a.png", "README.md")


def test_an_image_shown_in_markdown_passes(tmp_path):
    root = _tree(tmp_path, {"img/a.png": PNG,
                            "README.md": (README_40 + "\n![a](img/a.png)\n").encode()})
    _passes(root)


# -- launchers and installs in hooks ---------------------------------------------

@pytest.mark.parametrize("line", [
    "npx some-tool", "  bunx x", "uvx tool", "pipx run tool", "uv run tool.py",
    "pnpm dlx x", "yarn dlx x", "pip install requests", "x && npm install -g y",
    "pip3 install --user z", "out=$(npx foo)",
])
def test_a_launcher_or_install_in_a_hook_script_fails(tmp_path, line):
    root = _tree(tmp_path, {"scripts/hook.sh": f"#!/bin/sh\n{line}\n".encode()})
    _fails(root, "scripts/hook.sh")


@pytest.mark.parametrize("line", [
    "# pip install requests (a comment)",
    'echo "install jq first: brew install jq" >&2',
    "run_npx_free_path",
])
def test_mentions_that_are_not_commands_pass(tmp_path, line):
    _passes(_tree(tmp_path, {"scripts/hook.sh": f"#!/bin/sh\n{line}\n".encode()}))


# -- package manager files --------------------------------------------------------

@pytest.mark.parametrize("lock", ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock"])
def test_root_package_json_with_a_lockfile_fails(tmp_path, lock):
    _fails(_tree(tmp_path, {"package.json": b"{}", lock: b"{}"}), "package.json")


def test_root_package_json_alone_passes(tmp_path):
    _passes(_tree(tmp_path, {"package.json": b"{}"}))


@pytest.mark.parametrize("name", [".npmrc", "sub/bunfig.toml", "uv.toml"])
def test_package_manager_config_fails(tmp_path, name):
    _fails(_tree(tmp_path, {name: b"x = 1\n"}), name)

# The portal's own wording for ALLOWED_TOOLS_BROAD (observed 2026-10-02 on v0.37.0):
# "bare Bash, Bash(*), or a wildcard right after a shell, an interpreter, a package
# manager or runner, or curl, as in Bash(python3:*)" -- and, for a plugin's own script,
# "A relative path or a wildcard in the path is still held." (#859)
@pytest.mark.parametrize("value", [
    "Bash(node:*)",
    "Bash(ruby:*)",
    "Bash(perl:*)",
    "Bash(npm:*)",
    "Bash(npx:*)",
    "Bash(pnpm:*)",
    "Bash(yarn:*)",
    "Bash(bun:*)",
    "Bash(bunx:*)",
    "Bash(pip:*)",
    "Bash(pip3:*)",
    "Bash(uv:*)",
    "Bash(uvx:*)",
    "Bash(pipx:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(pwsh:*)",
    "Bash(powershell:*)",
    "Bash(python3 *)",
    "Bash(bash scripts/run.sh:*)",
    "Bash(./scripts/run.sh:*)",
    "Bash(${CLAUDE_PLUGIN_ROOT}/scripts/*:*)",
    "Bash(${CLAUDE_PLUGIN_ROOT}/scripts/*.sh:*)",
    "Bash(/usr/bin/python3:*)",
])
def test_portal_broad_forms_fail(tmp_path, value):
    body = f"---\ndescription: d\nallowed-tools: {value}\n---\n\nx\n".encode()
    _fails(_tree(tmp_path, {"commands/c.md": body}), "commands/c.md", "allowed-tools")


# Positive controls: the portal's own accepted examples must stay accepted.
@pytest.mark.parametrize("value", [
    "Bash(git status:*)",
    "Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run.py:*)",
    "Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh:*)",
    "Bash(${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh:*)",
    "Read",
])
def test_portal_accepted_forms_pass(tmp_path, value):
    body = f"---\ndescription: d\nallowed-tools: {value}\n---\n\nx\n".encode()
    _passes(_tree(tmp_path, {"commands/c.md": body}))
