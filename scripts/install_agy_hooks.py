#!/usr/bin/env python3
"""install_agy_hooks.py -- merge Remember's Antigravity (agy) hooks into the
shared ~/.gemini/config/hooks.json (#563).

Antigravity has no per-plugin hook manifest: a plugin's own bundled
hooks.json is copied into ~/.gemini/config/plugins/<name>/, counted, and
never loaded (#553). The only paths `agy` actually loads hooks from are
`~/.gemini/config/hooks.json` (shared, every plugin on the machine) and the
untested workspace-local `<workspace>/.agents/hooks.json`. This script
writes the shared one.

It also has no working variable substitution inside that shared file: agy
sets no plugin-root or project-dir environment variable
(pipeline/host.py's ANTIGRAVITY host declares neither -- confirmed by
dumping a live hook process's own environment, #563), and the
${extensionPath} substitution Gemini CLI extensions get is documented as
extension-local only, which a shared, non-extension file is not. So every
command has to be a literal, already-resolved absolute path -- there is no
manifest this script could check into git and have work unmodified on
another machine, unlike hooks/hooks.codex.json or .gemini/hooks/hooks.json.
This script exists to build and merge that literal manifest at install
time instead.

The target is a file every plugin on the machine may already be using, so a
merge here touches only this plugin's own top-level hook name ("remember")
and leaves every other key alone -- see test_merge_preserves_other_top_level_hook_names_563.

USAGE
    python3 scripts/install_agy_hooks.py [--target PATH] [--dry-run]

    --target PATH   Defaults to ~/.gemini/config/hooks.json.
    --dry-run       Print the merged JSON; do not write anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_TARGET = os.path.expanduser("~/.gemini/config/hooks.json")

# #563's own scope: SessionStart, PreInvocation and Stop are the events this
# plugin has an adapter script and a design for (see the three
# scripts/agy-*.sh comments). PostInvocation is confirmed to load and fire
# too (#553's final comment), but nothing here has a use for it yet -- it is
# left out on purpose, not omitted by oversight.
_EVENT_SCRIPTS = {
    "SessionStart": "agy-session-start-hook.sh",
    "PreInvocation": "agy-pre-invocation-hook.sh",
    "Stop": "agy-stop-hook.sh",
}

_TIMEOUT_SECONDS = 30


def build_remember_entry(plugin_root: str) -> dict:
    """The "remember" -> spec object this plugin owns in the shared file.

    Every command is a literal absolute path built from ``plugin_root`` --
    no ${VAR} placeholder, because nothing observed on Antigravity would
    expand one (see the module docstring).
    """
    plugin_root = os.path.abspath(plugin_root)
    entry: dict = {"enabled": True}
    for event, script_name in _EVENT_SCRIPTS.items():
        script_path = os.path.join(plugin_root, "scripts", script_name)
        # #569: this string is embedded, unmodified, into a shared JSON
        # manifest that agy itself later re-parses as a shell command line
        # on whatever OS the install happens to run on. A backslash-bearing
        # Windows path (os.path.join's own native separator there) is not
        # guaranteed to survive that re-encoding the same way on every
        # quoting model agy might use -- forward slashes are, since every
        # path API bash and Windows itself expose accepts them, and no
        # POSIX-style or cmd.exe-style quoting rule treats them specially.
        # Normalizing here removes the ambiguity outright rather than
        # betting on which model agy implements.
        command_path = script_path.replace("\\", "/")
        entry[event] = [
            {
                "type": "command",
                "command": f'bash "{command_path}"',
                "timeout": _TIMEOUT_SECONDS,
            }
        ]
    return entry


class CorruptHooksFile(Exception):
    """Raised when ``target`` exists but cannot be read as a JSON object.

    Deliberately NOT the same outcome as a genuinely absent file (#563
    review finding): the shared hooks.json may hold another plugin's real
    hook entries that a parser error just could not reach -- a hand edit
    with a trailing comma, a half-written file from a concurrent process,
    disk corruption. Treating "cannot parse" the same as "nothing here
    yet" silently discards whatever else was in it the moment
    ``install()`` writes back, with no warning and no way to recover it.
    An absent file has nothing to lose; a corrupt one might. install()
    refuses rather than guessing, and leaves the file untouched.
    """


def _load_existing(target: str) -> dict | None:
    """The rest of the shared file, or {} for a genuinely absent one.

    Returns ``None`` (never partial/guessed data) if ``target`` exists but
    is not a parseable JSON object -- ``install()`` turns that into
    ``CorruptHooksFile`` rather than silently coercing it to ``{}``. Only a
    missing file (``FileNotFoundError``) is treated as "nothing here yet".
    """
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def install(plugin_root: str, target: str = DEFAULT_TARGET) -> dict:
    """Merge this plugin's Antigravity hooks into ``target``, in place.

    Returns the full merged document (for --dry-run and for tests), and
    also writes it unless the caller only wanted the dry-run value -- see
    main() below for that split.

    Raises ``CorruptHooksFile`` -- and writes NOTHING -- if ``target``
    exists but cannot be read as a JSON object; see that exception's own
    docstring for why this is not the same case as an absent file.
    """
    data = _load_existing(target)
    if data is None:
        raise CorruptHooksFile(
            f"{target} exists but is not a parseable JSON object -- refusing to "
            "overwrite it (it may hold another plugin's real hook entries). "
            "Inspect and fix or remove it by hand, then re-run this installer."
        )
    data["remember"] = build_remember_entry(plugin_root)
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    plugin_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    if args.dry_run:
        existing = _load_existing(args.target)
        if existing is None:
            print(f"ERROR: {args.target} exists but is not a parseable JSON object", file=sys.stderr)
            return 1
        existing["remember"] = build_remember_entry(plugin_root)
        print(json.dumps(existing, indent=2, sort_keys=True))
        return 0

    try:
        install(plugin_root=plugin_root, target=args.target)
    except CorruptHooksFile as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
