#!/usr/bin/env python3
"""Refuse to publish a release tree whose plugin version does not move `release`
forward (#856).

A patch tag for an older line (say v0.36.2) pushed after v0.37.0 would otherwise
add the 0.36.2 tree as a child commit of the 0.37.0 one -- `release` would then
carry 0.36.2, and every install pinned to it would not move, because the version
dropped. This compares the new tree's `.claude-plugin/plugin.json` version
against the parent `release` commit's, both parsed as dotted-integer tuples
(e.g. "0.38.0" -> (0, 38, 0)), and refuses unless the new one is strictly
greater -- unless --allow-regression is passed, wired in release-branch.yml to
the workflow_dispatch `allow_version_regression` input, for intentionally
republishing an older line on purpose.

Usage:
    check_version_order.py --new 0.36.2 --parent 0.37.0 [--allow-regression]
"""

from __future__ import annotations

import argparse
import sys


def parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except ValueError as exc:
        raise SystemExit(f"not a dotted-integer version: {v!r} ({exc})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--new", required=True, help="the version about to be pushed")
    ap.add_argument("--parent", required=True, help="release's current HEAD version")
    ap.add_argument("--allow-regression", action="store_true",
                    help="skip the check (workflow_dispatch override)")
    args = ap.parse_args(argv)

    if args.allow_regression:
        print(f"allow-regression set -- not checking {args.new} against {args.parent}")
        return 0

    new_v, parent_v = parse_version(args.new), parse_version(args.parent)
    if new_v > parent_v:
        print(f"{args.new} > {args.parent} -- ok to publish")
        return 0

    print(f"::error::plugin.json version {args.new} is not strictly greater than "
          f"release's current {args.parent} -- refusing to publish (this would move "
          "the directory backwards). Re-run via workflow_dispatch with "
          "allow_version_regression if this is intentional.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
