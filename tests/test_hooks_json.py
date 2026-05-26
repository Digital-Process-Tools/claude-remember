"""Lint hooks/hooks.json for cross-shell dispatch safety.

Catches historical regressions:
  - 4d50166: unquoted ${CLAUDE_PLUGIN_ROOT} broke paths with spaces.
  - d18e02c: leftover `2>>` stderr redirects in hook command strings.
  - #82:     unquoted/unwrapped ${VAR} causes PowerShell ParserError on Windows.

Three layers:
  1. Structural — JSON shape, command non-empty, referenced script files exist.
  2. Static lint — regex checks for known foot-guns.
  3. Live parse — bash -n and pwsh -Command dry-parse; skip if shell missing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _iter_commands():
    """Yield (event, index, command_string) for every hook entry."""
    data = json.loads(HOOKS_JSON.read_text())
    for event, groups in data.get("hooks", {}).items():
        for gi, group in enumerate(groups):
            for hi, hook in enumerate(group.get("hooks", [])):
                assert hook.get("type") == "command", (
                    f"{event}[{gi}].hooks[{hi}]: unsupported type {hook.get('type')!r}"
                )
                cmd = hook.get("command", "")
                assert isinstance(cmd, str) and cmd.strip(), (
                    f"{event}[{gi}].hooks[{hi}]: empty/missing command"
                )
                yield f"{event}[{gi}].hooks[{hi}]", cmd


def test_hooks_json_is_valid_json():
    json.loads(HOOKS_JSON.read_text())


def test_every_referenced_script_exists():
    """${CLAUDE_PLUGIN_ROOT}/scripts/foo.sh must resolve to a real file."""
    pat = re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?/(scripts/[A-Za-z0-9_./-]+\.sh)")
    found_any = False
    for loc, cmd in _iter_commands():
        for rel in pat.findall(cmd):
            found_any = True
            path = REPO_ROOT / rel
            assert path.is_file(), f"{loc}: references missing script {rel}"
    assert found_any, "no script references found — regex drift?"


def test_plugin_root_var_is_double_quoted():
    """${CLAUDE_PLUGIN_ROOT} must sit inside double quotes (spaces in install path).

    Regression guard for 4d50166.
    """
    for loc, cmd in _iter_commands():
        for m in re.finditer(r"\$\{?CLAUDE_PLUGIN_ROOT\}?", cmd):
            before = cmd[: m.start()]
            after = cmd[m.end() :]
            opening = before.rfind('"')
            closing = after.find('"')
            assert opening != -1 and closing != -1, (
                f"{loc}: ${{CLAUDE_PLUGIN_ROOT}} not inside double quotes — "
                f"breaks on install paths with spaces"
            )


def test_no_stderr_redirects_in_command():
    """Hook commands must not contain `2>` / `2>>` — let Claude Code capture stderr.

    Regression guard for d18e02c.
    """
    for loc, cmd in _iter_commands():
        assert "2>>" not in cmd and "2>" not in cmd, (
            f"{loc}: contains stderr redirect — remove, Claude Code captures it"
        )


def test_no_bare_dollar_braces_outside_known_vars():
    """Flag ${VAR} patterns other than ${CLAUDE_PLUGIN_ROOT}.

    PowerShell parses ${...} as its own subexpression and chokes on most contents
    (issue #82). Only the known-safe variable is allowed. Anything else must be
    wrapped via `bash -c '...'` so PowerShell sees an opaque single-quoted string.
    """
    allowed = {"CLAUDE_PLUGIN_ROOT"}
    pat = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for loc, cmd in _iter_commands():
        # If command is wrapped in `bash -c '...'`, PowerShell sees opaque body — skip.
        if re.search(r"\bbash\s+-c\s+'", cmd):
            continue
        for var in pat.findall(cmd):
            assert var in allowed, (
                f"{loc}: ${{{var}}} risks PowerShell ParserError. "
                f"Either wrap command in `bash -c '...'` or use bare $VAR."
            )


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="Windows `bash` resolves to WSL launcher, not Git Bash — "
    "claude-code on Windows dispatches via pwsh anyway; "
    "bash-side coverage comes from ubuntu/macos legs.",
)
def test_commands_parse_under_bash():
    """`bash -n` dry-parses each command with a stubbed CLAUDE_PLUGIN_ROOT.

    Pipes the command to bash via stdin — no temp file, no Windows path
    translation hazards (Git Bash chokes on `C:\\...` style paths), no
    list2cmdline quote mangling.
    """
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": "/tmp/stub plugin root"}
    for loc, cmd in _iter_commands():
        result = subprocess.run(
            ["bash", "-n", "/dev/stdin"],
            input=cmd + "\n",
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, (
            f"{loc}: bash syntax error\ncmd: {cmd}\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )


PROBLEMATIC_PATHS = [
    # ── baseline ────────────────────────────────────────────────────
    "C:/Users/dev/plugin",
    # ── spaces (historical bug 4d50166) ─────────────────────────────
    "C:/Program Files/My Plugin",
    "C:/Users/Jane Doe/.claude/plugins/cache/org/remember/0.7.2",
    # ── trailing space (Windows quietly strips on save) ─────────────
    "C:/Users/dev /plugin",
    # ── non-ASCII usernames (Latin1 + extended) ─────────────────────
    "C:/Users/Émilie/plugin",
    "C:/Users/café/plugin",
    "C:/Users/Łukasz/plugin",
    # ── non-ASCII beyond Latin1 (combining marks, CJK, RTL, emoji) ──
    "C:/Users/Việt/plugin",
    "C:/Users/中文/plugin",
    "C:/Users/مرحبا/plugin",
    "C:/Users/dev🎉/plugin",
    # combining mark: e + U+0301 (not precomposed é)
    "C:/Users/café/plugin",
    # ── PowerShell-hostile chars ────────────────────────────────────
    "C:/Users/Jane's OneDrive/plugin",          # apostrophe (OneDrive)
    "C:/Users/dev/with`backtick/plugin",        # PS escape char
    "C:/Users/dev/with$dollar/plugin",          # PS var sigil
    "C:/Users/dev/with[brackets]/plugin",       # PS wildcard / index
    "C:/Users/dev/with(parens)/plugin",         # subexpression-ish
    "C:/Users/dev/with{braces}/plugin",         # scriptblock / var-name braces
    "C:/Users/dev/with;semi/plugin",            # statement separator
    "C:/Users/dev/with#hash/plugin",            # PS comment char
    "C:/Users/dev/with@at/plugin",              # here-string / splat
    # ── cmd.exe-passthrough flavour ─────────────────────────────────
    "C:/Users/dev/with%percent%/plugin",        # cmd var expansion
    "C:/Users/dev/with&amp/plugin",             # cmd command separator
    # ── slash mixing & oddities ─────────────────────────────────────
    "C:\\Users\\dev\\plugin",                    # all-backslash
    "C:\\Users/dev\\mixed/plugin",               # mixed
    "C:/PROGRA~1/plugin",                        # 8.3 short name
    # ── UNC paths ───────────────────────────────────────────────────
    "//server/share/plugin",
    "\\\\server\\share\\plugin",
    # ── mixed nightmare ─────────────────────────────────────────────
    "C:/Users/Émilie's Files/Café (work)/plugin",
    "C:/Users/中文 dev's/with `backtick` & $var [v2]/plugin",
]


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
@pytest.mark.parametrize("plugin_root", PROBLEMATIC_PATHS, ids=lambda p: p[:32])
def test_commands_parse_after_substitution(plugin_root):
    """Substitute ${CLAUDE_PLUGIN_ROOT} ourselves, then parse under PowerShell.

    Claude Code on Windows may expand the env variable before handing the
    command string to pwsh. Paths containing spaces / non-ASCII / apostrophes /
    backticks / dollar signs can then break the parser even though the raw
    template was fine. This guards every install-path shape the reporter or
    future users might have.
    """
    parser_probe = (
        "$src = [Console]::In.ReadToEnd(); "
        "$errors = $null; "
        "$null = [System.Management.Automation.Language.Parser]::ParseInput("
        "$src, [ref]$null, [ref]$errors); "
        "if ($errors) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    for loc, cmd in _iter_commands():
        substituted = re.sub(r"\$\{?CLAUDE_PLUGIN_ROOT\}?", lambda _m: plugin_root, cmd)
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", parser_probe],
            input=substituted + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, (
            f"{loc}: PowerShell ParserError after substituting "
            f"CLAUDE_PLUGIN_ROOT={plugin_root!r}\n"
            f"substituted cmd: {substituted}\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_commands_parse_under_powershell():
    """PowerShell dry-parses each command with a stubbed CLAUDE_PLUGIN_ROOT.

    Direct guard for #82 — the Windows ParserError surfaces here on any OS that
    has pwsh installed. GitHub-hosted runners ship pwsh on all three matrix legs.

    Pipes the command source via stdin and parses it through
    System.Management.Automation.Language.Parser — bypasses CLI arg encoding.
    """
    parser_probe = (
        "$env:CLAUDE_PLUGIN_ROOT = '/tmp/stub plugin root'; "
        "$src = [Console]::In.ReadToEnd(); "
        "$errors = $null; "
        "$null = [System.Management.Automation.Language.Parser]::ParseInput("
        "$src, [ref]$null, [ref]$errors); "
        "if ($errors) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    for loc, cmd in _iter_commands():
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", parser_probe],
            input=cmd + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, (
            f"{loc}: PowerShell ParserError\ncmd: {cmd}\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
