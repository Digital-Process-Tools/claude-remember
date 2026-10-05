"""Merge the layered config files into one JSON object, the jq-free path
of lib-memory-dir.sh's config merge (#726, #740, #744, #748, #757, #804,
#815).

Called by literal path through lib-slug.sh's `_remember_slug_run_python`
(#898 round 8 -- this body used to live inline in that file, as a
single-quoted here-string. Its `for`/`while` lines read as shell loops to
a line-oriented scanner, and once the release build inlines the library
into every hook, every later catch-all `case` arm read as sitting inside
a loop. Same program, same argv, same exit codes; only where it lives
changed).

argv: OUT_PATH UNTRUSTED_HAIKU_PATH STRIP_MODEL_REJECT DROP_MARKER_PATH
      SOURCE...
exit: 0 clean, 3 untrusted project layer dropped, 4 a trusted layer
      dropped, 5 both; anything else is a merge failure.

"The shell", "above" and "below" in the comments point into lib-memory-dir.sh,
where this program used to sit.
"""

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
# Empty string when the project layer's `haiku` block is trusted (external
# storage mode, or no project cfg at all) -- never equal to a real path then,
# so nothing is stripped (#726, see the case switch this mirrors above).
untrusted_haiku_path = sys.argv[2]
# #757: "1" only when the SAME untrusted source is also git-tracked --
# model/reject_pattern are stripped alongside haiku only then, never for
# an untracked (the operator's own) in-project config.
strip_model_reject = sys.argv[3] == "1"
# #748: empty when mktemp itself failed above -- tolerated the same way
# every other mktemp-failure path in this file is (fall through, don't
# crash the merge over the logging side-channel itself).
drop_marker_path = sys.argv[4]


def load_documents(path):
    """Parse every whitespace-concatenated JSON document in `path` (#740):
    the untrusted project layer may ship more than one, and a plain
    json.load() raises `JSONDecodeError` on any file with more than one --
    which used to take the WHOLE merge down with it (the `|| cp
    "$_bundled_cfg" ...` fallback below), dropping the trusted user-global
    layer too rather than just stripping `haiku` from this file's own
    documents and keeping everything else."""
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
        # #744: fail CLOSED -- if the untrusted file can't even be loaded
        # (unreadable, a permissions error, malformed JSON, anything
        # load_documents() itself doesn't already tolerate), drop just this
        # layer rather than let the exception propagate and crash the whole
        # merge down to the bundled-only fallback below, taking the trusted
        # user-global layer's own overrides with it for no reason connected
        # to them. `json.JSONDecodeError` (raised by decoder.raw_decode() on
        # invalid JSON) and `UnicodeDecodeError` (raised by f.read() on a
        # file that isn't valid text in the expected encoding) are both
        # ValueError subclasses -- catching only OSError let either one
        # through uncaught.
        try:
            docs = load_documents(path)
        except (OSError, ValueError):
            # #748: drop a marker for the shell to notice and report --
            # this process's own stdout/stderr are discarded by the caller.
            # #804: also record the drop in a flag that becomes THIS
            # process's own exit code below -- a second, independent
            # signal that does not depend on drop_marker_path's own
            # mktemp (the shell side, above) having succeeded at all.
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
    # #815: this is a TRUSTED source (bundled config, user-global config, or
    # the project config when it is NOT the untrusted-haiku source handled
    # above) -- but "trusted" only means the operator wrote it, not that it
    # parses. A malformed file here used to raise uncaught, exiting neither
    # 0 nor 3, so the shell's bundled-only fallback fired below with NO
    # disclosure at all -- the #804 gate only checks the drop-marker file
    # (which this path never touches) or rc == 3 (reserved for the
    # untrusted-layer drop above). Skip just this layer instead, the same
    # fail-CLOSED shape the untrusted branch already uses, and signal it on
    # the interpreter's own exit path rather than a second marker file.
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        _dropped_trusted_layer = True
        continue
    merged = deep_merge(merged, data)
# Strip `_`-prefixed doc keys, top-level only — same convention as the jq path.
merged = {k: v for k, v in merged.items() if not str(k).startswith("_")}
with open(out_path, "w") as f:
    json.dump(merged, f)
# #804: exit 3 means "merge above completed and $out_path was written, but
# the untrusted project layer was dropped" -- distinct from 0 (clean) and
# from any other non-zero exit (a genuine merge failure, still handled by
# the shell's own bundled-only fallback below).
# #815: exit 4 means the same, but for a malformed TRUSTED layer (bundled
# config, user-global config, or project config outside the untrusted-haiku
# case); exit 5 means both a trusted AND the untrusted layer were dropped.
# Distinct codes so the shell can choose which warning(s) to print without a
# second marker file.
if _dropped_project_layer and _dropped_trusted_layer:
    sys.exit(5)
elif _dropped_project_layer:
    sys.exit(3)
elif _dropped_trusted_layer:
    sys.exit(4)
