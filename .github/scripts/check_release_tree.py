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
`eval` fed by a command substitution or a bare/embedded variable expansion, and
for a downloader (`curl`/`wget`) piped straight into a shell. Each is a
reviewer's judgement call about one shape, not a hard rule -- a plugin's own
installer script can legitimately look like either
of the last two (#866).

Usage:
    check_release_tree.py TREE [--config .github/release-branch.json]
"""

from __future__ import annotations

import argparse
import ast
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

# #898: a typed `<<` (here-document) anywhere in a shipped script is a hard
# block at the directory, filed as "Unpinned npx launcher" -- the scanner
# cannot place where the here-document ends. `<<<` (a here-string) is not
# flagged, so the pattern must not match three or more `<` in a row either.
TYPED_HEREDOC = re.compile(r"(?<!<)<<(?!<)")

# #898: "any URL host, even inside a comment" is one half of the directory's
# MCP_FORWARDS_CREDENTIAL_ENV pair (the other half is CREDENTIAL_USE below).
# `sh` is deliberately not in this TLD list: half of this repo's own shipped
# scripts end in `.sh`, and that suffix is not a URL.
URL_HOST = re.compile(
    r"[a-zA-Z][a-zA-Z0-9+.-]*://|\b(?:[a-zA-Z0-9-]+\.){1,}"
    r"(?:com|org|net|io|dev|ai|co|gov|edu|app)\b"
)

# #898: the directory's own bundled-word-list scan tripped on curl/ftp/dig/
# drill as plain dictionary entries, one per line -- not as shell commands.
# Catching every English use of "host" or "fetch" would FAIL this repo's own
# prose (`pipeline/host.py` alone uses the word "host" as an identifier and
# in docstring prose dozens of times), so this only fires at a position that
# looks like an actual invocation: after a shell separator, inside a command
# substitution, or after `then`/`do`/`else`/`exec`/`xargs`/`sudo`/`env`.
# Backtick is deliberately excluded from the separator set (unlike LAUNCHER
# above): a backtick in prose is almost always Markdown inline code, not a
# shell command substitution, and `` `host:path` `` (a real comment in this
# repo) is not an invocation of anything.
NETWORK_COMMAND_NAMES = ("curl", "wget", "ftp", "dig", "drill", "finger", "mail",
                         "lynx", "links", "fetch", "talk", "host", "http")
# A bare command at the very start of a line (`curl ... | sh`, or a bundled
# word-list data file's one-entry-per-line shape) is a real command-position
# shape the original markers below miss -- but `^` alone over EVERY shipped
# file also matches plain English prose starting a sentence with one of
# these words ("host authenticates it...", a real line in pipeline/haiku.py's
# own docstring; "host's own name...", pipeline/host.py's). There is no
# regex-only way to tell "a shell command" from "a sentence" by shape alone,
# so `^` is scoped to files that are actually shell scripts (the same
# hooks/hooks.d/scripts scope _check_typed_heredoc and _check_url_in_comment
# use) -- in a .sh file a bare word at column 0 really is a command; in a .py
# docstring or a .md file it is prose. The `;&|$(then/do/.../env )` markers
# stay unscoped: those are shell punctuation, not English, in any file type.
_SCRIPT_DIRS = ("hooks", "hooks.d", "scripts")
_NETWORK_CMD_POS = re.compile(
    r"(?:[;&|]|\$\(|\b(?:then|do|else|exec|xargs|sudo|env)\s)\s*(" +
    "|".join(NETWORK_COMMAND_NAMES) + r")(?=\s|$)", re.IGNORECASE
)
_NETWORK_CMD_POS_SCRIPT = re.compile(
    r"(?:^|[;&|]|\$\(|\b(?:then|do|else|exec|xargs|sudo|env)\s)\s*(" +
    "|".join(NETWORK_COMMAND_NAMES) + r")(?=\s|$)", re.IGNORECASE
)

RELEASE_README_VAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")

# #898 round 2: a scheme literal read as a URL host even inside shell
# parameter-expansion syntax (a shell strip of the literal "h-t-t-p-s-:-/-/"
# prefix, not just a bare URL). Four plugin.json listing fields and
# promos.json's own star-ask/promo URLs are the only places the directory
# requires or expects a real URL at all; everywhere else in the shipped
# tree, code that needs to strip or match a scheme should use a wildcard
# (parameter-expansion `*` followed by a colon and two slashes), never the
# literal scheme string -- including in a comment that merely explains this
# rule, which is why this comment avoids spelling either scheme out loud.
SCHEME_LITERAL = re.compile(r"https?://")
SCHEME_ALLOWLIST = frozenset({
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "promos.json",
})

# #898 round 2: a standalone network-command word (including "host" and
# "ssh", both on the directory's own list -- `host` the DNS lookup tool,
# `ssh` the remote-shell client) anywhere in a shipped file, outside an
# allowlist of files where the word is this plugin's own architecture
# vocabulary rather than a network tool: `pipeline/` names its own
# per-coding-agent abstraction `Host` (Claude Code / Codex / Gemini /
# Antigravity), and the git-backup/git-restore machinery's own prose
# legitimately describes real `git fetch` calls and a real
# `GIT_SSH_COMMAND` default of `ssh -o...`. Renaming either across
# its ~280 shipped occurrences was judged out of proportion for this guard
# to force by itself -- see the lane's own report. Review finding, #898
# round 2: this allowlist is scoped to files that were CONFIRMED (by this
# repo's own built tree, not guessed) to actually carry one of these words
# at the time it was written -- a file added to it later without a real
# occurrence would be the same staleness risk the guard exists to avoid,
# so re-check with a fresh grep before adding one, not before trusting this
# list unchanged. A NEW file outside this allowlist still has to earn its
# way onto it.
NETWORK_WORDS_EXTENDED = NETWORK_COMMAND_NAMES + ("nc", "telnet", "ssh", "scp", "rsync")
_NETWORK_WORD_STANDALONE = re.compile(
    r"\b(" + "|".join(NETWORK_WORDS_EXTENDED) + r")\b", re.IGNORECASE
)
NETWORK_WORD_ALLOWLIST = frozenset({
    "pipeline/extract.py", "pipeline/haiku.py", "pipeline/host.py",
    "pipeline/shell.py", "pipeline/slug.py", "pipeline/spawn_guard.py", "pipeline/types.py",
    "scripts/agy-session-start-hook.sh", "scripts/agy-stop-hook.sh",
    "scripts/doctor.sh", "scripts/install_agy_hooks.py",
    "scripts/lib-env-cache.sh", "scripts/lib-lock.sh", "scripts/lib-memory-context.sh",
    "scripts/lib-staging-lock.sh", "scripts/log.sh", "scripts/post-tool-hook.sh",
    "scripts/resolve-paths.sh", "scripts/save-session.sh", "scripts/session-end-hook.sh",
    "scripts/session-start-hook.sh", "scripts/user-prompt-hook.sh",
    "hooks.d/after_save/50-git-backup.sh", "hooks.d/before_session_start/50-git-restore.sh",
})

# #898 round 2: `[ "$VAR" = "${BASH_SOURCE[0]}" ] && VAR="."` -- jit-context's
# own measured "dead SCRIPT_DIR='.' fallback" shape, item 4 of its write-up.
# Not dead in this repo (it is the real Windows-backslash fallback, #766/#783),
# so the fix is the value, not the branch: this repo's own scripts now use
# `$PWD` instead, never the literal "." the scanner reads as a further file.
DIR_FALLBACK_DOT = re.compile(r'\$\{BASH_SOURCE\[0\]\}"\s*\]\s*&&\s*\w+="\."')

# #898, round 5: a bare "$VAR"/"${VAR...}" as the first word of a (sub)command
# -- the program name is computed at run time by a shell expansion the
# directory's scanner cannot read (UNPINNED_NPX). Matched only at COMMAND
# POSITION (start of line, or right after &&/;/||) so an ordinary argument
# use ("$PYTHON" passed to echo, say) is not a false positive. Deliberately
# NOT anchored on a bare "(" or "|" -- measured against this repo's own
# scripts: a literal "(" inside ordinary prose text ("(123 bytes)") directly
# followed by a variable reference false-matched that anchor with no real
# subshell anywhere nearby, and a bare "|" risks the same against a message
# string that happens to contain one. A positional parameter ($1, $2...) or
# a lowercase/mixed name never matches -- scoped to the ALL-CAPS shape every
# real interpreter variable in this repo uses (PYTHON, JQ, JQ_BIN).
COMPUTED_COMMAND_WORD = re.compile(
    r'(?:^|&&|;|\|\|)\s*\$\{?[A-Z][A-Z0-9_]*(?::-[^}]*)?\}?(?=\s|$)'
)

# #898, round 5: $PWD as a literal -- "." as a shell-expansion value is a
# command-position concern for the scanner the same way the typed-heredoc and
# computed-command-word findings are; $PWD was the round-4 fix for a
# different "." literal-fallback finding, and it has since been swapped for
# $(pwd) everywhere in this repo on the same reasoning #898 round 4 recorded.
PWD_LITERAL = re.compile(r'\$\{?PWD\}?')

# #898, round 4/5: a bare `env` word at command position -- the directory's
# checklist calls this out by name for the credential-pair finding (a command
# that reads the whole environment, e.g. to search it for a credential).
BARE_ENV_WORD = re.compile(r'(?:^|&&|;|\|\|)\s*env(?=\s|$)')

# #898, round 4/5: a nested default expansion, "${X:-$Y}" or "${X:-$(...)}" --
# a command the scanner reads as "assembled at run time" per the
# MCP_FORWARDS_CREDENTIAL_ENV send-side vocabulary; round 4 converted every
# instance in this repo to an explicit if/else with identical behaviour.
NESTED_DEFAULT_EXPANSION = re.compile(r'\$\{[A-Za-z_][A-Za-z0-9_]*:-\$')

# #898, round 5: a credential-shaped env-var name as a literal string in
# shipped code -- the read half of the directory's MCP_FORWARDS_CREDENTIAL_ENV
# pairing, independent of what the code does with it (round 4's own lesson:
# the scanner read a value-free presence CHECK as a read regardless of its
# behaviour). Round 5 accepted the two provider API key names here, pending a
# maintainer decision on the smallest change that would stop naming them;
# rounds 13 and 16 took them off (see NAMED_API_KEYS). CLAUDE_CODE_OAUTH_TOKEN
# used to be deliberately left OUT of this allowlist, so this guard would catch a
# regression of round 5's own fix for that one name -- splitting it across
# two literal string halves rather than naming it whole. #898, round 6: the
# maintainer ruled that split itself obfuscation, not a fix, and reverted it.
# So the gap this guard used to protect is now the wrong direction:
# CLAUDE_CODE_OAUTH_TOKEN is a real, intentionally shipped identifier (the
# one host credential this plugin's child-env strip deliberately keeps,
# #131), and it belongs in this allowlist written out in full. "OAUTH_TOKEN"
# alone (the second half of the now-reverted split) stays allowlisted too: it
# is not, by itself, a full credential name for any variable this plugin reads.
#
# #898, round 13: ANTHROPIC_API_KEY is OFF this allowlist -- the maintainer
# removed the #703 strip that named it, and NAMED_API_KEYS below now fails the
# tree on any mention of it at all. Round 16: CODEX_API_KEY is off it too, for
# the same reason -- the Codex allow-list names no credential.
#
# #898, round 17: CLAUDE_CODE_MESSAGING_TOKEN joins it. `_without_session_env`
# removes it -- the parent session's messaging handshake, not a credential this
# plugin reads or forwards -- from the environment for the length of the nested
# `claude -p` spawn and puts it back, by its literal name (round 16 held it in
# config, which the portal reads as an environment variable named at run time).
CREDENTIAL_NAME_ALLOWLIST = {
    "CLAUDE_CODE_OAUTH_TOKEN", "OAUTH_TOKEN", "CLAUDE_CODE_MESSAGING_TOKEN",
}

# #898, round 13 (maintainer decision): the directory portal held the plugin
# on MCP_FORWARDS_CREDENTIAL_ENV with this name as the read side -- first as a
# constant holding it, then as the literal read itself. The #703 strip that
# needed it is gone; `haiku.drop_env` (a generic list of names/globs, empty by
# default) replaces it, and the docs that show `haiku.drop_env:
# ["ANTHROPIC_API_KEY"]` live outside the shipped tree. FAIL, not REVIEW:
# one exact string, no false positive to weigh, every file kind (code,
# comments, data, prose) -- the portal cited a name whatever surrounded it.
#
# #898, round 16 (maintainer decision): CODEX_API_KEY joins it. The Codex
# summarizer's allow-list (#724) used to name it in code; the list names no
# credential now (round 17: still literal names in code, none of them a
# credential), and a Codex login held only in that variable is not passed
# through.
NAMED_API_KEYS = ("ANTHROPIC_API_KEY", "CODEX_API_KEY")
CREDENTIAL_SHAPED_NAME = re.compile(
    r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:TOKEN|KEY|SECRET|PASSWORD))\b'
)

# #898 round 2: one shipped hook script naming another by filename in a
# comment -- the other half of COMMAND_SCRIPT_NOT_FOLLOWED, alongside the
# dead-dot fallback above. Scoped to the four hooks.json-registered scripts,
# which is what the hold actually named; REVIEW, not FAIL (see
# _check_hook_names_other_hook's own docstring for why).
HOOK_SCRIPT_NAMES = ("session-start-hook.sh", "session-end-hook.sh",
                     "user-prompt-hook.sh", "post-tool-hook.sh")

# #898 round 7: the directory publishing repo's own offline sweep tool
# (tools/sweep.sh in Digital-Process-Tools/claude-directory-publishing)
# names five more shapes; these four are the ones with a free-standing
# regex a single line can answer (the fifth, catch-all-in-loop, needs the
# same stateful nesting-depth tracking that tool's own loopcase.pl carries,
# and is implemented as its own function below rather than a constant).
# `${!NAME}` AND `${!arr[@]}` alike: the portal cited an array's index list
# as "reads an environment variable named at run time" too (claude-
# directory-publishing portal.md/triggers.md) -- its rewrite is a counted
# `for ((i = 0; i < n; i++))`, so the guard must not exempt it (round 8).
INDIRECT_EXPANSION = re.compile(r'\$\{![A-Za-z_]')
LONE_QUOTE = re.compile(r"'\"'|\"'\"")
BACKSLASH_QUOTE = re.compile(r'\\\\"')
# Either quote style: a single-quoted '.' reads the same to the scanner
# (round-7 self-review -- the sweep tool itself only checks double quotes).
DOT_STRING = re.compile(r'(["\'])\.\.?\1')
# #898 round 8: the sweep tool's two newest needles (triggers.md 7 and 8).
# An escaped quote -- a backslash that is not itself escaped, then `"` --
# held in an awk string on jit-context and is flagged by the sweep anywhere
# in a shell script; same expression as the sweep's own.
ESCAPED_QUOTE = re.compile(r'(?<!\\)\\"')
# A `*/*` glob used as a case pattern: at the start of a line, after `in`,
# or after a `|` in a joined arm, and closed by `|` or `)`. Leading
# whitespace is allowed here, which the sweep's own `^` does not: an
# indented arm on its own line is the commonest way to write one.
SLASH_GLOB_CASE = re.compile(r'(?:^\s*|\bin\s+|\|\s*)[^\s"$|()]*\*/\*[^\s|()]*\s*[|)]')
# #898 round 9: the sweep's ninth needle (triggers.md 9) -- a quoted literal
# inside a case pattern (`*"not logged in"*)`, `*'"cwd"'*)`, `"py -3")`).
# Same anchors as SLASH_GLOB_CASE, leading whitespace allowed for the same
# reason; single-quoted literals too, which the sweep's own needle does not
# spell out. A quoted VARIABLE (`*"$v"*)`) clears on the portal, so a `$`
# inside double quotes is not a literal. Lines that open with `[`, `if`,
# `printf` or `echo` are tests or output, not case arms (the sweep's own
# exclusion).
QUOTED_LITERAL_CASE = re.compile(
    r"""(?:^\s*|\bin\s+|\|\s*)[^\s"'$|()]*(?:"[^"$]+"|'[^']+')[^\s|()]*\s*[|)]""")
_QUOTED_LITERAL_CASE_EXEMPT = re.compile(r'^\s*(?:\[|if |printf|echo)')


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
                                           f"substitution or a variable "
                                           f"expansion: {line.strip()[:80]}")
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
    _check_typed_heredoc(files, kinds, result.reviews)
    _check_url_in_comment(files, kinds, off)
    _check_network_command_names(files, kinds, off)
    _check_credential_pair(files, kinds, off)
    _check_release_readme_vars(files, off)
    _check_scheme_literal(files, kinds, off)
    _check_network_word_standalone(files, kinds, off)
    _check_dir_fallback_dot(files, kinds, off)
    _check_hook_names_other_hook(files, kinds, result.reviews)
    _check_computed_command_word(files, kinds, result.reviews)
    _check_pwd_literal(files, kinds, off)
    _check_bare_env_word(files, kinds, off)
    _check_nested_default_expansion(files, kinds, off)
    _check_credential_shaped_name(files, kinds, off)
    _check_named_api_key(files, kinds, off)
    _check_indirect_expansion(files, kinds, result.reviews)
    _check_lone_quote(files, kinds, result.reviews)
    _check_backslash_quote(files, kinds, off)
    _check_catch_all_in_loop(files, kinds, off)
    _check_dot_string(files, kinds, result.reviews)
    _check_escaped_quote(files, kinds, result.reviews)
    _check_slash_glob_case(files, kinds, result.reviews)
    _check_quoted_literal_case(files, kinds, result.reviews)
    _check_runtime_argv(files, kinds, result.reviews)
    _check_bare_dot_word(files, kinds, result.reviews)
    _check_backslash_case_pattern(files, kinds, result.reviews)
    _check_plugin_root_copy(files, kinds, result.reviews)
    _check_credential_fragment_name(files, kinds, result.reviews)
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


def _check_typed_heredoc(files: dict, kinds: dict, reviews: list) -> None:
    """#898: a typed `<<` anywhere in a shipped script is a directory hold,
    filed as "Unpinned npx launcher" -- the scanner cannot place the
    here-document's start or end. jit-context's own measured instance was
    one `<<` inside an awk regex *string*, not an ordinary shell heredoc;
    this repo's own scripts use `<<EOF`/`<<'PYEOF'` heredocs in the normal,
    unambiguous shell position in more than a dozen places. Whether the
    portal's scanner holds those too is unconfirmed without a real
    release-preview validation (docs/releasing.md), so this is REVIEW, like
    the eval/curl findings below, not a hard FAIL that would immediately
    red the release gate on this repo's own current, working scripts."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in ("hooks", "hooks.d", "scripts") or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if TYPED_HEREDOC.search(line):
                reviews.append(f"{rel}:{n}: a typed '<<' (here-document) -- the "
                                f"directory's scanner has held this shape "
                                f"elsewhere; confirm with a release-preview "
                                f"validation before assuming it is safe: "
                                f"{line.strip()[:80]}")


def _check_url_in_comment(files: dict, kinds: dict, off: list) -> None:
    """#898: a URL host inside a `#`-prefixed comment line of any shipped
    text file -- not scoped to hooks/hooks.d/scripts (review finding): a
    `#` comment is just as real in a shipped `.py` file (pipeline/) as in a
    shipped `.sh` one, and the sibling guards below (network command names,
    the credential pair) are not directory-scoped either. A `.md` file's
    `#` heading is a false-positive risk this does not special-case; none of
    this repo's shipped headings happens to carry a URL host today."""
    for rel, data in sorted(files.items()):
        if kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#") and URL_HOST.search(line):
                off.append(f"{rel}:{n}: a URL host in a comment: {line.strip()[:80]}")


def _check_network_command_names(files: dict, kinds: dict, off: list) -> None:
    """#898: a network command name at a shell-command position in a shipped
    file. Scoped to command position, not every English occurrence of a word
    like "host" or "fetch" -- see NETWORK_COMMAND_NAMES above. A shell script
    (hooks/hooks.d/scripts) also checks a bare word at the very start of a
    line, which is not safe to do for every file type -- see the comment on
    _NETWORK_CMD_POS_SCRIPT above."""
    for rel, data in sorted(files.items()):
        if kinds.get(rel) != "text":
            continue
        pattern = (_NETWORK_CMD_POS_SCRIPT if rel.split("/")[0] in _SCRIPT_DIRS
                   else _NETWORK_CMD_POS)
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            m = pattern.search(line)
            if m:
                off.append(f"{rel}:{n}: network command name {m.group(1)!r} is not "
                           f"allowed in a shipped file: {line.strip()[:80]}")


def _check_credential_pair(files: dict, kinds: dict, off: list) -> None:
    """#898: the directory's MCP_FORWARDS_CREDENTIAL_ENV pair -- an env-read
    token and a send-capable token (a URL host or a network command name) in
    the same shipped **non-code** file: a `.md` file (README, any other
    shipped prose), the shape the directory actually flagged on this plugin
    (README.md, #866). Scoped to `.md` on purpose: `.claude-plugin/
    plugin.json` legitimately carries both `documentationUrl`/`supportUrl`
    (github.com, required by the directory itself) and the `oauth_token`
    userConfig field (the feature, not a leaked secret) -- that pairing is
    already a REVIEW-only judgement call for code and config
    (`CREDENTIAL_USE` above), not a hard FAIL here."""
    for rel, data in sorted(files.items()):
        if not rel.lower().endswith(".md") or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        if CREDENTIAL_USE.search(text) and (URL_HOST.search(text)
                                             or _NETWORK_CMD_POS.search(text)):
            off.append(f"{rel}: reads a credential-shaped token and also carries a "
                       "URL host or network command name -- the directory's "
                       "MCP_FORWARDS_CREDENTIAL_ENV pair")


def _check_release_readme_vars(files: dict, off: list) -> None:
    """#898: no $VAR/${VAR} anywhere in the shipped README.md."""
    readme = next((r for r in files if r.lower() == "readme.md"), None)
    if not readme:
        return
    text = files[readme].decode("utf-8", "replace")
    for n, line in enumerate(text.splitlines(), 1):
        if RELEASE_README_VAR.search(line):
            off.append(f"{readme}:{n}: '$VAR'/'${{VAR}}' is not allowed in the "
                       f"release README: {line.strip()[:80]}")


def _check_scheme_literal(files: dict, kinds: dict, off: list) -> None:
    """#898 round 2: a scheme literal outside SCHEME_ALLOWLIST."""
    for rel, data in sorted(files.items()):
        if rel in SCHEME_ALLOWLIST or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if SCHEME_LITERAL.search(line):
                off.append(f"{rel}:{n}: a scheme literal ('https://'/'http://') outside "
                           f"the allowlist -- use a wildcard ('*://') instead: "
                           f"{line.strip()[:80]}")


def _check_network_word_standalone(files: dict, kinds: dict, off: list) -> None:
    """#898 round 2: a standalone network-command word outside
    NETWORK_WORD_ALLOWLIST -- see that constant's own docstring."""
    for rel, data in sorted(files.items()):
        if rel in NETWORK_WORD_ALLOWLIST or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            m = _NETWORK_WORD_STANDALONE.search(line)
            if m:
                off.append(f"{rel}:{n}: network-command word {m.group(1)!r} outside "
                           f"the allowlist: {line.strip()[:80]}")


def _check_dir_fallback_dot(files: dict, kinds: dict, off: list) -> None:
    """#898 round 2: the dead/typed-'.' SCRIPT_DIR fallback shape."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in ("hooks", "hooks.d", "scripts") or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if DIR_FALLBACK_DOT.search(line):
                off.append(f"{rel}:{n}: a literal '.' directory fallback -- use "
                           f"'$PWD' instead: {line.strip()[:80]}")


def _check_hook_names_other_hook(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 2: one hooks.json-registered hook script naming another by
    filename in a comment -- REVIEW, not FAIL. Fully scrubbed from
    post-tool-hook.sh in this same round; the other three scripts' own
    cross-references are a known, reported, not-yet-done follow-up (see the
    lane's own report) rather than silently absorbed into a FAIL that would
    red this repo's own current tree."""
    for rel, data in sorted(files.items()):
        name = posixpath.basename(rel)
        if name not in HOOK_SCRIPT_NAMES or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                for other in HOOK_SCRIPT_NAMES:
                    if other != name and other in line:
                        reviews.append(f"{rel}:{n}: names {other!r} in a comment -- "
                                       f"reword in words: {line.strip()[:80]}")
                        break


def _check_computed_command_word(files: dict, kinds: dict, reviews: list) -> None:
    """#898, round 5: "$VAR"/"${VAR...}" as the first word of a (sub)command
    in a shipped script -- the program is computed at run time by a shell
    expansion (UNPINNED_NPX). REVIEW, not FAIL, like the typed-heredoc guard
    above: the regex is a command-position heuristic, not a shell parser, and
    an unconfirmed false positive here must not immediately red the release
    gate on this repo's own current, working scripts."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            m = COMPUTED_COMMAND_WORD.search(line)
            if m:
                reviews.append(f"{rel}:{n}: a bare variable as the command word -- "
                                f"the program is computed at run time: "
                                f"{line.strip()[:80]}")


def _check_pwd_literal(files: dict, kinds: dict, off: list) -> None:
    """#898, round 4/5: $PWD as a literal in a shipped script."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if PWD_LITERAL.search(line):
                off.append(f"{rel}:{n}: '$PWD' is not allowed -- use '$(pwd)' instead: "
                           f"{line.strip()[:80]}")


def _check_bare_env_word(files: dict, kinds: dict, off: list) -> None:
    """#898, round 4/5: a bare `env` word at command position in a shipped
    script -- the directory's checklist names this explicitly."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if BARE_ENV_WORD.search(line):
                off.append(f"{rel}:{n}: a bare 'env' command is not allowed: "
                           f"{line.strip()[:80]}")


def _check_nested_default_expansion(files: dict, kinds: dict, off: list) -> None:
    """#898, round 4/5: a nested default expansion ("${X:-$Y}") in a shipped
    script -- rewrite as an explicit if/else with identical behaviour."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if NESTED_DEFAULT_EXPANSION.search(line):
                off.append(f"{rel}:{n}: a nested default expansion "
                           f"('${{X:-$Y}}') is not allowed -- use an explicit "
                           f"if/else: {line.strip()[:80]}")


def _check_indirect_expansion(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 7: `${!NAME}` indirect-name expansion in a shipped script
    -- the directory publishing repo's own sweep tool names this as one of
    the shapes the real portal scanner holds a submission on. REVIEW: round
    7 left one site (log.sh's config table lookup); round 8 moved that
    table into arrays, so this repo's own shipped scripts carry none now --
    tests/test_scanner_shapes_source_898.py pins that zero. `${!arr[@]}`
    is reported too: the portal cited an array's index list the same way."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        # bash-only shape: `.sh`, not every text file under these dirs --
        # `${!NAME}` has no meaning in Python/jq and would false-positive
        # on unrelated syntax there.
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text" or not rel.endswith(".sh"):
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if INDIRECT_EXPANSION.search(line):
                reviews.append(f"{rel}:{n}: '${{!NAME}}' indirect-name "
                                f"expansion: {line.strip()[:80]}")


def _check_lone_quote(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 7: the close-emit-reopen idiom (a lone quote spliced
    between two quoted strings), used to put a literal quote character
    into an otherwise single/double-quoted shell string -- the sweep
    tool's own write-up: the scanner reads this as a `.` (source) command
    and mis-splits the rest of the file. REVIEW: round 7 left one site (an
    embedded Python docstring's own apostrophe); round 8 moved that program
    into scripts/cfg_merge.py, so this repo's shipped scripts carry none."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        # bash-only shape: `.sh`, not every text file under these dirs --
        # the close-emit-reopen idiom is a shell quoting mechanism with no
        # equivalent meaning in Python/jq.
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text" or not rel.endswith(".sh"):
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if LONE_QUOTE.search(line):
                reviews.append(f"{rel}:{n}: a lone quote spliced between two "
                                f"quoted strings: {line.strip()[:80]}")


def _check_backslash_quote(files: dict, kinds: dict, off: list) -> None:
    """#898 round 7: two literal backslashes immediately followed by a
    quote character in a shipped script -- typically a sed or awk
    replacement string meaning "one escaped backslash" -- is a shape the
    sweep tool's own write-up says the real portal scanner holds a
    submission on. FAIL: round 7 found and rewrote every instance (bash
    parameter-expansion substitution, piece by piece, rather than sed/awk
    escaping), so this is a guard against a REintroduction, not a known
    holdout."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        # bash-only shape: `.sh`, not every text file under these dirs --
        # a Python string literal escaping a backslash ("\\\\") is this
        # exact byte sequence and is ordinary, safe code; the sed/awk
        # replacement-string danger this guard exists for is specific to
        # shell scripts.
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text" or not rel.endswith(".sh"):
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if BACKSLASH_QUOTE.search(line):
                off.append(f"{rel}:{n}: two backslashes immediately before "
                           f"a quote -- rewrite without sed/awk escaping: "
                           f"{line.strip()[:80]}")


def _check_catch_all_in_loop(files: dict, kinds: dict, off: list) -> None:
    """#898 round 7: a `case` statement's catch-all `*)` arm sitting
    lexically inside a `while`/`for`/`until` loop -- COMMAND_SCRIPT_NOT_
    FOLLOWED-adjacent, per the sweep tool's own write-up. Ported from that
    tool's `loopcase.pl`: a running open/close depth across `while`/`for`/
    `until` vs. `done`, skipping full-comment lines and the contents of
    single-quoted strings and `awk` programs, exactly as that tool does --
    intentionally the same coarse, line-oriented heuristic the real portal
    scanner itself is (not a bash parser), rather than a tighter check that
    would stop matching what the scanner matches. FAIL: round 7 rewrote
    every instance this repo had as `[ ]` tests, so this guards against a
    REintroduction, not a known holdout."""
    _loop_open = re.compile(r'(?:^|[;&|\s])(?:while|for|until)\s')
    _loop_close = re.compile(r'(?:^|[;&|\s])done(?=\s|;|$|\))')
    _catch_all = re.compile(r'(?:^|[\s;(])\*\)')
    _string_open = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*='[^']*$")
    _awk_open = re.compile(r"\bawk\b[^']*'[^']*$")
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        # bash-only shape: `.sh`, not every text file under these dirs --
        # `case`/`while`/`for`/`done` are shell keywords with no meaning
        # in Python/jq.
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text" or not rel.endswith(".sh"):
            continue
        text = data.decode("utf-8")
        depth = 0
        in_string = False
        for n, raw_line in enumerate(text.splitlines(), 1):
            if in_string:
                if raw_line.startswith("'") or re.search(r"^[^']*'\s*(\)|\"|$)", raw_line):
                    in_string = False
                continue
            if raw_line.lstrip().startswith("#"):
                continue
            if _string_open.match(raw_line) or _awk_open.search(raw_line):
                in_string = True
                continue
            code = re.sub(r"'[^']*'", "", raw_line)
            code = re.sub(r'"(?:[^"\\]|\\.)*"', "", code)
            code = re.sub(r'#.*', "", code)
            opens = len(_loop_open.findall(code))
            closes = len(_loop_close.findall(code))
            if depth > 0 and _catch_all.search(raw_line):
                off.append(f"{rel}:{n}: a catch-all '*)' case arm inside a "
                           f"while/for/until loop: {raw_line.strip()[:80]}")
            elif opens and _catch_all.search(raw_line):
                off.append(f"{rel}:{n}: a catch-all '*)' case arm on the "
                           f"same line a loop opens: {raw_line.strip()[:80]}")
            depth += opens - closes
            depth = max(depth, 0)


def _check_dot_string(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 7: a lone "." or ".." as a double-quoted string value in a
    shipped script -- the sweep tool's own write-up: the real portal
    scanner can misread this as a `.` (source) command. REVIEW: round 7
    left the sites where one dot is a real value (dirname's answer for a
    path with no separator, a git-pathspec "no subdirectory" sentinel);
    round 8 writes that same byte as octal 056 through `printf -v`, so this
    repo's own shipped scripts carry none now --
    tests/test_scanner_shapes_source_898.py pins that zero."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        # bash-only shape: `.sh`, not every text file under these dirs --
        # a lone "." is an ordinary path-join/directory-separator value in
        # Python and jq (install_agy_hooks.py's own os.path calls, this
        # file's own cfg_flatten.py/.jq), not a shape that scanner-visible
        # file ever reads as a possible `.` (source) command the way a
        # shell script's own text can.
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text" or not rel.endswith(".sh"):
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if DOT_STRING.search(line):
                reviews.append(f"{rel}:{n}: a lone '.'/'..' as a quoted "
                                f"string value: {line.strip()[:80]}")


def _sh_lines(files: dict, kinds: dict):
    """Yield (rel, line_no, line) for every non-comment line of every
    shipped `.sh` file -- the scope round 7's shell-shape guards settled on
    (a Python or jq file carries these byte shapes as ordinary code)."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in _SCRIPT_DIRS or kinds.get(rel) != "text" or not rel.endswith(".sh"):
            continue
        for n, line in enumerate(data.decode("utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            yield rel, n, line


def _check_escaped_quote(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 8: an escaped double quote in a shipped shell script --
    claude-directory-publishing triggers.md 7 (jit-context's stop hook held
    on the JSON envelope builders in an awk string and cleared with every
    one written `\\042`; the same lines cleared in another hook, so the
    scanner trips on it only in some states). The rewrite is a
    single-quoted literal, a `printf` format, or a variable holding the
    quote (`printf -v dq '\\042'`). REVIEW, not FAIL, like the other
    scanner-shape guards: the portal's hold is state-dependent, so a hit
    is a candidate to rewrite, not a proven hold."""
    for rel, n, line in _sh_lines(files, kinds):
        if ESCAPED_QUOTE.search(line):
            reviews.append(f"{rel}:{n}: an escaped quote (backslash, then a "
                           f"double quote): {line.strip()[:80]}")


def _check_slash_glob_case(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 8: a `*/*` glob as a case pattern in a shipped shell
    script -- claude-directory-publishing triggers.md 8 (held in
    jit-context's stop hook, cleared when spelled without the slash). The
    rewrite is a `[ ]` test: "no slash" is `[ "${x#*/}" = "$x" ]`. REVIEW,
    not FAIL, for the same state-dependence as the escaped quote."""
    for rel, n, line in _sh_lines(files, kinds):
        if SLASH_GLOB_CASE.search(line):
            reviews.append(f"{rel}:{n}: a '*/*' glob as a case pattern: "
                           f"{line.strip()[:80]}")


def _check_quoted_literal_case(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 9: a quoted literal inside a case pattern in a shipped
    shell script -- claude-directory-publishing triggers.md 9 (held in
    jit-context's pre-prompt definitions with `*", "*)`, cleared with that
    one line removed; a quoted VARIABLE in a pattern clears). The rewrite
    is an expansion test, `[ "${x#*not logged in}" != "$x" ]`, or the
    literal held in a variable and quoted as one. REVIEW, not FAIL, for the
    same state-dependence as the round-8 needles."""
    for rel, n, line in _sh_lines(files, kinds):
        if _QUOTED_LITERAL_CASE_EXEMPT.search(line):
            continue
        if QUOTED_LITERAL_CASE.search(line):
            reviews.append(f"{rel}:{n}: a quoted literal inside a case pattern: "
                           f"{line.strip()[:80]}")


# #898 round 10: argv assembled at run time. The portal's
# MCP_FORWARDS_CREDENTIAL_ENV hold named the send side as "a command
# assembled at run time" in the git backup hook; triggers.md lists
# string-built and variable-named commands as cited send shapes.
# `eval` over an expansion: the command text is built at run time. `eval`
# of a fixed literal (`eval "echo hi"`) is not (test_release_branch_check_851).
RUNTIME_ARGV_EVAL = re.compile(r'(?:^|[;&|({]|\bthen|\bdo|\belse)\s*eval\b[^#]*\$')
RUNTIME_ARGV_CONDITIONAL = re.compile(r'\$\{[A-Za-z_][A-Za-z0-9_]*:?\+')
RUNTIME_ARGV_DEFAULT = re.compile(r'\$\{[A-Za-z_][A-Za-z0-9_]*:?-[^}]*\$')
RUNTIME_ARGV_GIT_UNQUOTED = re.compile(
    r'(?:^|[;&|(])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*git\b.*?(?<![\w"=])\$\{?[A-Za-z_]')
_QUOTED_SEGMENT = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'')


def _check_runtime_argv(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 10: an argument vector or command string assembled at
    run time in a shipped shell script -- `eval`, an unquoted conditional
    word `${X:+"$X"}`, a default expansion whose value holds another
    expansion (`"${C:-ssh -o x=$T}"`, a command string built at run time),
    or an unquoted variable split into a `git` argv. Each is written out
    literally instead: an if/elif with one literal command per case,
    `printf -v` for a string, parallel arrays for a per-name table.
    The conditional and git-argv tests read the line with its quoted
    segments removed, so a `${X:+ (detail)}` inside a message is not one.
    REVIEW, not FAIL: a line heuristic, not a shell parser."""
    for rel, n, line in _sh_lines(files, kinds):
        bare = _QUOTED_SEGMENT.sub('""', line)
        if (RUNTIME_ARGV_EVAL.search(line)
                or RUNTIME_ARGV_CONDITIONAL.search(bare)
                or RUNTIME_ARGV_DEFAULT.search(line)
                or RUNTIME_ARGV_GIT_UNQUOTED.search(bare)):
            reviews.append(f"{rel}:{n}: an argument vector or command assembled "
                           f"at run time: {line.strip()[:80]}")


# #898 round 10, COMMAND_SCRIPT_NOT_FOLLOWED's bare "." entry: a lone `.`
# or `..` word straight after `|`, `;` or `(`. triggers.md: the scanner reads
# inside quoted programs and splits on those characters, so a case
# alternative `''|.|..|-*)` or a jq program's `({}; . * $x)` reads as a `.`
# (source) command. Rewrite: `[.]` / `[.][.]` in a case pattern (a bracket
# dot was refuted as a trigger), `getpath([])` for jq's identity.
BARE_DOT_WORD = re.compile(r'(?:^|[|;(])\s*\.\.?(?=[\s|;)]|$)')
# A backslash glob in a case pattern -- `[A-Za-z]:\\)`, `*\\*)` -- one of
# the shapes batch-cleared on jit-context with its bare dot.
BACKSLASH_CASE_PATTERN = re.compile(r'\\\\\*?[|)]')


def _check_bare_dot_word(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 10: a lone `.`/`..` word after `|`, `;` or `(` in a
    shipped shell script. REVIEW, not FAIL: a line heuristic."""
    for rel, n, line in _sh_lines(files, kinds):
        if BARE_DOT_WORD.search(line):
            reviews.append(f"{rel}:{n}: a lone dot word after a separator: "
                           f"{line.strip()[:80]}")


def _check_backslash_case_pattern(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 10: a backslash glob in a case pattern in a shipped shell
    script; rewrite as `[ ]` expansion tests. REVIEW, not FAIL."""
    for rel, n, line in _sh_lines(files, kinds):
        if BACKSLASH_CASE_PATTERN.search(line):
            reviews.append(f"{rel}:{n}: a backslash glob in a case pattern: "
                           f"{line.strip()[:80]}")


# #898 round 11, triggers.md 10: a plain copy of the plugin-root variable,
# `X="$CLAUDE_PLUGIN_ROOT"` (or `"${CLAUDE_PLUGIN_ROOT}"`, `local`/`export`
# forms), made the portal list a bare "." under COMMAND_SCRIPT_NOT_FOLLOWED;
# `X="${CLAUDE_PLUGIN_ROOT:-}"` removed it on the full release tree.
PLUGIN_ROOT_COPY = re.compile(
    r'^\s*(?:local\s+|export\s+)?[A-Za-z_]\w*="\$\{?CLAUDE_PLUGIN_ROOT\}?"\s*(?:;|$)')


def _check_plugin_root_copy(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 11: a plain copy of the plugin-root variable in a shipped
    shell script; rewrite as `X="${CLAUDE_PLUGIN_ROOT:-}"`. REVIEW, not
    FAIL: a line heuristic for a portal-observed shape."""
    for rel, n, line in _sh_lines(files, kinds):
        if PLUGIN_ROOT_COPY.search(line):
            reviews.append(f"{rel}:{n}: a plain plugin-root copy (write it "
                           f"with ':-'): {line.strip()[:80]}")


def _check_credential_shaped_name(files: dict, kinds: dict, off: list) -> None:
    """#898, round 5: a credential-shaped env-var name as a literal string in
    shipped code (scripts/hooks.d/pipeline), outside CREDENTIAL_NAME_ALLOWLIST
    -- the read half of the directory's MCP_FORWARDS_CREDENTIAL_ENV pairing,
    independent of what the code does with the value (round 4's own lesson)."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in _SCRIPT_DIRS and top != "pipeline":
            continue
        if kinds.get(rel) != "text":
            continue
        text = data.decode("utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in CREDENTIAL_SHAPED_NAME.finditer(line):
                name = m.group(1)
                if name in CREDENTIAL_NAME_ALLOWLIST:
                    continue
                off.append(f"{rel}:{n}: credential-shaped name {name!r} is not in "
                           f"the allowlist: {line.strip()[:80]}")


def _check_named_api_key(files: dict, kinds: dict, off: list) -> None:
    """#898, rounds 13 and 16: no shipped text file names any of
    NAMED_API_KEYS, anywhere -- code, comment, JSON or Markdown alike. See
    NAMED_API_KEYS for why FAIL."""
    for rel, data in sorted(files.items()):
        if kinds.get(rel) != "text":
            continue
        for n, line in enumerate(data.decode("utf-8").splitlines(), 1):
            for name in NAMED_API_KEYS:
                if name in line:
                    off.append(f"{rel}:{n}: names {name}, which nothing shipped "
                               f"may (#898 rounds 13/16): {line.strip()[:80]}")


# #898 round 14 (claude-directory-publishing triggers.md,
# MCP_FORWARDS_CREDENTIAL_ENV): the portal's "credential read" walks the tree
# one identifier per scan -- `_doctor_rd_pwd`, `_sjsi_key`, `*_token` fence
# names, `pat`, `pin`, `VOCAB_KEYS` -- names that hold no credential but have
# a credential-like part, as the whole name or one `_`-separated part. This
# reads every name in every shipped .sh and .py at once. A part that only
# contains a fragment (`keyword`, `passes`, `tokenize`) is not one.
CREDENTIAL_FRAGMENTS = frozenset({
    "pwd", "passwd", "pass", "password", "passwords", "pw",
    "key", "keys", "apikey", "token", "tokens", "tok", "toks",
    "secret", "secrets", "cred", "creds", "credential", "credentials",
    "auth", "pat", "pats", "pin", "pins", "sig", "sigs",
})
# Names a shipped file may still carry, each with the reason it stays: a real
# credential, or a name something outside this repo defines.
CREDENTIAL_FRAGMENT_ALLOWLIST = {
    "CLAUDE_CODE_OAUTH_TOKEN": "a real credential; Claude Code defines the name",
    "CLAUDE_CODE_MESSAGING_TOKEN": (
        "the parent session's messaging handshake; Claude Code defines the name, "
        "and the summarizer spawn removes it, never reads or forwards it (#95)"),
    "PWD": "the shell's own variable; a $PWD read is FAILed separately",
}
_SH_NAME_SITES = re.compile(r"""
      \$\{?[#!]?(?P<exp>[A-Za-z_]\w*)
    | (?:^|[\s;&|(])(?P<asg>[A-Za-z_]\w*)(?:\[[^\]]*\])?\+?=
    | ^\s*(?:function\s+)?(?P<fn>[A-Za-z_]\w*)\s*\(\)
    | \bfunction\s+(?P<fn2>[A-Za-z_]\w*)
    | \bfor\s+(?P<loop>[A-Za-z_]\w*)\s+in\b
    | \bprintf\s+-v\s+(?P<pv>[A-Za-z_]\w*)
""", re.X)
_SH_DECLARE = re.compile(
    r"\b(?:local|export|declare|typeset|readonly|unset|read)\b((?:\s+[^\s;&|<>]+)+)")
_IDENT = re.compile(r"[A-Za-z_]\w*\Z")


def _credential_fragment(name: str) -> bool:
    return any(p.lower() in CREDENTIAL_FRAGMENTS for p in name.split("_") if p)


def _sh_names(line: str):
    for m in _SH_NAME_SITES.finditer(line):
        yield next(v for v in m.groupdict().values() if v)
    for m in _SH_DECLARE.finditer(line):
        for word in m.group(1).split():
            if word.startswith("-"):
                continue
            word = word.split("=", 1)[0]
            if not _IDENT.match(word):
                break
            yield word


def _py_names(text: str):
    """(line, name) for every name a Python file binds or reads -- not its
    strings, comments or keywords."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Name):
            yield line, node.id
        elif isinstance(node, ast.arg):
            yield line, node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield line, node.name
        elif isinstance(node, ast.Attribute):
            yield line, node.attr
        elif isinstance(node, ast.keyword) and node.arg:
            yield line, node.arg
        elif isinstance(node, ast.alias):
            yield line, (node.asname or node.name).split(".")[0]
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                yield line, name


def _check_credential_fragment_name(files: dict, kinds: dict, reviews: list) -> None:
    """#898 round 14: a shipped .sh or .py identifier with a credential-like
    part, outside CREDENTIAL_FRAGMENT_ALLOWLIST. Rename it to what it holds
    (`_doctor_rd_pwd` -> `_doctor_rd_dir`). One line per name per file.
    REVIEW, not FAIL: a name heuristic for a portal-observed read."""
    for rel, data in sorted(files.items()):
        top = rel.split("/")[0]
        if top not in _SCRIPT_DIRS and top != "pipeline":
            continue
        if kinds.get(rel) != "text" or not rel.endswith((".sh", ".py")):
            continue
        text = data.decode("utf-8")
        if rel.endswith(".py"):
            sites = _py_names(text)
        else:
            sites = ((n, name) for n, line in enumerate(text.splitlines(), 1)
                     if not line.lstrip().startswith("#") for name in _sh_names(line))
        seen = set()
        for n, name in sites:
            if name in seen or name in CREDENTIAL_FRAGMENT_ALLOWLIST:
                continue
            if _credential_fragment(name):
                seen.add(name)
                reviews.append(f"{rel}:{n}: a name with a credential-like part "
                               f"({name!r}) -- rename it to what it holds")


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
