"""Flatten merged config.json into `NAME\\tVALUE` lines, the jq-free fallback
`_config_load` (log.sh) uses when jq is not on PATH (#668).

Called by literal path through `_remember_slug_run_python` (#898 round 7 --
this body used to live inline in log.sh, as `_REMEMBER_CFG_FLATTEN_PY`, a
single-quoted shell variable holding a multi-line program kept on one line
for the PATH-shim spawn counters in tests/. A lone `for` statement inside a
quoted string is itself the shape a line-oriented scanner cannot tell from
real bash: moving the program to its own file removes that risk rather than
asking the scanner to reason about where a shell string ends).

Same contract jq's own one-liner (`_REMEMBER_CFG_FLATTEN_JQ`, log.sh) keeps:
same refusals, same "haiku" top-level key skipped, same textual form for
non-string values -- "true"/"false", never Python's "True"/"False" (the #159
near miss this whole dual-path design exists to avoid repeating).
"""

import json
import re
import sys


def walk(node, prefix, out):
    if isinstance(node, dict):
        for k, v in node.items():
            walk(v, prefix + [k], out)
    elif isinstance(node, list):
        return
    else:
        out.append((prefix, node))


def main() -> None:
    try:
        with open(sys.argv[1], encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:  # noqa: BLE001 -- any read/parse failure is a hard miss, by design
        sys.exit(1)

    rows = []
    walk(doc, [], rows)
    rows = [(p, v) for p, v in rows if p and p[0] != "haiku" and v is not None]

    ok = re.compile(r"^[A-Za-z0-9_]+$")
    for p, v in rows:
        if not all(ok.match(part) for part in p):
            print("#refuse a config key is outside [A-Za-z0-9_]")
            sys.exit(0)
        if isinstance(v, str) and ("\t" in v or "\n" in v):
            print("#refuse a config value contains a tab or a newline")
            sys.exit(0)
    slots = ["_".join(p) for p, _ in rows]
    if len(set(slots)) != len(slots):
        print("#refuse two config keys flatten to the same name")
        sys.exit(0)

    out = []
    for p, v in rows:
        out.append(".".join(p) + "\t" + (v if isinstance(v, str) else json.dumps(v)))
    sys.stdout.write("\n".join(out))


if __name__ == "__main__":
    main()
