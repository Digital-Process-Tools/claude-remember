#!/usr/bin/env python3
"""Check a built release tree against the Anthropic directory's pre-submission
checklist (https://claude.com/docs/plugins/pre-submission-checklist), with the
plugin folder = the root of TREE (#851).

Exit 1 and one `FAIL path: reason` line per offender -- every offender, not the
first -- for every row that can be decided mechanically:

Rows that block or stop validation
  - `.claude-plugin/plugin.json` present and valid JSON; `name` lowercase letters,
    digits and hyphens, 64 characters at most; `description`, `author`, `version` set
  - README.md of at least 40 words, fenced code blocks not counted
  - a LICENSE/LICENCE/COPYING file at the root, or `license` in plugin.json
  - no `.DS_Store`, `Thumbs.db`, `desktop.ini`, `__MACOSX`
  - names valid on Windows and macOS: no `<>:"|?*` or control characters, no
    trailing dot or space, no device name (con, prn, aux, nul, com1-9, lpt1-9,
    with any extension), no two paths that differ only by case
  - no symlink, no `.git` file or directory (a submodule), no Git LFS pointer
  - no `.gitattributes` setting export-ignore, export-subst or filter
  - every file under 5 MiB
  - hooks/hooks.json: valid JSON, a top-level `hooks` object, known events and
    hook types only, not also named by plugin.json's `hooks`; every command spells
    each path from `${CLAUDE_PLUGIN_ROOT}`, with no other variable, no `$(...)` or
    backticks and no `python -c`
  - YAML front matter with a non-empty text `description` in skills/*/SKILL.md,
    commands/*.md and agents/*.md
  - that same front matter's `allowed-tools` never grants unrestricted shell: bare
    `Bash`, `Bash(*)`, `Bash(:*)`, or `Bash(<command>:*)` / `Bash(<command> *)`
    where the command is a shell (bash, sh, zsh, dash, ksh, fish, pwsh, powershell,
    env), an interpreter (python, node, ruby, perl, ...), a package manager or
    runner (npm, npx, pip, uv, cargo, ...) or a downloader (curl, wget) and no
    plugin script is named -- the full list is _UNSCOPED_COMMANDS below; a
    relative path or a wildcard in a path is held too. A specific command
    (`Bash(git status:*)`) or a named `${CLAUDE_PLUGIN_ROOT}/...` script passes
  - nothing shaped like a real credential (Anthropic, GitHub, AWS, Slack, Google,
    GitLab keys, private key blocks)

Rows a reviewer holds on
  - every non-image, non-font file under 256 KiB; at most 512 files; only text
    (SVG included), PNG/JPEG/GIF/WebP and fonts -- decided by the bytes, not the
    extension; plus this repository's own total-size budget
  - a bundled image is not referenced from commands/, hooks/ or scripts/, and its
    path is not written in backticks or a code block
  - no launcher (npx, bunx, uvx, pipx run, uv run, pnpm dlx, yarn dlx) or package
    install at command position in a hook command or a hooks/, hooks.d/, scripts/ file
  - no root package.json together with a lockfile; no .npmrc, bunfig.toml, uv.toml

Reported, never failed: `REVIEW` lines for code that reads a credential from the
environment or a config file ("uses a credential from the user's machine"), for
`eval` fed by a command substitution, and for a downloader (`curl`/`wget`) piped
straight into a shell. Each is a reviewer's judgement call about one shape, not
a hard rule -- a plugin's own installer script can legitimately look like either
of the last two (#866).

Usage:
    check_release_tree.py TREE [--config .github/release-branch.json]
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "release-branch.json"
DEFAULT_BUDGET = {"max_file_bytes": 256 * 1024, "max_files": 512,
                  "max_total_bytes": 3 * 1024 * 1024}
HARD_CAP = 5 * 1024 * 1024

FORBIDDEN_ATTRIBUTES = ("export-ignore", "export-subst", "filter")
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "__macosx"}
DEVICE_NAMES = ({"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)}
                | {f"lpt{i}" for i in range(1, 10)})
WINDOWS_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

# Hook events and types Claude Code documents for hooks.json. A list kept by hand
# goes stale; an unknown name is reported with this list so the fix is obvious.
KNOWN_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest",
                "Notification", "UserPromptSubmit", "Stop", "SubagentStart",
                "SubagentStop", "PreCompact", "SessionStart", "SessionEnd"}
KNOWN_HOOK_TYPES = {"command", "prompt", "agent", "http"}

LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
             "bun.lockb", "bun.lock")
PM_CONFIGS = {".npmrc", "bunfig.toml", "uv.toml"}

_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
_FONT_MAGIC = (b"wOFF", b"wOF2", b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

CREDENTIAL_PATTERNS = [
    # Widened from a {20,}-char tail: the portal blocked v0.37.0 on short fixture
    # keys like `sk-ant-api03-example` that a long-tail regex never matched (#866).
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_-]+")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{40,}")),
]
CREDENTIAL_USE = re.compile(r"\b(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|[A-Z][A-Z0-9_]*_TOKEN"
                            r"|[A-Z][A-Z0-9_]*_API_KEY|oauth_token|api_key)\b")

# `(` alone is left out on purpose: "(pip3 install pytest)" inside an error string is
# far more common in these scripts than a bare `( npm install )` subshell.
_CMD_POS = r"(?:^|[;&|`]|\$\(|\b(?:then|do|else|exec|xargs|sudo|env)\s)\s*"
_LAUNCH = (r"(?:npx|bunx|uvx|pipx\s+run|uv\s+run|pnpm\s+dlx|yarn\s+dlx"
           r"|(?:pip3?|npm|pnpm|yarn|bun|brew|gem|cargo|go|apt|apt-get|uv\s+pip)"
           r"\s+(?:install|i|add)\b)")
LAUNCHER = re.compile(_CMD_POS + _LAUNCH + r"(?=\s|$|\))")
LAUNCHER_PY = re.compile(r"""['"](?:npx|bunx|uvx)['"]|['"]pip3?['"]\s*,\s*['"]install['"]""")

# #864/#875 learned both of these the hard way: `eval` fed by a command
# substitution runs whatever that substitution produced, and a downloader piped
# straight into a shell runs whatever the remote end served that day. Neither is
# a hard FAIL -- a plugin's own installer script can legitimately look like this
# -- so both are REVIEW-only, a reviewer's judgement call (#866).
#
# #891: the pattern below used to require `$(` immediately after `eval`
# (optionally through a quote), which never matched the two lines #864 itself
# had to remove by hand from scripts/log.sh -- `eval "_identity=$_identity_raw"`
# and `eval "$_assign"` -- because neither feeds eval a command substitution,
# both are a plain variable expansion. Three alternatives now, in the order a
# line is most likely to hit one: `eval $(...)`/`eval "$(...)"` (unchanged),
# a bare `eval $var` with no quotes, and `eval "...$var..."` -- a variable
# expansion anywhere inside a quoted argument, which is what both #864 shapes
# above actually are.
EVAL_OF_SUBSTITUTION = re.compile(
    r"\beval\b\s*(?:"
    r"['\"]?\$\("
    r"|\$[A-Za-z_][A-Za-z0-9_]*\b"
    r"|['\"][^'\"]*\$[A-Za-z_{]"
    r")"
)
_SHELL_NAMES = {"sh", "bash", "zsh", "dash", "ksh"}


def _download_piped_to_shell(line: str) -> bool:
    """True if LINE pipes a `curl`/`wget` download into a shell, with any
    number of hops (e.g. `tee`) in between -- not only an immediate `| sh`
    (#866 review). Checks the LAST pipe segment's own first word, so a
    trailing `.sh` filename earlier in the line (e.g. `curl -o x.sh ... |
    gzip`) is never mistaken for an invocation of `sh`."""
    if "|" not in line:
        return False
    segments = line.split("|")
    if not any(re.search(r"\b(?:curl|wget)\b", seg) for seg in segments[:-1]):
        return False
    last = re.sub(r"^\s*sudo\s+", "", segments[-1].strip())
    words = last.split()
    if not words:
        return False
    # The first word may carry punctuation from its context on either side
    # -- a Markdown code span's backticks on both ends, a version suffix
    # trailing it -- so strip any leading/trailing run of non-letters first,
    # then take the run of letters that remains.
    core = words[0].strip("`'\"*_")
    m = re.match(r"^[A-Za-z]+", core)
    return bool(m) and m.group(0) in _SHELL_NAMES


@dataclass
class CheckResult:
    offenders: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    files: int = 0
    total_bytes: int = 0


def sniff(data: bytes) -> str:
    """'image', 'font', 'text' or 'binary', from the leading bytes."""
    if data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return "image"
    if data.startswith(_FONT_MAGIC):
        return "font"
    if b"\x00" in data:
        return "binary"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "text"


def gitattributes_offences(text: str) -> list:
    """The forbidden attribute names a .gitattributes body sets, unsets or
    clears (`export-ignore`, `-filter`, `!filter`, `filter=lfs` all count)."""
    found = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for token in line.split()[1:]:
            name = token.lstrip("-!").split("=", 1)[0]
            if name in FORBIDDEN_ATTRIBUTES:
                found.add(name)
    return sorted(found)


# -- individual rows --------------------------------------------------------------

def name_problems(component: str) -> list:
    out = []
    if WINDOWS_BAD_CHARS.search(component):
        out.append("a character not valid in a Windows file name")
    if component.endswith((".", " ")) and component not in (".", ".."):
        out.append("a trailing dot or space (not valid on Windows)")
    if component.split(".", 1)[0].lower() in DEVICE_NAMES:
        out.append("a reserved Windows device name")
    return out


def readme_words(text: str) -> int:
    kept, fence = [], None
    for line in text.splitlines():
        m = re.match(r"^\s{0,3}(```|~~~)", line)
        if fence:
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        kept.append(line)
    return len(re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", "\n".join(kept)))


def hook_command_problems(command: str) -> list:
    out = []
    if "$(" in command or "`" in command:
        out.append("command substitution")
    if re.search(r"\bpython[0-9.]*\s+-c\b", command):
        out.append("python -c")
    for var in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", command):
        if var != "CLAUDE_PLUGIN_ROOT":
            out.append(f"variable ${var}")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return out + ["a command that does not parse"]
    for tok in tokens:
        if "/" in tok and not re.match(r"^\$\{?CLAUDE_PLUGIN_ROOT\}?/", tok):
            out.append(f"path {tok!r} not written from ${{CLAUDE_PLUGIN_ROOT}}")
    return out


def front_matter(text: str):
    """(mapping, None) or (None, problem)."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return None, "no YAML front matter"
    end = re.search(r"^---\s*$", text[4:], re.MULTILINE)
    if not end:
        return None, "unterminated YAML front matter"
    block = text[4:4 + end.start()]
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            return None, f"YAML front matter does not parse ({exc.__class__.__name__})"
    else:
        # Fallback without PyYAML: top-level `key: value` lines only. Good enough to
        # tell a present text description from a missing or list-valued one.
        data = {}
        for line in block.splitlines():
            m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
            if m:
                v = m.group(2).strip()
                data[m.group(1)] = [v] if v.startswith("[") else v.strip("'\"")
    if not isinstance(data, dict):
        return None, "YAML front matter is not a mapping"
    return data, None


def _in_code(text: str, needle: str) -> bool:
    fence = None
    for line in text.splitlines():
        m = re.match(r"^\s{0,3}(```|~~~)", line)
        if fence:
            if m and m.group(1) == fence:
                fence = None
            elif needle in line:
                return True
            continue
        if m:
            fence = m.group(1)
            continue
        if any(needle in span for span in re.findall(r"`+([^`]*)`+", line)):
            return True
    return False


# -- the walk ---------------------------------------------------------------------

def check_tree(root: Path, budget: dict) -> CheckResult:
    root = Path(root)
    b = dict(DEFAULT_BUDGET)
    b.update(budget or {})
    result = CheckResult()
    off = result.offenders
    files: dict = {}
    all_paths: list = []

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        keep = []
        for d in sorted(dirnames):
            rel = (rel_dir / d).as_posix()
            all_paths.append(rel)
            if os.path.islink(os.path.join(dirpath, d)):
                off.append(f"{rel}: symlink (not allowed in a release tree)")
            elif d == ".git":
                off.append(f"{rel}: a .git directory inside the tree (a submodule or a checkout)")
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = (rel_dir / name).as_posix()
            all_paths.append(rel)
            if os.path.islink(full):
                off.append(f"{rel}: symlink (not allowed in a release tree)")
                continue
            files[rel] = Path(full).read_bytes()

    for rel in all_paths:
        parts = rel.split("/")
        for comp in parts:
            if comp.lower() in JUNK_NAMES:
                off.append(f"{rel}: OS junk ({comp})")
                break
        for problem in name_problems(parts[-1]):
            off.append(f"{rel}: {problem}")
    by_case: dict = {}
    for rel in all_paths:
        by_case.setdefault(rel.casefold(), []).append(rel)
    for group in by_case.values():
        if len(group) > 1:
            off.append(f"{group[0]}: differs only by case from {', '.join(group[1:])}")

    kinds: dict = {}
    for rel, data in sorted(files.items()):
        name = posixpath.basename(rel)
        size = len(data)
        result.files += 1
        result.total_bytes += size
        if name == ".git":
            off.append(f"{rel}: a .git file inside the tree (a submodule)")
            continue
        if data.startswith(b"version https://git-lfs.github.com/spec/"):
            off.append(f"{rel}: a Git LFS pointer, not the file")
            continue
        if size >= HARD_CAP:
            off.append(f"{rel}: {size} bytes, not under the 5 MiB limit for any file")
        kind = sniff(data)
        kinds[rel] = kind
        if kind == "binary":
            off.append(f"{rel}: binary file (only text, PNG/JPEG/GIF/WebP images and "
                       "fonts are allowed)")
            continue
        if kind == "text" and size >= b["max_file_bytes"]:
            off.append(f"{rel}: {size} bytes, not under the "
                       f"{b['max_file_bytes'] // 1024} KiB limit for non-image files")
        if name == ".gitattributes":
            bad = gitattributes_offences(data.decode("utf-8"))
            if bad:
                off.append(f"{rel}: sets {', '.join(bad)} (the directory stops validating)")
        if name in PM_CONFIGS:
            off.append(f"{rel}: package-manager config ({name})")
        if kind == "text":
            text = data.decode("utf-8")
            for label, pat in CREDENTIAL_PATTERNS:
                if pat.search(text):
                    off.append(f"{rel}: looks like a real credential ({label})")
            uses = sorted(set(CREDENTIAL_USE.findall(text)))
            if uses:
                # Used to skip every .md file -- the portal flagged README.md
                # naming a credential env var, which that skip hid from REVIEW (#866).
                result.reviews.append(f"{rel}: reads or names {', '.join(uses)}")
            for n, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if EVAL_OF_SUBSTITUTION.search(line):
                    result.reviews.append(f"{rel}:{n}: eval fed by a command "
                                           f"substitution: {line.strip()[:80]}")
                if _download_piped_to_shell(line):
                    result.reviews.append(f"{rel}:{n}: downloads and pipes straight "
                                           f"into a shell: {line.strip()[:80]}")

    if result.files > b["max_files"]:
        off.append(f"tree: {result.files} files, more than the {b['max_files']} allowed")
    if result.total_bytes > b["max_total_bytes"]:
        off.append(f"tree: total {result.total_bytes} bytes is over the "
                   f"{b['max_total_bytes']} byte budget")

    manifest = _check_manifest(files, off)
    _check_readme_and_licence(files, manifest, off)
    _check_hooks(files, manifest, off)
    _check_front_matter(files, off)
    _check_images(files, kinds, off)
    _check_launchers(files, kinds, off)
    if "package.json" in files:
        locks = [lf for lf in LOCKFILES if lf in files]
        if locks:
            off.append(f"package.json: at the root together with {', '.join(locks)}")
    return result


def _check_manifest(files: dict, off: list):
    rel = ".claude-plugin/plugin.json"
    if rel not in files:
        off.append(f"{rel}: missing")
        return None
    try:
        m = json.loads(files[rel].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        off.append(f"{rel}: not valid JSON ({exc})")
        return None
    if not isinstance(m, dict):
        off.append(f"{rel}: not a JSON object")
        return None
    name = m.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        off.append(f"{rel}: name {name!r} must be 1-64 lowercase letters, digits or hyphens")
    for key in ("description", "version"):
        if not isinstance(m.get(key), str) or not m[key].strip():
            off.append(f"{rel}: {key} is not set")
    author = m.get("author")
    if not (isinstance(author, str) and author.strip()) and not (
            isinstance(author, dict) and str(author.get("name", "")).strip()):
        off.append(f"{rel}: author is not set")
    return m


def _check_readme_and_licence(files: dict, manifest, off: list) -> None:
    readme = next((r for r in files if r.lower() == "readme.md"), None)
    if not readme:
        off.append("README.md: missing")
    else:
        n = readme_words(files[readme].decode("utf-8", "replace"))
        if n < 40:
            off.append(f"{readme}: {n} words outside code blocks, the directory wants at least 40")
    has_file = any("/" not in r and r.upper().startswith(("LICENSE", "LICENCE", "COPYING"))
                   for r in files)
    has_field = isinstance(manifest, dict) and bool(str(manifest.get("license", "")).strip())
    if not has_file and not has_field:
        off.append("LICENSE: no licence file at the root and no `license` in plugin.json")


def _check_hooks(files: dict, manifest, off: list) -> None:
    rel = "hooks/hooks.json"
    if isinstance(manifest, dict) and "hooks" in manifest:
        listed = manifest["hooks"]
        listed = listed if isinstance(listed, list) else [listed]
        if any(isinstance(x, str) and posixpath.normpath(x) == rel for x in listed):
            off.append(f".claude-plugin/plugin.json: `hooks` also names {rel}, "
                       "which is loaded by default (double registration)")
    if rel not in files:
        # #858: a hookless tree of a plugin that works only through its hooks would
        # otherwise pass every gate silently and get pushed to the directory.
        off.append(f"{rel}: missing -- this plugin works only through its hooks, "
                   "so a release tree with none would be silently broken")
        return
    try:
        doc = json.loads(files[rel].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        off.append(f"{rel}: not valid JSON ({exc})")
        return
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks"), dict):
        off.append(f"{rel}: no top-level `hooks` object")
        return
    command_hooks = 0
    for event, groups in doc["hooks"].items():
        if event not in KNOWN_EVENTS:
            off.append(f"{rel}: unknown event {event!r} (known: {', '.join(sorted(KNOWN_EVENTS))})")
        if not isinstance(groups, list):
            off.append(f"{rel}: {event} is not a list")
            continue
        for group in groups:
            hooks = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(hooks, list):
                off.append(f"{rel}: a {event} entry has no `hooks` list")
                continue
            for hook in hooks:
                htype = hook.get("type") if isinstance(hook, dict) else None
                if htype not in KNOWN_HOOK_TYPES:
                    off.append(f"{rel}: {event} hook has unknown type {htype!r}")
                    continue
                if htype == "command":
                    command_hooks += 1
                    cmd = hook.get("command")
                    if not isinstance(cmd, str) or not cmd.strip():
                        off.append(f"{rel}: {event} command hook has no command")
                        continue
                    for problem in hook_command_problems(cmd):
                        off.append(f"{rel}: {event} command {cmd!r}: {problem}")
    if command_hooks == 0:
        # #858: a hooks.json with no `command` hook runs nothing -- same silent
        # breakage as a missing file, just one layer deeper.
        off.append(f"{rel}: declares hooks but has zero `command` hooks -- "
                   "a release that would run nothing")


# The directory holds ALLOWED_TOOLS_BROAD on "bare Bash, Bash(*), or a wildcard
# right after a shell, an interpreter, a package manager or runner, or curl, as in
# Bash(python3:*)", and for a plugin's own script "A relative path or a wildcard in
# the path is still held" (portal wording, observed 2026-10-02; #859). A command
# named here takes arbitrary arguments, so a pattern that starts with one and names
# no plugin script is not scoped to anything.
_UNSCOPED_COMMANDS = {
    # shells
    "bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "env",
    # interpreters
    "python", "python2", "python3", "node", "deno", "ruby", "perl", "php", "lua",
    # package managers and runners
    "npm", "npx", "pnpm", "yarn", "bun", "bunx", "pip", "pip3", "uv", "uvx", "pipx",
    "poetry", "cargo", "go", "gem", "brew",
    # downloaders
    "curl", "wget",
}
_PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}/"


def bash_grant_problem(entry: str) -> str | None:
    """None if `entry` is a safely scoped allowed-tools Bash grant; otherwise the
    reason the directory would hold it as broad shell access (#859)."""
    entry = entry.strip()
    if entry == "Bash":
        return "bare `Bash` grants every shell command"
    m = re.fullmatch(r"Bash\((.*)\)", entry)
    if not m:
        return None
    pattern = m.group(1).strip()
    if pattern in ("", "*", "**", ":*"):
        return f"`Bash({pattern})` grants every shell command"
    # The command part: drop a trailing `:*` (prefix match) or ` *` (any args).
    cmd = re.sub(r"(:\*|\s+\*)$", "", pattern).strip()
    words = cmd.split()
    if not words:
        return f"`Bash({pattern})` grants every shell command"
    paths = [w for w in words if "/" in w]
    for p in paths:
        if "*" in p or "?" in p:
            return f"`Bash({pattern})` has a wildcard in the path"
        if not p.startswith(_PLUGIN_ROOT):
            base = p.rsplit("/", 1)[-1].lower()
            if base in _UNSCOPED_COMMANDS or base.rstrip("0123456789.") in _UNSCOPED_COMMANDS:
                return f"`Bash({pattern})` grants an interpreter by absolute path, not one script"
            if not p.startswith("/"):
                return (f"`Bash({pattern})` names a relative path; name the plugin's script "
                        f"as {_PLUGIN_ROOT}...")
    first = words[0].rstrip("*").lower()
    if first in _UNSCOPED_COMMANDS and not any(p.startswith(_PLUGIN_ROOT) for p in paths):
        return f"`Bash({pattern})` is a wildcard after `{words[0]}`, not one plugin script"
    return None


def _check_front_matter(files: dict, off: list) -> None:
    for rel, data in sorted(files.items()):
        parts = rel.split("/")
        wanted = ((parts[0] == "skills" and parts[-1] == "SKILL.md" and len(parts) >= 3)
                  or (parts[0] in ("commands", "agents") and rel.endswith(".md")
                      and len(parts) >= 2))
        if not wanted:
            continue
        meta, problem = front_matter(data.decode("utf-8", "replace"))
        if problem:
            off.append(f"{rel}: {problem}")
            continue
        desc = meta.get("description")
        if not isinstance(desc, str) or not desc.strip():
            off.append(f"{rel}: front matter has no text `description`")
        allowed = meta.get("allowed-tools")
        if isinstance(allowed, list):
            entries = [str(x) for x in allowed]
        elif isinstance(allowed, str):
            # A comma-split alone used to merge a space-delimited list (no commas)
            # into one string, so `Bash(a.sh) Bash(curl)` fullmatched as a single
            # pattern and the unscoped half was never checked on its own (#866,
            # trap.d/859). Tokenize each `Bash(...)`/bare `Bash` entry directly.
            entries = []
            for piece in allowed.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                tokens = re.findall(r"Bash\([^()]*\)|\bBash\b|\S+", piece)
                entries.extend(tokens)
        else:
            entries = []
        for entry in entries:
            reason = bash_grant_problem(entry)
            if reason:
                off.append(f"{rel}: allowed-tools grants unrestricted shell "
                           f"({entry!r}): {reason}")


def _check_images(files: dict, kinds: dict, off: list) -> None:
    images = [r for r in files if kinds.get(r) == "image"
              or posixpath.splitext(r)[1].lower() in IMAGE_EXTS]
    if not images:
        return
    for img in images:
        needles = {img, posixpath.basename(img)}
        for rel, data in files.items():
            if rel == img or kinds.get(rel) != "text":
                continue
            text = data.decode("utf-8")
            if rel.split("/")[0] in ("commands", "hooks", "scripts", "hooks.d"):
                if any(n in text for n in needles):
                    off.append(f"{img}: bundled image referenced from {rel}")
            elif rel.lower().endswith(".md") and any(_in_code(text, n) for n in needles):
                off.append(f"{img}: path written in code (backticks or a code block) in {rel}")


def _check_launchers(files: dict, kinds: dict, off: list) -> None:
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in ("hooks", "hooks.d", "scripts") or kinds.get(rel) != "text":
            continue
        commands = [data.decode("utf-8")]
        if rel == "hooks/hooks.json":
            try:
                doc = json.loads(commands[0])
                commands = [h.get("command", "") for gs in doc.get("hooks", {}).values()
                            for g in gs for h in g.get("hooks", []) if isinstance(h, dict)]
            except (ValueError, AttributeError, TypeError):
                commands = []
        for text in commands:
            for n, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if LAUNCHER.search(line) or (rel.endswith(".py") and LAUNCHER_PY.search(line)):
                    off.append(f"{rel}:{n}: launcher or package install in a hook "
                               f"script: {line.strip()[:80]}")


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tree")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = ap.parse_args(argv)
    budget = {}
    if Path(args.config).is_file():
        budget = json.loads(Path(args.config).read_text(encoding="utf-8")).get("budget", {})
    tree = Path(args.tree)
    if not tree.is_dir():
        print(f"check_release_tree: {tree} is not a directory", file=sys.stderr)
        return 2
    result = check_tree(tree, budget)
    summary = f"{result.files} files, {result.total_bytes} bytes"
    for line in result.reviews:
        print(f"REVIEW {line}")
    if result.offenders:
        for line in result.offenders:
            print(f"FAIL {line}")
        print(f"check_release_tree: {len(result.offenders)} offender(s) in {tree} ({summary})")
        return 1
    print(f"check_release_tree: OK -- {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
