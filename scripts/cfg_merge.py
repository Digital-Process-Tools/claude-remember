import json
import sys
def deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = deep_merge(out[k], v) if k in out else v
        return out
    return b
out_path = sys.argv[1]
untrusted_haiku_path = sys.argv[2]
strip_model_reject = sys.argv[3] == "1"
drop_marker_path = sys.argv[4]
def load_documents(path):
    with open(path) as f:
        raw = f.read()
    decoder = json.JSONDecoder()
    idx, n, docs = 0, len(raw), []
    while idx < n:
        while idx < n and raw[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, idx = decoder.raw_decode(raw, idx)
        docs.append(obj)
    return docs
merged = {}
_dropped_project_layer = False
_dropped_trusted_layer = False
for path in sys.argv[5:]:
    if untrusted_haiku_path and path == untrusted_haiku_path:
        try:
            docs = load_documents(path)
        except (OSError, ValueError):
            _dropped_project_layer = True
            if drop_marker_path:
                try:
                    with open(drop_marker_path, "w") as _marker:
                        _marker.write("1")
                except OSError:
                    pass
            continue
        for data in docs:
            if isinstance(data, dict):
                drop = {"haiku"}
                if strip_model_reject:
                    drop |= {"model", "reject_pattern"}
                data = {k: v for k, v in data.items() if k not in drop}
            merged = deep_merge(merged, data)
        continue
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        _dropped_trusted_layer = True
        continue
    merged = deep_merge(merged, data)
merged = {k: v for k, v in merged.items() if not str(k).startswith("_")}
with open(out_path, "w") as f:
    json.dump(merged, f)
if _dropped_project_layer and _dropped_trusted_layer:
    sys.exit(5)
elif _dropped_project_layer:
    sys.exit(3)
elif _dropped_trusted_layer:
    sys.exit(4)
