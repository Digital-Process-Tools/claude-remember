import json
import re
import sys
def walk(node, prefix, out):
    if isinstance(node, dict):
        for k, v in node.items():
            walk(v, prefix + [k], out)
    elif isinstance(node, list):
        out.append((prefix, node))
    else:
        out.append((prefix, node))
def encode_list(items) -> str:
    if any(not isinstance(i, str) or i == "" or re.search(r"[ \t\n\r]", i) for i in items):
        return "#invalid"
    return "=" + " ".join(items)
def main() -> None:
    try:
        with open(sys.argv[1], encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
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
    for p, v in scalars:
        if not all(ok.match(part) for part in p):
            print("#refuse a config key is outside [A-Za-z0-9_]")
            sys.exit(0)
    slots = ["_".join(p) for p, _ in scalars + arrays]
    if len(set(slots)) != len(slots):
        print("#refuse two config keys flatten to the same name")
        sys.exit(0)
    if any(s.endswith(("__arr", "__obj")) for s in slots):
        print("#refuse a config key ends in a reserved suffix (__arr or __obj)")
        sys.exit(0)
    for p, v in scalars:
        if isinstance(v, str) and ("\t" in v or "\n" in v):
            print("#refuse a config value contains a tab or a newline")
            sys.exit(0)
    out = []
    for p, v in scalars:
        if v is None:
            continue
        out.append(".".join(p) + "\t" + (v if isinstance(v, str) else json.dumps(v)))
    for p, v in arrays:
        out.append(".".join(p) + "__arr\t" + encode_list(v))
    for k in objects:
        out.append(k + "__obj\t#object")
    sys.stdout.buffer.write("\n".join(out).encode("utf-8"))
if __name__ == "__main__":
    main()
