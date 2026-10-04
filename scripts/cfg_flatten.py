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
    except Exception:
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
