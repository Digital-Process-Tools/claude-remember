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
same refusals, same "haiku" top-level key and documentation-only "_comments"
block skipped, an array of single plain names carried as its own "<key>__arr"
row and a top-level object marked by a "<key>__obj" row (#1136), same textual
form for non-string values -- "true"/"false", never Python's "True"/"False" (the #159
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
        # #1136: an array is one row (see encode_list); main() decides which
        # arrays are carried. Paths INTO an array are never emitted.
        out.append((prefix, node))
    else:
        out.append((prefix, node))


def encode_list(items) -> str:
    """Same encoding as cfg_flatten.jq: "=" followed by the space-joined names
    for an array of single plain names, "#invalid" for anything else (#1136).
    The row itself is named "<key>__arr", so no string value can be mistaken
    for one. Whitespace is exactly space, tab, newline and carriage return --
    the jq side's set."""
    if any(not isinstance(i, str) or i == "" or re.search(r"[ \t\n\r]", i) for i in items):
        return "#invalid"
    return "=" + " ".join(items)


def main() -> None:
    try:
        with open(sys.argv[1], encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:  # noqa: BLE001 -- any read/parse failure is a hard miss, by design
        sys.exit(1)

    rows = []
    walk(doc, [], rows)
    rows = [
        (p, v)
        for p, v in rows
        if p and p[0] not in ("haiku", "_comments")
    ]
    ok = re.compile(r"\A[A-Za-z0-9_]+\Z")
    scalars = [(p, v) for p, v in rows if not isinstance(v, list)]
    # An array whose key is outside the alphabet is simply not carried: it
    # never causes that refusal and is not part of the collision check below.
    arrays = [
        (p, v) for p, v in rows if isinstance(v, list) and all(ok.match(part) for part in p)
    ]
    objects = []
    if isinstance(doc, dict):
        objects = [
            k
            for k, v in doc.items()
            if isinstance(v, dict) and k not in ("haiku", "_comments") and ok.match(k)
        ]

    # Same order as cfg_flatten.jq: every key, then collisions, then values.
    for p, v in scalars:
        if not all(ok.match(part) for part in p):
            print("#refuse a config key is outside [A-Za-z0-9_]")
            sys.exit(0)
    slots = ["_".join(p) for p, _ in scalars + arrays]
    if len(set(slots)) != len(slots):
        print("#refuse two config keys flatten to the same name")
        sys.exit(0)
    # the generated row names own the __arr / __obj suffixes
    if any(s.endswith(("__arr", "__obj")) for s in slots):
        print("#refuse a config key ends in a reserved suffix (__arr or __obj)")
        sys.exit(0)
    for p, v in scalars:
        if isinstance(v, str) and ("\t" in v or "\n" in v):
            print("#refuse a config value contains a tab or a newline")
            sys.exit(0)

    out = []
    # A null value takes part in every refusal above (as in cfg_flatten.jq,
    # where its path is still a path) but is never emitted.
    for p, v in scalars:
        if v is None:
            continue
        out.append(".".join(p) + "\t" + (v if isinstance(v, str) else json.dumps(v)))
    for p, v in arrays:
        out.append(".".join(p) + "__arr\t" + encode_list(v))
    for k in objects:
        out.append(k + "__obj\t#object")
    sys.stdout.write("\n".join(out))


if __name__ == "__main__":
    main()
