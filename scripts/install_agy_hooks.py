#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import os
import shlex
import sys
DEFAULT_TARGET = os.path.expanduser("~/.gemini/config/hooks.json")
_EVENT_SCRIPTS = {
    "SessionStart": "agy-session-start-hook.sh",
    "PreInvocation": "agy-pre-invocation-hook.sh",
    "Stop": "agy-stop-hook.sh",
}
_TIMEOUT_SECONDS = 30
def build_remember_entry(plugin_root: str) -> dict:
    plugin_root = os.path.abspath(plugin_root)
    entry: dict = {"enabled": True}
    for event, script_name in _EVENT_SCRIPTS.items():
        script_path = os.path.join(plugin_root, "scripts", script_name)
        command_path = script_path.replace("\\", "/")
        command_path = shlex.quote(command_path)
        entry[event] = [
            {
                "type": "command",
                "command": f"bash {command_path}",
                "timeout": _TIMEOUT_SECONDS,
            }
        ]
    return entry
class CorruptHooksFile(Exception):
    pass
def _load_existing(target: str) -> dict | None:
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
def install(plugin_root: str, target: str = DEFAULT_TARGET) -> dict:
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
        json.dump(_sorted_tree(data), f, indent=2)
        f.write("\n")
    return data
def _sorted_tree(value):
    if isinstance(value, dict):
        return {name: _sorted_tree(value[name]) for name in sorted(value)}
    if isinstance(value, list):
        return [_sorted_tree(item) for item in value]
    return value
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge Remember's Antigravity (agy) hooks into the shared "
                    "~/.gemini/config/hooks.json (#563).")
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
        print(json.dumps(_sorted_tree(existing), indent=2))
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
