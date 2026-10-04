"""Fallback for `jq -r '.dotted.key' FILE` when jq itself is not on PATH (#668).

Called from detect-tools.sh's own `_jq_fallback`, by literal path through
`_remember_run_python` (#898 round 7 -- this body used to live inline, as a
single-quoted here-string with the two embedded `.` separators escaped
through the close-emit-reopen quoting idiom; moved to its own file so the
shell side carries neither that quoting nor the embedded `for` loop a
line-oriented scanner cannot tell from real bash).

Prints nothing and exits 0 on ANY failure -- a missing key, a file that is
not valid JSON, a key argument that does not parse -- because every caller
already treats "no output" as the miss case; this mirrors `jq`'s own
silence on a null lookup rather than inventing a second failure channel.
"""

import json
import sys


def main() -> None:
    try:
        with open(sys.argv[1]) as f:
            data = json.load(f)
        keys = sys.argv[2].strip(".").split(".")
        val = data
        for k in keys:
            if k and isinstance(val, dict):
                val = val.get(k)
            if val is None:
                break
        if val is None:
            return
        # jq -r prints strings raw and everything else in jq's JSON textual
        # form -- crucially "true"/"false" for booleans, not Python's
        # capitalized str(True)/str(False). Getting this wrong silently
        # breaks every caller that does `[ "$x" = "true" ]` against a
        # boolean config key (e.g. git_backup.gpg_sign, allow_remote_change)
        # whenever jq is absent: the comparison never matches, so the key
        # always reads as false.
        print(val if isinstance(val, str) else json.dumps(val))
    except Exception:  # noqa: BLE001 -- any failure here is the miss case, by design (see header)
        return


if __name__ == "__main__":
    main()
